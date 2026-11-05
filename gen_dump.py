#!/usr/bin/env python3
"""Generate a large, realistic SVN dump file with migration edge cases.

usage: gen_dump.py OUT.dump NUM_REVS [--big]
"""
import hashlib, random, sys, datetime

random.seed(1234)
OUT, NREVS = sys.argv[1], int(sys.argv[2])
BIG = "--big" in sys.argv

AUTHORS = ["alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi",
           "ivan", "judy", "mallory", "oscar", "peggy", "trent", "victor",
           "walter", "j.doe", "CORP\\jsmith", "CORP\\mlee", "build-bot"]

f = open(OUT, "wb")
w = f.write
w(b"SVN-fs-dump-format-version: 2\n\n")

def props_blob(d):
    out = b""
    for k, v in d.items():
        k = k.encode(); v = v.encode() if isinstance(v, str) else v
        out += b"K %d\n%s\nV %d\n%s\n" % (len(k), k, len(v), v)
    return out + b"PROPS-END\n"

# ---- in-memory model of HEAD -------------------------------------------
files = {}      # path -> [content bytes, props dict]
dirs = set()    # directory paths
branches = {}   # name -> alive(bool)
tags = {}
trunk_snaps = {}  # rev -> (files subset, dirs subset) for trunk
rev = 0
date = datetime.datetime(2009, 3, 1, 9, 0, 0)
nodes = []      # nodes of the revision being built
stats = {"empty_revs": 0, "noauthor": 0}

def begin():
    global nodes
    nodes = []

def node(path, kind, action, text=None, props=None, copyfrom=None):
    nodes.append((path, kind, action, text, props, copyfrom))

def commit(log, author="__rand__", allow_empty=False):
    global rev, date
    if not nodes and not allow_empty:
        return
    rev += 1
    date += datetime.timedelta(minutes=random.randint(5, 600))
    rp = {}
    if log is not None:
        rp["svn:log"] = log
    if author == "__rand__":
        author = random.choice(AUTHORS)
    if author is not None:
        rp["svn:author"] = author
    else:
        stats["noauthor"] += 1
    rp["svn:date"] = date.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    if log and random.random() < 0.10:
        rp["bugtraq:issue"] = f"PROJ-{random.randint(1,5000)}"
        rp["review:approved-by"] = random.choice(AUTHORS)
        rp["svn:log"] = log + "\n\nDetails:\n- line one\n- l\u00edne tw\u00f6\n"
        stats["revprop_revs"] = stats.get("revprop_revs", 0) + 1
    pb = props_blob(rp)
    w(b"Revision-number: %d\nProp-content-length: %d\nContent-length: %d\n\n" % (rev, len(pb), len(pb)))
    w(pb + b"\n")
    for path, kind, action, text, props, copyfrom in nodes:
        w(b"Node-path: %s\n" % path.encode())
        if kind:
            w(b"Node-kind: %s\n" % kind.encode())
        w(b"Node-action: %s\n" % action.encode())
        if copyfrom:
            w(b"Node-copyfrom-rev: %d\nNode-copyfrom-path: %s\n" % (copyfrom[1], copyfrom[0].encode()))
        pb = props_blob(props) if props is not None else b""
        if props is not None:
            w(b"Prop-content-length: %d\n" % len(pb))
        if text is not None:
            w(b"Text-content-length: %d\nText-content-md5: %s\n" % (len(text), hashlib.md5(text).hexdigest().encode()))
        if props is not None or text is not None:
            w(b"Content-length: %d\n" % (len(pb) + (len(text) if text is not None else 0)))
        w(b"\n")
        w(pb)
        if text is not None:
            w(text)
        w(b"\n\n")
    if not nodes:
        stats["empty_revs"] += 1
    if rev % 100 == 0:
        snap_f = {p: (c, dict(pr)) for p, (c, pr) in files.items() if p.startswith("trunk/")}
        snap_d = {d for d in dirs if d == "trunk" or d.startswith("trunk/")}
        trunk_snaps[rev] = (snap_f, snap_d)

# ---- primitive operations (update model + emit nodes) -------------------
def mkdirs(path):
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        d = "/".join(parts[:i])
        if d not in dirs:
            dirs.add(d); node(d, "dir", "add", props={})

def add_file(path, content, props=None):
    mkdirs(path.rsplit("/", 1)[0])
    props = props or {}
    files[path] = [content, props]
    node(path, "file", "add", text=content, props=props)

def modify_file(path, content):
    files[path][0] = content
    node(path, "file", "change", text=content)

def delete_path(path):
    node(path, None, "delete")
    for p in [p for p in files if p == path or p.startswith(path + "/")]:
        del files[p]
    for d in [d for d in dirs if d == path or d.startswith(path + "/")]:
        dirs.discard(d)

def copy_tree(src, dst, from_rev=None, snapshot=None):
    """svn copy src@from_rev -> dst (dir)."""
    from_rev = from_rev or rev
    mkdirs(dst.rsplit("/", 1)[0])
    node(dst, "dir", "add", copyfrom=(src, from_rev))
    if snapshot:
        sf, sd = snapshot
    else:
        sf, sd = files, dirs
    for p, (c, pr) in list(sf.items()):
        if p.startswith(src + "/"):
            files[dst + p[len(src):]] = [c, dict(pr)]
    for d in list(sd):
        if d == src or d.startswith(src + "/"):
            dirs.add(dst + d[len(src):])

def copy_file(src, dst):
    mkdirs(dst.rsplit("/", 1)[0])
    c, pr = files[src]
    files[dst] = [c, dict(pr)]
    node(dst, "file", "add", copyfrom=(src, rev))

def files_under(prefix):
    return [p for p in files if p.startswith(prefix + "/")]

WORDS = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu".split()
def text_blob(n):
    return ("\n".join(" ".join(random.choices(WORDS, k=8)) for _ in range(n)) + "\n").encode()

# ---- r1..r2: layout + initial import -----------------------------------
begin()
for d in ("trunk", "branches", "tags"):
    mkdirs(d)
commit("Create standard layout", "alice")

begin()
MODULES = ["core", "web", "api", "cli", "docs", "module-a", "legacy/old stuff"]
for m in MODULES:
    for i in range(220):
        depth = random.randint(0, 3)
        sub = "/".join(random.choice(["util", "impl", "model", "io", "net"]) for _ in range(depth))
        path = f"trunk/{m}/{sub}/file_{i}.txt".replace("//", "/")
        if path not in files:
            add_file(path, text_blob(random.randint(5, 40)))
# special files
add_file("trunk/README with spaces.md", b"# Big repo\n")
add_file("trunk/docs/ünïcödé-日本.txt", "unicode name\n".encode())
add_file("trunk/scripts/build.sh", b"#!/bin/sh\necho build\n", {"svn:executable": "*"})
add_file("trunk/scripts/current", b"link build.sh", {"svn:special": "*"})
add_file("trunk/core/version.c", b"/* $Id$ */\n/* $Revision$ */\nint v;\n", {"svn:keywords": "Id Revision"})
add_file("trunk/core/native.txt", b"line1\nline2\n", {"svn:eol-style": "native"})
add_file("trunk/assets/logo.png", bytes(random.getrandbits(8) for _ in range(50000)), {"svn:mime-type": "image/png"})
mkdirs("trunk/empty/dir")  # empty directory (git cannot represent)
node("trunk", "dir", "change", props={"svn:ignore": "*.o\n*.class\ntarget\n", "svn:externals": "vendor https://example.com/svn/vendor/trunk\n"})
commit("Initial import of codebase", "alice")

# ---- main history -------------------------------------------------------
SPECIAL_BRANCHES = ["release 2.0", "feature..bad", "hotfix/1.x", "UPPER"]
SPECIAL_TAGS = ["v1.0~rc1", "release-1.0", "UPPER"]
fixed = {}
def at(frac):
    return max(5, int(NREVS * frac))
fixed[at(0.10)] = "branch_specials"
fixed[at(0.12)] = "subdir_branch"
fixed[at(0.15)] = "tag_specials"
fixed[at(0.20)] = "cross_commit"
fixed[at(0.25)] = "outside_layout"
fixed[at(0.30)] = "modify_tag"
fixed[at(0.35)] = "replace_file_with_dir"
fixed[at(0.40)] = "empty_rev"
fixed[at(0.45)] = "noauthor"
fixed[at(0.50)] = "empty_log"
fixed[at(0.55)] = "branch_from_old_rev"
fixed[at(0.60)] = "branch_from_branch"
fixed[at(0.65)] = "delete_branch_recreate"
fixed[at(0.70)] = "big_files" if BIG else "noop"
fixed[at(0.71)] = "delete_big_files" if BIG else "noop"
fixed[at(0.75)] = "prop_only"
fixed[at(0.80)] = "readd_deleted"

bcount = 0
MERGEINFO = {}
def alive_branches():
    return [b for b, a in branches.items() if a]

def new_branch(name, src="trunk", from_rev=None, snapshot=None):
    branches[name] = True
    copy_tree(src, f"branches/{name}", from_rev, snapshot)

def do_modify(base, n):
    cand = [p for p in files_under(base) if not files[p][1].get("svn:special") and not files[p][1].get("svn:mime-type")]
    for p in random.sample(cand, min(n, len(cand))):
        lines = files[p][0].split(b"\n")
        i = random.randrange(len(lines))
        lines.insert(i, f"r{rev+1} edit {random.random():.6f}".encode())
        modify_file(p, b"\n".join(lines))

while rev < NREVS:
    begin()
    target = rev + 1
    ev = fixed.get(target)
    log = None
    if ev == "branch_specials":
        for b in SPECIAL_BRANCHES:
            new_branch(b)
        log = "Create branches with awkward names"
    elif ev == "subdir_branch":
        branches["module-a-only"] = True
        copy_tree("trunk/module-a", "branches/module-a-only")
        log = "Branch only a subdirectory (non-root branch)"
    elif ev == "tag_specials":
        for t in SPECIAL_TAGS:
            tags[t] = True; copy_tree("trunk", f"tags/{t}")
        log = "Tags with awkward names"
    elif ev == "cross_commit":
        do_modify("trunk", 3); do_modify("branches/hotfix/1.x", 3)
        log = "One commit touching trunk AND a branch"
    elif ev == "outside_layout":
        add_file("sandbox/experiment.txt", b"outside trunk/branches/tags\n")
        log = "Commit outside standard layout"
    elif ev == "modify_tag":
        do_modify("tags/release-1.0", 2)
        log = "Someone committed to a tag"
    elif ev == "replace_file_with_dir":
        victim = "trunk/README with spaces.md"
        delete_path(victim)
        add_file(victim + "/index.md", b"now a directory\n")
        log = "Replace a file with a directory"
    elif ev == "empty_rev":
        commit("Empty revision (e.g. from svndumpfilter)", allow_empty=True); continue
    elif ev == "noauthor":
        do_modify("trunk", 1); commit("Commit with no svn:author", author=None); continue
    elif ev == "empty_log":
        do_modify("trunk", 1); log = ""
    elif ev == "branch_from_old_rev":
        old = max(r for r in trunk_snaps if r < rev - 50) if any(r < rev - 50 for r in trunk_snaps) else max(trunk_snaps)
        new_branch("maint-old", "trunk", old, trunk_snaps[old])
        log = f"Branch from old trunk r{old}"
    elif ev == "branch_from_branch":
        new_branch("sub-of-hotfix", "branches/hotfix/1.x")
        log = "Branch from a branch"
    elif ev == "delete_branch_recreate":
        commit_branch = "recreated"
        new_branch(commit_branch); commit("Create branch 'recreated'"); begin()
        do_modify("branches/recreated", 2); commit("Work on recreated v1"); begin()
        delete_path("branches/recreated"); branches["recreated"] = False; commit("Delete branch recreated"); begin()
        new_branch("recreated"); log = "Recreate branch 'recreated' from trunk"
    elif ev == "big_files":
        add_file("trunk/assets/video-60MB.bin", random.randbytes(60 * 1024 * 1024), {"svn:mime-type": "application/octet-stream"})
        add_file("trunk/assets/dataset-120MB.bin", random.randbytes(120 * 1024 * 1024), {"svn:mime-type": "application/octet-stream"})
        log = "Add large binaries"
    elif ev == "delete_big_files":
        delete_path("trunk/assets/dataset-120MB.bin")
        log = "Oops, remove 120MB file (still in history!)"
    elif ev == "prop_only":
        node("trunk/core/native.txt", "file", "change", props={"svn:eol-style": "native", "custom:reviewed": "yes"})
        files["trunk/core/native.txt"][1]["custom:reviewed"] = "yes"
        log = "Property-only change"
    elif ev == "readd_deleted":
        p = "trunk/core/version.c"
        delete_path(p); commit("Delete version.c"); begin()
        add_file(p, b"/* re-added, unrelated history */\n"); log = "Re-add version.c"
    elif ev == "noop":
        do_modify("trunk", 1); log = "regular change"
    else:
        r = random.random()
        ab = alive_branches()
        base = "trunk" if (not ab or random.random() < 0.7) else "branches/" + random.choice(ab)
        if base.startswith("branches/") and not files_under(base):
            base = "trunk"
        if r < 0.72:
            do_modify(base, random.randint(1, 8)); log = f"Change {random.randint(1000,9999)} in {base}"
        elif r < 0.80:
            for _ in range(random.randint(1, 5)):
                p = f"{base}/{random.choice(MODULES)}/new/file_{rev}_{random.randint(0,99999)}.txt"
                if p not in files:
                    add_file(p, text_blob(10))
            log = f"Add files in {base}"
        elif r < 0.84:
            c = [p for p in files_under(base) if not p.endswith("version.c")]
            for p in random.sample(c, min(2, len(c))):
                delete_path(p)
            log = f"Delete files in {base}"
        elif r < 0.87:
            c = files_under(base)
            if c:
                src = random.choice(c); dst = src.rsplit("/", 1)[0] + f"/renamed_{rev}.txt"
                if dst not in files:
                    copy_file(src, dst); delete_path(src)
            log = f"Rename in {base}"
        elif r < 0.89:
            bcount += 1
            src = "trunk"
            if ab and random.random() < 0.2:
                src = "branches/" + random.choice(ab)
            name = f"feature-{bcount}"
            new_branch(name, src); log = f"Create branch {name} from {src}"
        elif r < 0.905:
            src = "trunk" if random.random() < 0.8 or not ab else "branches/" + random.choice(ab)
            name = f"v{len(tags)+1}.{random.randint(0,9)}"
            if name not in tags:
                tags[name] = True; copy_tree(src, f"tags/{name}")
            log = f"Tag {name}"
        elif r < 0.925 and ab:
            b = random.choice([x for x in ab if x not in ("module-a-only",)] or ab)
            bp = f"branches/{b}"
            if b != "module-a-only" and files_under(bp):
                changed = [p for p in files_under(bp)
                           if ("trunk" + p[len(bp):]) in files and files[p][0] != files["trunk" + p[len(bp):]][0]
                           and not files[p][1].get("svn:special")]
                for p in changed[:20]:
                    modify_file("trunk" + p[len(bp):], files[p][0])
                MERGEINFO[f"/{bp}"] = rev
                node("trunk", "dir", "change", props={"svn:ignore": "*.o\n*.class\ntarget\n",
                     "svn:externals": "vendor https://example.com/svn/vendor/trunk\n",
                     "svn:mergeinfo": "\n".join(f"{k}:1-{v}" for k, v in MERGEINFO.items())})
                log = f"Merge {b} into trunk"
        elif r < 0.93 and len(ab) > 6:
            b = random.choice([x for x in ab if x.startswith("feature-")] or [None])
            if b:
                delete_path(f"branches/{b}"); branches[b] = False; log = f"Delete merged branch {b}"
        else:
            do_modify("trunk", 2); log = "misc"
    commit(log if log is not None else "change")

f.close()
print(f"revisions={rev} files_at_head={len(files)} branches={len(branches)} "
      f"(alive {len(alive_branches())}) tags={len(tags)} {stats}", file=sys.stderr)
