#!/usr/bin/env python3
"""Generate svn-all-fast-export rules from the actual copy history.

usage: make_rules.py LOG_JSON REPO_NAME [--project PREFIX] > rules.txt
       make_rules.py LOG_JSON --detect         # list project prefixes, one per line

Projects: every directory that ever contained a `trunk` (e.g. "/" for a standard
root layout, or "/projA", "/libs/projB" for multi-project repositories). With no
`trunk` anywhere the repository is non-standard and "NONE" is printed; the rules
then import the whole tree as `master`.

Branch/tag roots = every path under PREFIX/branches or PREFIX/tags that was ever the
target of an `svn copy` (includes deleted and nested ones like branches/hotfix/1.x).
Names illegal in git are sanitized; the renames are printed to stderr.
"""
import json, re, sys

revs = json.load(open(sys.argv[1]))
LAYOUT = ("trunk", "branches", "tags")

def projects():
    found = set()
    for v in revs.values():
        for p in v["paths"]:
            parts = p["path"].rstrip("/").split("/")
            if parts[-1] == "trunk" and p.get("action") in ("A", "R") and p.get("kind") in ("dir", "", None):
                parent = parts[1:-1]
                if not any(x in LAYOUT for x in parent):      # not a 'trunk' inside a branch copy
                    found.add("/" + "/".join(parent))
    # a candidate nested inside another (non-root) project is part of that project
    return sorted(f for f in found
                  if f == "/" or not any(o not in ("/", f) and f.startswith(o + "/") for o in found))

if "--detect" in sys.argv:
    ps = projects()
    print("\n".join(ps) if ps else "NONE")
    sys.exit(0)

name = sys.argv[2]
prefix = sys.argv[sys.argv.index("--project") + 1] if "--project" in sys.argv else (projects() or ["NONE"])[0]

def rx(s):
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", s)

out = [f"create repository {name}\nend repository\n"]
if prefix == "NONE":
    out.append(f"# non-standard layout: the whole repository becomes master\n"
               f"match /\n  repository {name}\n  branch master\nend match\n")
    print("\n".join(out)); print("layout=none (whole repository -> master)", file=sys.stderr); sys.exit(0)

P = "" if prefix == "/" else "/" + prefix.strip("/")
roots = set()
for v in revs.values():
    for p in v["paths"]:
        m = re.match(rf"^{rx(P)}/(branches|tags)/(.+)$", p["path"])
        if m and "copyfrom" in p and p["kind"] in ("dir", None, ""):
            roots.add((m.group(1), m.group(2).rstrip("/")))
roots = {(k, r) for k, r in roots
         if not any(r != o and r.startswith(o + "/") and k == ko for ko, o in roots)}

def sanitize(n):
    s = re.sub(r"\.\.+", ".", n)
    s = re.sub(r"[ ~^:?*\[\\]", "-", s)
    s = re.sub(r"@\{", "-", s).strip("/.").replace("/.", "/")
    return s

out.append(f"match {rx(P)}/trunk/\n  repository {name}\n  branch master\nend match\n")
renamed = []
for kind, r in sorted(roots, key=lambda x: -len(x[1])):
    g = sanitize(r)
    if g != r:
        renamed.append(f"{kind}/{r} -> {g}")
    tgt = f"refs/tags/{g}" if kind == "tags" else g
    out.append(f"match {rx(P)}/{kind}/{rx(r)}/\n  repository {name}\n  branch {tgt}\n"
               + ("  annotated true\n" if kind == "tags" else "") + "end match\n")
out.append(f"# layout containers themselves: nothing to import\nmatch {rx(P)}/(trunk|branches|tags)/\nend match\n")
if P:
    out.append(f"# anything else inside this project is kept on its own branch\n"
               f"match {rx(P)}/([^/]+)/\n  repository {name}\n  branch svn-outside-layout\n  prefix \\1/\nend match\n"
               f"# other projects are migrated separately\nmatch /\nend match\n")
else:
    others = [p for p in projects() if p != "/"]
    for o in others:   # nested projects under a root layout are migrated separately
        out.append(f"match {rx(o)}/\nend match\n")
    out.append(f"# anything outside the standard layout is kept on its own branch\n"
               f"match /([^/]+)/\n  repository {name}\n  branch svn-outside-layout\n  prefix \\1/\nend match\n")
print("\n".join(out))
print(f"project={prefix} roots={len(roots)} renamed={len(renamed)}", *renamed, sep="\n  ", file=sys.stderr)
