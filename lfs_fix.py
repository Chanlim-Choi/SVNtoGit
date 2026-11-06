#!/usr/bin/env python3
"""Move large files in ALL history to Git LFS, keeping git notes attached.

usage: lfs_fix.py GIT_REPO BYTES --paths-out FILE

Pass 1: `git lfs migrate import --everything --above=BYTES` converts every blob of at least
BYTES bytes. It also writes '/path filter=lfs ...' into the .gitattributes of every commit
it rewrites afterwards, even where that path is still small: a raw file under an LFS rule,
which every clone shows as modified. Pass 2 converts every version of those paths in every
commit (`--include`, all paths that pass 1 made pointers; `--fixup` is not used because
git-lfs reuses its result for a blob it saw earlier without the rule, e.g. on a branch where
the file was always small).
`git lfs migrate` rewrites every commit (new SHAs). Notes are keyed by commit SHA, so they
are set aside first and re-keyed afterwards through the object maps of all passes.
FILE receives every path that is an LFS pointer in some commit (for verify.py --lfs-paths).
"""
import os, re, subprocess, sys

USAGE = "usage: lfs_fix.py GIT_REPO BYTES --paths-out FILE   (BYTES: a whole number > 0)"
if (len(sys.argv) < 5 or "--paths-out" not in sys.argv[3:-1] or not sys.argv[2].isdigit()
        or int(sys.argv[2]) < 1):
    sys.exit("ERROR: " + USAGE)
repo, above = sys.argv[1], int(sys.argv[2])
paths_out = sys.argv[sys.argv.index("--paths-out", 3) + 1]
_lc = os.environ.get("LC_ALL", "")
env = dict(os.environ, LC_ALL=_lc if re.search("utf-?8", _lc, re.I) else "C.UTF-8")
G = ["git", "-C", repo]

def git(*a, **kw):
    return subprocess.run([*G, *a], check=True, capture_output=True, env=env, **kw).stdout.decode()

notes = {}
for line in git("for-each-ref", "--format=%(refname) %(objectname)", "refs/notes/").splitlines():
    ref, sha = line.rsplit(" ", 1)
    entries = {}
    for row in git("ls-tree", "-r", sha).splitlines():
        meta, path = row.split("\t")
        entries[path.replace("/", "")] = meta.split()[2]       # annotated sha -> note blob
    notes[ref] = (sha, entries)
    git("update-ref", "-d", ref)
print("notes set aside:", {r: len(e) for r, (_, e) in notes.items()})

gitdir = git("rev-parse", "--absolute-git-dir").strip()
maps = []
def migrate(n, *args):
    mapfile = os.path.join(gitdir, f"lfs-object-map-{n}.txt")
    r = subprocess.run([*G, "lfs", "migrate", "import", "--everything", *args, f"--object-map={mapfile}", "--yes"],
                       capture_output=True, text=True, env=env)
    shown = " ".join(a if len(a) < 200 else a[:200] + "..." for a in args)
    print(f"--- git lfs migrate import {shown}\n", r.stdout[-800:], r.stderr[-1500:])
    if r.returncode:
        return False
    maps.append(dict(l.strip().split(",") for l in open(mapfile) if "," in l))
    return True

def lfs_paths():
    r = subprocess.run([*G, "lfs", "ls-files", "--all", "--name-only"], capture_output=True, env=env)
    if r.returncode:
        return None
    return sorted({p for p in r.stdout.decode("utf-8", "surrogateescape").split("\n") if p})

ok = migrate(1, f"--above={above}")
paths = lfs_paths() if ok else []
if ok and paths is None:
    ok = False; print("git lfs ls-files --all failed")
if ok and paths:
    # exact paths as patterns: ASCII punctuation and spaces become '?' (glob characters, the ','
    # list separator, '[[:space:]]' escaping), so git-lfs converts exactly what the rules match
    pats = ["/" + "".join(c if not c.isascii() or c.isalnum() or c in "._/-" else "?" for c in p) for p in paths]
    chunks, cur, size = [], [], 0
    for p in pats:                       # one argument must stay below the 128 KiB kernel limit
        if cur and size + len(os.fsencode(p)) + 1 > 100000:
            chunks.append(cur); cur, size = [], 0
        cur.append(p); size += len(os.fsencode(p)) + 1
    chunks.append(cur)
    for i, ch in enumerate(chunks):
        ok = ok and migrate(f"2-{i + 1}", "--include=" + ",".join(ch))

def new_sha(old):
    for m in maps:
        old = m.get(old, old)
    return old

for ref, (sha, entries) in notes.items():
    if not maps:                         # nothing was rewritten: put the notes back as they were
        git("update-ref", ref, sha); continue
    msg = b"Re-keyed notes after LFS migration\n"
    stream = bytearray(b"commit %s\ncommitter SVN Migration <migration@example.com> 0 +0000\n"
                       b"data %d\n%s" % (ref.encode(), len(msg), msg))
    moved = 0
    for old, blob in entries.items():
        new = new_sha(old)
        moved += new != old
        stream += b"N %s %s\n" % (blob.encode(), new.encode())
    if subprocess.run([*G, "fast-import", "--quiet"], input=bytes(stream), env=env).returncode:
        sys.exit(f"ERROR: re-keying {ref} after the LFS migration failed (message above); the old notes are commit "
                 f"{sha}: restore them with git update-ref {ref} {sha}")
    print(f"{ref}: {len(entries)} notes, {moved} re-keyed to rewritten commits")
if not ok:
    sys.exit("ERROR: git lfs migrate failed (output above); notes "
             + ("re-keyed to the commits it rewrote" if maps else "restored"))

paths = lfs_paths()
if paths is None:
    sys.exit("ERROR: git lfs ls-files --all failed after the migration")
with open(paths_out, "w", encoding="utf-8", errors="surrogateescape") as f:
    f.write("".join(p + "\n" for p in paths))
print(f"LFS paths (pointers in some commit): {len(paths)} -> {paths_out}")
r = subprocess.run([*G, "lfs", "fsck", "--pointers"], capture_output=True, env=env)
if r.returncode:
    print("WARN: git lfs fsck --pointers on HEAD: files under a filter=lfs rule are not LFS pointers, so clones "
          "will show them as modified:\n" + r.stdout.decode("utf-8", "replace")[-1500:])
git("reflog", "expire", "--expire=now", "--all")
git("gc", "-q", "--prune=now")
print(git("count-objects", "-vH"))
