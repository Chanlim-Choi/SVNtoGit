#!/usr/bin/env python3
"""Rebuild merge commits from svn:mergeinfo, fix placeholder identities, keep notes.

usage: add_merges.py GIT_REPO LOG_JSON [REPORT_JSON] [--no-author "Name <email>"] [--svn-url URL]
                     --layout FILE

svn-all-fast-export imports every SVN merge as a plain commit, and adds a merge parent
whenever a file is copied from another branch (a cherry-pick that adds a file would claim
the whole source branch as merged). Those parents are dropped. Merge parents are added
from svn:mergeinfo instead (layout.json gives trunk, the branch/tag roots and their
lifetimes) - but only for COMPLETE merges: every commit of the source path's lifetime up
to the merge point is in the merged ranges or already in the target's history, the same
rule `git svn` uses. Partial merges (cherry-picks) are not representable in git; they are
listed in the report and remain recorded in the notes.

Placeholder identities (nobody <nobody@localhost>, written by the converter for revisions
without svn:author) are replaced by --no-author on authors, committers and taggers.

With --svn-url, every path SVN replaced (action R) is re-checked against the SVN content
of that revision: files the converter did not write are added.

History is rewritten (fast-export | fast-import, in place); notes in refs/notes/* are
re-keyed to the new commit IDs.
"""
import bisect, heapq, json, os, re, shutil, subprocess, sys, tempfile, urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict

USAGE = ("usage: add_merges.py GIT_REPO LOG_JSON [REPORT_JSON] [--no-author 'Name <email>'] "
         "[--svn-url URL] --layout FILE")

def opt(name):
    if name not in sys.argv:
        return None
    i = sys.argv.index(name)
    if i + 1 >= len(sys.argv):
        sys.exit(f"ERROR: {name} needs a value\n{USAGE}")
    return sys.argv[i + 1]

if len(sys.argv) < 3 or sys.argv[1].startswith("--") or sys.argv[2].startswith("--"):
    sys.exit(USAGE)
repo, logf = sys.argv[1], sys.argv[2]
report_f = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else None
if not opt("--layout"):
    sys.exit("ERROR: --layout FILE is required (layout.json from make_rules.py --layout-out)\n" + USAGE)
_lc = os.environ.get("LC_ALL", "")
env = dict(os.environ, LC_ALL=_lc if re.search("utf-?8", _lc, re.I) else "C.UTF-8")
revs = {int(k): v for k, v in json.load(open(logf, encoding="utf-8")).items()}
LAY = json.load(open(opt("--layout"), encoding="utf-8"))
SVN_URL = (opt("--svn-url") or "").rstrip("/")
NOAUTHOR = (opt("--no-author") or "Unknown <unknown@example.com>").encode()
PLACEHOLDER = b"nobody <nobody@localhost>"
G = ["git", "-C", repo]
INF = 1 << 62

def git(*a, **kw):
    return subprocess.run([*G, *a], check=True, capture_output=True, env=env, **kw).stdout

# ---------- layout: trunk, branch/tag roots, region ---------------------------------
PROJECT = LAY.get("project", "/")
BASE = (LAY.get("base") or "").rstrip("/")
EXCL = [e.rstrip("/") for e in LAY.get("exclude") or [] if e.rstrip("/")]
ROOTS = {r["path"].rstrip("/"): r for r in LAY.get("roots") or []}
FLAT = PROJECT in ("NONE", "OTHER")          # no trunk/branches/tags: everything is the region
TRUNK = None if FLAT else BASE + "/trunk"
BRANCHES = set(ROOTS) | ({TRUNK} if TRUNK else set())

def under(p, d):
    return p == d or p.startswith(d + "/")

def is_git_path(p):
    return any(c.lower() == ".git" for c in p.split("/"))

# ---------- export the stream (no file contents) and parse it ------------------------
# Full ref names, not --branches --tags: with those, a tag named like a branch (refs/tags/x next to
# refs/heads/x) is silently left out of the stream and keeps the old, un-rewritten history alive.
EXPORT_REFS = git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags").decode().split()
raw = subprocess.run([*G, "fast-export", "--no-data", "--signed-tags=strip", "--tag-of-filtered-object=rewrite",
                      "--reencode=no", "--mark-tags", "--show-original-ids", "--topo-order",
                      *EXPORT_REFS], capture_output=True, check=True, env=env).stdout
START = (b"commit ", b"tag ", b"reset ", b"feature ", b"progress ", b"checkpoint", b"done", b"blob")
OPS = (b"M ", b"D ", b"R ", b"C ", b"N ", b"deleteall")
blocks, cur, pos = [], None, 0
while pos < len(raw):
    nl = raw.find(b"\n", pos)
    nl = len(raw) - 1 if nl < 0 else nl
    line = raw[pos:nl + 1]; pos = nl + 1
    if line.startswith(b"data "):
        if cur is None or "data" in cur or line[5:7] == b"<<":
            sys.exit(f"ERROR: unexpected {line[:40]!r} in the fast-export stream")
        n = int(line[5:]); cur["data"] = line + raw[pos:pos + n]; pos += n
        continue
    if line.strip() == b"":
        continue
    if cur is None or line.startswith(START):
        cur = {"kind": line.split()[0].decode(), "pre": [], "post": []}; blocks.append(cur)
    cur["post" if "data" in cur else "pre"].append(line)

commits_b, tags_b, mark_orig, orig_b = [], [], {}, {}
for b in blocks:
    for l in b["pre"]:
        if l.startswith(b"mark :"):
            b["mark"] = l[5:].strip()
        elif l.startswith(b"original-oid "):
            b["orig"] = l[13:].strip().decode()
    if b["kind"] == "tag":
        tags_b.append(b)
    if b["kind"] != "commit":
        continue
    if "data" not in b or "mark" not in b or "orig" not in b:
        sys.exit(f"ERROR: incomplete commit in the fast-export stream: {b['pre'][:2]!r}")
    b["ref"] = b["pre"][0][7:].strip()
    b["from"], b["ops"], b["i"] = None, [], len(commits_b)
    for l in b["post"]:
        if l.startswith(b"from "):
            b["from"] = l[5:].strip()
        elif l.startswith(b"merge "):
            pass    # parents the converter inferred from file copies (F30): dropped
        elif l.startswith(OPS):
            b["ops"].append(l)
        else:
            sys.exit(f"ERROR: unexpected line in a fast-export commit: {l[:80]!r}")
    mark_orig[b["mark"]] = b["orig"]; orig_b[b["orig"]] = b; commits_b.append(b)
for b in tags_b:
    if "mark" in b and "orig" in b:
        mark_orig[b["mark"]] = b["orig"]
n_git = int(git("rev-list", "--count", "--branches", "--tags") or b"0")
if len(commits_b) != n_git:
    sys.exit(f"ERROR: fast-export stream has {len(commits_b)} commits, the repository {n_git}; nothing rewritten")
dropped = sum(1 for b in commits_b for l in b["post"] if l.startswith(b"merge "))

def first_parent(b):
    f = b["from"]
    return None if f is None else mark_orig.get(f, f.decode())

fp = {b["orig"]: first_parent(b) for b in commits_b}

# ---------- commit <-> (svn path, rev) from notes ------------------------------------
def notes_tree(ref):
    """annotated object -> note blob"""
    out = {}
    for row in git("ls-tree", "-r", "-z", ref).split(b"\0"):
        if row:
            meta, path = row.split(b"\t", 1)
            out[path.replace(b"/", b"").decode()] = meta.split()[2].decode()
    return out

def cat_blobs(shas):
    shas = list(dict.fromkeys(shas))
    if not shas:
        return {}
    data = subprocess.run([*G, "cat-file", "--batch"], input="\n".join(shas).encode() + b"\n",
                          capture_output=True, check=True, env=env).stdout
    out, i = {}, 0
    for s in shas:
        e = data.index(b"\n", i); hdr = data[i:e].split()
        if len(hdr) < 3:            # "<sha> missing"
            i = e + 1; continue
        n = int(hdr[2]); out[s] = data[e + 1:e + 1 + n]; i = e + 2 + n
    return out

notes = {}
for line in git("for-each-ref", "--format=%(refname) %(objectname)", "refs/notes/").decode().splitlines():
    ref, sha = line.rsplit(" ", 1)
    notes[ref] = notes_tree(sha)
NOTE_RX = [("refs/notes/commits", re.compile(rb"^svn path=(.*?); revision=(\d+)", re.M), 1, 2),   # ';' may be in a name
           ("refs/notes/svn", re.compile(rb"SVN-Revision: r(\d+)\nSVN-Path: ([^\n]*)"), 2, 1)]
at, rev_of, path_of = {}, {}, {}
by_path = defaultdict(list)     # "/trunk" -> sorted [(rev, sha)]
for ref, rx, gp, gr in NOTE_RX:
    entries = notes.get(ref, {})
    todo = [b["orig"] for b in commits_b if b["orig"] not in rev_of and b["orig"] in entries]
    texts = cat_blobs([entries[s] for s in todo])
    for s in todo:
        m = rx.search(texts.get(entries[s], b""))
        if not m:
            continue
        path, rev = m.group(gp).decode("utf-8", "replace").rstrip("/") or "/", int(m.group(gr))
        rev_of[s], path_of[s] = rev, path
        if (path, rev) not in at:
            at[path, rev] = s
            by_path[path].append((rev, s))
for v in by_path.values():
    v.sort()
if not at:
    sys.exit("ERROR: no commit<->revision mapping found in refs/notes/commits or refs/notes/svn")

def commit_at_or_before(path, rev):
    lst = by_path.get(path, [])
    i = bisect.bisect_right(lst, (rev, "\uffff")) - 1
    return lst[i] if i >= 0 else None

# ---------- lifetimes ---------------------------------------------------------------
_births = {}
def births(path):
    """revisions where path is (re)created: A/R of itself, or a copy of an ancestor folder"""
    if path not in _births:
        _births[path] = [r for r in sorted(revs) if any(
            p.get("action") in ("A", "R") and (p["path"].rstrip("/") == path or
                                               ("copyfrom" in p and path.startswith(p["path"].rstrip("/") + "/")))
            for p in revs[r]["paths"])]
    return _births[path]

LIFE = {p: r.get("lifetimes") or [] for p, r in ROOTS.items()}
if TRUNK and isinstance(LAY.get("trunk"), dict):
    LIFE[TRUNK] = LAY["trunk"].get("lifetimes") or []

def lifetime_start(path, rev):
    for a, b in LIFE.get(path, []):
        if a <= rev and (b is None or rev < b):
            return a
    return max([b for b in births(path) if b <= rev] or [0])

# ---------- merges from svn:mergeinfo ---------------------------------------------------
def parse_mi(text):
    """'/branches/x:5-7,9*\n' -> {'/branches/x': [(5,7),(9,9)]}  (non-inheritable '*' kept)"""
    out = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        src, ranges = line.rsplit(":", 1)
        rl = []
        for r in ranges.split(","):
            r = r.strip().rstrip("*")
            if not r:
                continue
            a, _, b = r.partition("-")
            try:
                rl.append((int(a), int(b or a)))
            except ValueError:
                continue
        if rl:
            out[src.rstrip("/") or "/"] = rl
    return out

def covered(rl, r):
    return any(a <= r <= b for a, b in rl)

grafts = defaultdict(list)            # target sha -> [extra parent shas]

def parents(s):
    p = fp.get(s)
    return ([p] if p else []) + grafts.get(s, [])

class Ancestry:
    """ancestors of one merge target over first parents + merge parents added so far,
    expanded lazily from the highest SVN revision down (an ancestor never has a higher one)"""
    def __init__(self, tgt):
        self.seen, self.heap = {tgt}, [(-rev_of.get(tgt, INF), tgt)]

    def add_parent(self, p):
        if p not in self.seen:
            self.seen.add(p); heapq.heappush(self.heap, (-rev_of.get(p, INF), p))

    def has(self, s):
        want = rev_of.get(s, 0)
        while self.heap and -self.heap[0][0] >= want:
            k, n = heapq.heappop(self.heap)
            for p in parents(n):
                if p not in self.seen:
                    self.seen.add(p)
                    heapq.heappush(self.heap, (max(k, -rev_of[p]) if p in rev_of else k, p))
        return s in self.seen

current = {}                          # branch path -> parsed mergeinfo
partial, full = [], []
for r in sorted(revs):
    for p in revs[r]["paths"]:
        path = p["path"].rstrip("/") or "/"
        if path not in BRANCHES:
            continue
        if p.get("action") in ("A", "R") and "copyfrom" in p:
            src = p["copyfrom"].rsplit("@", 1)[0].rstrip("/") or "/"
            current[path] = dict(current.get(src, {}))   # mergeinfo is inherited on copy
        if "props" not in p:
            continue
        new = parse_mi(p["props"].get("svn:mergeinfo", ""))
        old = current.get(path, {})
        current[path] = new
        tgt = at.get((path, r))
        if not tgt:
            continue
        anc, cands = None, []
        for src, rl in new.items():
            if rl == old.get(src) or src == path or src not in BRANCHES or src not in by_path:
                continue
            cand = commit_at_or_before(src, min(max(b for _, b in rl), r))
            if cand:
                cands.append((cand, src, rl))
        # newest source first: an older one reachable through it then needs no parent of its own
        for cand, src, rl in sorted(cands, key=lambda x: (-x[0][0], x[1])):
            # complete merge = every commit of the source's lifetime up to the merge point is merged
            # now or earlier, or already in the target's history; within one lifetime the source
            # commits form a first-parent chain, so below the first one in history all are in it
            lo = lifetime_start(src, cand[0])
            done = old.get(src, [])
            anc = anc or Ancestry(tgt)
            missing, lst = [], by_path[src]
            for cr, s in reversed(lst[bisect.bisect_left(lst, (lo, "")):bisect.bisect_right(lst, (cand[0], "\uffff"))]):
                if covered(rl, cr) or covered(done, cr):
                    continue
                if anc.has(s):
                    break
                missing.append(cr)
            if missing:
                partial.append({"rev": r, "into": path, "from": src, "unmerged_revs": sorted(missing)[:20]})
                continue
            if anc.has(cand[1]):
                continue  # already in history
            grafts[tgt].append(cand[1]); anc.add_parent(cand[1])
            full.append({"rev": r, "into": path, "from": src, "source_rev": cand[0]})
print(f"merges: {len(full)} complete -> merge commits, {len(partial)} partial (cherry-picks, kept in notes only)")

# ---------- file ops: deletions first (a file replaced by a directory), then repairs --------
for b in commits_b:
    b["ops"] = ([l for l in b["ops"] if l.startswith(b"deleteall")] + [l for l in b["ops"] if l.startswith(b"D ")]
                + [l for l in b["ops"] if not l.startswith((b"deleteall", b"D "))])

def unquote(p):
    if not p.startswith(b'"'):
        return p
    esc = {b"n": b"\n", b"t": b"\t", b"a": b"\a", b"b": b"\b", b"f": b"\f", b"r": b"\r", b"v": b"\v"}
    out, i = bytearray(), 1
    while i < len(p) - 1:
        c = p[i:i + 1]
        if c != b"\\":
            out += c; i += 1
        elif p[i + 1:i + 2].isdigit():
            out.append(int(p[i + 1:i + 4], 8)); i += 4
        else:
            out += esc.get(p[i + 1:i + 2], p[i + 1:i + 2]); i += 2
    return bytes(out)

def cquote(p):
    if p.startswith(b'"') or b"\n" in p or b'"' in p or b"\\" in p:
        return b'"' + p.replace(b"\\", b"\\\\").replace(b'"', b'\\"').replace(b"\n", b"\\n") + b'"'
    return p

region_at = {}
for s, path in path_of.items():
    if FLAT or (path != TRUNK and path not in ROOTS):
        region_at.setdefault(rev_of[s], s)

def repair_target(path, r):
    """(commit sha, git path) that holds SVN path `path` at revision r, or None"""
    if is_git_path(path) or any(under(path, e) for e in EXCL):
        return None
    for b in sorted(BRANCHES, key=len, reverse=True):
        if path.startswith(b + "/"):
            return at.get((b, r)), path[len(b) + 1:]
        if path == b:
            return None              # whole branch replaced: the converter resets or rewrites it
    if not FLAT:
        if BASE and not under(path, BASE):
            return None              # outside this project
        if any(under(x, path) for x in list(BRANCHES) + [BASE + "/branches", BASE + "/tags"]):
            return None              # holds layout folders: not region content
    return region_at.get(r), (path[len(BASE) + 1:] if BASE else path.lstrip("/"))

repairs = defaultdict(list)            # commit sha -> [fast-import file ops]
if SVN_URL:
    e_blob = git("hash-object", "-w", "--stdin", input=b"").strip()
    for r in sorted(revs):
        for p in revs[r]["paths"]:
            if p.get("action") != "R":
                continue
            path = p["path"].rstrip("/")
            t = repair_target(path, r)
            if not t or not t[0] or not t[1]:
                continue
            sha, rel = t[0], t[1].encode()
            conv = {}
            for l in orig_b[sha]["ops"] + repairs[sha]:
                if l.startswith(b"M "):
                    f = l.rstrip(b"\n").split(b" ", 3)
                    conv[unquote(f[3])] = (f[1], l)
            td = tempfile.mkdtemp(prefix="replace-")
            dst = os.path.join(td, "x")
            x = subprocess.run(["svn", "export", "-q", "--non-interactive", "--ignore-externals", "--ignore-keywords",
                                "-r", str(r), f"{SVN_URL}{urllib.parse.quote(path)}@{r}", dst], capture_output=True, env=env)
            if x.returncode:
                sys.exit(f"ERROR: cannot export {path}@{r} to repair its replace: "
                         + x.stderr.decode("utf-8", "replace").strip())
            files, links, empty = [], [], []        # (git path, local file); git paths of empty dirs
            if os.path.isdir(dst) and not os.path.islink(dst):
                for dp, ds, fs in os.walk(dst):
                    sub = os.path.relpath(dp, dst).replace(os.sep, "/")
                    here = rel if sub == "." else rel + b"/" + os.fsencode(sub)
                    if not ds and not fs:
                        empty.append(here)
                    for d in list(ds):
                        if os.path.islink(os.path.join(dp, d)):
                            fs.append(d); ds.remove(d)
                    for fn in list(ds) + list(fs):   # .git folders and excluded projects are not migrated
                        sp = path + "/" + (fn if sub == "." else sub + "/" + fn)
                        if is_git_path(sp) or any(under(sp, e) for e in EXCL):
                            (ds if fn in ds else fs).remove(fn)
                    for fn in fs:
                        fp_ = os.path.join(dp, fn)
                        (links if os.path.islink(fp_) else files).append((here + b"/" + os.fsencode(fn), fp_))
            else:
                (links if os.path.islink(dst) else files).append((rel, dst))
            ops, have = [b"D " + cquote(rel) + b"\n"], set()
            hashes = git("hash-object", "-w", "--no-filters", "--stdin-paths",
                         input=b"".join(os.fsencode(f) + b"\n" for _, f in files)).split() if files else []
            new = [(gp, b"120000", git("hash-object", "-w", "--stdin", input=os.fsencode(os.readlink(f))).strip())
                   for gp, f in links]
            new += [(gp, b"100755" if os.stat(f).st_mode & 0o111 else b"100644", h) for (gp, f), h in zip(files, hashes)]
            new += [(d + b"/.gitignore", b"100644", e_blob) for d in empty]
            for gp, mode, h in new:     # the converter's own blob wins (raw SVN bytes, svn:ignore text)
                ops.append(conv[gp][1] if gp in conv and conv[gp][0] == mode else
                           b"M " + mode + b" " + h + b" " + cquote(gp) + b"\n")
                have.add(gp)
            ops += [l for gp, (_, l) in conv.items() if gp not in have and gp.startswith(rel + b"/")
                    and gp.rsplit(b"/", 1)[-1] == b".gitignore"]
            shutil.rmtree(td)
            repairs[sha] += ops
    print(f"replace repairs: {len(repairs)} commits")
for s, ops in repairs.items():
    orig_b[s]["ops"] += ops

# ---------- versioned .gitignore files (F25) ---------------------------------------------------
# svn-all-fast-export --svn-ignore rewrites or deletes a folder's .gitignore whenever the folder's
# properties change (svn:ignore, the svn:mergeinfo of a merge, ...): a .gitignore that is versioned in
# SVN gets its SVN content back
XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
def svn_files(paths, r):
    """the svn paths that are files at revision r"""
    out = set()
    for i in range(0, len(paths), 100):
        x = subprocess.run(["svn", "info", "--xml", "--non-interactive", "-r", str(r)]
                           + [f"{SVN_URL}{urllib.parse.quote(p)}@{r}" for p in paths[i:i + 100]], capture_output=True, env=env)
        try:
            ents = ET.fromstring(XML_BAD.sub("?", x.stdout.decode("utf-8", "replace"))).iter("entry")
        except ET.ParseError:
            sys.exit(f"ERROR: svn info r{r} failed: " + x.stderr.decode("utf-8", "replace").strip())
        root = urllib.parse.unquote(SVN_URL)
        for e in ents:
            u = urllib.parse.unquote(e.findtext("url") or "")
            if e.get("kind") == "file" and u.startswith(root + "/"):
                out.add(u[len(root):])
    return out

kept_ignores = 0
if SVN_URL and any(p["path"].rstrip("/").rsplit("/", 1)[-1] == ".gitignore" for v in revs.values() for p in v["paths"]):
    for b in commits_b:
        s, last = b["orig"], {}
        if s not in rev_of:
            continue
        for l in b["ops"]:                     # the last op on each .gitignore path wins
            if l.startswith(b"M "):
                f = l.rstrip(b"\n").split(b" ", 3)
                gp, blob = unquote(f[3]), f[2]
            elif l.startswith(b"D "):
                gp, blob = unquote(l[2:].rstrip(b"\n")), None
            else:
                continue
            if gp.rsplit(b"/", 1)[-1] == b".gitignore":
                last[gp] = blob
        if not last:
            continue
        r, path = rev_of[s], path_of[s]
        top = path if not FLAT and (path == TRUNK or path in ROOTS) else BASE
        svnp = {top + "/" + gp.decode("utf-8", "surrogateescape"): gp for gp in last}
        for sp in sorted(svn_files(sorted(svnp), r)):
            gp = svnp[sp]
            td = tempfile.mkdtemp(prefix="gitignore-")
            dst = os.path.join(td, "x")
            x = subprocess.run(["svn", "export", "-q", "--non-interactive", "--ignore-keywords", "-r", str(r),
                                f"{SVN_URL}{urllib.parse.quote(sp)}@{r}", dst], capture_output=True, env=env)
            if x.returncode:
                sys.exit(f"ERROR: cannot export {sp}@{r}: " + x.stderr.decode("utf-8", "replace").strip())
            if os.path.islink(dst):
                mode, h = b"120000", git("hash-object", "-w", "--stdin", input=os.fsencode(os.readlink(dst))).strip()
            else:
                mode = b"100755" if os.stat(dst).st_mode & 0o111 else b"100644"
                h = git("hash-object", "-w", "--no-filters", dst).strip()
            shutil.rmtree(td)
            if last[gp] != h:
                b["ops"].append(b"M " + mode + b" " + h + b" " + cquote(gp) + b"\n")
                kept_ignores += 1
    print(f"versioned .gitignore files restored after the converter's svn:ignore handling: {kept_ignores}")

# ---------- placeholder identities ------------------------------------------------------
fixed = defaultdict(int)
def fix_ident(lines):
    out = []
    for l in lines:
        kind, _, rest = l.partition(b" ")
        if kind in (b"author", b"committer", b"tagger") and rest.startswith(PLACEHOLDER + b" "):
            l = kind + b" " + NOAUTHOR + rest[len(PLACEHOLDER):]; fixed[kind.decode()] += 1
        if not l.startswith(b"original-oid "):
            out.append(l)
    return out

# ---------- rewrite: first parents + reconstructed merge parents, topological order -------
orig_mark = {b["orig"]: b["mark"] for b in commits_b}
injected = 0
for b in commits_b:
    extra = [orig_mark[g] for g in grafts.get(b["orig"], []) if g in orig_mark]
    if len(extra) != len(grafts.get(b["orig"], [])):
        sys.exit("ERROR: rewrite failed: merge parent outside exported history")
    b["parents"] = list(dict.fromkeys(([b["from"]] if b["from"] else []) + extra))
    injected += len(b["parents"]) - (1 if b["from"] else 0)
children = {b["mark"]: [] for b in commits_b}
indeg = {}
for b in commits_b:
    deps = [p for p in b["parents"] if p in children]
    indeg[b["mark"]] = len(deps)
    for p in deps:
        children[p].append(b)
heap = [(b["i"], b["mark"]) for b in commits_b if indeg[b["mark"]] == 0]
heapq.heapify(heap)
by_mark = {b["mark"]: b for b in commits_b}
order = []
while heap:
    _, m = heapq.heappop(heap); order.append(by_mark[m])
    for c in children[m]:
        indeg[c["mark"]] -= 1
        if indeg[c["mark"]] == 0:
            heapq.heappush(heap, (c["i"], c["mark"]))
if len(order) != len(commits_b):
    sys.exit("ERROR: rewrite failed: merge reconstruction would create a cycle")

out = bytearray()
for b in blocks:
    if b["kind"] == "feature":
        out += b"".join(b["pre"])
for b in order:
    if not b["parents"]:
        out += b"reset %s\n\n" % b["ref"]      # root commit: no implicit parent
    out += b"".join(fix_ident(b["pre"])) + b["data"] + b"\n"
    if b["parents"]:
        out += b"from %s\n" % b["parents"][0] + b"".join(b"merge %s\n" % m for m in b["parents"][1:])
    out += b"".join(b["ops"]) + b"\n"
for b in tags_b:                               # the LF after a tag's data ends it: no blank line here
    if "data" not in b or b["post"]:
        sys.exit(f"ERROR: unexpected tag in the fast-export stream: {b['pre'][:1]!r}")
    out += b"".join(fix_ident(b["pre"])) + b["data"] + b"\n"
for line in git("for-each-ref", "--format=%(refname) %(objectname) %(objecttype)", "refs/heads", "refs/tags").decode().splitlines():
    ref, sha, typ = line.rsplit(" ", 2)
    if typ == "commit":                        # pin every branch / lightweight tag to its rewritten tip
        if sha not in orig_mark:
            sys.exit(f"ERROR: rewrite failed: {ref} points outside the exported history")
        out += b"reset %s\nfrom %s\n\n" % (ref.encode(), orig_mark[sha])
fd, marksf = tempfile.mkstemp(prefix="marks-"); os.close(fd)
if subprocess.run([*G, "fast-import", "--quiet", "--force", f"--export-marks={marksf}"], input=bytes(out), env=env).returncode:
    sys.exit("ERROR: git fast-import rejected the rewritten history (message above); refs are unchanged")
old2new = {}
for l in open(marksf):
    mk, new = l.split()
    if mk.encode() in mark_orig:
        old2new[mark_orig[mk.encode()]] = new
os.remove(marksf)
missing_marks = [b["orig"] for b in commits_b if b["orig"] not in old2new]
if missing_marks:
    sys.exit(f"ERROR: rewrite failed: {len(missing_marks)} commits not imported, e.g. {missing_marks[:3]}")

for ref, entries in notes.items():
    msg = b"Re-keyed notes after merge reconstruction\n"
    stream = bytearray(b"commit %s\ncommitter SVN Migration <migration@example.com> 0 +0000\n"
                       b"data %d\n%s" % (ref.encode(), len(msg), msg))
    stream += b"deleteall\n"
    for old, blob in entries.items():
        stream += b"N %s %s\n" % (blob.encode(), old2new.get(old, old).encode())
    if subprocess.run([*G, "fast-import", "--quiet", "--force"], input=bytes(stream), env=env).returncode:
        sys.exit(f"ERROR: re-keying {ref} failed (message above); history is rewritten but the notes are not")
print(f"rewrote {len(commits_b)} commits, injected {injected} merge parents, dropped {dropped} converter merge parents "
      f"(file copies between branches), fixed {fixed['author']} placeholder authors and {fixed['tagger']} taggers, "
      f"re-keyed notes in {len(notes)} refs")
git("reflog", "expire", "--expire=now", "--all")
git("gc", "-q", "--prune=now")
if report_f:
    json.dump({"merges_complete": full, "merges_partial": partial}, open(report_f, "w", encoding="utf-8"), indent=1)
