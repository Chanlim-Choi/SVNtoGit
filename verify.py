#!/usr/bin/env python3
"""Verify a migrated Git repo against its SVN source.

usage: verify.py SVN_URL GIT_REPO LOG_JSON [--samples N] [--trailer]

Checks
  1. every SVN branch/tag at HEAD has a Git ref, with identical file manifests
     (path, content, exec bit, symlink); and vice versa
  2. historical commits (--samples N, default all): tree == SVN path@rev it came from
  3. every SVN revision is represented by >=1 commit (or explained)
  4. commit message / author / date identical to svn log, for every commit
  5. every commit has a refs/notes/svn note carrying its revision + all revprops
  6. every complete SVN merge (svn:mergeinfo) is a git ancestor of the merging commit
  7. GitHub limits: blobs >100 MB (hard reject), >50 MB (warning)
Exit status 0 only if nothing outside the "info"/"explained" categories was found.
"""
import hashlib, json, os, random, re, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

url, repo, logf = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
_s = sys.argv[sys.argv.index("--samples") + 1] if "--samples" in sys.argv else "all"
N = 10**9 if _s == "all" else int(_s)
JOBS = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else (os.cpu_count() or 4) * 2
revs = {int(k): v for k, v in json.load(open(logf)).items()}
env = dict(os.environ, LC_ALL="C.UTF-8")
random.seed(7)
PROJECT = sys.argv[sys.argv.index("--project") + 1] if "--project" in sys.argv else "/"
NONSTD = PROJECT == "NONE"
PX = "" if PROJECT in ("/", "NONE") else PROJECT.strip("/") + "/"   # svn path prefix of this project
AUTHORS = {}   # svn login -> "Name <email>" (git-svn authors file format)
if "--authors" in sys.argv:
    for line in open(sys.argv[sys.argv.index("--authors") + 1], encoding="utf-8"):
        if "=" in line:
            k, v = line.split("=", 1)
            AUTHORS[k.strip()] = v.strip()
problems = {}
STATS = {}

def bad(cat, msg):
    problems.setdefault(cat, []).append(msg)

def run(*a, **kw):
    return subprocess.run(a, check=True, capture_output=True, env=env, **kw).stdout

def git(*a):
    return run("git", "-C", repo, *a)

def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

def svn_manifest(path, rev):
    with tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(logf))) as td:
        dst = os.path.join(td, "x")
        try:
            run("svn", "export", "-q", "--ignore-externals", "--ignore-keywords",
                "-r", str(rev), f"{url}/{path}".rstrip("/") + f"@{rev}", dst)
        except subprocess.CalledProcessError:
            return None
        man, empty = {}, []
        for root, ds, fs in os.walk(dst):
            if not ds and not fs and root != dst:
                empty.append(os.path.relpath(root, dst))
            for d in list(ds):
                p = os.path.join(root, d)
                if os.path.islink(p):
                    fs.append(d); ds.remove(d)
            for fn in fs:
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, dst)
                if os.path.islink(p):
                    man[rel] = ("120000", blob_sha(os.readlink(p).encode()))
                else:
                    mode = "100755" if os.stat(p).st_mode & 0o111 else "100644"
                    data = open(p, "rb").read()
                    man[rel] = (mode, blob_sha(data))
                    if len(data) > 1 << 20:
                        BIG256[man[rel]] = hashlib.sha256(data).hexdigest()
        return man, empty

BIG256 = {}
LFS = re.compile(rb"^version https://git-lfs\S*\noid sha256:([0-9a-f]{64})\n")
def lfs_ok(sv, gv):
    """git blob is an LFS pointer to exactly the SVN content"""
    if sv[0] != gv[0] or sv not in BIG256:
        return False
    m = LFS.match(git("cat-file", "blob", gv[1]))
    return bool(m) and m.group(1).decode() == BIG256[sv]

def git_manifest(ref):
    man = {}
    for line in git("ls-tree", "-r", "-z", "--full-tree", ref).split(b"\0"):
        if not line:
            continue
        meta, p = line.split(b"\t", 1)
        mode, _, sha = meta.decode().split()
        man[p.decode()] = (mode, sha)
    man.pop(".gitignore", None)
    return man

def compare(label, svnpath, rev, ref, prefix=""):
    r = svn_manifest(svnpath, rev)
    if r is None:
        bad("tree", f"{label}: cannot export {svnpath}@{rev}"); return False
    s, empty = r
    g = git_manifest(ref)
    if prefix:   # content kept under a folder of a collecting branch (svn-outside-layout)
        s = {f"{prefix}/{k}": v for k, v in s.items()}
        empty = [f"{prefix}/{d}" for d in empty]
        g = {k: v for k, v in g.items() if k.startswith(prefix + "/")}
    # .gitignore files generated from svn:ignore / to keep empty dirs are not SVN content
    g = {p: v for p, v in g.items() if not (p.rsplit("/", 1)[-1] in (".gitignore", ".gitattributes") and p not in s)}
    placeholders = {p.rsplit("/", 1)[0] for p in git_manifest(ref) if p.endswith("/.gitignore")}
    lost = [d for d in empty if d not in placeholders]
    if lost:
        problems.setdefault("info: empty dirs not preserved (git cannot store them)", []).append(
            f"{label}: {len(lost)} e.g. {lost[:2]}")
    elif empty:
        STATS["empty dirs kept via .gitignore placeholder"] = STATS.get("empty dirs kept via .gitignore placeholder", 0) + len(empty)
    for p in [p for p in set(s) & set(g) if s[p] != g[p] and lfs_ok(s[p], g[p])]:
        STATS["large files verified through LFS pointer"] = STATS.get("large files verified through LFS pointer", 0) + 1
        g[p] = s[p]
    if s == g:
        return True
    miss = sorted(set(s) - set(g)); extra = sorted(set(g) - set(s))
    diff = sorted(p for p in set(s) & set(g) if s[p] != g[p])
    kinds = sorted({"mode" if s[p][0] != g[p][0] and s[p][1] == g[p][1] else "content" for p in diff})
    bad("tree", f"{label} vs {svnpath}@{rev}: missing={len(miss)} extra={len(extra)} "
                f"differ={len(diff)}{kinds} e.g. {(miss + extra + diff)[:3]}")
    return False

# ---------- commit <-> revision map -----------------------------------------
RX = [re.compile(r"\n*git-svn-id: \S+?(/[^@\s]*)?@(\d+) \S+\s*$"),
      re.compile(r"\n*svn path=([^;]*); revision=(\d+)\s*$")]
NOTE_RX = re.compile(r"svn path=([^;]*); revision=(\d+)")
commits = {}
out = git("log", "--branches", "--tags", "--format=%H%x00%an <%ae>%x00%aI%x00%B%x00%N%x01", "--notes=commits").decode("utf-8", "replace")
for rec in out.split("\x01"):
    rec = rec.lstrip("\n")
    if not rec:
        continue
    sha, who, date, body, note = (rec.split("\x00") + [""] * 5)[:5]
    for rx in RX:
        mt = rx.search(body) or (NOTE_RX.search(note) if rx is RX[1] else None)
        if mt:
            clean = rx.sub("", body) if rx.search(body) else body
            path = mt.group(1) or "/"
            if rx is RX[0]:  # git-svn-id carries the full URL: make it relative to the repo URL
                full = re.search(r"git-svn-id: (\S+)@\d+ ", body).group(1)
                path = full[len(url):] or "/" if full.startswith(url) else path
            commits[sha] = dict(path=path, rev=int(mt.group(2)), who=who, date=date, msg=clean)
            break
    else:
        commits[sha] = dict(path=None, rev=None, who=who, date=date, msg=body)

mapped = {c["rev"] for c in commits.values() if c["rev"]}
print(f"git commits={len(commits)}  mapped to SVN={sum(1 for c in commits.values() if c['rev'])}  "
      f"svn revisions={len(revs)}  revisions represented={len(mapped)}")

# ---------- 3. revision coverage -------------------------------------------
LAYOUT = re.compile(r"^/" + re.escape(PX) + r"(trunk|branches|tags)(/|$)") if not NONSTD else re.compile(r"^/")
for r, v in sorted(revs.items()):
    if r in mapped:
        continue
    paths = [p["path"] for p in v["paths"]]
    if not paths:
        why = "empty revision (no changes)"
    elif PX and not any(p.startswith("/" + PX) or ("/" + PX).startswith(p.rstrip("/") + "/") for p in paths):
        why = "belongs to another project"
    elif not any(LAYOUT.match(p) for p in paths):
        why = "outside trunk/branches/tags: " + ", ".join(paths[:2])
    elif all(p.get("action") == "D" for p in v["paths"]):
        why = "only deletions (e.g. branch delete): " + ", ".join(paths[:2])
    elif all(p.get("action") == "A" and "copyfrom" in p and p["path"].startswith(f"/{PX}tags/") for p in v["paths"]):
        why = "tag creation (represented as git tag)"
    elif r == 1:
        why = "layout creation (no files)"
    else:
        why = None
    bad("revisions missing (explained)" if why else "revisions missing (UNEXPLAINED)", f"r{r}: {why or paths[:3]}")

# ---------- 4. message / author / date ---------------------------------------
def norm(s):
    return "\n".join(l.rstrip() for l in s.strip().splitlines())
for sha, c in commits.items():
    if not c["rev"]:
        continue
    v = revs[c["rev"]]
    if norm(c["msg"]) != norm(v["log"]):
        bad("message", f"r{c['rev']} {sha[:9]}: svn={v['log'][:40]!r} git={c['msg'][:40]!r}")
    if v["date"] and c["date"][:19] != v["date"][:19].replace("Z", ""):
        # git prints local offset; compare in UTC
        from datetime import datetime, timezone
        gd = datetime.fromisoformat(c["date"]).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        if gd != v["date"][:19]:
            bad("date", f"r{c['rev']}: svn={v['date']} git={c['date']}")
    want = AUTHORS.get(v["author"] or "(no author)")
    if (want and c["who"] != want) or "@localhost>" in c["who"] or re.search(r"@[0-9a-f]{8}-[0-9a-f-]{27}>", c["who"]):
        bad("author", f"r{c['rev']}: svn={v['author']!r} git={c['who']!r} expected={want!r}")

# ---------- 1. refs vs SVN branches/tags at HEAD -----------------------------
head = max(revs)
def svn_ls(p):
    try:
        return [x.rstrip("/") for x in run("svn", "ls", f"{url}/{p}").decode().splitlines() if x.endswith("/")]
    except subprocess.CalledProcessError:
        return []
svn_branches = set() if NONSTD else set(svn_ls(PX + "branches"))
svn_tags = set() if NONSTD else set(svn_ls(PX + "tags"))
refs = {}
for line in git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags").decode().splitlines():
    name, sha = line.split()
    refs[name] = sha

def sanitize(n):  # same renaming as make_rules.py / migrate_gitsvn.sh
    s = re.sub(r"\.\.+", ".", n)
    s = re.sub(r"[ ~^:?*\[\\]", "-", s)
    return re.sub(r"@\{", "-", s).strip("/.").replace("/.", "/")

seen = set()
for kind, items, prefix in (("branch", svn_branches, "refs/heads/"), ("tag", svn_tags, "refs/tags/")):
    for n in sorted(items):
        cands = [prefix + n, prefix + sanitize(n), prefix + n.replace(" ", "%20")]
        ref = next((c for c in cands if c in refs), None)
        if not ref and any(r.startswith(prefix + n + "/") for r in refs):
            # container directory (e.g. branches/hotfix holding 1.x): check its children
            sub = "branches" if kind == "branch" else "tags"
            for child in svn_ls(f"{PX}{sub}/{n}"):
                cref = prefix + sanitize(f"{n}/{child}")
                if cref in refs:
                    seen.add(cref)
                    compare(f"{kind} {n}/{child}", f"{PX}{sub}/{n}/{child}", head, cref + "^{commit}")
                else:
                    bad("refs: SVN branch/tag with no git ref", f"{kind} {n}/{child!r}")
            continue
        if not ref:
            bad("refs: SVN branch/tag with no git ref", f"{kind} {n!r}")
            continue
        seen.add(ref)
        compare(f"{kind} {n}", f"{PX}{'branches' if kind == 'branch' else 'tags'}/{n}", head, ref + "^{commit}")
main = "refs/heads/main" if "refs/heads/main" in refs else "refs/heads/master"
seen.add(main)
compare("trunk" if not NONSTD else "repository root", PX + "trunk" if not NONSTD else "", head, main)
for ref in sorted(set(refs) - seen):
    problems.setdefault("info: extra git refs (deleted SVN branches / nested dirs)", []).append(ref)

# ---------- 2. random historical commits ------------------------------------
pool = [s for s, c in commits.items() if c["rev"] and c["path"] is not None and (NONSTD or c["path"] != "/")]
sample = random.sample(pool, min(N, len(pool)))
def check_commit(sha):
    c = commits[sha]
    rel = c["path"].strip("/")[len(PX):] if c["path"].strip("/").startswith(PX) else c["path"].strip("/")
    outside = not NONSTD and rel.split("/")[0] not in ("trunk", "branches", "tags")
    return compare(f"commit {sha[:9]} r{c['rev']}", c["path"].strip("/"), c["rev"], sha,
                   prefix=rel.split("/")[0] if outside else "")
with ThreadPoolExecutor(JOBS) as ex:
    ok = sum(ex.map(check_commit, sample))
print(f"historical commits: {ok}/{len(sample)} trees identical to SVN "
      f"({'ALL commits' if len(sample) == len(pool) else 'random sample'})")

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
            out[src.rstrip("/")] = [tuple(int(x) for x in (r.rstrip("*").split("-") * 2)[:2])
                                    for r in rr.split(",") if r.strip()]
    return out
cur, merges_ok, merges_partial = {}, 0, 0
for r in sorted(revs):
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
            if not tgt or rl == old.get(src) or src not in by_path or src == path:
                continue
            top = min(max(b for _, b in rl), r)
            srcs = [x for x in by_path[src] if x[0] <= top]
            if not srcs:
                continue
            anc = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", srcs[-1][1], tgt]).returncode == 0
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
blobs = {}
if notes:
    p = subprocess.run(["git", "-C", repo, "cat-file", "--batch"], input="\n".join(notes[s] for s in need if s in notes).encode(),
                       capture_output=True, check=True).stdout
    i = 0
    while i < len(p):
        hdr_end = p.index(b"\n", i); sha, _, size = p[i:hdr_end].decode().split()
        blobs[sha] = p[hdr_end + 1: hdr_end + 1 + int(size)].decode("utf-8", "replace"); i = hdr_end + 2 + int(size)
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

# ---------- 6. GitHub limits -------------------------------------------------
objs = run("sh", "-c", f"git -C '{repo}' rev-list --objects --all | git -C '{repo}' cat-file --batch-check='%(objecttype) %(objectsize) %(rest)'").decode()
for line in objs.splitlines():
    t, size, *rest = line.split(" ", 2)
    if t == "blob" and int(size) > 50 * 2**20:
        cat = "GITHUB BLOCKER: blob >100MB in history" if int(size) > 100 * 2**20 else "info: github warns on blobs >50MB"
        bad(cat, f"{int(size)/2**20:.0f} MB {rest[0] if rest else ''}")
size = run("git", "-C", repo, "count-objects", "-vH").decode()
print("repo size:", " ".join(l for l in size.splitlines() if l.startswith(("size-pack", "count"))))

# ---------- report -----------------------------------------------------------
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
if "--json" in sys.argv:
    json.dump({"problems": problems, "stats": STATS, "fatal": fatal},
              open(sys.argv[sys.argv.index("--json") + 1], "w"), indent=1, ensure_ascii=False)
print("\nRESULT:", "PASS - every check passed" if not fatal else f"FAIL - {len(fatal)} failing check(s)")
sys.exit(1 if fatal else 0)
