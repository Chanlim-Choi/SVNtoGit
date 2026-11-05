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

revs = json.load(open(sys.argv[1], encoding="utf-8"))
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

def sanitize(n):   # keep identical in verify.py and migrate_gitsvn.sh
    s = re.sub(r"\.\.+", ".", n)
    s = re.sub(r"[\x00-\x20\x7f~^:?*\[\\]", "-", s)
    s = s.replace("@{", "-")
    parts = []
    for c in s.split("/"):          # git-check-ref-format rules per path component
        c = c.strip(".")
        if c.endswith(".lock"):
            c = c[:-5] + "-lock"
        if c:
            parts.append(c)
    s = "/".join(parts)
    return "-" if s == "@" else (s or "unnamed")

# Refuse names that would silently merge two SVN branches into one git ref, or that
# git cannot store side by side. Checked here, before the (long) conversion runs.
RESERVED = {"branches": {"master", "main", "svn-outside-layout"}, "tags": set()}
targets, clashes = {}, []
for kind, r in sorted(roots):
    g = sanitize(r)
    if g in RESERVED[kind] or (kind == "tags" and g.startswith("svn-deleted/")):
        clashes.append(f"{kind}/{r} -> '{g}' is reserved by the migration")
    if (kind, g) in targets:
        clashes.append(f"{kind}/{targets[kind, g]} and {kind}/{r} both become '{g}'")
    targets[kind, g] = r
for (kind, g), r in targets.items():
    for (k2, g2), r2 in targets.items():
        if k2 == kind and g2.startswith(g + "/"):
            clashes.append(f"{kind}/{r} ('{g}') and {kind}/{r2} ('{g2}') cannot both exist as git refs")
if clashes:
    sys.exit("ERROR: SVN branch/tag names collide after conversion to git ref names:\n  "
             + "\n  ".join(clashes) + "\nRename the affected branches/tags in SVN (svn mv) and run again.")

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
