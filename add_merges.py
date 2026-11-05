#!/usr/bin/env python3
"""Rebuild merge commits from svn:mergeinfo, fix placeholder authors, keep notes.

usage: add_merges.py GIT_REPO LOG_JSON [REPORT_JSON] [--no-author "Name <email>"] [--svn-url URL]

With --svn-url it also repairs SVN "replace" actions (e.g. a file replaced by a
directory), whose new content svn-all-fast-export drops: the replaced path is set to
the exact SVN content of that revision.

svn-all-fast-export imports every SVN merge as a plain commit. This script reads
svn:mergeinfo from the SVN metadata (svn_meta.py) and, for every revision where a
branch root's mergeinfo grew, adds the merged source commit as a second parent -
but only for COMPLETE merges (every revision of the source branch up to the merge
point is in the merged ranges), the same rule `git svn` uses. Partial merges
(cherry-picks) are not representable in git; they are listed in the report and
remain recorded in the notes.

History is rewritten (fast-export | fast-import, in place); notes in refs/notes/*
are re-keyed to the new commit IDs.
"""
import bisect, json, re, subprocess, sys
from collections import defaultdict

repo, logf = sys.argv[1], sys.argv[2]
report_f = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else None
revs = {int(k): v for k, v in json.load(open(logf, encoding="utf-8")).items()}
G = ["git", "-C", repo]

def git(*a, **kw):
    return subprocess.run([*G, *a], check=True, capture_output=True, **kw).stdout.decode()

# ---------- commit <-> (svn path, rev) from notes -------------------------------
NOTE = re.compile(r"svn path=([^;]*); revision=(\d+)|SVN-Revision: r(\d+)\nSVN-Path: (\S[^\n]*)")
by_path = defaultdict(list)     # "/trunk" -> sorted [(rev, sha)]
at = {}                         # (path, rev) -> sha
for ref in ("commits", "svn"):
    try:
        out = git("log", "--branches", "--tags", f"--notes={ref}", "--format=%H%x00%N%x01")
    except subprocess.CalledProcessError:
        continue
    for rec in out.split("\x01"):
        rec = rec.strip("\n")
        if "\x00" not in rec:
            continue
        sha, note = rec.split("\x00", 1)
        m = NOTE.search(note)
        if not m:
            continue
        path, rev = (m.group(1), m.group(2)) if m.group(1) is not None else (m.group(4), m.group(3))
        key = (path.rstrip("/") or "/", int(rev))
        if key not in at:
            at[key] = sha
            by_path[key[0]].append((int(rev), sha))
for v in by_path.values():
    v.sort()
if not at:
    sys.exit("no commit<->revision mapping found in notes")

def commit_at_or_before(path, rev):
    lst = by_path.get(path, [])
    i = bisect.bisect_right(lst, (rev, "￿")) - 1
    return lst[i] if i >= 0 else None

# ---------- mergeinfo parsing ----------------------------------------------------
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
            rl.append((int(a), int(b or a)))
        out[src.rstrip("/")] = rl
    return out

def covered(rl, r):
    return any(a <= r <= b for a, b in rl)

roots = set(by_path)
current = {}                     # branch root -> parsed mergeinfo
grafts = {}                      # sha -> [extra parent shas]
partial, full = [], []
for r in sorted(revs):
    v = revs[r]
    for p in v["paths"]:
        path = p["path"].rstrip("/")
        if p.get("action") in ("A", "R") and "copyfrom" in p and path in roots:
            src = p["copyfrom"].rsplit("@", 1)[0].rstrip("/")
            current[path] = dict(current.get(src, {}))   # mergeinfo is inherited on copy
        if "props" not in p or path not in roots:
            continue
        new = parse_mi(p["props"].get("svn:mergeinfo", ""))
        old = current.get(path, {})
        current[path] = new
        tgt = at.get((path, r))
        if not tgt:
            continue
        for src, rl in new.items():
            if rl == old.get(src) or src not in by_path or src == path:
                continue
            top = max(b for _, b in rl)
            cand = commit_at_or_before(src, min(top, r))
            if not cand:
                continue
            # complete merge = every commit of the source branch up to the merge point is in range
            # (revisions already merged earlier, or inherited by copy, count as covered)
            missing = [cr for cr, _ in by_path[src] if cr <= cand[0] and not covered(rl, cr)
                       and not covered(old.get(src, []), cr)
                       and subprocess.run([*G, "merge-base", "--is-ancestor", at[(src, cr)], tgt]).returncode]
            if missing:
                partial.append({"rev": r, "into": path, "from": src, "unmerged_revs": missing[:20]})
                continue
            if subprocess.run([*G, "merge-base", "--is-ancestor", cand[1], tgt]).returncode == 0:
                continue  # already in history
            grafts.setdefault(tgt, []).append(cand[1])
            full.append({"rev": r, "into": path, "from": src, "source_rev": cand[0]})

print(f"merges: {len(full)} complete -> merge commits, {len(partial)} partial (cherry-picks, kept in notes only)")

# ---------- rewrite: inject extra parents into the fast-export stream + author fix --
notes = {}
for line in git("for-each-ref", "--format=%(refname) %(objectname)", "refs/notes/").splitlines():
    ref, sha = line.split()
    entries = {}
    for row in git("ls-tree", "-r", sha).splitlines():
        meta, path = row.split("\t")
        entries[path.replace("/", "")] = meta.split()[2]
    notes[ref] = entries

import heapq, os, tempfile, shutil

# ---------- repairs for SVN replace ("R") actions ----------------------------------
SVN_URL = sys.argv[sys.argv.index("--svn-url") + 1].rstrip("/") if "--svn-url" in sys.argv else None
repairs = defaultdict(list)            # original commit sha -> [fast-import file ops]
def cquote(pth):
    if any(ch in pth for ch in '"\\\n') or pth.startswith('"'):
        return '"' + pth.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return pth
if SVN_URL:
    roots_sorted = sorted(by_path, key=len, reverse=True)
    for r in sorted(revs):
        for p in revs[r]["paths"]:
            if p.get("action") != "R":
                continue
            path = p["path"].rstrip("/")
            root = next((x for x in roots_sorted if x != "/" and path.startswith(x + "/")), None)
            if not root or (root, r) not in at:
                continue
            rel = path[len(root) + 1:]
            td = tempfile.mkdtemp(prefix="replace-")
            dst = os.path.join(td, "x")
            subprocess.run(["svn", "export", "-q", "--ignore-externals", "--ignore-keywords", "-r", str(r),
                            f"{SVN_URL}{path}@{r}", dst], check=True, capture_output=True)
            ops = [f"D {cquote(rel)}\n".encode()]
            def add(fp, gp):
                if os.path.islink(fp):
                    sha = subprocess.run([*G, "hash-object", "-w", "--stdin"], input=os.readlink(fp).encode(),
                                         capture_output=True, check=True).stdout.decode().strip()
                    mode = "120000"
                else:
                    sha = git("hash-object", "-w", "--no-filters", fp).strip()
                    mode = "100755" if os.stat(fp).st_mode & 0o111 else "100644"
                ops.append(f"M {mode} {sha} {cquote(gp)}\n".encode())
            if os.path.isdir(dst) and not os.path.islink(dst):
                for dp, ds, fs in os.walk(dst):
                    for d in list(ds):
                        if os.path.islink(os.path.join(dp, d)):
                            fs.append(d); ds.remove(d)
                    if not ds and not fs and dp != dst:   # keep empty dirs like --empty-dirs does
                        e = git("hash-object", "-w", "--stdin", input=b"").strip()
                        ops.append(f"M 100644 {e} {cquote(rel + '/' + os.path.relpath(dp, dst) + '/.gitignore')}\n".encode())
                    for fn in fs:
                        fp = os.path.join(dp, fn)
                        add(fp, rel + "/" + os.path.relpath(fp, dst))
            else:
                add(dst, rel)
            shutil.rmtree(td)
            repairs[at[(root, r)]] += ops
    print(f"replace repairs: {sum(1 for _ in repairs)} commits")
NOAUTHOR = sys.argv[sys.argv.index("--no-author") + 1] if "--no-author" in sys.argv else "Unknown <unknown@example.com>"
FIX = {b"nobody <nobody@localhost>": NOAUTHOR.encode()}

# Export WITHOUT file contents (blobs stay in this repo and are referenced by id), so the
# whole stream fits in memory and commits can be re-ordered: a merge source must be
# imported before the commit that merges it, which date order alone does not guarantee
# (SVN commits can share a timestamp).
raw = subprocess.run([*G, "fast-export", "--no-data", "--signed-tags=strip", "--tag-of-filtered-object=rewrite",
                      "--reencode=no", "--mark-tags", "--show-original-ids", "--topo-order",
                      "--branches", "--tags"], capture_output=True, check=True).stdout
blocks, cur, pos = [], None, 0
START = (b"commit ", b"tag ", b"reset ", b"feature ", b"progress ", b"done")
while pos < len(raw):
    nl = raw.index(b"\n", pos); line = raw[pos:nl + 1]; pos = nl + 1
    if line.startswith(b"data "):
        n = int(line[5:]); cur["lines"].append(line + raw[pos:pos + n]); pos += n
        continue
    if line.startswith(START) or cur is None:
        cur = {"kind": line.split()[0].decode(), "lines": [line]}; blocks.append(cur)
        continue
    cur["lines"].append(line)

fixed_authors = injected = 0
mark_orig, orig_mark, by_mark, commits_b = {}, {}, {}, []
for b in blocks:
    keep = []
    for l in b["lines"]:
        if l.startswith(b"mark :"):
            b["mark"] = l[5:].strip()
        elif l.startswith(b"original-oid "):
            b["orig"] = l[13:].strip().decode(); continue
        elif b["kind"] == "commit" and l.startswith((b"author ", b"committer ")):
            for x, y in FIX.items():
                if x in l:
                    l = l.replace(x, y); fixed_authors += l.startswith(b"author ")
        keep.append(l)
    while keep and keep[-1] == b"\n":   # exactly one optional LF is re-added on output
        keep.pop()
    b["lines"] = keep
    if b["kind"] in ("commit", "tag") and "mark" in b and "orig" in b:
        mark_orig[b["mark"]] = b["orig"]
    if b["kind"] == "commit":
        b["ref"] = b["lines"][0].split(b" ", 1)[1].strip()
        orig_mark[b["orig"]] = b["mark"]; by_mark[b["mark"]] = b; commits_b.append(b)

for b in commits_b:                        # repaired content for SVN replace actions
    if b["orig"] in repairs:              # after the converter's own file ops, so they win
        b["lines"] += repairs[b["orig"]]
for i, b in enumerate(commits_b):          # parents, plus reconstructed merge parents
    b["i"] = i
    b["parents"] = [l.split(b" ", 1)[1].strip() for l in b["lines"] if l.startswith((b"from ", b"merge "))]
    extra = [orig_mark[g] for g in grafts.get(b["orig"], []) if g in orig_mark and orig_mark[g] not in b["parents"]]
    if len(extra) != len(grafts.get(b["orig"], [])):
        sys.exit("rewrite failed: merge parent outside exported history")
    if extra:
        last = max((k for k, l in enumerate(b["lines"]) if l.startswith((b"from ", b"merge ", b"data "))))
        if not any(l.startswith(b"from ") for l in b["lines"]):
            b["lines"].insert(last + 1, b"from %s\n" % extra[0]); last += 1
            extra_lines = [b"merge %s\n" % m for m in extra[1:]]
        else:
            extra_lines = [b"merge %s\n" % m for m in extra]
        b["lines"][last + 1:last + 1] = extra_lines
        b["parents"] += extra; injected += len(extra)

# topological order, ties broken by the original (topo) order
children = {b["mark"]: [] for b in commits_b}
indeg = {}
for b in commits_b:
    deps = [p for p in b["parents"] if p in by_mark]
    indeg[b["mark"]] = len(deps)
    for p in deps:
        children[p].append(b["mark"])
heap = [(b["i"], b["mark"]) for b in commits_b if indeg[b["mark"]] == 0]
heapq.heapify(heap)
order = []
while heap:
    _, m = heapq.heappop(heap); order.append(by_mark[m])
    for c in children[m]:
        indeg[c] -= 1
        if indeg[c] == 0:
            heapq.heappush(heap, (by_mark[c]["i"], c))
if len(order) != len(commits_b):
    sys.exit("rewrite failed: merge reconstruction would create a cycle")

out = bytearray()
for b in blocks:
    if b["kind"] == "feature":
        out += b"".join(b["lines"])
for b in order:
    if not any(l.startswith(b"from ") for l in b["lines"]):
        out += b"reset %s\n\n" % b["ref"]      # root commit: no implicit parent
    out += b"".join(b["lines"]) + b"\n"
for b in blocks:
    if b["kind"] == "tag":
        out += b"".join(b["lines"]) + b"\n"
for line in git("for-each-ref", "--format=%(refname) %(objectname) %(objecttype)", "refs/heads", "refs/tags").splitlines():
    ref, sha, typ = line.split()
    if typ == "commit":                        # pin every branch / lightweight tag to its rewritten tip
        out += b"reset %s\nfrom %s\n\n" % (ref.encode(), orig_mark[sha])
fd, marksf = tempfile.mkstemp(prefix="marks-"); os.close(fd)
subprocess.run([*G, "fast-import", "--quiet", "--force", f"--export-marks={marksf}"], input=bytes(out), check=True)
old2new = {}
for l in open(marksf):
    mk, new = l.split()
    if mk.encode() in mark_orig:
        old2new[mark_orig[mk.encode()]] = new
os.remove(marksf)
orig_mark_count = len(commits_b)

for ref, entries in notes.items():
    msg = b"Re-keyed notes after merge reconstruction\n"
    stream = bytearray(b"commit %s\ncommitter SVN Migration <migration@example.com> 0 +0000\n"
                       b"data %d\n%s" % (ref.encode(), len(msg), msg))
    stream += b"deleteall\n"
    for old, blob in entries.items():
        stream += b"N %s %s\n" % (blob.encode(), old2new.get(old, old).encode())
    subprocess.run([*G, "fast-import", "--quiet", "--force"], input=bytes(stream), check=True)
print(f"rewrote {orig_mark_count} commits, injected {injected} merge parents, "
      f"fixed {fixed_authors} placeholder authors, re-keyed notes in {len(notes)} refs")
git("reflog", "expire", "--expire=now", "--all")
git("gc", "-q", "--prune=now")
if report_f:
    json.dump({"merges_complete": full, "merges_partial": partial}, open(report_f, "w"), indent=1)
