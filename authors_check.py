#!/usr/bin/env python3
"""Check an --authors file against the SVN logins and write the identity maps.

usage: authors_check.py AUTHORS_FILE META_DIR

AUTHORS_FILE has one "login = Full Name <email>" line per SVN login ("#" comments and
blank lines are skipped; "(no author) = ..." covers revisions without svn:author). It may
be UTF-8 (with or without BOM) or UTF-16 with BOM, with LF or CRLF line endings.
Reads META_DIR/authors.txt (every SVN login, written by svn_meta.py) and writes
  META_DIR/authors.txt    "login = ident"  (git-svn authors file; verify.py reads it)
  META_DIR/identity.map   "login = ident"  (svn-all-fast-export --identity-map)
  META_DIR/noauthor       identity for revisions without an author
Exits 1 with "ERROR: ..." when the file cannot be decoded, a line is malformed, a login or
identity contains "#" (svn-all-fast-export cuts identity-map lines at "#"), or an SVN login
is missing.
"""
import re, sys

src, meta = sys.argv[1], sys.argv[2]
NOAUTHOR = "(no author)"
# one "<", one ">" at the end, an "@" in the address, and a space before "<" after a name
IDENT = re.compile(r"^(?:[^<>]*\s)?<[^<>]*@[^<>]*>$")

def fail(msg, items=()):
    sys.exit("ERROR: " + msg + "".join("\n  " + i for i in items))

need = []                       # every SVN login (svn_meta.py lists them, plus "(no author)")
for line in open(f"{meta}/authors.txt", encoding="utf-8"):
    login = line.rstrip("\r\n").split(" = ", 1)[0]
    if login and login != NOAUTHOR:
        need.append(login)

data = open(src, "rb").read()
if data.startswith((b"\xff\xfe", b"\xfe\xff")):
    enc = "utf-16"
elif b"\x00" in data:
    fail(f"{src} contains NUL bytes (UTF-16 without a byte order mark?); save it as UTF-8")
else:
    enc = "utf-8-sig"          # also strips a UTF-8 byte order mark
try:
    text = data.decode(enc)
except UnicodeDecodeError:
    lines = data.decode(enc, errors="replace").splitlines()
    n = next((i for i, l in enumerate(lines, 1) if "�" in l), len(lines))
    fail(f"{src} line {n} is not valid {'UTF-16' if enc == 'utf-16' else 'UTF-8'}; "
         "save the file as UTF-8", [repr(lines[n - 1]) if lines else ""])

amap, malformed, hashes, twice = {}, [], [], []
for n, line in enumerate(text.splitlines(), 1):
    s = line.strip()
    if not s or s.startswith("#"):
        continue
    sep = " = " if " = " in s else "="       # " = " first: a login may contain "="
    if sep not in s:
        malformed.append(f"line {n}: {s}")
        continue
    k, v = (x.strip() for x in s.split(sep, 1))
    if not k or not IDENT.match(v):
        malformed.append(f"line {n}: {s}")
        continue
    if "#" in v or ("#" in k and k not in need):
        hashes.append(f"line {n}: {s}")
    if k in amap and amap[k] != v:
        twice.append(f"{k}: '{amap[k]}' replaced by '{v}' (line {n})")
    amap[k] = v

svn_hash = [a for a in need if "#" in a]
missing = [a for a in need if a not in amap and "#" not in a]
errors = []
if malformed:
    errors.append(("malformed lines in the authors file (need 'login = Full Name <email>'):", malformed))
if hashes:
    errors.append(("'#' outside a comment in the authors file (svn-all-fast-export cuts "
                   "identity-map lines at '#'); remove it:", hashes))
if svn_hash:
    errors.append(("SVN logins containing '#' cannot be mapped by svn-all-fast-export; change "
                   "svn:author of their revisions in SVN (svn propset --revprop -r N svn:author "
                   "NEWLOGIN URL) and run again:", svn_hash))
if missing:
    errors.append(("authors missing from --authors file:", missing))
if errors:
    for msg, items in errors:
        print("ERROR: " + msg + "".join("\n  " + i for i in items), file=sys.stderr)
    sys.exit(1)
for t in twice:
    print(f"WARN: login listed twice in {src}, the last line wins: {t}")

amap.setdefault(NOAUTHOR, "Unknown <unknown@example.com>")
with open(f"{meta}/authors.txt", "w", encoding="utf-8") as f:
    for k, v in amap.items():
        f.write(f"{k} = {v}\n")
with open(f"{meta}/identity.map", "w", encoding="utf-8") as f:
    for k, v in amap.items():
        if k != NOAUTHOR:
            f.write(f"{k} = {v}\n")
    if "nobody" not in amap:   # placeholder login some svn-all-fast-export builds look up
        f.write(f"nobody = {amap[NOAUTHOR]}\n")
with open(f"{meta}/noauthor", "w", encoding="utf-8") as f:
    f.write(amap[NOAUTHOR])
print(f"authors: all {len(need)} SVN logins mapped from {src}"
      + (" (UTF-16)" if enc == "utf-16" else ""))
