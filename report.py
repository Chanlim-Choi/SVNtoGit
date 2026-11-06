#!/usr/bin/env python3
"""Write MIGRATION_REPORT.md + suggested.gitattributes: everything git cannot carry
over automatically, listed by name so nothing is lost silently.

usage: report.py SVN_URL OUT_DIR GIT_REPO [PROJECT_ID] [--rev N] [--layout FILE]
  reads OUT_DIR/meta/{log.json,preflight.txt,revmap.json}, OUT_DIR/{renames.txt,merges.json,
        verify.json,verify.log,lfs.log,lfs-paths.txt} (when present); every svn:externals
        definition at r N is read once and cached in OUT_DIR/meta/externals.json
"""
import base64, json, os, re, subprocess, sys, unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from urllib.parse import quote, unquote

args, opt = [], {}
it = iter(sys.argv[1:])
for a in it:
    if a in ("--rev", "--layout"):
        opt[a] = next(it, "")
    else:
        args.append(a)
if len(args) not in (3, 4) or not opt.get("--rev", "0").isdigit() or opt.get("--layout") == "":
    sys.exit("usage: report.py SVN_URL OUT_DIR GIT_REPO [PROJECT_ID] [--rev N] [--layout FILE]")
url, out, repo = args[0].rstrip("/"), args[1], args[2]
project = args[3] if len(args) > 3 else "/"
lc = os.environ.get("LC_ALL", "")
env = dict(os.environ, LC_ALL=lc if re.search("utf-?8", lc, re.I) else "C.UTF-8")

def load(name, default):
    p = os.path.join(out, name)
    if not os.path.exists(p):
        return default
    return json.load(open(p, encoding="utf-8")) if name.endswith(".json") else open(p, encoding="utf-8", errors="replace").read()

revs = load("meta/log.json", {})
REV = opt.get("--rev") or (str(max(map(int, revs))) if revs else "HEAD")
layout = {}
if "--layout" in opt:
    try:
        layout = json.load(open(opt["--layout"], encoding="utf-8"))
    except (OSError, ValueError) as e:
        sys.exit(f"ERROR: cannot read --layout {opt['--layout']}: {e}")
LAYOUT = project not in ("NONE", "OTHER")
BASE = layout.get("base", "" if project in ("/", "NONE", "OTHER") else "/" + project.strip("/")).rstrip("/")
EXCLUDE = [e.rstrip("/") for e in layout.get("exclude", []) if e.rstrip("/")]
MAIN_SVN = BASE + "/trunk" if LAYOUT else ""     # svn path whose content is main ("" = SVN_URL itself)
ON_MAIN = "on trunk" if LAYOUT else "on main"
L = []
w = L.append

def code(s):   # markdown code span that survives backticks in names
    s = str(s).replace("\n", " ")
    f = "`" * (max(map(len, re.findall("`+", s)), default=0) + 1)
    return f"{f} {s} {f}" if s[:1] == "`" or s[-1:] == "`" else f"{f}{s}{f}"

def human(n):
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{n:.1f} {u}"
        n /= 1024

def inside(p, d):   # svn path p is d or below it ("" = everything)
    return not d or p == d or p.startswith(d + "/")

def in_project(p):   # paths with a .git component are never migrated
    return (inside(p, BASE) and not any(inside(p, e) for e in EXCLUDE)
            and not any(c.lower() == ".git" for c in p.split("/")))

def rel(p, d):
    return p[len(d):].lstrip("/")

# ---- svn: every call pinned to r REV; svn prints URI-encoded URLs ----------------------
def svn_xml(*a, warn=True):
    """(parsed XML, None) or (None, error text)"""
    try:
        r = subprocess.run(["svn", "--non-interactive", *a], capture_output=True, check=True, env=env)
        # svn info/ls write svn:author raw: control characters would make the XML invalid
        return ET.fromstring(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "?", r.stdout.decode("utf-8", "replace"))), None
    except subprocess.CalledProcessError as e:
        msg = " ".join(e.stderr.decode("utf-8", "replace").split())
        if warn:
            print(f"WARN: svn {a[0]} failed (the report says what is missing): {msg}", file=sys.stderr)
        return None, msg

def value(p):   # svn base64-encodes property values that are not XML-safe
    if p is None:
        return ""
    if p.get("encoding") != "base64":
        return p.text or ""
    raw = base64.b64decode(p.text or "")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return "base64:" + base64.b64encode(raw).decode()

info, _ = svn_xml("info", "--xml", f"{url}@{REV}")
CANON = (info.findtext("entry/url") if info is not None else None) or url
TOP = unquote(CANON)

def svn_path(target):   # URL printed by svn -> svn path below SVN_URL ("" = SVN_URL)
    u = unquote(target or "")
    return u[len(TOP):] if u == TOP or u.startswith(TOP + "/") else u

def target(p, rev=None):
    return f"{CANON}{quote(p)}@{rev or REV}"

def area(p, rev=None):
    """svn paths covering p minus the excluded subtrees, each to be read recursively"""
    if any(inside(p, e) for e in EXCLUDE):
        return []
    if not any(inside(e, p) for e in EXCLUDE):
        return [p]
    x, _ = svn_xml("list", "--xml", target(p, rev))
    return [p] if x is None else [q for n in x.findall("list/entry/name") for q in area(f"{p}/{n.text}", rev)]

def props_below(paths, rev=None, warn=True):
    """({svn path: {name: value}} for every node at/below the paths, error text or None)"""
    res = {}
    for i in range(0, len(paths), 100):
        x, err = svn_xml("proplist", "-R", "-v", "--xml", *(target(p, rev) for p in paths[i:i + 100]), warn=warn)
        if x is None:
            return {}, err
        for t in x.findall("target"):
            res[svn_path(t.get("path"))] = {q.get("name"): value(q) for q in t.findall("property")}
    return res, None

def externals():
    """every svn:externals at r REV below SVN_URL, read once per run (cache: meta/externals.json)"""
    cache, logf = os.path.join(out, "meta", "externals.json"), os.path.join(out, "meta", "log.json")
    if os.path.exists(cache) and not (os.path.exists(logf) and os.path.getmtime(cache) < os.path.getmtime(logf)):
        return {("" if k == "/" else k): v for k, v in json.load(open(cache, encoding="utf-8")).items()}, None
    x, err = svn_xml("propget", "-R", "--xml", "svn:externals", target(""))
    if x is None:
        return {}, err
    ext = {svn_path(t.get("path")): value(t.find("property")) for t in x.findall("target")}
    try:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({(k or "/"): v for k, v in ext.items()}, f, ensure_ascii=False, indent=0, sort_keys=True)
    except OSError as e:
        print(f"WARN: cannot cache svn:externals in {cache}: {e}", file=sys.stderr)
    return ext, None

# ---- git ------------------------------------------------------------------------------
def git(*a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, env=env).stdout.decode("utf-8", "replace")

refs = git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags", "refs/notes", "refs/backups").split("\n")
refs = [r for r in refs if r]
MAIN = "refs/heads/main" if "refs/heads/main" in refs else "refs/heads/master"

# ---- verdict: only a complete verify.json of this run counts (F27) ---------------------
vfile, logf = os.path.join(out, "verify.json"), os.path.join(out, "meta", "log.json")
verify, unverified = {}, ""
if not os.path.exists(vfile):
    unverified = "verify.json is missing: verify.py did not finish (see verify.log)"
elif os.path.exists(logf) and os.path.getmtime(vfile) < os.path.getmtime(logf):
    unverified = "verify.json is older than meta/log.json (left from an earlier run)"
else:
    try:
        verify = json.load(open(vfile, encoding="utf-8"))
    except ValueError as e:
        unverified = f"verify.json is unreadable ({e})"
    if isinstance(verify, dict) and ("crashed" in verify or "crashed" in (verify.get("fatal") or [])):
        unverified = f"verify.py crashed: {verify.get('crashed') or 'unknown error'} (see verify.log)"
    elif not isinstance(verify, dict) or not isinstance(verify.get("fatal"), list):
        verify, unverified = {}, unverified or "verify.json holds no verification result"
problems = verify.get("problems") or {}   # a crashed run's partial findings are still shown
merges = load("merges.json", {"merges_complete": [], "merges_partial": []})

w("# SVN → Git migration report\n")
if unverified:
    w(f"**Verification: NOT VERIFIED** — {unverified}\n")
    print(f"WARN: {out}/MIGRATION_REPORT.md says NOT VERIFIED: {unverified}", file=sys.stderr)
else:
    fatal = verify.get("fatal", [])
    w(f"**Verification: {'FAIL' if fatal else 'PASS'}**" + (f" — failing checks: {', '.join(fatal)}" if fatal else "") + "\n")
w("| | |\n|---|---|")
w(f"| SVN revisions | {len(revs)} (up to r{REV}) |")
w(f"| Git commits | {git('rev-list', '--count', '--branches', '--tags').strip()} |")
w(f"| Branches | {sum(r.startswith('refs/heads/') for r in refs)} |")
w(f"| Tags | {sum(r.startswith('refs/tags/') for r in refs)} |")
w(f"| Merge commits rebuilt from svn:mergeinfo | {len(merges['merges_complete'])} |")
w(f"| Notes refs | {', '.join(r for r in refs if r.startswith('refs/notes/')) or 'none'} |")
w("")
for line in load("verify.log", "").splitlines():
    if line.startswith(("historical commits:", "merges:", "notes:", "git commits=")):
        w(f"- {line}")
w("\nEvery commit carries a note in `refs/notes/svn` with its SVN revision, path, original author, "
  "date, custom revision properties, changed paths and SVN properties. View with "
  "`git log --notes=svn`; push with `git push origin 'refs/notes/*'`; after a clone fetch them with `git fetch origin 'refs/notes/*:refs/notes/*'`.\n")

w("## Needs a human decision\n")
pre = load("meta/preflight.txt", "").splitlines()
msgs = [l for l in pre if l.startswith(("STOP: ", "WARN: "))]
outside = [l[len("OUTSIDE: "):] for l in pre if l.startswith("OUTSIDE: ")]
w(f"### Pre-flight warnings for the whole SVN repository ({len(msgs)})\n")
w("Content that is not migrated or not where you may expect it (outside the selected project, "
  "`trunk`/`branches`/`tags` spelled in another case, `.git` folders). A `STOP` listed here was "
  "overridden with `--allow-split-history`: that history is split between repositories.\n")
for l in msgs[:100]:
    w(f"- {code(l)}")
if len(msgs) > 100:
    w(f"- ... {len(msgs) - 100} more in `meta/preflight.txt`")
if not msgs:
    w("- none")
if outside:
    w("\nContent outside the migrated projects. Without `--project` it is migrated to the extra repository "
      "`<source>-other`; with `--project` it is not migrated:\n")
    w("\n".join(f"- {code(p)}" for p in outside[:100]))

ext, ext_err = externals()
ext = {p: v for p, v in ext.items() if in_project(p)}
w(f"\n### svn:externals ({len(ext)}) — no git equivalent\n")
w(f"Re-create as git submodules, subtrees, or a package dependency. Definitions at r{REV}:\n")
for p, v in sorted(ext.items()):
    for d in v.strip().splitlines():
        w(f"- {code(p or '/')}: {code(d.strip())}")
if ext_err:
    w(f"- could not be read: {code(ext_err)}")
elif not ext:
    w("- none")

MREV = REV   # trunk deleted before REV (F09): main is its last state, so read that state
gone = [int(r) for r, v in revs.items() if LAYOUT and int(r) <= int(REV)
        for c in v["paths"] if c.get("action") in ("D", "R") and inside(MAIN_SVN, c["path"])]
parts = area(MAIN_SVN)
mprops, mprops_err = props_below(parts, warn=not gone) if parts else ({}, None)
if mprops_err and gone:
    MREV = str(max(gone) - 1)
    parts = area(MAIN_SVN, MREV)
    mprops, mprops_err = props_below(parts, MREV) if parts else ({}, None)
mprops = {rel(p, MAIN_SVN): v for p, v in mprops.items() if in_project(p) and inside(p, MAIN_SVN) and p != MAIN_SVN}
kw = {p: v["svn:keywords"] for p, v in mprops.items() if "svn:keywords" in v}
w(f"\n### svn:keywords ({len(kw)} files {ON_MAIN}) — not expanded by git\n")
if MREV != REV:
    w(f"{MAIN_SVN} does not exist at r{REV} (deleted in r{max(gone)}); main and this report use its last "
      f"state, r{MREV}.\n")
w("Files contain literal `$Id$`/`$Revision$` text. Usually fine; otherwise drop the keywords or use a "
  "`filter` in `.gitattributes`.\n")
for p, v in sorted(kw.items())[:50]:
    w(f"- {code(p)}: {v}")
if mprops_err:
    w(f"- could not be read ({MAIN_SVN or '/'} at r{MREV}): {code(mprops_err)}")
partial = merges["merges_partial"]
w(f"\n### Partial merges / cherry-picks ({len(partial)}) — git cannot represent\n")
w("Content is correct; git just does not record them as merges. The SVN merge record stays in the notes.\n")
for m in partial[:100]:
    w(f"- r{m['rev']}: {m['from']} → {m['into']} (unmerged source revisions e.g. {m['unmerged_revs'][:5]})")
if not partial:
    w("- none")

# ---- pre-flight: paths that break checkouts on Windows / macOS ---------------------
WIN_BAD = re.compile(r'(^|/)((con|prn|aux|nul|com[1-9]|lpt[1-9])(\.[^/]*)?|[^/]*[<>:"|?*\\\x00-\x1f][^/]*|[^/]*[. ])(/|$)', re.I)
case_hits, win_hits, long_hits, nfc_hits = [], [], [], []
for ref in [r for r in refs if r.startswith("refs/heads/")]:
    same = defaultdict(list)   # macOS/Windows compare names case-insensitively, macOS also Unicode-normalized
    for p in git("ls-tree", "-r", "--name-only", "-z", ref).split("\0"):
        if p:
            n = unicodedata.normalize("NFC", p)
            same[n.casefold()].append(p)
            if n != p:
                nfc_hits.append(f"{ref[11:]}: {code(p)} (decomposed Unicode, not NFC: macOS shows it as deleted + untracked)")
            if WIN_BAD.search(p):
                win_hits.append(f"{ref[11:]}: {code(p)}")
            if len(p) > 200:
                long_hits.append(f"{ref[11:]}: {len(p)} chars {code(p[:80] + '…')}")
    case_hits += [f"{ref[11:]}: " + ", ".join(code(x) for x in v) for v in same.values() if len(v) > 1]
hits = case_hits[:50] + nfc_hits[:50] + win_hits[:50] + long_hits[:20]
w(f"\n### Paths that break checkouts on Windows/macOS ({len(case_hits) + len(nfc_hits) + len(win_hits) + len(long_hits)})\n")
w("Git stores these fine, but a clone on a case-insensitive (macOS/Windows), Unicode-normalizing (macOS) "
  "or Windows file system will fail or silently lose files. Rename them in SVN before the final run, or on main after.\n")
for h in hits:
    w(f"- {h}")
if not hits:
    w("- none")

w("\n## Done automatically — for your information\n")
ren = load("renames.txt", "").strip()
w("### Branch/tag names changed (illegal in git, or colliding)\n")
w("\n".join(f"- {code(l.strip())}" for l in ren.splitlines() if "->" in l) or "- none")
dele = [r for r in refs if r.startswith("refs/tags/svn-deleted/")]
dtags = [r for r in dele if r.startswith("refs/tags/svn-deleted/tags/")]
dbr = [r for r in dele if r not in dtags]
w(f"\n### Deleted SVN branches kept ({len(dbr)})\n")
w("Their full history is preserved as tags `svn-deleted/<name>@<revision that deleted or replaced it>`.\n")
w("\n".join(f"- {code(r)}" for r in dbr[:200]) or "- none")
w(f"\n### Deleted SVN tags kept ({len(dtags)})\n")
w("Preserved as tags `svn-deleted/tags/<name>@<revision that deleted or replaced it>`.\n")
w("\n".join(f"- {code(r)}" for r in dtags[:200]) or "- none")
left = [r for r in refs if r.startswith("refs/backups/")]
if left:
    w(f"\n**Not pushed** — backup refs of the converter that were not renamed ({len(left)}):\n")
    w("\n".join(f"- {code(r)}" for r in left[:200]))
revmap = load("meta/revmap.json", {})
w(f"\n### SVN revision numbers changed by loading the dump ({len(revmap)})\n")
if revmap:
    w("The dump file's revision numbers have gaps or start late, so the loaded repository numbers them "
      "differently. Notes and this report use the loaded numbers; the note of each affected commit "
      "has an `SVN-Original-Revision: rN` line.\n")
    for k, v in sorted(revmap.items(), key=lambda kv: int(kv[0]))[:50]:
        w(f"- r{k} was r{v} in the dump")
    if len(revmap) > 50:
        w(f"- ... {len(revmap) - 50} more in `meta/revmap.json`")
else:
    w("- none")
if any(r.endswith("/svn-outside-layout") for r in refs):
    w("\n### Paths outside trunk/branches/tags\n\nKept on branch `svn-outside-layout`.")
lfs = load("lfs.log", "")
w("\n### Large files moved to Git LFS\n")
m = re.findall(r"^\s*(\*\.?\S*|\S+)\s+(\d[\d.]* [KMG]?B)\s+(\d+/\d+ files?)", lfs, re.M)
items = [f"- `{a}` {b} ({c})" for a, b, c in m]
lfs_paths = [l for l in load("lfs-paths.txt", "").splitlines() if l.strip()]
store = os.path.join(git("rev-parse", "--absolute-git-dir").strip() or repo, "lfs", "objects")
sizes = [os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(store) for f in fs]
if lfs_paths or sizes:
    items += [f"- {len(lfs_paths)} paths are LFS pointers in at least one commit (`lfs-paths.txt`)",
              f"- LFS store: {len(sizes)} objects, {human(sum(sizes))} (counts toward GitHub LFS storage and bandwidth)"]
w("\n".join(items) or ("- see lfs.log" if lfs.strip() else "- none needed"))
EOLCAT = "info: SVN files stored with line endings that differ from svn:eol-style"
eolx = problems.get(EOLCAT) or next((v for k, v in problems.items() if "line endings" in k), [])
w(f"\n### Files stored with line endings that differ from svn:eol-style ({len(eolx)})\n")
w("SVN converts these on checkout; git keeps the bytes SVN stored. Verification accepted them because only "
  "the line endings differ.\n")
w("\n".join(f"- {code(x)}" for x in eolx[:50]) or "- none")
if len(eolx) > 50:
    w(f"- ... {len(eolx) - 50} more in `verify.json`")
for cat, v in problems.items():
    if "empty dirs" in cat:
        w(f"\n### Empty directories\n\n{cat}: {len(v)}")
w("\nEmpty SVN directories are kept with an empty `.gitignore` placeholder (`--empty-dirs`); "
  "`svn:ignore` is converted to `.gitignore` throughout history (`--svn-ignore`).")

# ---- suggested .gitattributes from svn:eol-style / svn:mime-type on main ---------------------
# fixed CRLF/CR styles are stored with those endings: '-text' keeps them, so clones stay clean (F34)
EOL = {"native": "text", "LF": "text eol=lf", "CRLF": "-text", "CR": "-text"}
attrs = {}
for p, v in mprops.items():
    a = EOL.get(v.get("svn:eol-style", "").strip())
    if "svn:mime-type" in v and not v["svn:mime-type"].startswith("text/"):
        a = "binary"
    if a:
        attrs[p] = a
tree = [p for p in git("ls-tree", "-r", "-z", "--name-only", "--full-tree", MAIN).split("\0") if p]
# 'text' would show files stored with CR in SVN as modified in every clone: keep them as stored
cr = {p.split(":", 1)[1] for p in git("grep", "-a", "-l", "-z", "-e", "\r", MAIN, "--").split("\0") if ":" in p}
kept = sorted(p for p, a in attrs.items() if a.startswith("text") and p in cr)
for p in kept:
    attrs[p] = "-text"
all_ext = defaultdict(int)
for p in tree:
    all_ext[os.path.splitext(p)[1]] += 1
by_ext = defaultdict(set)
for p, a in attrs.items():
    by_ext[os.path.splitext(p)[1]].add(a)

def pattern(p):   # .gitattributes pattern matching exactly the literal path p
    s = re.sub(r"([\\*?\[])", r"\\\1", p)
    s = "\\" + s if s[:1] in ("#", "!") else s
    return s.replace(" ", "[[:space:]]")

lines = [f"# Generated from svn:eol-style / svn:mime-type {ON_MAIN} at r{MREV}. Review before committing."]
done = set()
for e, s in sorted(by_ext.items()):
    n = sum(1 for p in attrs if os.path.splitext(p)[1] == e)
    if e and len(s) == 1 and n == all_ext[e]:
        lines.append(f"*{pattern(e)} {next(iter(s))}"); done |= {p for p in attrs if os.path.splitext(p)[1] == e}
for p, a in sorted(attrs.items()):
    if p not in done:
        lines.append(f"{pattern('/' + p)} {a}")
open(os.path.join(out, "suggested.gitattributes"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
w(f"\n### Line endings / binary flags\n\n{len(attrs)} files had `svn:eol-style` or a binary `svn:mime-type`. "
  "Git does not enforce these by itself; review and commit `suggested.gitattributes` as `.gitattributes`. "
  "Files with `svn:eol-style` CRLF or CR are marked `-text`: git keeps their line endings as stored.")
if kept:
    w(f"\n{len(kept)} files with `svn:eol-style` native/LF are stored with CR line endings in SVN; they are "
      "marked `-text` as well (`text` would show them as modified in every clone):\n")
    w("\n".join(f"- {code(p)}" for p in kept[:50]))
if mprops_err:
    w(f"\nProperties could not be read, so no rules were generated: {code(mprops_err)}")

w("\n## Cannot be migrated (by design of git)\n")
w("- **Path-based read permissions (SVN authz)**: anyone who can read a GitHub repo reads all of it. "
  "Read-restricted paths must become separate repositories. Write restrictions map to GitHub rulesets / CODEOWNERS.")
w("- **Revision numbers**: git commits get new IDs; the SVN revision is in each commit's note.")
w("- **One SVN commit touching several branches** becomes one git commit per branch.")
open(os.path.join(out, "MIGRATION_REPORT.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
print(f"wrote {out}/MIGRATION_REPORT.md and {out}/suggested.gitattributes")
