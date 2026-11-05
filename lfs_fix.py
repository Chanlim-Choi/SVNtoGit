#!/usr/bin/env python3
"""Move large files in ALL history to Git LFS, keeping git notes attached.

usage: lfs_fix.py GIT_REPO [THRESHOLD, default 50MB]

`git lfs migrate` rewrites every commit (new SHAs). Notes are keyed by commit
SHA, so they are set aside first and re-keyed afterwards with the object map.
"""
import subprocess, sys

repo = sys.argv[1]
above = sys.argv[2] if len(sys.argv) > 2 else "50MB"
G = ["git", "-C", repo]

def git(*a, **kw):
    return subprocess.run([*G, *a], check=True, capture_output=True, **kw).stdout.decode()

notes = {}
for line in git("for-each-ref", "--format=%(refname) %(objectname)", "refs/notes/").splitlines():
    ref, sha = line.split()
    entries = {}
    for row in git("ls-tree", "-r", sha).splitlines():
        meta, path = row.split("\t")
        entries[path.replace("/", "")] = meta.split()[2]       # annotated sha -> note blob
    notes[ref] = (sha, entries)
    git("update-ref", "-d", ref)
print("notes set aside:", {r: len(e) for r, (_, e) in notes.items()})

mapfile = f"{repo}/lfs-object-map.txt"
r = subprocess.run([*G, "lfs", "migrate", "import", "--everything", f"--above={above}",
                    f"--object-map={mapfile}", "--yes"], capture_output=True, text=True)
print(r.stdout[-800:], r.stderr[-1500:])
if r.returncode:
    for ref, (sha, _) in notes.items():
        git("update-ref", ref, sha)
    sys.exit("lfs migrate failed; notes restored")

old2new = dict(l.strip().split(",") for l in open(mapfile) if "," in l)
for ref, (_, entries) in notes.items():
    stream = bytearray(b"commit %s\ncommitter SVN Migration <migration@example.com> 0 +0000\n"
                       b"data 34\nRe-keyed notes after LFS migration\n" % ref.encode())
    moved = 0
    for old, blob in entries.items():
        new = old2new.get(old, old)
        moved += new != old
        stream += b"N %s %s\n" % (blob.encode(), new.encode())
    subprocess.run([*G, "fast-import", "--quiet"], input=bytes(stream), check=True)
    print(f"{ref}: {len(entries)} notes, {moved} re-keyed to rewritten commits")
git("reflog", "expire", "--expire=now", "--all")
git("gc", "-q", "--prune=now")
print(git("count-objects", "-vH"))
