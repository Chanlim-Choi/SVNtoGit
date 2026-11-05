#!/usr/bin/env bash
# SVN -> GitHub migration, one command.
#
# usage: svn2github.sh SOURCE OUTDIR [options]
#   SOURCE   local SVN repository directory, an svnadmin dump file, or an svn URL
#            (mirrored with svnrdump, then checked against the server)
# options:
#   --authors FILE          "login = Full Name <email>" per SVN user (required to push;
#                           may contain "(no author) = ..." for revisions without an author)
#   --project PREFIX        migrate one project ("/" = root layout, "/projA", or NONE for a
#                           repository without trunk/branches/tags). Default: every project
#                           found, each into its own git repository OUTDIR/<name>
#   --lfs-above SIZE        move larger files to Git LFS (default 50MB; "off" to skip)
#   --apply-gitattributes   commit the generated .gitattributes (line endings, binaries) on main
#   --push OWNER/NAME       push when verification passes; with several projects use
#                           OWNER/{name} ({name} = project name)
#   --push-remote URL       push to any git remote instead ({name} allowed as above)
#   --samples N|all         historical commits to verify against SVN (default all)
set -euo pipefail
export LC_ALL=C.UTF-8
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$1; OUT=$2; shift 2
AUTHORS=""; LFS_ABOVE=50MB; PUSH=""; PUSH_REMOTE=""; SAMPLES=all; PROJECT=""; GITATTR=0
while [ $# -gt 0 ]; do
  case $1 in
    --authors) AUTHORS=$(realpath "$2"); shift 2;;
    --project) PROJECT=$2; shift 2;;
    --lfs-above) LFS_ABOVE=$2; shift 2;;
    --apply-gitattributes) GITATTR=1; shift;;
    --push) PUSH=$2; shift 2;;
    --push-remote) PUSH_REMOTE=$2; shift 2;;
    --samples) SAMPLES=$2; shift 2;;
    *) echo "unknown option $1"; exit 2;;
  esac
done
for t in svn svnadmin svnrdump svnlook git git-lfs svn-all-fast-export python3 numfmt; do
  command -v $t >/dev/null || { echo "missing tool: $t"; exit 1; }
done
[ -e "$OUT" ] && { echo "ERROR: $OUT exists; use a new output directory"; exit 1; }
mkdir -p "$OUT"; OUT=$(realpath "$OUT"); cd "$OUT"
step() { echo; echo "=== $* ==="; }

step "mirror: local copy of the SVN repository"
if [ -f "$SRC/format" ] && [ -d "$SRC/db" ]; then
  SVNREPO=$(realpath "$SRC")
else
  svnadmin create svn-mirror; SVNREPO=$OUT/svn-mirror
  printf '#!/bin/sh\nexit 0\n' > svn-mirror/hooks/pre-revprop-change; chmod +x svn-mirror/hooks/pre-revprop-change
  if [ -f "$SRC" ]; then svnadmin load -q svn-mirror < "$SRC"
  else
    svnrdump dump -q "$SRC" | svnadmin load -q svn-mirror
    echo "checking mirror against $SRC (full log, revision properties, file listing)"
    cmp -s <(svn log --xml -v --with-all-revprops "$SRC") <(svn log --xml -v --with-all-revprops "file://$SVNREPO") ||
      { echo "ERROR: mirror log differs from the server; aborting"; exit 1; }
    cmp -s <(svn ls -R -v "$SRC") <(svn ls -R -v "file://$SVNREPO") ||
      { echo "ERROR: mirror file listing differs from the server; aborting"; exit 1; }
    echo "mirror identical to server"
  fi
fi
URL="file://$SVNREPO"
echo "SVN: $SVNREPO (r$(svnlook youngest "$SVNREPO"))"

step "metadata: authors, messages, revision properties, file properties"
python3 -I "$HERE/svn_meta.py" "$URL" meta
NOAUTHOR="Unknown <unknown@example.com>"
if [ -n "$AUTHORS" ]; then
  python3 -I - "$AUTHORS" meta <<'EOF'
import sys
amap = {}
for l in open(sys.argv[1], encoding="utf-8"):
    if "=" in l and not l.lstrip().startswith("#"):
        k, v = l.split("=", 1); amap[k.strip()] = v.strip()
need = [l.split(" = ")[0] for l in open(f"{sys.argv[2]}/authors.txt", encoding="utf-8")]
missing = [a for a in need if a not in amap and a != "(no author)"]
if missing:
    sys.exit("ERROR: authors missing from --authors file:\n  " + "\n  ".join(missing))
bad = [f"{k} = {v}" for k, v in amap.items() if not (v.endswith(">") and "<" in v and "@" in v.split("<")[-1])]
if bad:
    sys.exit("ERROR: malformed identities (need 'Full Name <email>'):\n  " + "\n  ".join(bad))
amap.setdefault("(no author)", "Unknown <unknown@example.com>")
with open(f"{sys.argv[2]}/authors.txt", "w", encoding="utf-8") as f:
    for k, v in amap.items(): f.write(f"{k} = {v}\n")
with open(f"{sys.argv[2]}/identity.map", "w", encoding="utf-8") as f:
    for k, v in amap.items():
        if k != "(no author)": f.write(f"{k} {v}\n")
open(f"{sys.argv[2]}/noauthor", "w").write(amap["(no author)"])
EOF
  NOAUTHOR=$(cat meta/noauthor)
  echo "authors: all $(grep -c . meta/authors.txt) SVN identities mapped from $AUTHORS"
else
  echo "WARNING: no --authors file; using PLACEHOLDER identities (login@example.com). Pushing is disabled."
fi

if [ -n "$PROJECT" ]; then PROJECTS=("$PROJECT")
else mapfile -t PROJECTS < <(python3 -I "$HERE/make_rules.py" meta/log.json --detect); fi
echo "projects: ${PROJECTS[*]}"
# name of the source repository: the directory, dump file (without extension) or last URL segment,
# never the internal "svn-mirror" directory used for dumps and URLs
SRCNAME=$(basename "${SRC%/}"); SRCNAME=${SRCNAME%.dump}; SRCNAME=${SRCNAME%.svndump}
pname() { case $1 in /|NONE) echo "$SRCNAME" | sed 's/[^A-Za-z0-9._-]/-/g';; *) echo "${1#/}" | sed 's#/#-#g; s/[^A-Za-z0-9._-]/-/g';; esac; }

migrate_project() {   # $1 = project prefix
  local P=$1 NAME; NAME=$(pname "$P")
  local D="$OUT/$NAME"; [ ${#PROJECTS[@]} -eq 1 ] && D="$OUT"
  mkdir -p "$D"; cd "$D"; [ "$D" = "$OUT" ] || ln -sfn "$OUT/meta" meta
  # NOTE: errexit does not apply inside a function used with ||, so every step checks itself
  local GIT="$D/project"
  step "[$NAME] rules from copy history (project $P)"
  python3 -I "$HERE/make_rules.py" "$OUT/meta/log.json" project --project "$P" > rules.txt 2> renames.txt || return 1
  cat renames.txt

  step "[$NAME] convert (svn-all-fast-export)"
  svn-all-fast-export --identity-map "$OUT/meta/identity.map" --identity-domain example.com \
    --rules rules.txt --add-metadata-notes --svn-ignore --empty-dirs --stats \
    "$SVNREPO" > convert.log 2>&1 || { tail -30 convert.log; return 1; }
  git -C "$GIT" branch -m master main || return 1
  git -C "$GIT" for-each-ref --format='%(refname)' refs/tags/backups/ | while IFS= read -r r; do
    git -C "$GIT" update-ref "refs/tags/svn-deleted/${r#refs/tags/backups/}" "$r"; git -C "$GIT" update-ref -d "$r"
  done

  step "[$NAME] SVN metadata as git notes (refs/notes/svn)"
  python3 -I "$HERE/svn_notes.py" "$GIT" "$OUT/meta/log.json" || return 1

  step "[$NAME] merge commits from svn:mergeinfo"
  TMPDIR=$D python3 -I "$HERE/add_merges.py" "$GIT" "$OUT/meta/log.json" merges.json --no-author "$NOAUTHOR" --svn-url "$URL" || return 1

  step "[$NAME] large files -> Git LFS"
  if [ "$LFS_ABOVE" != off ] && git -C "$GIT" rev-list --objects --branches --tags |
       git -C "$GIT" cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' |
       awk -v lim="$(numfmt --from=iec "${LFS_ABOVE%B}")" '$1=="blob" && $2+0>lim+0 {f=1} END {exit !f}'; then
    python3 -I "$HERE/lfs_fix.py" "$GIT" "$LFS_ABOVE" > lfs.log 2>&1 || { cat lfs.log; return 1; }
    cat lfs.log
  else
    echo "no files above $LFS_ABOVE" | tee lfs.log
  fi
  git -C "$GIT" gc -q --aggressive --prune=now || return 1

  step "[$NAME] verify against SVN"
  set +e
  local LFSARG=()
  [ "$LFS_ABOVE" = off ] || LFSARG=(--lfs-above "$(numfmt --from=iec "${LFS_ABOVE%B}")")
  python3 -I "$HERE/verify.py" "$URL" "$GIT" "$OUT/meta/log.json" --samples "$SAMPLES" --project "$P" \
    --authors "$OUT/meta/authors.txt" ${LFSARG[@]+"${LFSARG[@]}"} --json verify.json > verify.log 2>&1
  local VERIFY=$?
  set -e
  sed -n '/^git commits=/,$p' verify.log

  step "[$NAME] report"
  python3 -I "$HERE/report.py" "$URL" "$D" "$GIT" "$P" || return 1
  if [ $GITATTR = 1 ] && [ $VERIFY = 0 ] && grep -qvE '^(#|[[:space:]]*$)' suggested.gitattributes; then
    local WT; WT=$(mktemp -d -p "$D")
    git -C "$GIT" worktree add -q "$WT" main
    cp suggested.gitattributes "$WT/.gitattributes"
    git -C "$WT" add .gitattributes
    git -C "$WT" -c user.name="SVN Migration" -c user.email=migration@example.com \
      commit -q -m "Add .gitattributes generated from svn:eol-style / svn:mime-type"
    git -C "$GIT" worktree remove --force "$WT"
    echo "committed .gitattributes on main"
  fi

  if [ -n "$PUSH$PUSH_REMOTE" ]; then
    [ $VERIFY = 0 ] || { echo "[$NAME] NOT PUSHING: verification failed (see $D/verify.log)"; return 1; }
    [ -n "$AUTHORS" ] || { echo "[$NAME] NOT PUSHING: real author identities required (--authors)"; return 1; }
    step "[$NAME] push"
    if [ -n "$PUSH_REMOTE" ]; then "$HERE/push_github.sh" "$GIT" --remote "${PUSH_REMOTE//\{name\}/$NAME}" || return 1
    else "$HERE/push_github.sh" "$GIT" "${PUSH//\{name\}/$NAME}" || return 1; fi
  fi
  echo "[$NAME] git repo: $GIT   report: $D/MIGRATION_REPORT.md"
  return $VERIFY
}

if [ ${#PROJECTS[@]} -gt 1 ] && [ -n "$PUSH$PUSH_REMOTE" ] && [[ "$PUSH$PUSH_REMOTE" != *"{name}"* ]]; then
  echo "ERROR: ${#PROJECTS[@]} projects found; use OWNER/{name} (or a URL with {name}) to push each to its own repository"
  exit 2
fi
FAILED=()
for P in "${PROJECTS[@]}"; do
  migrate_project "$P" || FAILED+=("$P")
done
echo
if [ ${#FAILED[@]} -eq 0 ]; then echo "ALL PROJECTS PASSED VERIFICATION (${PROJECTS[*]})"
else echo "FAILED: ${FAILED[*]}"; exit 1; fi
