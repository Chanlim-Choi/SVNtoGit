#!/usr/bin/env python3
"""Generate svn-all-fast-export rules and layout.json from the actual copy history.

usage: make_rules.py LOG_JSON --detect
       make_rules.py LOG_JSON --preflight --migrate "ID,ID,..." [--explicit]
       make_rules.py LOG_JSON REPO_NAME --project ID [--exclude "ID,..."] [--layout-out FILE] > rules.txt

Project ids: "/" (root layout), "/x" or "/x/y" (a folder holding trunk/branches/tags), NONE (no
layout: the whole repository is one tree), OTHER (everything outside the migrated projects).
--detect lists every folder that ever held a lowercase `trunk` (not inside another layout folder)
and every folder created as a copy of such a project folder; a candidate nested inside another
non-root project belongs to that project. Without any project it prints NONE.

Branch/tag roots: folders under P/branches or P/tags that were the target of a copy (also when
branches/, tags/ or the project folder was copied as a whole), plus folders created directly in
P/branches or P/tags without a copy that never hold a root below them. A root inside another root
is content of the outer one. Git names are sanitized and made unique; every rename is printed to
stderr ("kind/name -> gitname"). Paths with a `.git` component are never migrated. Content of a
project that is not trunk and not in a root (files in the project folder or in branches/ and
tags/, other folders) goes to branch svn-outside-layout. When the project folder, a folder above
it, branches/, tags/ or another folder in them that is no branch/tag is copied or deleted as a
whole, the converter is told to look inside it in that revision, so its branches, tags and files
are created or deleted too; the same for folder copies that hold a .git entry (see
dotgit_recursions).

--preflight prints "STOP: ...", "WARN: ..." and "OUTSIDE: <svn path>" lines; exit 3 if any STOP.
"""
import argparse, json, re, sys
from bisect import bisect_left
from collections import defaultdict

LAYOUT = ("trunk", "branches", "tags")
RESERVED = {"branches": {"master", "main", "svn-outside-layout", "svn-migration-tmp", "HEAD"},
            "tags": {"svn-deleted", "backups", "HEAD"}}   # and every name below them (git D/F)
sys.setrecursionlimit(10000)

ap = argparse.ArgumentParser(description="svn-all-fast-export rules from the SVN copy history")
ap.add_argument("log")
ap.add_argument("repo", nargs="?")
ap.add_argument("--detect", action="store_true")
ap.add_argument("--preflight", action="store_true")
ap.add_argument("--migrate", default="")
ap.add_argument("--explicit", action="store_true")
ap.add_argument("--project")
ap.add_argument("--exclude", default="")
ap.add_argument("--layout-out")
A = ap.parse_args()


def norm(p):
    return p.rstrip("/") or "/"


def join(d, n):
    return (d if d != "/" else "") + "/" + n


def norm_id(x):
    return x.strip() if x.strip() in ("NONE", "OTHER") else norm("/" + x.strip("/"))


def ids(s):
    # split only before "/", NONE or OTHER: commas inside folder names survive
    return [norm_id(x) for x in re.split(r",(?=/|(?:NONE|OTHER)(?:,|$))", s) if x.strip()]


revs = {int(k): v for k, v in json.load(open(A.log, encoding="utf-8")).items()}
LAST = max(revs, default=0)
EV = defaultdict(list)          # svn path -> [(rev, action, copyfrom path, copyfrom rev, kind)]
for r in sorted(revs):
    for p in revs[r]["paths"]:
        sp = sr = None
        if p.get("copyfrom"):
            sp, _, sr = p["copyfrom"].rpartition("@")
            sp, sr = norm(sp), int(sr)
        EV[norm(p["path"])].append((r, p.get("action"), sp, sr, p.get("kind") or ""))
PATHS = sorted(EV)


def under(p, d):
    return d == "/" or p == d or p.startswith(d + "/")


def below(d):
    """logged paths strictly inside folder d"""
    if d == "/":
        return [p for p in PATHS if p != "/"]
    return PATHS[bisect_left(PATHS, d + "/"):bisect_left(PATHS, d + "0")]   # '0' sorts right after '/'


def dotgit(p):
    return any(c.lower() == ".git" for c in p.split("/"))


_isdir = {}
def is_dir(p):
    if p not in _isdir:
        _isdir[p] = True                       # unknown kind: a folder, as before (also guards cycles)
        kinds = {e[4] for e in EV.get(p, ())}
        src = next((e[2] for e in EV.get(p, ()) if e[2]), None)
        if "dir" not in kinds:
            _isdir[p] = False if "file" in kinds else bool(below(p)) or src is None or is_dir(src)
    return _isdir[p]


def ev_dir(p, e):
    return e[4] == "dir" or (not e[4] and is_dir(p))


_life = {}
def lifetimes(p, upto=None):
    """[[created, deleted or None, (copy source, its rev) or None], ...] of path p from revisions
    <= upto. Created: A/R of p, or a copy of an ancestor whose source held p. Deleted: D/R of p
    or of an ancestor."""
    upto = LAST if upto is None else upto
    if (p, upto) in _life:
        return _life[p, upto]
    parts = p.strip("/").split("/") if p != "/" else []
    evs = [(e[0], i, q, e) for i in range(1, len(parts) + 1) for q in ["/" + "/".join(parts[:i])]
           for e in EV.get(q, ()) if e[0] <= upto]
    evs += [(e[0], len(parts) + 1, q, e) for q in below(p) for e in EV[q] if e[0] <= upto and e[1] != "D"]
    evs.sort(key=lambda x: (x[0], x[1]))
    out, cur = [], None
    for r, depth, q, (_, act, sp, sr, _k) in evs:
        if depth > len(parts) or (act == "M" and q == p):
            if cur is None:                    # p exists here: only reached if a creation was missed
                cur = [r, None, None]
            continue
        if act in ("D", "R") and cur:
            if cur[0] < r:
                cur[1] = r
                out.append(cur)
            cur = None
        if act in ("A", "R") and cur is None and (q == p or sp is not None):
            src = (sp if sp != "/" else "") + p[len(q):] if sp is not None else None
            if q == p or exists(src, sr):
                cur = [r, None, (src, sr) if src else None]
    if cur:
        out.append(cur)
    _life[p, upto] = out
    return out


def exists(p, rev):
    lt = lifetimes(p, rev)
    return bool(lt) and lt[-1][1] is None


def children(d, rev):
    """{name: is a folder} of the entries directly in folder d at revision rev"""
    out = {}
    lt = lifetimes(d, rev)
    if lt and lt[-1][1] is None and lt[-1][2]:
        out.update(children(*lt[-1][2]))
    for p in below(d):
        n = p[len(d) + 1:].split("/")[0]
        out.setdefault(n, is_dir(join(d, n)))
    return {n: k for n, k in out.items() if exists(join(d, n), rev)}


COPIES = [(p, e[0], e[2], e[3]) for p in PATHS for e in EV[p]
          if e[1] in ("A", "R") and e[2] is not None and ev_dir(p, e)]
PROJECT_COPIES = []             # (new project folder, rev, project copied, its revision)


def projects():
    found = set()
    for p in PATHS:
        parts = p.split("/")[1:]
        if parts[-1] == "trunk" and not any(x in LAYOUT for x in parts[:-1]) \
                and any(e[1] in ("A", "R") and ev_dir(p, e) for e in EV[p]):
            found.add("/" + "/".join(parts[:-1]))
    del PROJECT_COPIES[:]
    changed = True
    while changed:              # a copy of a project folder (or of a folder holding one) is a project too
        changed = False
        for t, r, sp, sr in COPIES:
            for y in sorted(found):
                if y == "/" or not under(y, sp):
                    continue
                x = t + (y[len(sp):] if sp != "/" else y)
                if x in found or dotgit(x) or any(c in LAYOUT for c in x.split("/")[1:]) \
                        or not exists(y + "/trunk", sr):
                    continue
                found.add(x)
                PROJECT_COPIES.append((x, r, y, sr))
                changed = True
    return sorted(f for f in found if f == "/" or not any(o not in ("/", f) and f.startswith(o + "/") for o in found))


def sanitize(n):   # keep identical in migrate_gitsvn.sh (gitname)
    s = re.sub(r"\.\.+", ".", n)
    s = re.sub(r"[\x00-\x20\x7f~^:?*\[\\#]", "-", s)   # '#' starts a comment in svn-all-fast-export rules
    s = s.replace("@{", "-").replace("${", "-{")        # '${' would be expanded as a rules variable
    parts = []
    for c in s.split("/"):          # git-check-ref-format rules per path component
        c = c.strip(".")
        if c.endswith(".lock"):
            c = c[:-5] + "-lock"
        if c:
            parts.append(c)
    s = "/".join(parts)
    return "-" if s == "@" else (s or "unnamed")


def rx(s):
    """literal text -> QRegExp. '#' and '$' become hex escapes: the rules parser cuts every line
    at '#' and expands '${...}'"""
    out = []
    for c in s:
        if c == "#":
            out.append("\\x0023")
        elif c == "$":
            out.append("\\x0024")
        elif c in ".^*+?()[]{}|\\":
            out.append("\\" + c)
        elif ord(c) < 0x20 or ord(c) == 0x7f:
            out.append("\\x%04x" % ord(c))
        else:
            out.append(c)
    return "".join(out)


def find_roots(P):
    """branch/tag roots of project folder P ('' for the root layout) ->
    ({(kind, name)}, {(kind, nested name): (kind, outer name)})"""
    copied, made = set(), set()
    for kind in ("branches", "tags"):
        base = P + "/" + kind
        for p in below(base):
            name = p[len(base) + 1:]
            if dotgit(name):
                continue
            for e in EV[p]:
                if e[1] in ("A", "R") and ev_dir(p, e):
                    if e[2] is not None:
                        copied.add((kind, name))
                    elif "/" not in name:
                        made.add((kind, name))
        for t, r, _sp, _sr in COPIES:      # branches/tags (or a folder holding them) copied as a whole
            if under(base, t):
                copied |= {(kind, n) for n, isdir in children(base, r).items() if isdir and not dotgit(n)}
    holders = {(k, n[:i]) for k, n in copied for i in range(len(n)) if n[i] == "/"}
    roots = copied | (made - holders)
    nested = {}
    for k, n in roots:
        outer = next((n[:i] for i in range(len(n)) if n[i] == "/" and (k, n[:i]) in roots), None)
        if outer is not None:
            nested[(k, n)] = (k, outer)
    return roots - set(nested), nested


def layout_info(pid):
    P = "" if pid == "/" else pid
    roots, nested = find_roots(P)
    path = {kn: "%s/%s/%s" % (P, kn[0], kn[1]) for kn in roots | set(nested) | set(nested.values())}
    return P, roots, nested, path, {kn: lifetimes(path[kn]) for kn in path}


def recursions(P, roots):
    """({rev: folders}, {revs}): a copy or delete of the project folder or a folder above it
    creates or removes it as a whole, so the converter has to look inside it in that revision
    instead of ignoring it (exact folders); the same for branches/, tags/ and every other folder
    in them that is no branch/tag (revisions in which P/(branches|tags)/ folders are recursed)"""
    parts = P.strip("/").split("/") if P else []
    struct = {"/" + "/".join(parts[:i]) for i in range(1, len(parts) + 1)}
    out, cont = defaultdict(set), set()
    for f in struct:
        for r, act, sp, _sr, _k in EV.get(f, ()):
            if act == "D" or (act in ("A", "R") and sp is not None):
                out[r] |= {g for g in struct if under(g, f)}
                cont.add(r)
    for kind in ("branches", "tags"):
        base = P + "/" + kind
        mine = {m for k, m in roots if k == kind}
        for q in [base] + below(base):
            revs_q = [r for r, act, sp, _sr, _k in EV.get(q, ()) if act in ("D", "R") or (q == base and sp is not None)]
            comps = q[len(base) + 1:].split("/") if q != base else []
            if revs_q and not dotgit(q[len(base):]) and is_dir(q) \
                    and not any("/".join(comps[:i]) in mine for i in range(1, len(comps) + 1)):
                cont.update(revs_q)
    return out, cont


def dotgit_recursions(base, whole, excl):
    """{rev: folders}: a folder copy whose source holds a .git file or folder is dumped from SVN
    without asking the rules about files, and --empty-dirs puts .gitignore files into the empty
    folders of a .git folder, so the converter recurses from the copy down to the folder holding
    the .git entry instead, where the .git rule ignores it. Not for copies that git makes itself
    (a whole branch to a whole branch, paths in `whole`) and not for replaces (R), whose old
    content only a dump removes."""
    inst = {p for p in PATHS if p.rsplit("/", 1)[-1].lower() == ".git" and not dotgit(p.rsplit("/", 1)[0])}
    out = defaultdict(set)
    if not inst:
        return out
    for t, r, sp, sr in sorted(COPIES, key=lambda c: c[1]):
        if sp == "/" or dotgit(t):
            continue
        new = {t + g[len(sp):] for g in inst if g.startswith(sp + "/") and exists(g, sr)}
        inst |= new
        if not new or (t in whole and sp in whole) or any(e[0] == r and e[1] == "R" for e in EV.get(t, ())):
            continue
        for g in new:
            comps = g[len(t) + 1:].split("/")[:-1]
            for d in [t] + [t + "/" + "/".join(comps[:i]) for i in range(1, len(comps) + 1)]:
                if (under(d, base or "/") or under(base or "/", d)) and not any(under(d, x) for x in excl):
                    out[r].add(d)
    return out


def git_names(roots, path, life):
    """git name per root: sanitized, then made unique (same name, reserved names, D/F conflicts)"""
    first = {kn: min([x[0] for x in life[kn]] or [x[0] for x in EV.get(path[kn], ())] or [LAST]) for kn in roots}
    last = {kn: max([x[0] for x in life[kn]] or [0]) for kn in roots}
    alive = {kn: bool(life[kn]) and life[kn][-1][1] is None for kn in roots}
    names = {kn: sanitize(kn[1]) for kn in roots}
    why = {}
    groups = defaultdict(list)
    for kn in sorted(roots):
        groups[kn[0], names[kn]].append(kn)
    for (k, g), members in groups.items():
        if len(members) > 1:        # alive at the end (the literal name first), else the most recent, keeps it
            keep = max(members, key=lambda kn: (alive[kn], alive[kn] and kn[1] == g, last[kn], kn[1]))
            for kn in members:
                if kn != keep:
                    names[kn] = "%s-svn-r%d" % (g, first[kn])
                    why[kn] = "%s/%s keeps the git name %s" % (k, keep[1], g)
    for kn in sorted(roots):
        g = names[kn]
        head = g.split("/")[0]
        if head in RESERVED[kn[0]] or (kn[0] == "branches" and head.startswith("svn-migration-tmp-")):
            names[kn] = "svn-tag-" + g if kn[0] == "tags" else "%s-svn-r%d%s" % (head, first[kn], g[len(head):])
            why[kn] = "git name %s is used by the migration" % head
    for _ in range(1000):
        changed = False
        for k in ("branches", "tags"):
            mine = sorted(x for x in roots if x[0] == k)
            taken = {names[x] for x in mine}
            for kn in mine:
                g = names[kn]
                if any(o.startswith(g + "/") for o in taken):       # g and g/... cannot both be refs
                    names[kn] = "%s-svn-r%d" % (g, first[kn])
                    why[kn] = "git cannot store %s next to %s/..." % (g, g)
                    changed = True
            used = set()
            for kn in mine:
                g, i = names[kn], 2
                if g in used:
                    taken = {names[x] for x in mine}
                    while "%s-%d" % (g, i) in taken:
                        i += 1
                    names[kn] = "%s-%d" % (g, i)
                    why[kn] = "git name %s is already used" % g
                    changed = True
                used.add(names[kn])
        if not changed:
            break
    return names, why


# ---------- --detect ---------------------------------------------------------------------------
if A.detect:
    print("\n".join(projects()) or "NONE")
    sys.exit(0)


# ---------- --preflight ------------------------------------------------------------------------
if A.preflight:
    migrate = ids(A.migrate)
    if not migrate:
        sys.exit("ERROR: --preflight needs --migrate ID,...")
    projs = projects()
    M = [m for m in migrate if m not in ("NONE", "OTHER")]
    stop, warn = [], []

    def layout_owner(p):
        """the detected project whose trunk, branches or tags folder holds path p"""
        own = [y for y in projs if any(under(p, join(y, d)) for d in LAYOUT)]
        return max(own, key=len) if own else None

    for p in PATHS:
        x, _, leaf = p.rpartition("/")
        x = x or "/"
        if leaf != "trunk" or x not in M:
            continue
        for r, act, sp, sr, _k in EV[p]:
            if act not in ("A", "R") or sp is None:
                continue
            y, _, sleaf = sp.rpartition("/")
            if (sleaf == "trunk" and (y or "/") != x) or layout_owner(sp) not in (None, x):
                stop.append("STOP: trunk copied from another project: %s (r%d) is a copy of %s@%d; the git "
                            "history of %s would start at r%d without the history before the copy"
                            % (p, r, sp, sr, x, r))
    for x, r, y, sr in PROJECT_COPIES:
        if x in M:
            stop.append("STOP: project folder copied: %s (r%d) is a copy of project %s@%d; it would become a "
                        "separate repository whose history starts at r%d" % (x, r, y, sr, r))
    for m in M:
        P, roots, nested, path, life = layout_info(m)
        for kn, outer in sorted(nested.items()):
            ok = [lt for lt in life[outer] if lt[2]]       # lifetimes that began as a copy
            for c, _, _ in life[kn]:
                if not any(a <= c and (b is None or c < b) for a, b, _ in ok):
                    stop.append("STOP: nested branch/tag in a reused folder: %s (created r%d) lies inside %s, which "
                                "is no copied branch/tag at r%d (copied: %s); its content and merges would be "
                                "folded into %s" % (path[kn], c, path[outer], c, ", ".join(
                                    "r%d-%s" % (a, "r%d" % b if b else "HEAD") for a, b, _ in ok) or "never",
                                    path[outer]))
    for m in M:
        if m not in projs and not any(under(p, join(m, "trunk")) for p in PATHS):
            warn.append("WARN: %s never had a lowercase trunk folder: check the project path and its case" % m)

    case, git = {}, {}
    for p in PATHS:
        comps = p.split("/")[1:]
        if comps[-1].lower() in LAYOUT and comps[-1] not in LAYOUT \
                and not any(c.lower() in LAYOUT for c in comps[:-1]) and is_dir(p):
            case.setdefault(p, EV[p][0][0])
        if dotgit(p):
            i = next(i for i, c in enumerate(comps) if c.lower() == ".git")
            g = "/" + "/".join(comps[:i + 1])
            git[g] = min(git.get(g, LAST + 1), EV[p][0][0])
    warn += ["WARN: layout folder name in another case is not used as trunk/branches/tags: %s (r%d); "
             "its content is migrated as ordinary folders" % (p, r) for p, r in sorted(case.items())]
    gl = ["WARN: .git folder not migrated: %s (r%d%s)" % (g, r, "" if is_dir(g) else ", a file")
          for g, r in sorted(git.items())]
    warn += gl[:20] + (["WARN: %d more .git paths not migrated" % (len(gl) - 20)] if len(gl) > 20 else [])

    outside = []
    if not any(m in ("/", "NONE", "OTHER") for m in migrate):
        for p in PATHS:
            if p == "/" or dotgit(p) or any(under(p, m) or under(m, p) for m in M):
                continue
            comps = p.split("/")[1:]
            g = next("/" + "/".join(comps[:i]) for i in range(1, len(comps) + 1)
                     if not any(under(m, "/" + "/".join(comps[:i])) for m in M))
            if g not in outside:
                outside.append(g)
        outside.sort()
        if A.explicit:
            warn += ["WARN: not migrated (outside the selected project): %s" % g for g in outside]
    print("\n".join(stop + warn + ["OUTSIDE: " + g for g in outside]))
    sys.exit(3 if stop else 0)


# ---------- rules --------------------------------------------------------------------------------
if not A.repo:
    sys.exit("ERROR: REPO_NAME missing")
name = A.repo
if re.search(r"[\s#]|\$\{", name):
    sys.exit("ERROR: repository name %r cannot be used in a rules file" % name)
pid = norm_id(A.project) if A.project else (projects() or ["NONE"])[0]
excl = [x for x in ids(A.exclude) if x not in ("/", "NONE", "OTHER", pid)]

out = ["create repository %s\nend repository\n" % name,
       "# .git folders cannot be stored in git\nmatch .*/\\.[Gg][Ii][Tt](/|$)\nend match\n"]
lay = {"project": pid, "base": "" if pid in ("/", "NONE", "OTHER") else pid, "exclude": excl,
       "region_ref": "refs/heads/main" if pid in ("NONE", "OTHER") else "refs/heads/svn-outside-layout",
       "roots": []}
info, renamed, nroots = [], [], 0


def recurse_rules(rec):
    return ["match %s/$\n  min revision %d\n  max revision %d\n  action recurse\nend match\n" % (rx(g), r, r)
            for r in sorted(rec) for g in sorted(rec[r])]


if pid == "OTHER" and not excl:
    sys.exit("ERROR: --project OTHER needs --exclude with every project migrated in this run")
if pid in ("NONE", "OTHER"):
    out += recurse_rules(dotgit_recursions("", set(), excl))
    if pid == "OTHER":
        out += ["# migrated separately\nmatch %s/\nend match\n" % rx(x) for x in excl]
    out.append("# the whole %s becomes master\nmatch /\n  repository %s\n  branch master\nend match\n"
               % ("repository" if pid == "NONE" else "rest of the repository", name))
else:
    P, roots, nested, path, life = layout_info(pid)
    nroots = len(roots)
    names, why = git_names(roots, path, life)
    rec, cont = recursions(P, roots)
    for r, ds in dotgit_recursions(P, {P + "/trunk"} | {path[kn] for kn in roots}, excl).items():
        rec[r] |= ds
    out += recurse_rules(rec)
    out.append("match %s/trunk/\n  repository %s\n  branch master\nend match\n" % (rx(P), name))
    for kn in sorted(roots, key=lambda kn: (-len(kn[1]), kn)):
        k, n = kn
        g = names[kn]
        if g != n:
            renamed.append("%s/%s -> %s%s" % (k, n, g, "  (%s)" % why[kn] if kn in why else ""))
        out.append("match %s/%s/%s/\n  repository %s\n  branch %s\n%send match\n"
                   % (rx(P), k, rx(n), name, "refs/tags/" + g if k == "tags" else g,
                      "  annotated true\n" if k == "tags" else ""))
        lt = life[kn]
        lay["roots"].append({"kind": "tag" if k == "tags" else "branch", "path": path[kn],
                             "ref": ("refs/tags/" if k == "tags" else "refs/heads/") + g,
                             "lifetimes": [[a, b] for a, b, _ in lt], "alive": bool(lt) and lt[-1][1] is None})
    lay["roots"].sort(key=lambda x: (x["kind"], x["path"]))
    tl = lifetimes(P + "/trunk")
    lay["trunk"] = {"path": P + "/trunk", "lifetimes": [[a, b] for a, b, _ in tl],
                    "alive": bool(tl) and tl[-1][1] is None}
    info = ["%s/%s is content of %s/%s (nested)" % (kn + nested[kn]) for kn in sorted(nested)]
    out.append("# files in branches/ or tags/ outside every branch/tag\nmatch %s/((branches|tags)/.*[^/])$\n"
               "  repository %s\n  branch svn-outside-layout\n  prefix \\1\nend match\n" % (rx(P), name))
    out += ["match %s/(branches|tags)/\n  min revision %d\n  max revision %d\n  action recurse\nend match\n"
            % (rx(P), r, r) for r in sorted(cont)]
    out.append("# layout containers themselves: nothing to import\nmatch %s/(trunk|branches|tags)/\nend match\n" % rx(P))
    out += ["# migrated separately\nmatch %s/\nend match\n" % rx(x) for x in excl if under(x, pid)]
    out.append("# anything else in the project folder is kept on its own branch\n"
               "match %s/([^/]+)/\n  repository %s\n  branch svn-outside-layout\n  prefix \\1/\nend match\n"
               "match %s/([^/]+)$\n  repository %s\n  branch svn-outside-layout\n  prefix \\1\nend match\n"
               % (rx(P), name, rx(P), name))
    if P:
        out.append("# outside this project\nmatch /\nend match\n")
print("\n".join(out))
if A.layout_out:
    with open(A.layout_out, "w", encoding="utf-8") as f:
        json.dump(lay, f, indent=1, ensure_ascii=False)
        f.write("\n")
print("project=%s roots=%d renamed=%d" % (pid, nroots, len(renamed)), *(renamed + info), sep="\n  ", file=sys.stderr)
