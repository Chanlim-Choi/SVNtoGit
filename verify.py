#!/usr/bin/env python3
"""Verify a migrated Git repo against its SVN source.

usage: verify.py SVN_URL GIT_REPO LOG_JSON --layout FILE [--rev N] [--samples N|all] [--authors FILE]
                 [--lfs-above BYTES] [--lfs-paths FILE] [--jobs N] [--json OUT]

LOG_JSON is svn_meta.py's log.json, FILE the layout.json of make_rules.py --layout-out. Every svn call
is pinned to a revision (--rev, default: the last revision in LOG_JSON).

Checks
  1. at --rev: main == trunk, every branch/tag root that exists in SVN == its git ref, and the content
     outside trunk and the roots (the region) == the region branch (svn-outside-layout; main for
     NONE/OTHER). Files are compared by path, content, exec bit and symlink.
  2. historical commits (--samples N, default all): tree == SVN content of its revision
  3. every SVN revision is represented by >=1 commit (or explained)
  4. commit message / author / date identical to svn log; annotated tags have real taggers
  5. every commit has a refs/notes/svn note carrying its revision + all revprops
  6. every complete SVN merge (svn:mergeinfo) between trunk and branch/tag roots is a git ancestor of
     the merging commit (merges from or into other folders are listed as info)
  7. GitHub limits: blobs >100 MB (hard reject), >50 MB (warning); with LFS, every file that
     .gitattributes sends to LFS is a pointer at every branch/tag tip
Exit status 0 only if nothing outside the "info"/"explained" categories was found. The --json file is
always written; a crash is recorded as the failing check "crashed".
"""
import datetime, hashlib, json, os, random, re, stat, subprocess, sys, tempfile, traceback
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote

OPTS = ("--layout", "--rev", "--samples", "--authors", "--lfs-above", "--lfs-paths", "--jobs", "--json")
JSON_OUT = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv[1:-1] else None
_lc = os.environ.get("LC_ALL", "")
env = dict(os.environ, LC_ALL=_lc if re.search(r"utf-?8", _lc, re.I) else "C.UTF-8")
problems, STATS = {}, {}
EOLCAT = "info: SVN files stored with line endings that differ from svn:eol-style"
PLACEHOLDER = re.compile(r"@localhost>$|@[0-9a-f]{8}-[0-9a-f-]{27}>$")   # converter / git-svn fallbacks
printed = set()

def bad(cat, msg):
    problems.setdefault(cat, []).append(msg)

def run(*a, **kw):
    return subprocess.run(a, check=True, capture_output=True, env=env, **kw).stdout

def git(*a, **kw):
    return run("git", "-C", repo, *a, **kw)

def last_line(b):
    t = (b or b"").decode("utf-8", "replace").strip().splitlines()
    return t[-1] if t else ""

XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
def xml(b):   # svn info/ls --xml write svn:author raw (control or non-UTF-8 bytes); names, kinds and revisions suffice
    return ET.fromstring(XML_BAD.sub("?",b.decode("utf-8", "replace")))

def setup():
    global url, repo, revs, REVLIST, REV, PROJECT, FLAT, BASE, EXCLUDE, REGION_REF, ROOT_AT, TRUNK
    global N, JOBS, LFS_MIN, LFS_PATHS, AUTHORS, TMP, SKIP, SKIP_ANC, CANON, GITDIR
    pos, o, a, i = [], {}, sys.argv[1:], 0
    while i < len(a):
        if a[i] in OPTS and i + 1 < len(a):
            o[a[i]] = a[i + 1]; i += 2
        elif a[i].startswith("--"):
            raise ValueError(f"unknown option or missing value: {a[i]}")
        else:
            pos.append(a[i]); i += 1
    if len(pos) != 3 or "--layout" not in o:
        raise ValueError("usage: verify.py SVN_URL GIT_REPO LOG_JSON --layout FILE [--rev N] [--samples N|all] "
                         "[--authors FILE] [--lfs-above BYTES] [--lfs-paths FILE] [--jobs N] [--json OUT]")
    url, repo, logf = pos[0].rstrip("/"), pos[1], pos[2]
    allrevs = {int(k): v for k, v in json.load(open(logf, encoding="utf-8")).items()}
    REV = int(o["--rev"]) if "--rev" in o else max(allrevs)
    revs = {r: v for r, v in allrevs.items() if r <= REV}
    REVLIST = sorted(revs)
    lay = json.load(open(o["--layout"], encoding="utf-8"))
    PROJECT = lay["project"]
    FLAT = PROJECT in ("NONE", "OTHER")          # no trunk/branches/tags: everything is region content
    BASE = (lay.get("base") or "").rstrip("/")
    EXCLUDE = [e.rstrip("/") for e in lay.get("exclude") or []]
    REGION_REF = lay.get("region_ref") or ("refs/heads/main" if FLAT else "refs/heads/svn-outside-layout")
    ROOT_AT = {r["path"].rstrip("/"): r for r in lay.get("roots") or []}
    TRUNK = None if FLAT else BASE + "/trunk"
    s = o.get("--samples", "all")
    N = 10**9 if s == "all" else int(s)
    JOBS = max(1, int(o["--jobs"])) if "--jobs" in o else (os.cpu_count() or 4) * 2
    # SVN files that may have become LFS pointers: paths git lfs converted (any size, F53) and anything
    # above the --lfs-above threshold (bytes), capped at 1 MiB so a missing flag still covers the default
    LFS_MIN = min(int(o["--lfs-above"]), 1 << 20) if "--lfs-above" in o else 1 << 20
    LFS_PATHS = set()
    if "--lfs-paths" in o:
        LFS_PATHS = {l.rstrip("\r\n") for l in open(o["--lfs-paths"], encoding="utf-8", errors="surrogateescape")
                     if l.strip()}
    AUTHORS = {}   # svn login -> "Name <email>" (git-svn authors file format)
    if "--authors" in o:
        for line in open(o["--authors"], encoding="utf-8-sig"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split(" = ", 1) if " = " in line else line.split("=", 1)
                AUTHORS[k.strip()] = v.strip()
    TMP = os.path.dirname(os.path.abspath(logf))
    # svn paths that are not region content (2.2), and the folders that hold them
    SKIP = set(EXCLUDE) | (set() if FLAT else {TRUNK} | set(ROOT_AT))
    SKIP_ANC = set() if FLAT else {BASE + "/branches", BASE + "/tags"}
    for p in SKIP:
        parts = p.split("/")
        SKIP_ANC.update("/".join(parts[:k]) for k in range(1, len(parts)))
    GITDIR = run("git", "-C", repo, "rev-parse", "--absolute-git-dir").decode().strip()
    CANON = unquote(xml(run("svn", "info", "--xml", "--non-interactive", f"{url}@{REV}")).findtext("entry/url"))

# ---------- SVN side --------------------------------------------------------------
def svn_url(path, rev):
    return url + quote(path) + f"@{rev}"

def svn_ls(path, rev):
    """[(name, kind)] of an svn folder at rev, None if it does not exist"""
    r = subprocess.run(["svn", "ls", "--xml", "--non-interactive", "-r", str(rev), svn_url(path, rev)],
                       capture_output=True, env=env)
    if r.returncode:
        if re.search(rb"[WE](160013|170000)", r.stderr):   # path not found (file/svn/http access)
            return None
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    return [(e.findtext("name"), e.get("kind")) for e in xml(r.stdout).iter("entry")]

def is_git(p):   # never migrated (rules ignore every path with a .git component)
    return any(c.lower() == ".git" for c in p.split("/"))

def region_items(rev):
    """Region content (spec 2.2) at rev as export items [(svn path, path in git, depth)], plus the skipped
    paths that exist at rev (trunk, roots, excluded projects). Items are None when BASE does not exist."""
    items, present = [], set()
    def walk(d):
        ents = svn_ls(d, rev)
        if ents is None:
            return False
        if any(k == "file" for _, k in ents):
            items.append((d, d[len(BASE):].lstrip("/"), "files"))
        for n, k in ents:
            p = f"{d}/{n}"
            if n.lower() == ".git":
                continue
            if p in SKIP:
                present.add(p)
            elif k == "dir" and p in SKIP_ANC:
                walk(p)
            elif k == "dir":
                items.append((p, p[len(BASE):].lstrip("/"), "infinity"))
        return True
    if BASE in SKIP_ANC:
        return (items, present) if walk(BASE) else (None, present)
    return ([(BASE, "", "infinity")] if BASE == "" or svn_ls(BASE, rev) is not None else None), present

def export(items, rev, dst):
    for p, rel, depth in items:
        d = os.path.join(dst, rel) if rel else dst
        os.makedirs(os.path.dirname(d), exist_ok=True)
        run("svn", "export", "-q", "--non-interactive", "--ignore-externals", "--ignore-keywords",
            "--depth", depth, "-r", str(rev), svn_url(p, rev), d)

def chunks_of(f, n=None):
    while n is None or n > 0:
        c = f.read(1 << 20 if n is None else min(n, 1 << 20))
        if not c:
            if n:
                raise EOFError("short read from git cat-file")
            return
        if n is not None:
            n -= len(c)
        yield c

def hash_file(p, size, want256):   # streamed (F28): git blob sha1, plus sha256 for LFS candidates
    h1, h2 = hashlib.sha1(b"blob %d\0" % size), hashlib.sha256() if want256 else None
    with open(p, "rb") as f:
        for c in chunks_of(f):
            h1.update(c)
            if h2:
                h2.update(c)
    return h1.hexdigest(), h2.hexdigest() if h2 else None

def svn_manifest(dst):
    """path -> (mode, blob sha1, size, sha256 or None), empty dirs, path -> exported file"""
    man, empty, local = {}, [], {}
    for root, ds, fs in os.walk(dst):
        ds[:] = [d for d in ds if d.lower() != ".git"]
        fs = [f for f in fs if f.lower() != ".git"]
        for d in list(ds):
            if os.path.islink(os.path.join(root, d)):
                fs.append(d); ds.remove(d)
        rr = os.path.relpath(root, dst).replace(os.sep, "/")
        rr = "" if rr == "." else rr + "/"
        if not ds and not fs and root != dst:
            empty.append(rr.rstrip("/"))
        for fn in fs:
            p, rel = os.path.join(root, fn), rr + fn
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                t = os.fsencode(os.readlink(p))
                man[rel] = ("120000", hashlib.sha1(b"blob %d\0" % len(t) + t).hexdigest(), len(t), None)
            else:
                h1, h2 = hash_file(p, st.st_size, rel in LFS_PATHS or st.st_size >= LFS_MIN)
                man[rel] = ("100755" if st.st_mode & 0o111 else "100644", h1, st.st_size, h2)
                local[rel] = p
    return man, empty, local

def eol_styles(paths, rev):
    """svn path -> svn:eol-style at rev, for the paths that have one"""
    out = {}
    for i in range(0, len(paths), 100):
        r = subprocess.run(["svn", "propget", "--xml", "--non-interactive", "-r", str(rev), "svn:eol-style"]
                           + [svn_url(p, rev) for p in paths[i:i + 100]], capture_output=True, env=env)
        try:
            targets = xml(r.stdout).iter("target")
        except ET.ParseError:
            continue
        for t in targets:
            u = unquote(t.get("path") or "")
            if u.startswith(CANON):
                out[u[len(CANON):]] = t.findtext("property") or ""
    return out

def eol_digest(chunks):
    """sha1 of the content with CRLF and CR turned into LF"""
    h, cr = hashlib.sha1(), False
    for c in chunks:
        c = b"\r" + c if cr else c
        cr = c.endswith(b"\r")
        h.update((c[:-1] if cr else c).replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    if cr:
        h.update(b"\n")
    return h.hexdigest()

# ---------- git side --------------------------------------------------------------
def cat_batch(shas):
    """object id -> content (small objects), one git cat-file --batch"""
    out, data, i = {}, git("cat-file", "--batch", input="".join(s + "\n" for s in shas).encode()), 0
    while i < len(data):
        j = data.index(b"\n", i)
        h = data[i:j].split()
        if len(h) < 3:
            raise RuntimeError(f"git object missing: {data[i:j].decode('utf-8', 'replace')}")
        n = int(h[2]); out[h[0].decode()] = data[j + 1:j + 1 + n]; i = j + 2 + n
    return out

def blob_digests(shas, td, fn):
    """fn(chunks) of each blob, streamed through one git cat-file --batch"""
    lst = os.path.join(td, "blobs.txt")
    with open(lst, "w") as f:
        f.write("".join(s + "\n" for s in shas))
    out = {}
    with open(lst) as fin:
        p = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"], stdin=fin, stdout=subprocess.PIPE, env=env)
        try:
            for s in shas:
                h = p.stdout.readline().split()
                out[s] = fn(chunks_of(p.stdout, int(h[2])))
                p.stdout.read(1)
        finally:
            p.stdout.close(); p.wait()
    return out

def git_manifest(ref):
    """path -> (mode, sha, size) of the whole tree of ref ({} for None)"""
    man = {}
    for e in (git("ls-tree", "-r", "-z", "-l", "--full-tree", ref).split(b"\0") if ref else []):
        if e:
            meta, p = e.split(b"\t", 1)
            mode, _, sha, size = meta.split()
            man[p.decode("utf-8", "surrogateescape")] = (mode.decode(), sha.decode(), int(size) if size != b"-" else 0)
    return man

def attr_lines(b):
    """.gitattributes as git lfs migrate rewrites it (lines without CR, duplicates dropped), without
    the filter=lfs lines it adds and without trailing blank lines"""
    lines, seen, out = b.split(b"\n"), set(), []
    if lines[-1] == b"":
        lines.pop()
    for l in lines:
        l = l[:-1] if l.endswith(b"\r") else l
        if l not in seen and b"filter=lfs" not in l:
            seen.add(l); out.append(l)
    while out and not out[-1].strip():
        out.pop()
    return out

POINTER = re.compile(rb"version https://git-lfs\S*\noid sha256:([0-9a-f]{64})\nsize (\d+)\n")

def explained(diff, s, g, local, rev, svn_base, td, ev):
    """content differences that are not migration errors: LFS pointers to the SVN content, the lines
    git lfs adds to the root .gitattributes (F25), EOL-only differences of svn:eol-style files (F51)"""
    ok = set()
    same = [p for p in diff if s[p][0] == g[p][0] != "120000"]
    ptr = [p for p in same if s[p][3] and g[p][2] < 1024]
    blobs = cat_batch(sorted({g[p][1] for p in ptr})) if ptr else {}
    lfs_obj = {}    # pointers whose object is not the exported file: LFS object file, for the EOL check
    for p in ptr:
        m = POINTER.match(blobs.get(g[p][1], b""))
        if m and m.group(1).decode() == s[p][3] and int(m.group(2)) == s[p][2]:
            ok.add(p); ev.append(("stat", "large files verified through LFS pointer", 1))
        elif m:
            oid = m.group(1).decode()
            lfs_obj[p] = os.path.join(GITDIR, "lfs", "objects", oid[:2], oid[2:4], oid)
    a = ".gitattributes"
    if a in same and a not in ok and max(s[a][2], g[a][2]) <= 1 << 20:
        with open(local[a], "rb") as f:
            if attr_lines(f.read()) == attr_lines(cat_batch([g[a][1]])[g[a][1]]):
                ok.add(a); ev.append(("stat", "root .gitattributes identical apart from git lfs lines", 1))
    rest = [p for p in same if p not in ok]
    styles = eol_styles([f"{svn_base}/{p}" for p in rest], rev) if rest else {}
    rest = [p for p in rest if styles.get(f"{svn_base}/{p}")]
    if rest:
        gb = sorted({g[p][1] for p in rest if p not in lfs_obj})
        gd = blob_digests(gb, td, eol_digest) if gb else {}
        for p in rest:
            if p in lfs_obj:   # svn export translated the line endings, the LFS object holds the raw bytes
                if not os.path.isfile(lfs_obj[p]):
                    continue
                with open(lfs_obj[p], "rb") as f:
                    want = eol_digest(chunks_of(f))
            else:
                want = gd[g[p][1]]
            with open(local[p], "rb") as f:
                if eol_digest(chunks_of(f)) == want:
                    ok.add(p); ev.append(("eol", f"{svn_base}/{p}", rev))
    return ok

def compare(label, items, rev, ref, svn_base):
    """SVN content (export items at rev) vs the whole git tree of ref -> (identical, events)"""
    ev = []
    with tempfile.TemporaryDirectory(dir=TMP) as td:
        dst = os.path.join(td, "x")
        try:
            export(items, rev, dst)
        except subprocess.CalledProcessError as e:
            ev.append(("bad", "tree", f"{label}: cannot export {svn_base or '/'}@{rev}: {last_line(e.stderr)}"))
            return False, ev
        try:
            s, empty, local = svn_manifest(dst)
            g = git_manifest(ref)
            placeholders = {p.rpartition("/")[0] for p in g if p.rpartition("/")[2] == ".gitignore"}
            # .gitignore files generated from svn:ignore or kept for empty dirs, and the .gitattributes
            # written by git lfs, are not SVN content
            g = {p: v for p, v in g.items() if p in s or p.rpartition("/")[2] not in (".gitignore", ".gitattributes")}
            lost = [d for d in empty if d not in placeholders]
            if lost:
                ev.append(("bad", "info: empty dirs not preserved (git cannot store them)",
                           f"{label}: {len(lost)} e.g. {lost[:2]}"))
            elif empty:
                ev.append(("stat", "empty dirs kept via .gitignore placeholder", len(empty)))
            diff = [p for p in sorted(set(s) & set(g)) if s[p][:2] != g[p][:2]]
            ok = explained(diff, s, g, local, rev, svn_base, td, ev) if diff else set()
        except subprocess.CalledProcessError as e:
            ev.append(("bad", "tree", f"{label}: {' '.join(map(str, e.cmd[:4]))} failed: {last_line(e.stderr)}"))
            return False, ev
    diff = [p for p in diff if p not in ok]
    miss, extra = sorted(set(s) - set(g)), sorted(set(g) - set(s))
    if not (miss or extra or diff):
        return True, ev
    kinds = sorted({"mode" if s[p][0] != g[p][0] and s[p][1] == g[p][1] else "content" for p in diff})
    ev.append(("bad", "tree", f"{label} vs {svn_base or '/'}@{rev}: missing={len(miss)} extra={len(extra)} "
                              f"differ={len(diff)}{kinds} e.g. {(miss + extra + diff)[:3]}"))
    return False, ev

EOL_SEEN = {}
def merge_events(ev):   # task results are merged in submission order: the report is deterministic
    for e in ev:
        if e[0] == "bad":
            bad(e[1], e[2])
        elif e[0] == "stat":
            STATS[e[1]] = STATS.get(e[1], 0) + e[2]
        else:
            EOL_SEEN[e[1]] = max(EOL_SEEN.get(e[1], 0), e[2])

# ---------- lifetimes of svn paths (from log.json) ----------------------------------
_LT = {}
def lifetimes(path, upto):
    """[[created, deleted or None]] of an svn path in revisions <= upto: created by an add or replace of
    the path, or by a copy of an ancestor whose source had it; ended by a delete or replace of the path
    or an ancestor"""
    if (path, upto) in _LT:
        return _LT[path, upto]
    out = []
    for r in REVLIST:
        if r > upto:
            break
        evs = [p for p in revs[r]["paths"]
               if p["path"].rstrip("/") in ("", path) or path.startswith(p["path"].rstrip("/") + "/")]
        for p in sorted(evs, key=lambda p: len(p["path"].rstrip("/"))):
            q, act, alive = p["path"].rstrip("/"), p.get("action"), bool(out) and out[-1][1] is None
            if act in ("D", "R") and alive:
                out[-1][1] = r; alive = False
            if act in ("A", "R") and not alive:
                if q == path:
                    out.append([r, None])
                elif "copyfrom" in p:
                    src, _, srev = p["copyfrom"].rpartition("@")
                    if alive_at(src.rstrip("/") + path[len(q):], int(srev)):
                        out.append([r, None])
    _LT[path, upto] = out
    return out

def alive_at(path, rev):
    return any(a <= rev and (b is None or rev < b) for a, b in lifetimes(path, rev))

def last_alive(path):
    lt = lifetimes(path, REV)
    return (REV if lt[-1][1] is None else lt[-1][1] - 1) if lt else None

# ---------- commit <-> revision map --------------------------------------------------
NOTE_RX = re.compile(r"^svn path=(.*?); revision=(\d+)", re.M)
GSID = re.compile(r"git-svn-id: (\S+)@(\d+) \S+")

def svn_rel(u):   # svn path of a git-svn-id URL inside SVN_URL, else None
    a, b = unquote(u).rstrip("/"), unquote(url)
    return "/" if a == b else (a[len(b):] if a.startswith(b + "/") else None)

def load_commits():
    shas = git("rev-list", "--branches", "--tags").decode().split()
    try:
        nl = [l.split() for l in git("notes", "--ref=commits", "list").decode().splitlines()]
    except subprocess.CalledProcessError:
        nl = []
    nb = cat_batch(sorted({n for n, _ in nl})) if nl else {}
    notes = {c: nb[n].decode("utf-8", "replace") for n, c in nl}
    raw = cat_batch(shas) if shas else {}
    commits = {}
    for sha in shas:
        hdr, _, body = raw[sha].partition(b"\n\n")
        m = next(re.match(rb"author (.*) (-?\d+) [-+]\d{4}$", l) for l in hdr.split(b"\n") if l.startswith(b"author "))
        msg = body.decode("utf-8", "replace")
        c = dict(path=None, rev=None, who=m.group(1).decode("utf-8", "replace"), epoch=int(m.group(2)), msg=msg)
        mt = NOTE_RX.search(notes.get(sha, ""))
        if mt:
            c.update(path=mt.group(1), rev=int(mt.group(2)))
        else:   # git svn: only a final git-svn-id line naming a URL inside SVN_URL counts (F52)
            lines = msg.rstrip().split("\n")
            gm = GSID.fullmatch(lines[-1])
            if gm and svn_rel(gm.group(1)) is not None:
                c.update(path=svn_rel(gm.group(1)), rev=int(gm.group(2)), msg="\n".join(lines[:-1]))
        commits[sha] = c
    return commits

def show_summary(commits):
    mapped = {c["rev"] for c in commits.values() if c["rev"]}
    print(f"git commits={len(commits)}  mapped to SVN={sum(1 for c in commits.values() if c['rev'])}  "
          f"svn revisions={len(revs)}  revisions represented={len(mapped & set(revs))}")
    printed.add("git commits=")
    return mapped

# ---------- the checks ----------------------------------------------------------------
def main():
    setup()
    global refs
    commits = load_commits()
    refs = {}
    for line in git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags").decode().splitlines():
        name, sha = line.rsplit(" ", 1)
        refs[name] = sha
    mapped = show_summary(commits)
    for s, c in commits.items():
        if c["rev"] is None:
            bad("info: git commits with no SVN revision", f"{s[:9]} {c['msg'].strip().splitlines()[0][:60] if c['msg'].strip() else ''}")
        elif c["rev"] not in revs:
            bad("mapping: commit names an SVN revision missing from log.json or after --rev",
                f"{s[:9]} r{c['rev']} (log.json ends at r{REV}; SVN changed after the snapshot?)")
            c["rev"] = None

    # ---------- 3. revision coverage -------------------------------------------
    def tag_ref_exists(root):
        g = root["ref"].split("/", 2)[-1]
        return root["ref"] in refs or any(x.startswith((f"refs/tags/svn-deleted/tags/{g}@", f"refs/tags/svn-deleted/{g}@"))
                                         or x == f"refs/tags/svn-deleted/{g}" for x in refs)
    def why(p):
        q, act = p["path"].rstrip("/"), p.get("action")
        if any(q == e or q.startswith(e + "/") for e in EXCLUDE) or (BASE and not (q == BASE or q.startswith(BASE + "/"))):
            return "outside this repository"
        if is_git(q):
            return ".git path (not migrated)"
        if act == "D":
            return "deletion"
        if p.get("kind") == "dir" and (act == "M" or (act == "A" and "copyfrom" not in p)):
            return "folder add/property change"
        if p.get("kind") == "dir" and act == "R" and "copyfrom" not in p:
            return "folder replaced by an empty folder"   # = deletion + folder add; refs checked at HEAD
        root = ROOT_AT.get(q)
        if root and root.get("kind") == "tag" and act in ("A", "R") and "copyfrom" in p and tag_ref_exists(root):
            return "tag creation (git tag exists)"
        return None
    for r in REVLIST:
        if r in mapped:
            continue
        paths = revs[r]["paths"]
        reasons = [why(p) for p in paths]
        if not paths:
            bad("revisions missing (explained)", f"r{r}: empty revision (no changes)")
        elif all(reasons):
            bad("revisions missing (explained)", f"r{r}: {', '.join(sorted(set(reasons)))}: "
                                                 f"{', '.join(p['path'] for p in paths[:2])}")
        else:
            bad("revisions missing (UNEXPLAINED)", f"r{r}: " + ", ".join(
                f"{p.get('action')} {p['path']}" for p, w in zip(paths, reasons) if not w)[:300])

    # ---------- 4. message / author / date, taggers --------------------------------
    def norm(s):
        return "\n".join(l.rstrip() for l in s.strip().splitlines())
    for sha, c in commits.items():
        if not c["rev"]:
            continue
        v = revs[c["rev"]]
        if norm(c["msg"]) != norm(v["log"]):
            bad("message", f"r{c['rev']} {sha[:9]}: svn={v['log'][:40]!r} git={c['msg'][:40]!r}")
        gd = datetime.datetime.fromtimestamp(c["epoch"], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        if v["date"] and gd != v["date"][:19]:
            bad("date", f"r{c['rev']}: svn={v['date']} git={gd}Z")
        want = AUTHORS.get(v["author"] or "(no author)")
        if (want and c["who"] != want) or PLACEHOLDER.search(c["who"]):
            bad("author", f"r{c['rev']}: svn={v['author']!r} git={c['who']!r} expected={want!r}")
    for line in git("for-each-ref", "--format=%(objecttype) %(refname) %(taggername) %(taggeremail)", "refs/tags").decode().splitlines():
        typ, ref, who = (line.split(" ", 2) + [""])[:3]
        if typ == "tag" and PLACEHOLDER.search(who):   # F54
            bad("tagger", f"{ref}: annotated tag by placeholder identity {who!r}")

    # ---------- 1. HEAD: trunk, branch/tag roots, region ------------------------------
    main_ref = next((r for r in ("refs/heads/main", "refs/heads/master") if r in refs), None)
    head = []    # (label, export items, rev, git ref, svn path the items are relative to)
    seen = {REGION_REF}
    items, present = region_items(REV)
    if items is None:        # the project folder itself is gone: the converter deleted its region content too
        items = []
        STATS[f"project folder {BASE} absent at r{REV}: region branch must be empty"] = \
            f"deleted at r{(lifetimes(BASE, REV) or [[0, '?']])[-1][1]}"
    if not FLAT:
        trev = REV if TRUNK in present else last_alive(TRUNK)
        if trev is None:
            bad("tree", f"trunk: {TRUNK} never existed in SVN, nothing to compare {main_ref} with")
        elif main_ref is None:
            bad("refs: SVN branch/tag with no git ref", f"trunk {TRUNK} -> refs/heads/main")
        else:
            seen.add(main_ref)
            if trev != REV:
                STATS[f"trunk absent at r{REV}: main compared with trunk at its last revision"] = f"r{trev}"
            head.append(("trunk", [(TRUNK, "", "infinity")], trev, main_ref, TRUNK))
    for p in sorted(present):
        root = ROOT_AT.get(p)
        if not root:
            continue
        if root.get("ref") not in refs:
            bad("refs: SVN branch/tag with no git ref", f"{root.get('kind')} {p} -> {root.get('ref')}")
            continue
        seen.add(root["ref"])
        head.append((f"{root['kind']} {p}", [(p, "", "infinity")], REV, root["ref"] + "^{commit}", p))
    head.append(({"NONE": "main (whole repository)", "OTHER": "main (content outside the migrated projects)"}.get(
        PROJECT, f"{REGION_REF[11:]} (content outside trunk and the branch/tag roots)"),
        items, REV, REGION_REF if REGION_REF in refs else None, BASE))
    for p, root in sorted(ROOT_AT.items()):   # F05: a deleted SVN branch/tag must not stay a live git ref
        if p not in present and root.get("ref") in refs:
            lt = lifetimes(p, REV)
            bad("refs: git ref of an SVN branch/tag that is deleted at --rev",
                f"{root['ref']} ({p}, deleted at r{lt[-1][1] if lt and lt[-1][1] else '?'})")
            seen.add(root["ref"])
    for ref in sorted(set(refs) - seen):
        problems.setdefault("info: extra git refs (deleted SVN branches / nested dirs)", []).append(ref)

    # ---------- 2. historical commits ------------------------------------------------
    # The converter starts a new svn-outside-layout from the source commit when its first revision copies
    # trunk or a root into the region (F07 sandbox): the first-parent chain ends in that branch's history.
    region = set()
    if REGION_REF in refs and not FLAT:
        for s in git("rev-list", "--first-parent", REGION_REF).decode().split():
            p = commits.get(s, {}).get("path")
            if p is not None and commits[s]["rev"] and p.rstrip("/") in SKIP:
                break
            region.add(s)
    pool = [s for s, c in commits.items() if c["rev"]]
    sample = random.sample(pool, min(N, len(pool)))
    def check_commit(sha):
        c = commits[sha]
        if FLAT or sha in region:
            try:
                its = region_items(c["rev"])[0] or []
            except subprocess.CalledProcessError as e:
                return False, [("bad", "tree", f"commit {sha[:9]} r{c['rev']}: cannot list {BASE or '/'}: {last_line(e.stderr)}")]
            return compare(f"commit {sha[:9]} r{c['rev']} (region)", its, c["rev"], sha, BASE)
        p = c["path"].rstrip("/")
        return compare(f"commit {sha[:9]} r{c['rev']}", [(p, "", "infinity")], c["rev"], sha, p)
    def lfs_fsck(refs_):   # F53: a file .gitattributes sends to LFS but stored raw shows as modified in clones
        r = subprocess.run(["git", "-C", repo, "lfs", "fsck", "--pointers", refs_[0]], capture_output=True, env=env)
        if r.returncode == 0:
            return True, []
        out = (r.stdout + r.stderr).decode("utf-8", "replace").strip().splitlines()
        return False, [("bad", "lfs: files .gitattributes sends to LFS are not pointers",
                        f"{', '.join(refs_[:3])}{' ...' if len(refs_) > 3 else ''}: {out[0] if out else 'git lfs fsck failed'}"
                        + (f" (+{len(out) - 1} more)" if len(out) > 1 else ""))]
    by_tree = {}   # one git lfs fsck per distinct tree (tags copied from trunk share trees)
    if LFS_PATHS and refs:
        names = sorted(refs)
        out = git("cat-file", "--batch-check=%(objectname)", input="".join(f"{r}^{{tree}}\n" for r in names).encode())
        for r, t in zip(names, out.decode().splitlines()):
            if not t.endswith(" missing"):
                by_tree.setdefault(t, []).append(r)
    with ThreadPoolExecutor(JOBS) as ex:   # F45: the HEAD comparisons run in the pool too
        hf = [ex.submit(compare, *h) for h in head]
        lf = [ex.submit(lfs_fsck, rs) for rs in by_tree.values()]
        cf = [ex.submit(check_commit, s) for s in sample]
        try:
            n_ok = 0
            for f in hf:
                ok, ev = f.result(); merge_events(ev); n_ok += ok
            nroots = sum(1 for h in head if h[0].startswith(("branch ", "tag ")))
            print(f"HEAD r{REV}: {n_ok}/{len(hf)} trees identical to SVN"
                  + ("" if FLAT else f" (trunk, {nroots} branch/tag roots, content outside them)"))
            for f in lf:
                merge_events(f.result()[1])
            n_ok = 0
            for f in cf:
                ok, ev = f.result(); merge_events(ev); n_ok += ok
        except BaseException:
            for f in hf + lf + cf:   # do not wait for the queued work before reporting the crash
                f.cancel()
            raise
    print(f"historical commits: {n_ok}/{len(sample)} trees identical to SVN "
          f"({'ALL commits' if len(sample) == len(pool) else 'random sample'})")
    for p in sorted(EOL_SEEN):
        problems.setdefault(EOLCAT, []).append(f"{p}@{EOL_SEEN[p]}")

    # ---------- 6. merges --------------------------------------------------------
    at = {(c["path"].rstrip("/") or "/", c["rev"]): s for s, c in commits.items() if c["rev"]}
    by_path = {}
    for (p, r), s in at.items():
        by_path.setdefault(p, []).append((r, s))
    for v in by_path.values():
        v.sort()
    def parse_mi(t):
        out = {}
        for line in (t or "").splitlines():
            if ":" in line:
                src, rr = line.rsplit(":", 1)
                out[src.rstrip("/")] = [tuple(int(x) for x in (r.strip().rstrip("*").split("-") * 2)[:2])
                                        for r in rr.split(",") if r.strip()]
        return out
    def lifetime_at(src, top):   # the source lifetime that holds the merged revision (F33)
        lts = (ROOT_AT.get(src) or {}).get("lifetimes") or lifetimes(src, REV)
        c = [lt for lt in sorted(lts, key=lambda x: x[0]) if lt[0] <= top]
        return c[-1] if c else None
    cur, merges_ok, merges_partial = {}, 0, 0
    LINES = set(ROOT_AT) | ({TRUNK} if TRUNK else set())
    for r in REVLIST:
        for p in revs[r]["paths"]:
            path = p["path"].rstrip("/")
            if p.get("action") in ("A", "R") and "copyfrom" in p and path in by_path:
                cur[path] = dict(cur.get(p["copyfrom"].rsplit("@", 1)[0].rstrip("/"), {}))
            if "props" not in p or path not in by_path:
                continue
            new, old = parse_mi(p["props"].get("svn:mergeinfo", "")), cur.get(path, {})
            cur[path] = new
            tgt = at.get((path, r))
            for src, rl in new.items():
                if rl == old.get(src) or src not in by_path or src == path or not rl:
                    continue
                top = min(max(b for _, b in rl), r)
                lt = lifetime_at(src, top)
                srcs = [x for x in by_path[src] if x[0] <= top and (lt is None or x[0] >= lt[0])]
                if not srcs:
                    continue
                if path not in LINES or src not in LINES:   # add_merges.py rebuilds merges between these only
                    problems.setdefault("info: SVN merges outside trunk/branches/tags (git cannot represent them; "
                                        "kept in notes)", []).append(f"r{r} {src} -> {path}")
                    continue
                if not tgt:
                    problems.setdefault("info: SVN merges in revisions with no git commit on the target", []).append(
                        f"r{r} {src} -> {path}")
                    continue
                anc = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", srcs[-1][1], tgt], env=env).returncode == 0
                if anc:
                    merges_ok += 1
                    continue
                unmerged = [cr for cr, _ in srcs if not any(a <= cr <= b for a, b in rl + old.get(src, []))]
                if unmerged:
                    merges_partial += 1
                    problems.setdefault("info: partial merges (cherry-picks; git cannot represent, kept in notes)", []).append(
                        f"r{r} {src} -> {path}, not merged: r{unmerged[:5]}")
                else:
                    bad("merge not represented in git history", f"r{r}: {src}@{srcs[-1][0]} -> {path}")
    print(f"merges: {merges_ok} SVN merges are git ancestry, {merges_partial} partial (cherry-pick) merges")

    # ---------- 5. notes ---------------------------------------------------------
    try:
        notes = {l.split()[1]: l.split()[0] for l in git("notes", "--ref=svn", "list").decode().splitlines()}
    except subprocess.CalledProcessError:
        notes = {}
    need = [s for s, c in commits.items() if c["rev"]]
    blobs = {k: v.decode("utf-8", "replace") for k, v in cat_batch(sorted({notes[s] for s in need if s in notes})).items()} \
        if any(s in notes for s in need) else {}
    for s in need:
        if s not in notes:
            bad("notes", f"{s[:9]} r{commits[s]['rev']}: no note"); continue
        body, rv = blobs[notes[s]], commits[s]["rev"]
        if f"SVN-Revision: r{rv}\n" not in body:
            bad("notes", f"{s[:9]}: note does not name r{rv}")
        for k, v in revs[rv]["revprops"].items():
            if f"{k}: {v.splitlines()[0] if v else ''}" not in body:
                bad("notes", f"r{rv}: revprop {k} missing from note")
    print(f"notes: {sum(1 for s in need if s in notes)}/{len(need)} commits have an svn note "
          f"({sum(1 for s in need if revs[commits[s]['rev']]['revprops'])} with custom revprops)")

    # ---------- 7. GitHub limits -------------------------------------------------
    objs = git("cat-file", "--batch-check=%(objecttype) %(objectsize) %(rest)", input=git("rev-list", "--objects", "--all")).decode()
    for line in objs.splitlines():
        t, size, *rest = line.split(" ", 2)
        if t == "blob" and int(size) > 50 * 2**20:
            cat = "GITHUB BLOCKER: blob >100MB in history" if int(size) > 100 * 2**20 else "info: github warns on blobs >50MB"
            bad(cat, f"{int(size)/2**20:.0f} MB {rest[0] if rest else ''}")
    # the rules never migrate .git paths; one that slipped through anyway makes GitHub reject the push
    logged = git("log", "--branches", "--tags", "--format=", "--name-only", "--no-renames", "-z").decode("utf-8", "surrogateescape")
    for p in sorted({p for p in re.split(r"[\0\n]", logged) if p and is_git(p)})[:50]:
        bad("GITHUB BLOCKER: .git path in history", p)
    size = run("git", "-C", repo, "count-objects", "-vH").decode()
    print("repo size:", " ".join(l for l in size.splitlines() if l.startswith(("size-pack", "count"))))

def report():
    print("\n================ REPORT ================")
    for k, v in STATS.items():
        print(f"[ok] {k}: {v}")
    fatal = [c for c in problems if not (c.startswith("info") or "(explained)" in c)]
    for cat, items in sorted(problems.items(), key=lambda kv: kv[0] in fatal):
        print(f"[{'FAIL' if cat in fatal else 'note'}] {cat} x{len(items)}")
        for i in items[:8]:
            print("   ", i)
        if len(items) > 8:
            print(f"    ... {len(items) - 8} more")
    return fatal

def write_json(fatal, crashed=None):
    if JSON_OUT:
        d = {"problems": problems, "stats": STATS, "fatal": fatal}
        if crashed is not None:
            d["crashed"] = crashed
        with open(JSON_OUT, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, ensure_ascii=False)

if __name__ == "__main__":
    write_json(["crashed"], "verify.py did not finish")   # replaced below; stays if the process is killed
    try:
        main()
    except (Exception, KeyboardInterrupt):
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        crash = tb.strip().splitlines()[-1]
        if "git commits=" not in printed:   # the driver shows verify.log from this line on
            print("git commits=? (verify.py stopped before mapping commits)")
        bad("crashed", crash)
        fatal = report()
        write_json(fatal, crash)
        print(f"\nRESULT: FAIL - verify.py crashed: {crash}")
        sys.exit(1)
    fatal = report()
    write_json(fatal)
    print("\nRESULT:", "PASS - every check passed" if not fatal else f"FAIL - {len(fatal)} failing check(s)")
    sys.exit(1 if fatal else 0)
