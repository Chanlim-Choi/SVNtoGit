#!/usr/bin/env python3
"""Attach full SVN metadata to every migrated commit as git notes (refs/notes/svn).

usage: svn_notes.py GIT_REPO LOG_JSON

Commit -> revision mapping comes from a 'git-svn-id: URL@REV' trailer (git svn)
or an 'svn path=/x/; revision=N' line (svn-all-fast-export --add-metadata), in the
commit message or in refs/notes/commits.
"""
import json, re, subprocess, sys

repo, logf = sys.argv[1], sys.argv[2]
revs = json.load(open(logf, encoding="utf-8"))
G = ["git", "-C", repo]

def git(*a, **kw):
    return subprocess.run([*G, *a], check=True, capture_output=True, **kw).stdout

RX = [re.compile(rb"git-svn-id: \S+?(/[^@\s]*)?@(\d+) "), re.compile(rb"svn path=([^;]*); revision=(\d+)")]

def mapping():
    """commit sha -> (svn path, rev)"""
    m = {}
    out = git("log", "--all", "--format=%H%x00%B%x00%N%x01", "--notes=commits")
    for rec in out.split(b"\x01"):
        rec = rec.strip(b"\n")
        if not rec:
            continue
        sha, body, note = (rec.split(b"\x00") + [b"", b""])[:3]
        for rx in RX:
            mt = rx.search(body) or rx.search(note)
            if mt:
                m[sha.decode()] = ((mt.group(1) or b"/").decode("utf-8", "replace"), int(mt.group(2)))
                break
    return m

def note_text(path, r):
    v = revs.get(str(r))
    if v is None:
        return f"SVN-Revision: r{r}\nSVN-Path: {path}\n"
    out = [f"SVN-Revision: r{r}", f"SVN-Path: {path}",
           f"SVN-Author: {v['author'] if v['author'] else '(no author)'}", f"SVN-Date: {v['date']}"]
    if v["revprops"]:
        out.append("SVN-Revprops:")
        out += [f"  {k}: {val}".replace("\n", "\n    ") for k, val in sorted(v["revprops"].items())]
    out.append("SVN-Changed-Paths:")
    for p in v["paths"]:
        line = f"  {p['action']} {p['path']}"
        if "copyfrom" in p:
            line += f" (from {p['copyfrom']})"
        out.append(line)
    props = [p for p in v["paths"] if "props" in p]
    if props:
        out.append("SVN-Properties (after this revision):")
        for p in props:
            for k, val in sorted(p["props"].items()):
                out.append(f"  {p['path']}: {k} = {val}".replace("\n", "\n    ").rstrip())
    return "\n".join(out) + "\n"

m = mapping()
stream = bytearray(b"commit refs/notes/svn\ncommitter SVN Migration <migration@example.com> 0 +0000\n"
                   b"data 22\nSVN metadata as notes\n")
try:
    git("rev-parse", "--verify", "-q", "refs/notes/svn")
    stream += b"from refs/notes/svn^0\n"
except subprocess.CalledProcessError:
    pass
for sha, (path, r) in m.items():
    data = note_text(path, r).encode()
    stream += b"N inline %s\ndata %d\n%s\n" % (sha.encode(), len(data), data)
subprocess.run([*G, "fast-import", "--quiet"], input=bytes(stream), check=True)
print(f"notes written for {len(m)} commits ({len(set(r for _, r in m.values()))} distinct SVN revisions)")
