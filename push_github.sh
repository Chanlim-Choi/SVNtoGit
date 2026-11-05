#!/usr/bin/env bash
# Push a migrated repository to GitHub: LFS objects first, then refs in batches
# (stays under GitHub's 2 GB-per-push limit), notes, default branch; then checks
# that every local ref arrived with the same commit.
#
# usage: push_github.sh GIT_REPO OWNER/NAME [--dry-run]          (uses gh; creates a private repo if missing)
#        push_github.sh GIT_REPO --remote URL [--dry-run]       (any git remote, e.g. for a test)
set -euo pipefail
REPO=$1; shift
TARGET=""; REMOTE=""; DRY=0
while [ $# -gt 0 ]; do
  case $1 in
    --remote) REMOTE=$2; shift 2;;
    --dry-run) DRY=1; shift;;
    *) TARGET=$1; shift;;
  esac
done
cd "$REPO"
run() { if [ $DRY = 1 ]; then echo "DRY-RUN: $*"; else "$@"; fi; }

# refuse anything GitHub will reject anyway
big=$(git rev-list --objects --branches --tags | git cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' |
      awk '$1=="blob" && $2>100*1024*1024 {print $3" ("int($2/1048576)" MB)"}')
[ -z "$big" ] || { echo "ERROR: files >100 MB in history (run lfs_fix.py first):"; echo "$big"; exit 1; }

if [ -z "$REMOTE" ]; then
  [ -n "$TARGET" ] || { echo "need OWNER/NAME or --remote URL"; exit 2; }
  gh auth status >/dev/null 2>&1 || { echo "ERROR: gh is not authenticated (gh auth login, or set GH_TOKEN)"; exit 1; }
  if ! gh repo view "$TARGET" >/dev/null 2>&1; then
    run gh repo create "$TARGET" --private --description "Migrated from SVN"
  elif [ "$(gh api "repos/$TARGET" --jq .size)" != "0" ]; then
    echo "ERROR: $TARGET already has content; refusing to push into a non-empty repository"; exit 1
  fi
  REMOTE="https://github.com/$TARGET.git"
fi
# a local path that does not exist yet is created as a bare repository (handy for tests)
if [[ "$REMOTE" != *://* && "$REMOTE" != *@*:* && ! -e "$REMOTE" ]]; then git init -q --bare "$REMOTE"; fi
git remote remove github 2>/dev/null || true
git remote add github "$REMOTE"

if [ -d lfs/objects ] || [ -d .git/lfs/objects ]; then
  echo "## LFS objects"
  run git lfs push --all github
fi

echo "## main (in chunks of 2000 commits for very long histories)"
mapfile -t chain < <(git rev-list --first-parent --reverse main)
for ((i=1999; i<${#chain[@]}-1; i+=2000)); do run git -c push.negotiate=false push -q github "${chain[$i]}:refs/heads/main"; done
run git -c push.negotiate=false push -q github main:refs/heads/main

echo "## branches and tags (batches of 50)"
push_batch() { [ $# -eq 0 ] || run git -c push.negotiate=false push -q github "$@"; }
mapfile -t refs < <(git for-each-ref --format='%(refname)' refs/heads/ refs/tags/ | grep -vx refs/heads/main)
batch=()
for r in "${refs[@]}"; do batch+=("$r:$r"); if [ ${#batch[@]} -ge 50 ]; then push_batch "${batch[@]}"; batch=(); fi; done
push_batch "${batch[@]}"
echo "## notes"
run git -c push.negotiate=false push -q github 'refs/notes/*:refs/notes/*'
if [ -n "$TARGET" ] && [ $DRY = 0 ]; then gh repo edit "$TARGET" --default-branch main >/dev/null; fi
if [ -z "$TARGET" ] && [ $DRY = 0 ] && [ -d "$REMOTE" ]; then git --git-dir="$REMOTE" symbolic-ref HEAD refs/heads/main; fi

[ $DRY = 1 ] && { echo "dry run finished"; exit 0; }
echo "## checking remote refs"
diff <(git for-each-ref --format='%(objectname) %(refname)' refs/heads refs/tags refs/notes | sort) \
     <(git ls-remote github | grep -v -e 'HEAD$' -e '\^{}$' | awk '{print $1" "$2}' | sort) \
  && echo "OK: all $(git for-each-ref refs/heads refs/tags refs/notes | wc -l) refs on remote match" \
  || { echo "ERROR: remote refs differ (see above)"; exit 1; }
