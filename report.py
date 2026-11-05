#!/usr/bin/env python3
"""Write MIGRATION_REPORT.md + suggested.gitattributes: everything git cannot carry
over automatically, listed by name so nothing is lost silently.

usage: report.py SVN_URL OUT_DIR GIT_REPO [PROJECT_PREFIX]
  reads OUT_DIR/meta/log.json, OUT_DIR/renames.txt, OUT_DIR/merges.json,
        OUT_DIR/verify.json, OUT_DIR/verify.log, OUT_DIR/lfs.log (when present)
"""
import json, os, re, subprocess, sys
import xml.etree.ElementTree as ET
from collections import defaultdict

url, out, repo = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
project = sys.argv[4] if len(sys.argv) > 4 else "/"
TRUNK = "" if project == "NONE" else ("trunk" if project == "/" else project.strip("/") + "/trunk")
env = dict(os.environ, LC_ALL="C.UTF-8")

def svn_props(name, path):
    try:
        x = subprocess.run(["svn", "propget", "-R", "--xml", name, f"{url}/{path}"],
                           capture_output=True, check=True, env=env).stdout
    except subprocess.CalledProcessError:
        return {}
    pre = f"{url}/{path}".rstrip("/") + "/"
    return {t.get("path").replace(pre, "", 1) if t.get("path").startswith(pre) else "": (t.findtext("property") or "")
            for t in ET.fromstring(x).findall("target")}

def load(name, default):
    p = os.path.join(out, name)
    if not os.path.exists(p):
        return default
    return json.load(open(p, encoding="utf-8")) if name.endswith(".json") else open(p, encoding="utf-8", errors="replace").read()

revs = load("meta/log.json", {})
verify = load("verify.json", {"problems": {}, "fatal": []})
merges = load("merges.json", {"merges_complete": [], "merges_partial": []})
L = []
w = L.append

def git(*a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout

refs = git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags", "refs/notes").split()
w("# SVN → Git migration report\n")
verdict = "PASS" if not verify.get("fatal") else "FAIL"
w(f"**Verification: {verdict}**" + ("" if verdict == "PASS" else f" — failing checks: {', '.join(verify['fatal'])}") + "\n")
w("| | |\n|---|---|")
w(f"| SVN revisions | {len(revs)} |")
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
ext = svn_props("svn:externals", "")
w(f"### svn:externals ({len(ext)}) — no git equivalent\n")
w("Re-create as git submodules, subtrees, or a package dependency. Definitions at SVN HEAD:\n")
for p, v in sorted(ext.items()):
    for d in v.strip().splitlines():
        w(f"- `/{p}`: `{d.strip()}`")
if not ext:
    w("- none")
kw = svn_props("svn:keywords", TRUNK)
w(f"\n### svn:keywords ({len(kw)} files on trunk) — not expanded by git\n")
w("Files contain literal `$Id$`/`$Revision$` text. Usually fine; otherwise drop the keywords or use a "
  "`filter` in `.gitattributes`.\n")
for p, v in sorted(kw.items())[:50]:
    w(f"- `{p}`: {v}")
partial = merges["merges_partial"]
w(f"\n### Partial merges / cherry-picks ({len(partial)}) — git cannot represent\n")
w("Content is correct; git just does not record them as merges. The SVN merge record stays in the notes.\n")
for m in partial[:100]:
    w(f"- r{m['rev']}: {m['from']} → {m['into']} (unmerged source revisions e.g. {m['unmerged_revs'][:5]})")
if not partial:
    w("- none")

# ---- pre-flight: paths that break checkouts on Windows / macOS ---------------------
WIN_BAD = re.compile(r'(^|/)((con|prn|aux|nul|com[1-9]|lpt[1-9])(\.[^/]*)?|[^/]*[<>:"|?*\\\x00-\x1f][^/]*|[^/]*[. ])(/|$)', re.I)
case_hits, win_hits, long_hits = [], [], []
for ref in [r for r in refs if r.startswith("refs/heads/")]:
    paths = git("ls-tree", "-r", "--name-only", "-z", ref).split("\0")
    low = defaultdict(list)
    for p in paths:
        if p:
            low[p.lower()].append(p)
            if WIN_BAD.search(p):
                win_hits.append(f"{ref[11:]}: `{p}`")
            if len(p) > 200:
                long_hits.append(f"{ref[11:]}: {len(p)} chars `{p[:80]}…`")
    case_hits += [f"{ref[11:]}: " + ", ".join(f"`{x}`" for x in v) for v in low.values() if len(v) > 1]
w(f"\n### Paths that break checkouts on Windows/macOS ({len(case_hits) + len(win_hits) + len(long_hits)})\n")
w("Git stores these fine, but a clone on a case-insensitive (macOS/Windows) or Windows file system "
  "will fail or silently lose files. Rename them in SVN before the final run, or on main after.\n")
for h in (case_hits[:50] + win_hits[:50] + long_hits[:20]):
    w(f"- {h}")
if not (case_hits or win_hits or long_hits):
    w("- none")

w("\n## Done automatically — for your information\n")
ren = load("renames.txt", "").strip()
w("### Branch/tag names changed (illegal in git)\n")
w("\n".join(f"- `{l.strip()}`" for l in ren.splitlines() if "->" in l) or "- none")
dele = [r for r in refs if "/svn-deleted/" in r]
w(f"\n### Deleted SVN branches/tags kept ({len(dele)})\n")
w("Their full history is preserved under `svn-deleted/<name>@<last revision>`.\n")
for r in dele[:200]:
    w(f"- `{r}`")
if any(r.endswith("/svn-outside-layout") for r in refs):
    w("\n### Paths outside trunk/branches/tags\n\nKept on branch `svn-outside-layout`.")
lfs = load("lfs.log", "")
w("\n### Large files moved to Git LFS\n")
m = re.findall(r"^\s*(\*\.?\S*|\S+)\s+(\d[\d.]* [KMG]?B)\s+(\d+/\d+ files?)", lfs, re.M)
w("\n".join(f"- `{a}` {b} ({c})" for a, b, c in m) or ("- see lfs.log" if lfs.strip() else "- none needed"))
for cat, items in verify.get("problems", {}).items():
    if "empty dirs" in cat:
        w(f"\n### Empty directories\n\n{cat}: {len(items)}")
w("\nEmpty SVN directories are kept with an empty `.gitignore` placeholder (`--empty-dirs`); "
  "`svn:ignore` is converted to `.gitignore` throughout history (`--svn-ignore`).")

# ---- suggested .gitattributes from svn:eol-style / svn:mime-type at trunk HEAD -------
eol, mime = svn_props("svn:eol-style", TRUNK), svn_props("svn:mime-type", TRUNK)
attrs = {}
for p, v in eol.items():
    attrs[p] = {"native": "text", "LF": "text eol=lf", "CRLF": "text eol=crlf", "CR": "text"}.get(v.strip(), "text")
for p, v in mime.items():
    if not v.startswith("text/"):
        attrs[p] = "binary"
by_ext = defaultdict(set)
all_ext = defaultdict(int)
for p in subprocess.run(["svn", "ls", "-R", f"{url}/{TRUNK}".rstrip("/")], capture_output=True, text=True, env=env).stdout.splitlines():
    if not p.endswith("/"):
        all_ext[os.path.splitext(p)[1]] += 1
for p, a in attrs.items():
    by_ext[os.path.splitext(p)[1]].add(a)
lines = ["# Generated from svn:eol-style / svn:mime-type on trunk at SVN HEAD. Review before committing."]
done = set()
for e, s in sorted(by_ext.items()):
    n = sum(1 for p in attrs if os.path.splitext(p)[1] == e)
    if e and len(s) == 1 and n == all_ext[e]:
        lines.append(f"*{e} {next(iter(s))}"); done |= {p for p in attrs if os.path.splitext(p)[1] == e}
for p, a in sorted(attrs.items()):
    if p not in done:
        lines.append(f"/{p.replace(' ', '[[:space:]]')} {a}")
open(os.path.join(out, "suggested.gitattributes"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
w(f"\n### Line endings / binary flags\n\n{len(attrs)} files had `svn:eol-style` or a binary `svn:mime-type`. "
  "Git does not enforce these by itself; review and commit `suggested.gitattributes` as `.gitattributes`.")

w("\n## Cannot be migrated (by design of git)\n")
w("- **Path-based read permissions (SVN authz)**: anyone who can read a GitHub repo reads all of it. "
  "Read-restricted paths must become separate repositories. Write restrictions map to GitHub rulesets / CODEOWNERS.")
w("- **Revision numbers**: git commits get new IDs; the SVN revision is in each commit's note.")
w("- **One SVN commit touching several branches** becomes one git commit per branch.")
open(os.path.join(out, "MIGRATION_REPORT.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
print(f"wrote {out}/MIGRATION_REPORT.md and {out}/suggested.gitattributes")
