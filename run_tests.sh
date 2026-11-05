#!/usr/bin/env bash
# Regression test: build a synthetic SVN repository full of migration edge cases,
# migrate it end to end (including a push to a local remote) and require a PASS.
#
# usage: run_tests.sh [REVISIONS, default 1500] [WORKDIR]
set -euo pipefail
export LC_ALL=C.UTF-8
HERE=$(cd "$(dirname "$0")" && pwd)
N=${1:-1500}
W=${2:-$(mktemp -d)}
mkdir -p "$W"; W=$(realpath "$W"); cd "$W"   # absolute: used in file:// URLs below
echo "workdir: $W"
python3 -I "$HERE/gen_dump.py" test.dump "$N" --big
svnadmin create svnrepo && svnadmin load -q svnrepo < test.dump
svn log -q "file://$W/svnrepo" | awk -F'|' '/^r/{gsub(/^ +| +$/,"",$2); print $2}' | sort -u |
  while IFS= read -r a; do
    [ "$a" = "(no author)" ] && continue
    l=${a##*\\}; printf '%s = %s Tester <%s@test.invalid>\n' "$a" "$l" "$l"
  done > authors.txt
echo "(no author) = Nobody Known <unknown@test.invalid>" >> authors.txt
git init -q --bare remote.git
"$HERE/svn2github.sh" "$W/svnrepo" "$W/out" --authors authors.txt --push-remote "$W/remote.git"
# the pushed copy must be a complete, LFS-resolvable clone
git clone -q "$W/remote.git" "$W/clone"
(cd "$W/clone" && git lfs pull -q && git lfs fsck >/dev/null && git fsck -q --no-dangling)
[ "$(git -C "$W/remote.git" for-each-ref | wc -l)" = "$(git -C "$W/out/project" for-each-ref refs/heads refs/tags refs/notes | wc -l)" ]
echo "TEST PASSED ($N revisions)"
