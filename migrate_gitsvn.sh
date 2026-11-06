#!/usr/bin/env bash
# SVN -> Git with git svn (works against a remote URL, slower; no LFS, no svn-outside-layout
# content). The result OUTDIR/repo.git is verified against SVN and described in
# OUTDIR/MIGRATION_REPORT.md, like svn2github.sh does.
#
# usage: migrate_gitsvn.sh SVN_URL OUTDIR [--authors FILE] [--samples N|all]
#   SVN_URL    repository root or project folder that holds trunk/branches/tags
#              (a local repository directory works too)
#   --authors  "login = Full Name <email>" per SVN user, checked like svn2github.sh does;
#              without it the commits get PLACEHOLDER identities
#   --samples  historical commits verify.py compares with SVN (default all)
# Re-running resumes: OUTDIR/work is updated with `git svn fetch`, then main, the branches and
# the tags are rebuilt from it. Exit status 1 when verification fails.
set -euo pipefail
case $(uname -s) in MINGW*|MSYS*|CYGWIN*) echo "ERROR: Windows is not supported: run under Linux or WSL2"; exit 1;; esac
(( BASH_VERSINFO[0] * 100 + BASH_VERSINFO[1] >= 404 )) || { echo "ERROR: bash 4.4 or newer is required"; exit 1; }
for t in git svn python3; do command -v $t >/dev/null || { echo "ERROR: missing tool: $t"; exit 1; }; done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 7))' 2>/dev/null || { echo "ERROR: python3 3.7 or newer is required"; exit 1; }
git svn --version >/dev/null 2>&1 || { echo "ERROR: git svn does not work: install git-svn and the Subversion Perl bindings"; exit 1; }
L=$(locale -a 2>/dev/null || true)
loc=$(grep -ixE 'c\.utf-?8' <<< "$L" | head -1 || true)
[ -n "$loc" ] || loc=$(grep -ixE 'en_us\.utf-?8' <<< "$L" | head -1 || true)
[ -n "$loc" ] || loc=$(grep -iE '\.utf-?8$' <<< "$L" | head -1 || true)
[ -n "$loc" ] || { echo "ERROR: no UTF-8 locale is installed (locale -a); install one, e.g. C.UTF-8"; exit 1; }
export LC_ALL=$loc
HERE=$(cd "$(dirname "$0")" && pwd)
[ $# -ge 2 ] || { sed -n '6,13p' "$0"; exit 2; }
URL=$1; OUT=$2; shift 2
AUTHORS=""; SAMPLES=all
while [ $# -gt 0 ]; do
  case $1 in
    --authors) [ -f "${2:-}" ] || { echo "ERROR: --authors file not found: ${2:-}"; exit 2; }; AUTHORS=$(realpath "$2"); shift 2;;
    --samples) SAMPLES=${2:?--samples needs N or all}; shift 2;;
    *) echo "unknown option $1"; exit 2;;
  esac
done
if [ -f "$URL/format" ] && [ -d "$URL/db" ]; then URL="file://$(realpath "$URL")"; fi
mkdir -p "$OUT"; cd "$OUT"; OUT=$(pwd)

# pin the revision: everything below (log, clone/fetch, verify) describes r$REV
URL=$(svn info --non-interactive --show-item url "$URL@HEAD") || { echo "ERROR: cannot read $URL with svn"; exit 1; }
ROOT=$(svn info --non-interactive --show-item repos-root-url "$URL@HEAD")
REV=$(svn info --non-interactive --show-item revision "$URL@HEAD")
PID=$(python3 -I -c 'import sys, urllib.parse as u; print(u.unquote(sys.argv[1][len(sys.argv[2]):]) or "/")' "$URL" "$ROOT")
echo "SVN: $URL at r$REV (project $PID of $ROOT)"
python3 -I "$HERE/svn_meta.py" "$URL" meta --rev "$REV" || exit 1
if [ -n "$AUTHORS" ]; then
  python3 -I "$HERE/authors_check.py" "$AUTHORS" meta || exit 1
  grep -q '^(no author) = ' meta/authors.txt || printf '(no author) = %s\n' "$(cat meta/noauthor)" >> meta/authors.txt
else
  echo "WARN: no --authors file; commits get PLACEHOLDER identities (login@example.com). Do not push this result;"
  echo "      for real identities delete $OUT/work and run again with --authors FILE."
fi
# git svn writes each identity once: commits already in work/ keep the ones they were converted with
if [ -f work/.git/svn2github-authors.txt ] && grep -qvxF -f meta/authors.txt work/.git/svn2github-authors.txt; then
  echo "ERROR: author identities changed since $OUT/work was converted (e.g. a first run without --authors);"
  echo "       delete $OUT/work and run again to convert all of it with the new identities."
  exit 1
fi
rm -f layout.json
python3 -I "$HERE/make_rules.py" meta/log.json repo --project "$PID" --layout-out layout.json > /dev/null 2> make_rules.log ||
  { cat make_rules.log; echo "ERROR: make_rules.py failed (layout for verification)"; exit 1; }

if [ -d work/.git ]; then
  (cd work && git -c commit.gpgsign=false svn fetch -q -r "BASE:$REV" --authors-file=../meta/authors.txt) > clone.log 2>&1 ||
    { tail -30 clone.log; echo "ERROR: git svn fetch failed (log: $OUT/clone.log); run again to resume"; exit 1; }
else
  rm -rf work
  git -c init.defaultBranch=main -c commit.gpgsign=false svn clone -q -r "0:$REV" --stdlayout --prefix=svn/ \
    --authors-file=meta/authors.txt "$URL" work > clone.log 2>&1 ||
    { tail -30 clone.log; echo "ERROR: git svn clone failed (log: $OUT/clone.log); run again to resume"; exit 1; }
fi
cp meta/authors.txt work/.git/svn2github-authors.txt
cd work
git rev-parse -q --verify refs/remotes/svn/trunk >/dev/null ||
  { echo "ERROR: git svn found no $URL/trunk; this script needs trunk/branches/tags under SVN_URL (use svn2github.sh)"; exit 1; }
# main = trunk on every run (git svn fetch only moves refs/remotes/svn/*), plus the svn:ignore commit
git checkout -q -f --no-track -B main refs/remotes/svn/trunk
if git cat-file -e refs/remotes/svn/trunk:.gitignore 2>/dev/null; then
  echo "WARN: trunk has a versioned .gitignore; svn:ignore properties are not converted"
else
  git svn show-ignore > .gitignore 2>/dev/null || true
  if [ -s .gitignore ]; then
    git add .gitignore
    git -c user.name="SVN Migration" -c user.email=migration@example.com -c commit.gpgsign=false \
      -c core.hooksPath=/dev/null commit -q --no-verify -m "Convert svn:ignore to .gitignore"
  else rm -f .gitignore; fi
fi
# start ref conversion from scratch (idempotent)
{ git for-each-ref --format='delete %(refname)' refs/heads/ refs/tags/ | grep -vx 'delete refs/heads/main' || true; } |
  git update-ref --stdin

# branches and tags from refs/remotes/svn/*: live ones by their verify layout name, deleted ones as
# refs/tags/svn-deleted/[tags/]<name>@<last revision>; tags point at the copied commit unless someone
# committed into the tag afterwards
python3 -I - "$URL" "$REV" "${PID%/}" <<'EOF'
import json, os, re, subprocess, sys, urllib.parse as u
url, rev, base = sys.argv[1:4]

def sanitize(n):   # keep identical in make_rules.py
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

def git(*a, **kw):
    return subprocess.run(["git", *a], check=True, capture_output=True, **kw).stdout.decode("utf-8", "surrogateescape")

def live(sub):
    p = subprocess.run(["svn", "ls", "--non-interactive", f"{url}/{sub}@{rev}"], capture_output=True)
    return {l.rstrip("/") for l in p.stdout.decode("utf-8", "replace").splitlines()} if p.returncode == 0 else set()

layout, life = {}, {}
if os.path.exists("../layout.json"):
    for r in json.load(open("../layout.json", encoding="utf-8"))["roots"]:
        layout[r["kind"], r["path"]] = r["ref"].split("/", 2)[2]
        life[r["kind"], r["path"]] = r.get("lifetimes") or []
LIVE = {"branch": live("branches"), "tag": live("tags")}
used, updates, renames = {"refs/heads/main": "trunk"}, [], []
for ref in git("for-each-ref", "--format=%(refname)", "refs/remotes/svn/").splitlines():
    s = ref[len("refs/remotes/svn/"):]
    if s == "trunk":
        continue
    kind, s = ("tag", s[5:]) if s.startswith("tags/") else ("branch", s)
    sub = "tags" if kind == "tag" else "branches"
    raw = u.unquote(s)                  # git svn escapes illegal ref characters as %XX
    path = f"{base}/{sub}/{raw}"
    name = layout.get((kind, path)) or sanitize(raw)
    if re.search(r"@\d+$", raw) or (raw not in LIVE[kind] and raw.split("/")[0] not in LIVE[kind]):
        if not re.search(r"@\d+$", raw):   # (git svn names an older line of a re-created path name@REV)
            ids = re.findall(r"^git-svn-id: \S+@(\d+) ", git("log", "-1", "--format=%B", ref), re.M)
            last = int(ids[-1]) if ids else 0
            # @<revision that deleted it>, like svn2github.sh; else its last revision
            name += "@" + str(next((d for c, d in life.get((kind, path), []) if d and c <= last < d), last))
        dest = "refs/tags/svn-deleted/" + ("tags/" if kind == "tag" else "") + name
    else:
        dest = ("refs/tags/" if kind == "tag" else "refs/heads/") + name
        if name != raw:
            renames.append(f"{sub}/{raw} -> {name}\n")
    if dest in used:
        sys.exit(f"ERROR: SVN {used[dest]} and {sub}/{raw} would both become git ref {dest}; "
                 "rename one of them in SVN (svn mv) or use svn2github.sh, which renames such refs")
    used[dest] = f"{sub}/{raw}"
    sha = git("rev-parse", ref).strip()
    if kind == "branch":
        updates.append(f"update {dest} {sha}\n")
        continue
    target = sha
    par = subprocess.run(["git", "rev-parse", "-q", "--verify", sha + "^"], capture_output=True, text=True).stdout.strip()
    if par and git("rev-parse", sha + "^{tree}") == git("rev-parse", par + "^{tree}"):
        target = par
    an, ae, ad, msg = git("log", "-1", "--format=%an%x00%ae%x00%aI%x00%B", sha).split("\x00", 3)
    msg = "\n".join(l for l in msg.splitlines() if not l.startswith("git-svn-id: ")).strip() + "\n"
    subprocess.run(["git", "tag", "-a", "--cleanup=verbatim", "-F", "-", dest[len("refs/tags/"):], target],
                   input=msg.encode("utf-8", "surrogateescape"), check=True, capture_output=True,
                   env=dict(os.environ, GIT_COMMITTER_NAME=an, GIT_COMMITTER_EMAIL=ae, GIT_COMMITTER_DATE=ad))
subprocess.run(["git", "update-ref", "--stdin"], input="".join(updates).encode("utf-8", "surrogateescape"), check=True)
open("../renames.txt", "w", encoding="utf-8").write("".join(renames))
print(f"refs: main + {sum(d.startswith('refs/heads/') for d in used) - 1} branches, "
      f"{sum(d.startswith('refs/tags/') and '/svn-deleted/' not in d for d in used)} tags, "
      f"{sum('/svn-deleted/' in d for d in used)} deleted branches/tags kept as refs/tags/svn-deleted/*")
EOF
python3 -I "$HERE/svn_notes.py" . ../meta/log.json || exit 1
cd .. && rm -rf repo.git && git clone -q --bare work repo.git
git -C repo.git fetch -q ../work 'refs/notes/*:refs/notes/*' 'refs/tags/*:refs/tags/*'
n=$(nproc 2>/dev/null || echo 2)
git -C repo.git -c pack.windowMemory=256m -c pack.threads=$(( n < 8 ? n : 8 )) gc -q --aggressive --prune=now
git -C repo.git count-objects -vH | grep size-pack

echo "=== verify against SVN ==="
set +e
python3 -I "$HERE/verify.py" "$ROOT" repo.git meta/log.json --layout layout.json --rev "$REV" --samples "$SAMPLES" \
  ${AUTHORS:+--authors meta/authors.txt} --json verify.json > verify.log 2>&1
VERIFY=$?
set -e
sed -n '/^git commits=/,$p' verify.log
python3 -I "$HERE/report.py" "$ROOT" "$OUT" repo.git "$PID" --rev "$REV" --layout layout.json || exit 1
echo "git repo: $OUT/repo.git   report: $OUT/MIGRATION_REPORT.md"
if [ $VERIFY != 0 ]; then
  echo "ERROR: verification failed (see $OUT/verify.log); do not push this repository. git svn imports only"
  echo "       trunk, branches/* and tags/*: for content elsewhere or nested branches use svn2github.sh."
  exit 1
fi
[ -n "$AUTHORS" ] || echo "WARN: PLACEHOLDER identities (no --authors): do not push this repository"
