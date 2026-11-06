#!/usr/bin/env bash
# Layout regression tests: multi-project repository and non-standard layout.
# usage: layout_tests.sh [WORKDIR]
set -euo pipefail
export LC_ALL=C.UTF-8
HERE=$(cd "$(dirname "$0")" && pwd)
W=${1:-$(mktemp -d)}; mkdir -p "$W"; W=$(realpath "$W"); cd "$W"
echo "workdir: $W"
c() { svn commit -q --username "$1" -m "$2"; }

# ---------- 1. two projects in one repository ------------------------------------
svnadmin create multi; U=file://$W/multi
svn mkdir -q --parents --username alice -m "Create two projects" \
  $U/projA/trunk $U/projA/branches $U/projA/tags $U/projB/trunk $U/projB/branches $U/projB/tags
svn co -q $U wc-multi && cd wc-multi
printf 'a\n' > projA/trunk/a.txt; printf 'b\n' > projB/trunk/b.txt; svn add -q projA/trunk/a.txt projB/trunk/b.txt
c alice "Initial files in both projects (one revision, two projects)"
printf 'x\r\n' > projA/trunk/win.txt; svn add -q projA/trunk/win.txt; svn ps -q svn:eol-style CRLF projA/trunk/win.txt
printf 'readme\n' > projA/trunk/README; printf 'other\n' > projA/trunk/readme; printf 'aux\n' > projA/trunk/aux.h
svn add -q projA/trunk/README projA/trunk/readme projA/trunk/aux.h
c bob "Files that break Windows/macOS checkouts + eol-style"
svn up -q; svn cp -q projA/trunk projA/branches/dev; c bob "Branch projA dev"
printf 'dev work\n' >> projA/branches/dev/a.txt; c bob "Work on projA dev"
printf 'b2\n' >> projB/trunk/b.txt; c carol "projB change"
svn up -q; (cd projA/trunk && svn merge -q ^/projA/branches/dev .); c alice "Merge dev into projA trunk"
svn up -q; svn cp -q projB/trunk projB/tags/1.0; c carol "Tag projB 1.0"
cd "$W"
printf 'alice = Alice A <alice@test.invalid>\nbob = Bob B <bob@test.invalid>\ncarol = Carol C <carol@test.invalid>\n' > authors.txt
mkdir -p remotes
"$HERE/svn2github.sh" "$W/multi" "$W/out-multi" --authors authors.txt --apply-gitattributes \
  --push-remote "$W/remotes/{name}.git"
for p in projA projB; do
  test -d "remotes/$p.git" || { echo "missing remote $p"; exit 1; }
done
git -C remotes/projA.git cat-file -e main:.gitattributes || { echo ".gitattributes not applied"; exit 1; }
# svn:eol-style CRLF maps to -text; a fresh clone must not show the CRLF file as modified
# (a new index forces a content comparison)
rm -rf clone-projA; git clone -q remotes/projA.git clone-projA
a=$(git -C clone-projA check-attr text -- win.txt)
[ "$a" = "win.txt: text: unset" ] || { echo "svn:eol-style CRLF not mapped to -text: $a"; exit 1; }
rm -f clone-projA/.git/index; git -C clone-projA reset -q > /dev/null 2>&1 || true
[ -z "$(git -C clone-projA status --porcelain)" ] ||
  { echo "fresh clone of projA is not clean:"; git -C clone-projA status --porcelain; exit 1; }
[ "$(git -C remotes/projA.git rev-list --merges main | wc -l)" = 1 ] || { echo "projA merge missing"; exit 1; }
git -C remotes/projB.git rev-parse -q --verify refs/tags/1.0 >/dev/null || { echo "projB tag missing"; exit 1; }
git -C remotes/projA.git ls-tree -r --name-only main | grep -q '^b.txt$' && { echo "projB file leaked into projA"; exit 1; }
grep -q 'README' out-multi/projA/MIGRATION_REPORT.md || { echo "case conflict not reported"; exit 1; }
grep -q 'aux.h' out-multi/projA/MIGRATION_REPORT.md || { echo "windows name not reported"; exit 1; }

# ---------- 2. non-standard layout (no trunk) --------------------------------------
svnadmin create flat; U=file://$W/flat
svn co -q $U wc-flat && cd wc-flat
mkdir -p src docs; printf 'x\n' > src/x.c; printf 'd\n' > docs/d.md; svn add -q src docs; c alice "Flat import"
printf 'y\n' >> src/x.c; c bob "Edit"
svn mv -q docs documentation; c carol "Rename folder"
cd "$W"
"$HERE/svn2github.sh" "$W/flat" "$W/out-flat" --authors authors.txt --push-remote "$W/remotes/{name}.git"
git -C remotes/flat.git ls-tree -r --name-only main | grep -q '^documentation/d.md$' || { echo "flat layout wrong"; exit 1; }
echo "LAYOUT TESTS PASSED"
