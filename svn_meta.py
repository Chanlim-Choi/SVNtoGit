#!/usr/bin/env python3
"""Extract SVN metadata that Git has no native place for.

usage: svn_meta.py SVN_URL OUT_DIR [--rev N]

Writes (revisions 1..N; default N = HEAD):
  OUT_DIR/authors.txt   - git-svn authors file (every author, incl. "(no author)")
  OUT_DIR/identity.map  - svn-all-fast-export identity map ("login = Name <email>")
  OUT_DIR/log.json      - per-revision: author, date, message, all revprops,
                          changed paths (+ property changes on those paths)
Values are the real SVN values: svn's XML escapes ('?\\NNN' control characters, base64) are
undone. Author/date/message bytes that are not UTF-8 are decoded with U+FFFD replacements;
other property values that are not UTF-8 are stored as "base64:<base64 of the bytes>".
"""
import base64, json, os, re, subprocess, sys
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote

args, REV = sys.argv[1:], None
if "--rev" in args:
    i = args.index("--rev")
    REV = args[i + 1] if i + 1 < len(args) else ""
    del args[i:i + 2]
if len(args) != 2 or (REV is not None and not REV.isdigit()):
    sys.exit("usage: svn_meta.py SVN_URL OUT_DIR [--rev N]")
url, out = args[0].rstrip("/"), args[1]
os.makedirs(out, exist_ok=True)
lc = os.environ.get("LC_ALL", "")
env = dict(os.environ, LC_ALL=lc if re.search("utf-?8", lc, re.I) else "C.UTF-8")

def svn(*a):
    return subprocess.run(["svn", "--non-interactive", *a], check=True, capture_output=True, env=env).stdout

XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
def xml(data):   # svn writes non-UTF-8 author bytes raw into its XML (svn info/ls also control characters)
    return ET.fromstring(XML_BAD.sub("?", data.decode("utf-8", "replace")))

def why(e):
    return " ".join(e.stderr.decode("utf-8", "replace").split()) if hasattr(e, "stderr") else str(e)

def must(*a):
    try:
        return xml(svn(*a))
    except subprocess.CalledProcessError as e:
        sys.exit(f"ERROR: svn {' '.join(a)} failed: {why(e)}")

def value(p, text=False):
    """value of an svn XML <property>; svn base64-encodes values that are not XML-safe"""
    v = p.text or ""
    if p.get("encoding") != "base64":
        return v
    raw = base64.b64decode(v)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace") if text else "base64:" + base64.b64encode(raw).decode()

info = must("info", "--xml", f"{url}@{REV or 'HEAD'}")
repo_root = info.find("entry/repository/root").text
REV = int(REV or info.find("entry").get("revision"))
ROOT = unquote(repo_root)

def svn_path(target):   # URL printed by svn (URI-encoded) -> svn path
    u = unquote(target or "")
    return (u[len(ROOT):] or "/") if u == ROOT or u.startswith(ROOT + "/") else None

def proplists(r, paths):
    """svn path -> its properties after revision r (one svn call per 100 paths)"""
    res = {}
    for i in range(0, len(paths), 100):
        chunk = paths[i:i + 100]
        try:
            x = xml(svn("proplist", "-v", "--xml", *(f"{repo_root}{quote(p)}@{r}" for p in chunk)))
            got = {svn_path(t.get("path")): t for t in x.findall("target")}
            if not set(got) <= set(chunk):
                raise ValueError("unexpected target")
            for p in chunk:   # a path whose properties were all deleted prints no <target>
                res[p] = {q.get("name"): value(q) for q in got[p].findall("property")} if p in got else {}
        except (subprocess.CalledProcessError, ET.ParseError, ValueError):
            for p in chunk:   # one unreadable path fails the whole call: retry one by one
                try:
                    x = xml(svn("proplist", "-v", "--xml", f"{repo_root}{quote(p)}@{r}"))
                    res[p] = {q.get("name"): value(q) for q in x.findall("target/property")}
                except (subprocess.CalledProcessError, ET.ParseError) as e:
                    print(f"WARN: properties of {p} in r{r} not recorded (missing from the notes; merges "
                          f"from its svn:mergeinfo not rebuilt): {why(e)}. Check read access to this path",
                          file=sys.stderr)
    return res

# svn log --xml writes control characters of author and message as '?\NNN' (svn_xml_fuzzy_escape)
# and bytes that are not UTF-8 raw (decoded above as U+FFFD): such values are read again exactly
INEXACT = re.compile(r"\?\\[0-9]{3}|�")
not_utf8 = []
def revprop(r, name):
    p = must("propget", "--revprop", "-r", str(r), "--xml", name, repo_root).find("revprops/property")
    if p is None:
        return None
    if p.get("encoding") == "base64":
        try:
            base64.b64decode(p.text or "").decode("utf-8")
        except UnicodeDecodeError:
            not_utf8.append((r, name))
    return value(p, text=True)

def changed_props(r, items):
    """properties of items (no prop-mods in the log) whose properties differ from before revision r:
    the same path in r-1 (M), the copy source (A/R with copyfrom), nothing (A/R without)"""
    after, before = proplists(r, [i["path"] for i in items]), {}
    groups = {}
    for i in items:
        if i["action"] == "M":
            groups.setdefault(r - 1, []).append((i["path"], i["path"]))
        elif "copyfrom" in i:
            src, _, srev = i["copyfrom"].rpartition("@")
            groups.setdefault(int(srev), []).append((i["path"], src))
    for rr, pairs in groups.items():
        got = proplists(rr, sorted({s for _, s in pairs}))
        before.update({p: got.get(s, {}) for p, s in pairs})
    return {i["path"]: after[i["path"]] for i in items
            if i["path"] in after and after[i["path"]] != before.get(i["path"], {})}

root = must("log", "--xml", "-v", "--with-all-revprops", "-r", f"{REV}:1", f"{url}@{REV}") if REV else ET.Element("log")
revs, no_propmods = {}, []
for e in root.findall("logentry"):
    r = int(e.get("revision"))
    rp = {p.get("name"): value(p) for p in e.findall("revprops/property")}
    paths, need, unknown = [], [], []
    for p in e.findall("paths/path"):
        item = {"path": p.text, "action": p.get("action"), "kind": p.get("kind")}
        if p.get("copyfrom-path"):
            item["copyfrom"] = f'{p.get("copyfrom-path")}@{p.get("copyfrom-rev")}'
        if p.get("action") != "D":
            if p.get("prop-mods") == "true":
                need.append(item)
            elif p.get("prop-mods") is None:   # servers older than 1.7 do not say (F21)
                unknown.append(item)
        paths.append(item)
    got = proplists(r, [p["path"] for p in need]) if need else {}
    if unknown:
        no_propmods.append(r)
        got.update(changed_props(r, unknown))
        need += unknown
    for item in need:
        if item["path"] in got:
            item["props"] = got[item["path"]]
    # svn puts the standard revprops in <author>/<date>/<msg>, custom ones in <revprops>
    author = e.findtext("author", rp.pop("svn:author", None))
    date = e.findtext("date", rp.pop("svn:date", None))
    log = e.findtext("msg", rp.pop("svn:log", ""))
    for k in ("svn:author", "svn:date", "svn:log"):
        rp.pop(k, None)
    if author and INEXACT.search(author):
        author = revprop(r, "svn:author")
    if log and INEXACT.search(log):
        log = revprop(r, "svn:log")
    revs[r] = {"author": author, "date": date, "log": log or "", "revprops": rp, "paths": paths}

json.dump(revs, open(f"{out}/log.json", "w", encoding="utf-8"), ensure_ascii=False, indent=0)
if no_propmods:
    print(f"WARN: the server's log has no prop-mods information ({len(no_propmods)} revisions, Subversion server older "
          f"than 1.7): the properties of every changed path were compared with their previous state instead",
          file=sys.stderr)
if not_utf8:
    bad = [f"r{r} {n}" for r, n in sorted(not_utf8)]
    print(f"WARN: not valid UTF-8 ({len(bad)}): {', '.join(bad[:10])}{' ...' if len(bad) > 10 else ''}. "
          f"log.json and the notes show U+FFFD for those bytes, git gets them raw, and identity.map cannot map "
          f"such an author. Fix the values in SVN (svnadmin setrevprop) for a clean migration", file=sys.stderr)

authors = sorted({v["author"] for v in revs.values() if v["author"]})
def ident(a):
    login = a.split("\\")[-1]
    return f"{login.replace('.', ' ').title()} <{login}@example.com>"
broken = [a for a in authors if "\n" in a or "\r" in a]
for a in broken:
    rs = sorted(r for r, v in revs.items() if v["author"] == a)
    print(f"WARN: SVN author {a!r} (r{', r'.join(map(str, rs[:5]))}{' ...' if len(rs) > 5 else ''}) contains a "
          f"line break and cannot be listed in authors.txt or identity.map: change svn:author of these "
          f"revisions before migrating", file=sys.stderr)
with open(f"{out}/authors.txt", "w", encoding="utf-8") as f:
    for a in authors:
        if a not in broken:   # identity.map cannot hold a login with '#': the converter's fallback applies
            f.write(f"{a} = {ident(a) if '#' not in a else f'{a} <{a}@example.com>'}\n")
    f.write("(no author) = Unknown <unknown@example.com>\n")
with open(f"{out}/identity.map", "w", encoding="utf-8") as f:
    for a in authors:   # git-svn form "login = ident": svn-all-fast-export keeps spaces in the login
        if a not in broken and "#" not in a:   # it cuts lines at '#'
            f.write(f"{a} = {ident(a)}\n")
    # svn-all-fast-export substitutes "nobody" for revisions without svn:author
    if "nobody" not in authors:
        f.write("nobody = Unknown <unknown@example.com>\n")
print(f"revisions={len(revs)} authors={len(authors)} "
      f"with_revprops={sum(1 for v in revs.values() if v['revprops'])} "
      f"noauthor={sum(1 for v in revs.values() if not v['author'])}")
