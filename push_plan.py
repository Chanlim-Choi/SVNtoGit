#!/usr/bin/env python3
"""Plan the pushes of a migrated repository so that each push stays under a size limit
(GitHub refuses a push over 2 GiB).

usage: push_plan.py GIT_REPO [--limit BYTES (default 1 GiB)] [--max-commits 2000] [--max-refs 50]

Prints one push per line (space-separated refspecs). The estimate of a push is the sum of
the sizes of the blobs it sends, i.e. the blobs that no earlier line has sent (uncompressed,
so it is on the safe side). Order:
  1. main, cut along its first-parent chain: "<sha>:refs/heads/main" lines (at most
     --max-commits commits each, cut earlier at --limit), then "refs/heads/main:refs/heads/main".
     A merge that brings in more than one push can carry sends each merged line first, in
     chunks, through a temporary branch refs/heads/svn-migration-tmp[-<depth>[-<n>]] (at any
     depth: merges inside a merged line are split the same way).
  2. the other branches and tags (oldest first) in groups of at most --max-refs, halved while
     a group is over the limits; a single ref that is still over them goes in first-parent
     chunks through refs/heads/svn-migration-tmp. A final line deletes every temporary branch.
A single commit above the limit cannot be split: "ERROR: ..." on stderr, exit 1.
"""
import os, re, subprocess, sys

def opt(name, default):
    if name not in sys.argv:
        return default
    v = (sys.argv[sys.argv.index(name) + 1:] + [""])[0]
    if not v.isdigit() or int(v) < 1:
        sys.exit(f"ERROR: {name} needs a positive whole number, got {v!r}")
    return int(v)

if len(sys.argv) < 2 or sys.argv[1].startswith("-"):
    sys.exit(__doc__)
repo = sys.argv[1]
LIMIT, MAXC, MAXR = opt("--limit", 1 << 30), opt("--max-commits", 2000), opt("--max-refs", 50)
MAIN, TMP = "refs/heads/main", "refs/heads/svn-migration-tmp"
lc = os.environ.get("LC_ALL", "")
env = dict(os.environ, LC_ALL=lc if re.search("utf-?8", lc, re.I) else "C.UTF-8")

def git(*a, inp=None):
    r = subprocess.run(["git", "--no-replace-objects", "-C", repo, *a], input=inp, capture_output=True, env=env)
    if r.returncode:
        sys.exit(f"ERROR: git {a[0]} failed in {repo}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout

def human(b):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if b < 1024 or unit == "GiB":
            return f"{b} B" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024

def raw_diff(out):
    """(commit, blob, path) per path whose blob differs from every parent (diff-tree -c -z output)"""
    toks, cur, i = out.split(b"\0"), None, 0
    while i < len(toks):
        t = toks[i]
        if t[:1] == b":":
            n = len(t) - len(t.lstrip(b":"))          # one ':' per parent
            f = t[n:].split()
            if f[n] not in (b"000000", b"160000"):     # not deleted, not a submodule
                yield cur, f[2 * n + 1], toks[i + 1]
            i += 2
        else:
            cur = t.strip() or cur
            i += 1

# ---------- refs, commit graph, blobs each commit introduces ---------------------
refs = []                        # (refname, commit or None), main excluded, oldest first
tips = {}
for line in git("for-each-ref", "--sort=creatordate", "--format=%(refname) %(objecttype) %(objectname) "
                "%(*objecttype) %(*objectname)", "refs/heads", "refs/tags").decode("utf-8", "surrogateescape").splitlines():
    name, typ, sha, ptyp, psha = line.split(" ")
    c = sha if typ == "commit" else psha if ptyp == "commit" else None
    if typ == "tag" and c is None:               # tag of a tag
        r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "-q", name + "^{commit}"],
                           capture_output=True, env=env)
        c = r.stdout.decode().strip() or None
    tips[name] = c
    if name != MAIN:
        refs.append((name, c))
if not tips.get(MAIN):
    sys.exit(f"ERROR: {repo} has no branch main; nothing to push")

revlist = git("rev-list", "--parents", "--branches", "--tags")
parents = {}
for line in revlist.decode().splitlines():
    c, *ps = line.split()
    parents[c] = ps
intro = {}                       # commit -> blobs it adds that none of its parents has at that path
for c, blob, _ in raw_diff(git("diff-tree", "--stdin", "-r", "-c", "--root", "-z", "--no-renames", inp=revlist)):
    intro.setdefault(c.decode(), []).append(blob)
size = {}
blobs = {b for v in intro.values() for b in v}
if blobs:
    for line in git("cat-file", "--batch-check=%(objectname) %(objectsize)", inp=b"\n".join(blobs) + b"\n").split(b"\n"):
        f = line.split()
        if len(f) == 2 and f[1].isdigit():
            size[f[0]] = int(f[1])

# ---------- planning -------------------------------------------------------------
planned, seen = set(), set()     # commits and blobs sent by the lines so far
lines = []                       # (refspecs, estimate)

def new_commits(heads):
    got = {h for h in heads if h and h not in planned}
    stack = list(got)
    while stack:
        for p in parents.get(stack.pop(), ()):
            if p not in planned and p not in got:
                got.add(p)
                stack.append(p)
    return got

def cost(cs):
    bs = {b for c in cs for b in intro.get(c, ()) if b not in seen}
    return sum(size.get(b, 0) for b in bs), bs

def over(b, n):
    return b > LIMIT or n > MAXC

def describe(c):
    """commit id with its SVN revision and path from the notes"""
    for ref in ("svn", "commits"):
        r = subprocess.run(["git", "-C", repo, "notes", "--ref=" + ref, "show", c], capture_output=True, env=env)
        m = re.search(r"SVN-Revision: r(\d+)\nSVN-Path: ([^\n]*)|svn path=([^;]*); revision=(\d+)",
                      r.stdout.decode("utf-8", "replace"))
        if m:
            return f"{c} (SVN r{m.group(1) or m.group(4)}, {m.group(2) or m.group(3)})"
    return c

def too_big(c, b, cs):
    if len(cs) > 1:
        what = f"brings in {len(cs) - 1} commits of merged history"
    else:
        big = sorted(((size.get(bl, 0), p.decode("utf-8", "replace")) for _, bl, p in
                      raw_diff(git("diff-tree", "-r", "-c", "--root", "-z", "--no-renames", c))), reverse=True)
        what = "adds " + ", ".join(f"{p} ({human(s)})" for s, p in big[:5]) + (" ..." if len(big) > 5 else "")
    sys.exit(f"ERROR: commit {describe(c)} {what}: {human(b)} of new file content in one push, more than the "
             f"limit of {human(LIMIT)} (GitHub refuses pushes over 2 GiB). Move the large files of that revision to "
             f"Git LFS with a lower --lfs-above, or, if the content compresses well (source text: the estimate "
             f"counts uncompressed sizes), set PUSH_LIMIT_BYTES above {b} and push again.")

def tmp_ref(depth, j):
    """temporary branch for the j-th merged line split off at this depth"""
    t = TMP + (f"-{depth}" if depth or j else "") + (f"-{j}" if j else "")
    if t in tips:
        sys.exit(f"ERROR: the repository has a branch named {t[11:]}, which this push needs as a temporary "
                 "branch; rename that branch (git branch -m) and push again.")
    if t not in used_tmp:
        used_tmp.append(t)
    return t

def chunks(tip, dst, final, depth):
    """send the unsent history of tip to dst in first-parent chunks; the last chunk is the line `final`.
    A merge whose merged history is over the limits first sends its merged lines, largest first, through
    temporary branches of the next depth (one per merged line: moving a temporary branch on to another
    line before dst holds the merge would drop the first one from the remote)"""
    force = "+" if dst.startswith(TMP) else ""
    chain, c = [], tip
    while c and c not in planned:
        chain.append(c)
        c = (parents.get(c) or [None])[0]
    acc_b = acc_n = 0
    last = None
    for c in reversed(chain):
        cs = new_commits([c])
        b, bs = cost(cs)
        if len(cs) > 1 and over(b, len(cs)):
            if acc_n:
                lines.append((f"{force}{last}:{dst}", acc_b))
                acc_b = acc_n = 0
            side = sorted((q for q in parents[c][1:] if q not in planned), key=lambda q: -cost(new_commits([q]))[0])
            for j, p in enumerate(side):
                if p not in planned:
                    t = tmp_ref(depth, j)
                    chunks(p, t, f"+{p}:{t}", depth + 1)
                cs = new_commits([c])
                b, bs = cost(cs)
                if not over(b, len(cs)):
                    break
        if b > LIMIT:
            too_big(c, b, cs)
        if acc_n and over(acc_b + b, acc_n + len(cs)):
            lines.append((f"{force}{last}:{dst}", acc_b))
            acc_b = acc_n = 0
        planned.update(cs)
        seen.update(bs)
        acc_b += b
        acc_n += len(cs)
        last = c
    if last:
        lines.append((final, acc_b))

def push_group(group):
    cs = new_commits([c for _, c in group])
    b, bs = cost(cs)
    if over(b, len(cs)) and len(group) > 1:
        h = len(group) // 2
        push_group(group[:h])
        push_group(group[h:])
    elif over(b, len(cs)):
        name, c = group[0]
        chunks(c, tmp_ref(0, 0), f"{name}:{name}", 1)
    else:
        planned.update(cs)
        seen.update(bs)
        lines.append((" ".join(f"{n}:{n}" for n, _ in group), b))

used_tmp = []
chunks(tips[MAIN], MAIN, f"{MAIN}:{MAIN}", 0)
for i in range(0, len(refs), MAXR):
    push_group(refs[i:i + MAXR])
if used_tmp:
    lines.append((" ".join(":" + t for t in used_tmp), 0))
sys.stdout.buffer.write("".join(l + "\n" for l, _ in lines).encode("utf-8", "surrogateescape"))
print(f"push plan: {len(lines)} pushes, largest about {human(max(e for _, e in lines))} of new file content "
      f"(limit {human(LIMIT)}, at most {MAXC} commits / {MAXR} refs per push)", file=sys.stderr)
