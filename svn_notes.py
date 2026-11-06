#!/usr/bin/env python3
"""Attach full SVN metadata to every migrated commit as git notes (refs/notes/svn).

usage: svn_notes.py GIT_REPO LOG_JSON [--revmap FILE]

Commit -> revision mapping: the 'svn path=/x/; revision=N' note that svn-all-fast-export
writes to refs/notes/commits. Only for commits without one (git svn), a 'git-svn-id: URL@REV
UUID' trailer counts, and only as the last line of the message with URL under the
repository's svn-remote URL: a git-svn-id quoted inside an SVN log message is ignored.
--revmap FILE (meta/revmap.json, {"mirror rev": original rev}) adds the line
'SVN-Original-Revision: rN' where loading a dump file renumbered the revision.
"""
import json, os, re, subprocess, sys, urllib.parse

USAGE = "usage: svn_notes.py GIT_REPO LOG_JSON [--revmap FILE]"
if len(sys.argv) < 3 or sys.argv[1].startswith("--") or sys.argv[2].startswith("--"):
    sys.exit(USAGE)
repo, logf = sys.argv[1], sys.argv[2]
revmap = {}
if "--revmap" in sys.argv:
    i = sys.argv.index("--revmap")
    if i + 1 >= len(sys.argv):
        sys.exit("ERROR: --revmap needs a file\n" + USAGE)
    revmap = {str(k): int(v) for k, v in json.load(open(sys.argv[i + 1], encoding="utf-8")).items()}
revs = json.load(open(logf, encoding="utf-8"))
_lc = os.environ.get("LC_ALL", "")
env = dict(os.environ, LC_ALL=_lc if re.search("utf-?8", _lc, re.I) else "C.UTF-8")
G = ["git", "-C", repo]

def git(*a, **kw):
    return subprocess.run([*G, *a], check=True, capture_output=True, env=env, **kw).stdout

def config(key, *where):
    r = subprocess.run([*G, "config", *where, "--get", key], capture_output=True, env=env)
    return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""

def cat_objects(shas):
    """sha -> raw object content (one git cat-file --batch for all)"""
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

NOTE = re.compile(rb"^svn path=(.*?); revision=(\d+)", re.M)   # ';' may be part of a branch name
GSID = re.compile(rb"git-svn-id: (\S+)@(\d+) \S+")

def mapping():
    """commit sha -> (svn path, rev)"""
    shas = git("rev-list", "--branches", "--tags").decode().split()
    m, notes = {}, {}
    if subprocess.run([*G, "rev-parse", "-q", "--verify", "refs/notes/commits"], capture_output=True, env=env).returncode == 0:
        for row in git("ls-tree", "-r", "-z", "refs/notes/commits").split(b"\0"):
            if row:
                meta, path = row.split(b"\t", 1)
                notes[path.replace(b"/", b"").decode()] = meta.split()[2].decode()
    texts = cat_objects([notes[s] for s in shas if s in notes])
    for s in shas:
        mt = NOTE.search(texts.get(notes.get(s), b""))
        if mt:
            m[s] = (mt.group(1).decode("utf-8", "replace"), int(mt.group(2)))
    rest = [s for s in shas if s not in m]
    base = urllib.parse.unquote(config("svn-remote.svn.url")).rstrip("/")
    if rest and base:                   # git svn: trailer on the last line of the message
        gitdir = git("rev-parse", "--absolute-git-dir").decode().strip()
        root = urllib.parse.unquote(config("svn-remote.svn.reposRoot", "-f", os.path.join(gitdir, "svn", ".metadata"))
                                    ).rstrip("/") or base
        for s, obj in cat_objects(rest).items():
            lines = [l for l in obj.split(b"\n\n", 1)[-1].split(b"\n") if l.strip()]
            mt = GSID.fullmatch(lines[-1].rstrip()) if lines else None
            if not mt:
                continue
            u = urllib.parse.unquote(mt.group(1).decode("utf-8", "replace")).rstrip("/")
            if (u == base or u.startswith(base + "/")) and (u == root or u.startswith(root + "/")):
                m[s] = (u[len(root):] or "/", int(mt.group(2)))
    return m, len(shas)

def note_text(path, r):
    v = revs.get(str(r))
    out = [f"SVN-Revision: r{r}", f"SVN-Path: {path}"]
    if revmap.get(str(r), r) != r:
        out.append(f"SVN-Original-Revision: r{revmap[str(r)]}")
    if v is None:
        return "\n".join(out) + "\n"
    out += [f"SVN-Author: {v['author'] if v['author'] else '(no author)'}", f"SVN-Date: {v['date']}"]
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

m, total = mapping()
stream = bytearray(b"commit refs/notes/svn\ncommitter SVN Migration <migration@example.com> 0 +0000\n"
                   b"data 22\nSVN metadata as notes\n")
if subprocess.run([*G, "rev-parse", "-q", "--verify", "refs/notes/svn"], capture_output=True, env=env).returncode == 0:
    stream += b"from refs/notes/svn^0\n"
for sha, (path, r) in m.items():
    data = note_text(path, r).encode()
    stream += b"N inline %s\ndata %d\n%s\n" % (sha.encode(), len(data), data)
if subprocess.run([*G, "fast-import", "--quiet"], input=bytes(stream), env=env).returncode:
    sys.exit("ERROR: git fast-import rejected the notes (message above); refs/notes/svn is unchanged")
renum = sum(1 for _, r in m.values() if revmap.get(str(r), r) != r)
print(f"notes written for {len(m)} commits ({len(set(r for _, r in m.values()))} distinct SVN revisions"
      + (f", {renum} with SVN-Original-Revision" if renum else "") + ")"
      + (f"; {total - len(m)} commits have no SVN revision" if total > len(m) else ""))
