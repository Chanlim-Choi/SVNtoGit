#!/usr/bin/env bash
# Push a migrated repository to GitHub: LFS objects first, then refs in the pushes planned by
# push_plan.py (each under GitHub's 2 GB-per-push limit), notes; then checks that every local
# ref arrived with the same commit and that main is the default branch.
#
# usage: push_github.sh GIT_REPO [HOST/]OWNER/NAME [--dry-run] [--resume]
#            gh logged in to HOST (default: $GH_HOST or github.com); creates a private repo if missing
#        push_github.sh GIT_REPO --remote URL [--dry-run] [--resume]
#            any git remote, e.g. for a test (a local path that does not exist becomes a bare repo)
#   --resume   continue an interrupted push into a non-empty remote (refs already there are skipped)
#   PUSH_LIMIT_BYTES (environment) sets push_plan.py --limit (default 1 GiB)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
[ $# -ge 2 ] || { sed -n '6,11p' "$0"; exit 2; }
REPO=$1; shift
TARGET=""; REMOTE=""; DRY=0; RESUME=0; HOST=""
while [ $# -gt 0 ]; do
  case $1 in
    --remote) REMOTE=${2:?--remote needs a URL}; shift 2;;
    --dry-run) DRY=1; shift;;
    --resume) RESUME=1; shift;;
    -*) echo "unknown option $1"; exit 2;;
    *) TARGET=$1; shift;;
  esac
done
if [ -z "$TARGET$REMOTE" ] || { [ -n "$TARGET" ] && [ -n "$REMOTE" ]; }; then
  echo "ERROR: give either [HOST/]OWNER/NAME or --remote URL"; exit 2
fi
AGAIN="$0 $REPO ${TARGET:---remote $REMOTE}"
# a local path remote (no URL scheme, not the scp form host:path) is relative to the caller
if [ -n "$REMOTE" ] && [[ $REMOTE != *://* ]] && [[ $REMOTE != *:* || ${REMOTE%%:*} == */* ]]; then
  REMOTE=$(realpath -m -- "$REMOTE")
fi
cd "$REPO"
run() { if [ $DRY = 1 ]; then echo "DRY-RUN: $*"; else "$@"; fi; }
human() { awk -v b="$1" 'BEGIN { split("B KiB MiB GiB TiB", u, " "); i = 1
  while (b >= 1024 && i < 5) { b /= 1024; i++ }; printf (i == 1 ? "%d %s" : "%.1f %s"), b, u[i] }'; }
svnrev() { git notes --ref=svn show "$1" 2>/dev/null | sed -n 's/^SVN-Revision: \(r[0-9]*\).*/ (SVN \1)/p' | head -1; true; }
failed() {
  echo "ERROR: push failed: $1"
  echo "  GH013 / 'repository rule violations' / 'push protection': a ruleset or secret scanning rejected a"
  echo "    commit of the SVN history; ask an admin for a bypass for this push (or allow the secret via the link)."
  echo "  'workflow' scope: run gh auth refresh -h ${HOST:-github.com} -s workflow"
  [ "$LFS_N" = 0 ] || echo "  LFS quota or file size: see the LFS store size printed above."
  echo "After fixing the cause, continue with: $AGAIN --resume"
  exit 1
}
push() { if [ $DRY = 1 ]; then echo "DRY-RUN: git push github $*"; else git -c push.negotiate=false push -q github "$@" || failed "git push github $*"; fi; }

git rev-parse -q --verify refs/heads/main >/dev/null || { echo "ERROR: $REPO has no branch main"; exit 1; }
# refuse anything GitHub will reject anyway
big=$(git rev-list --objects --branches --tags | git cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' |
      awk '$1=="blob" && $2>100*1024*1024 {print $3" ("int($2/1048576)" MB)"}')
[ -z "$big" ] || { echo "ERROR: files >100 MB in history (run lfs_fix.py first):"; echo "$big"; exit 1; }

# GitHub LFS limits: 2 GiB per file on Free/Pro, 4 GiB on Team, 5 GiB on Enterprise Cloud; 10 GiB storage included on Free/Pro
LFS_N=0; LFSDIR=$(git rev-parse --absolute-git-dir)/lfs/objects
if [ -d "$LFSDIR" ]; then
  read -r LFS_N total max oid < <(find "$LFSDIR" -type f -printf '%s %f\n' |
    awk '{n++; t += $1; if ($1 > m) {m = $1; o = $2}} END {printf "%d %.0f %.0f %s\n", n, t, m, o}')
fi
if [ "$LFS_N" -gt 0 ]; then
  echo "LFS store: $LFS_N objects, $(human "$total") in total, largest $(human "$max")"
  if [ "$max" -gt $((2 << 30)) ]; then
    what=$(git lfs ls-files --all --long 2>/dev/null | awk -v o="$oid" '$1 == o {sub(/^[^ ]+ [-*] /, ""); print; exit}' || true)
    what="LFS file ${what:-with oid $oid}"
    if [ "$max" -gt $((5 << 30)) ]; then
      echo "ERROR: $what is $(human "$max"); GitHub refuses LFS files over 5 GiB on every plan. Remove it from the"
      echo "SVN history (svnadmin dump | svndumpfilter exclude PATH) or keep it outside git, and migrate again."
      exit 1
    fi
    echo "WARN: $what is $(human "$max"); GitHub accepts LFS files over 2 GiB only on Team (4 GiB) and Enterprise Cloud (5 GiB)"
  fi
  if [ "$total" -gt $((10 << 30)) ]; then
    echo "WARN: the LFS objects total $(human "$total"); GitHub Free and Pro include 10 GiB of LFS storage, above that the"
    echo "push fails unless the account has a payment method or budget for Git LFS"
  fi
fi

echo "## push plan"
PLAN=$(python3 -I "$HERE/push_plan.py" . ${PUSH_LIMIT_BYTES:+--limit "$PUSH_LIMIT_BYTES"}) || exit 1
mapfile -t PLAN_LINES <<< "$PLAN"

if [ -n "$TARGET" ]; then
  case $TARGET in
    *//*|/*|*/|*/*/*/*) ;;
    */*/*) HOST=${TARGET%%/*}; NWO=${TARGET#*/};;
    */*) HOST=${GH_HOST:-github.com}; NWO=$TARGET;;
  esac
  [ -n "$HOST" ] || { echo "ERROR: '$TARGET' is neither OWNER/NAME nor HOST/OWNER/NAME (use --remote URL for other remotes)"; exit 2; }
  command -v gh >/dev/null || { echo "ERROR: gh (GitHub CLI) is needed to push to $HOST/$NWO"; exit 1; }
  export GH_HOST=$HOST
  AUTH=$(gh auth status --hostname "$HOST" 2>&1) || {
    printf '%s\n' "$AUTH"; echo "ERROR: gh is not logged in to $HOST: run gh auth login -h $HOST (or set GH_TOKEN / GH_ENTERPRISE_TOKEN)"; exit 1; }
  wf=$(git rev-list -1 --full-history --branches --tags -- .github/workflows)
  if [ -n "$wf" ]; then
    scopes=$(printf '%s\n' "$AUTH" | grep -i 'token scopes' | head -1 || true)
    if [ -z "$scopes" ] || printf '%s' "$scopes" | grep -qi none; then
      echo "WARN: the history has .github/workflows files (commit $wf$(svnrev "$wf")); gh shows no token scopes, so make sure the token may update workflows"
    elif ! printf '%s' "$scopes" | grep -q workflow; then
      echo "ERROR: the history has .github/workflows files (commit $wf$(svnrev "$wf")) and the gh token for $HOST lacks the"
      echo "workflow scope, so GitHub would reject the push. Run: gh auth refresh -h $HOST -s workflow"
      exit 1
    fi
  fi
  CREATED=0
  if ! gh repo view "$NWO" --json name >/dev/null 2>&1; then
    run gh repo create "$NWO" --private --description "Migrated from SVN" || { echo "ERROR: could not create $HOST/$NWO"; exit 1; }
    CREATED=1
  fi
  if [ $DRY = 1 ] && [ $CREATED = 1 ]; then REMOTE="https://$HOST/$NWO.git"
  else REMOTE=$(gh repo view "$NWO" --json url -q .url).git || { echo "ERROR: cannot read the URL of $HOST/$NWO"; exit 1; }; fi
  # let git and git-lfs authenticate with the gh login (repository-local; the empty value
  # clears helpers inherited from global config so a stale stored token is not used)
  case $REMOTE in http://*|https://*)
    CRED=$(printf '%s\n' "$REMOTE" | sed -E 's#^(https?://[^/]+).*#\1#')
    git config --local --unset-all "credential.$CRED.helper" 2>/dev/null || true
    git config --local --add "credential.$CRED.helper" ''
    git config --local --add "credential.$CRED.helper" '!gh auth git-credential';;
  esac
elif [[ $REMOTE == /* ]] && [ ! -e "$REMOTE" ] && [ $DRY = 0 ]; then
  git init -q --bare "$REMOTE"
fi
git remote remove github 2>/dev/null || true
git remote add github "$REMOTE"

# what the remote already has (non-empty only with --resume); refspecs whose result is there are skipped
if [ $DRY = 1 ]; then RREFS=$(GIT_TERMINAL_PROMPT=0 git ls-remote github 2>/dev/null || true)
else RREFS=$(git ls-remote github) || { echo "ERROR: cannot read the remote $REMOTE"; exit 1; }; fi
if [ -n "$RREFS" ] && [ $RESUME = 0 ]; then
  echo "ERROR: $REMOTE already has content; refusing to push into a non-empty repository."
  echo "To continue a push that was interrupted, run again with --resume (refs already there are skipped)."
  exit 1
fi
declare -A RHAVE=()
while read -r sha ref; do
  if [ -n "$ref" ] && [[ $ref != *'^{}' ]]; then RHAVE[$ref]=$sha; fi
done <<< "$RREFS"
done_spec() {   # $1 = refspec; true when the remote already has its result
  local src=${1%%:*} dst=${1#*:} have want
  src=${src#+}; have=${RHAVE[$dst]:-}
  if [ -z "$src" ]; then [ -z "$have" ]; return; fi
  [ -n "$have" ] || return 1
  want=$(git rev-parse -q --verify "$src") || return 1
  [ "$have" = "$want" ] || { [[ $dst == refs/heads/* ]] && git merge-base --is-ancestor "$want" "$have" 2>/dev/null; }
}
record() {
  local src=${1%%:*} dst=${1#*:}
  src=${src#+}
  if [ -z "$src" ]; then unset "RHAVE[$dst]"; else RHAVE[$dst]=$(git rev-parse "$src"); fi
}

if [ "$LFS_N" -gt 0 ]; then
  echo "## LFS objects"
  if [ $DRY = 1 ]; then echo "DRY-RUN: git lfs push --all github"
  else git lfs push --all github || failed "git lfs push --all github"; fi
fi
echo "## branches and tags (${#PLAN_LINES[@]} pushes)"
for line in "${PLAN_LINES[@]}"; do
  read -ra specs <<< "$line"
  todo=()
  for s in "${specs[@]}"; do done_spec "$s" || todo+=("$s"); done
  if [ ${#todo[@]} -gt 0 ]; then
    push "${todo[@]}"
    for s in "${todo[@]}"; do record "$s"; done
  fi
done
for x in "${!RHAVE[@]}"; do   # temporary branches left over by an interrupted earlier push
  if [[ $x == refs/heads/svn-migration-tmp || $x == refs/heads/svn-migration-tmp-* ]] && ! git rev-parse -q --verify "$x" >/dev/null; then
    push ":$x"
  fi
done
if [ -n "$(git for-each-ref refs/notes)" ]; then
  echo "## notes"
  push 'refs/notes/*:refs/notes/*'
fi
if [ $DRY = 1 ]; then echo "dry run finished"; exit 0; fi
if [ -z "$TARGET" ] && [ -d "${REMOTE#file://}" ]; then git --git-dir="${REMOTE#file://}" symbolic-ref HEAD refs/heads/main; fi

echo "## checking remote refs"
diff <(git for-each-ref --format='%(objectname) %(refname)' refs/heads refs/tags refs/notes | sort) \
     <(git ls-remote github 'refs/heads/*' 'refs/tags/*' 'refs/notes/*' | grep -v '\^{}$' | awk '{print $1" "$2}' | sort) ||
  { echo "ERROR: remote refs differ (above: < only local, > only remote); continue with: $AGAIN --resume"; exit 1; }
echo "OK: all $(git for-each-ref refs/heads refs/tags refs/notes | wc -l) refs on remote match"
if [ -n "$TARGET" ]; then
  def=$(gh repo view "$NWO" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || true)
  if [ -z "$def" ]; then
    echo "WARN: could not read the default branch of $HOST/$NWO from gh; make sure it is main (Settings > General)"
  elif [ "$def" != main ]; then
    if gh repo edit "$NWO" --default-branch main >/dev/null 2>&1; then echo "default branch set to main"
    else echo "WARN: the default branch of $HOST/$NWO is '${def:-unknown}', not main, and changing it needs admin rights: change it under Settings > General"; fi
  fi
fi
