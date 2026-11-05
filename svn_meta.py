#!/usr/bin/env python3
"""Extract SVN metadata that Git has no native place for.

usage: svn_meta.py SVN_URL OUT_DIR

Writes:
  OUT_DIR/authors.txt   - git-svn authors file (every author, incl. "(no author)")
  OUT_DIR/identity.map  - svn-all-fast-export identity map
  OUT_DIR/log.json      - per-revision: author, date, message, all revprops,
                          changed paths (+ property changes on those paths)
"""
import json, os, subprocess, sys
import xml.etree.ElementTree as ET

url, out = sys.argv[1].rstrip("/"), sys.argv[2]
os.makedirs(out, exist_ok=True)
env = dict(os.environ, LC_ALL="C.UTF-8")

def svn(*args):
    return subprocess.run(["svn", "--non-interactive", *args], check=True,
                          capture_output=True, env=env).stdout

root = ET.fromstring(svn("log", "--xml", "-v", "--with-all-revprops", url))
repo_root = ET.fromstring(svn("info", "--xml", url)).find("entry/repository/root").text

revs = {}
for e in root.findall("logentry"):
    r = int(e.get("revision"))
    rp = {p.get("name"): (p.text or "") for p in e.findall("revprops/property")}
    paths = []
    for p in e.findall("paths/path"):
        item = {"path": p.text, "action": p.get("action"), "kind": p.get("kind")}
        if p.get("copyfrom-path"):
            item["copyfrom"] = f'{p.get("copyfrom-path")}@{p.get("copyfrom-rev")}'
        if p.get("prop-mods") == "true" and p.get("action") != "D":
            try:
                x = ET.fromstring(svn("proplist", "-v", "--xml", f"{repo_root}{p.text}@{r}"))
                item["props"] = {q.get("name"): (q.text or "") for q in x.findall("target/property")}
            except subprocess.CalledProcessError:
                pass
        paths.append(item)
    # svn puts the standard revprops in <author>/<date>/<msg>, custom ones in <revprops>
    author = e.findtext("author", rp.pop("svn:author", None))
    date = e.findtext("date", rp.pop("svn:date", None))
    log = e.findtext("msg", rp.pop("svn:log", ""))
    for k in ("svn:author", "svn:date", "svn:log"):
        rp.pop(k, None)
    revs[r] = {"author": author, "date": date, "log": log or "", "revprops": rp, "paths": paths}

json.dump(revs, open(f"{out}/log.json", "w"), ensure_ascii=False, indent=0)

authors = sorted({v["author"] for v in revs.values() if v["author"]})
def ident(a):
    login = a.split("\\")[-1]
    return f"{login.replace('.', ' ').title()} <{login}@example.com>"
with open(f"{out}/authors.txt", "w") as f:
    for a in authors:
        f.write(f"{a} = {ident(a)}\n")
    f.write("(no author) = Unknown <unknown@example.com>\n")
with open(f"{out}/identity.map", "w") as f:
    for a in authors:
        f.write(f"{a} {ident(a)}\n")
    # svn-all-fast-export substitutes "nobody" for revisions without svn:author
    if "nobody" not in authors:
        f.write("nobody Unknown <unknown@example.com>\n")
print(f"revisions={len(revs)} authors={len(authors)} "
      f"with_revprops={sum(1 for v in revs.values() if v['revprops'])} "
      f"noauthor={sum(1 for v in revs.values() if not v['author'])}")
