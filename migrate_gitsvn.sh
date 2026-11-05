#!/usr/bin/env bash
# SVN -> Git with git svn (works against a remote URL, slower).
# usage: migrate_gitsvn.sh SVN_URL OUTDIR
# Re-running resumes: an existing OUTDIR/work is updated with `git svn fetch`.
set -euo pipefail
export LC_ALL=C.UTF-8
HERE=$(cd "$(dirname "$0")" && pwd)
URL=$1; OUT=$2
mkdir -p "$OUT"; cd "$OUT"
python3 -I "$HERE/svn_meta.py" "$URL" meta
if [ -d work/.git ]; then
  (cd work && git svn fetch -q --authors-file=../meta/authors.txt) > clone.log 2>&1 || { tail -30 clone.log; exit 1; }
else
  time git -c init.defaultBranch=main svn clone -q --stdlayout --prefix=svn/ \
    --authors-file=meta/authors.txt "$URL" work > clone.log 2>&1 || { tail -30 clone.log; exit 1; }
fi
cd work
git branch -m "$(git symbolic-ref --short HEAD)" main 2>/dev/null || true
git checkout -q main
if ! git log -1 --format=%s | grep -q '^Convert svn:ignore'; then
  git svn show-ignore > .gitignore 2>/dev/null || true
  if [ -s .gitignore ]; then git add .gitignore; git -c user.name="SVN Migration" -c user.email=migration@example.com commit -qm "Convert svn:ignore to .gitignore"; fi
fi
# start ref conversion from scratch (idempotent)
git for-each-ref --format='%(refname)' refs/heads/ refs/tags/ | grep -vx refs/heads/main | while IFS= read -r r; do git update-ref -d "$r"; done

# git svn escapes illegal ref chars as %XX: decode, then sanitize the same way as make_rules.py
gitname() { python3 -I -c 'import re,sys,urllib.parse as u
s=u.unquote(sys.argv[1]); s=re.sub(r"\.\.+",".",s); s=re.sub(r"[\x00-\x20\x7f~^:?*\[\\]","-",s)
s=s.replace("@{","-")
parts=[c[:-5]+"-lock" if c.endswith(".lock") else c for c in (c.strip(".") for c in s.split("/"))]
s="/".join(c for c in parts if c)
print("-" if s=="@" else (s or "unnamed"))' "$1"; }
LIVE_B=$(svn ls "$URL/branches" | sed 's#/$##'); LIVE_T=$(svn ls "$URL/tags" | sed 's#/$##')
islive() { printf '%s\n' "$2" | grep -qxF -- "$1" || printf '%s\n' "$2" | grep -qxF -- "${1%%/*}"; }

git for-each-ref --format='%(refname:strip=3)' refs/remotes/svn/ | grep -v -e '^tags/' -e '^trunk$' |
while IFS= read -r b; do
  raw=$(python3 -I -c 'import sys,urllib.parse as u;print(u.unquote(sys.argv[1]))' "$b")
  n=$(gitname "$b")
  case "$raw" in *@[0-9]*) n="svn-deleted/$n";; *) islive "$raw" "$LIVE_B" || n="svn-deleted/$n";; esac
  git branch -q -f "$n" "refs/remotes/svn/$b"
done
# tags: tag the copied commit, unless someone committed into the tag afterwards
git for-each-ref --format='%(refname:strip=4)' refs/remotes/svn/tags/ |
while IFS= read -r t; do
  r="refs/remotes/svn/tags/$t"
  raw=$(python3 -I -c 'import sys,urllib.parse as u;print(u.unquote(sys.argv[1]))' "$t")
  n=$(gitname "$t")
  case "$raw" in *@[0-9]*) n="svn-deleted/$n";; *) islive "$raw" "$LIVE_T" || n="svn-deleted/$n";; esac
  target=$r
  if [ "$(git rev-parse "$r^{tree}")" = "$(git rev-parse "$r^^{tree}" 2>/dev/null)" ]; then target="$r^"; fi
  GIT_COMMITTER_DATE="$(git log -1 --format=%aI "$r")" GIT_COMMITTER_NAME="$(git log -1 --format=%an "$r")" \
  GIT_COMMITTER_EMAIL="$(git log -1 --format=%ae "$r")" git tag -f -a "$n" -F <(git log -1 --format=%B "$r") "$target" >/dev/null
done
python3 -I "$HERE/svn_notes.py" . ../meta/log.json
cd .. && rm -rf repo.git && git clone -q --bare work repo.git && cd repo.git
git fetch -q ../work 'refs/notes/*:refs/notes/*' 'refs/tags/*:refs/tags/*'
git gc -q --aggressive --prune=now
git count-objects -vH | grep size-pack
