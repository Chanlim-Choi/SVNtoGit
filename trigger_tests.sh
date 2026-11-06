#!/usr/bin/env bash
# Trigger tests: each confirmed failure scenario (FINDINGS F01-F66) as a tiny SVN repository built with
# svnmucc/svn/svnadmin, migrated with svn2github.sh (or migrate_gitsvn.sh / push_plan.py) into a fresh
# OUT and checked against the behaviour the fix specification promises.
#
# usage: trigger_tests.sh [WORKDIR] [NAME...]
#   NAME     scenario to run: t_f05_deltag, f05_deltag, or a prefix such as f05 (default: all)
#   TT_JOBS  scenarios run at the same time (default 4)
# Prints "PASS t_x" or "FAIL t_x: reason" (plus the tail of its log) per scenario, then
# "TRIGGER TESTS: n passed, m failed"; exit status 1 when a scenario failed.
# Scenarios run with an empty global git configuration (GIT_CONFIG_GLOBAL), so nothing may depend on
# the operator's user.name/user.email or other personal git settings.
set -uo pipefail
export LC_ALL=C.UTF-8
HERE=$(cd "$(dirname "$0")" && pwd)
S2G=$HERE/svn2github.sh

# ---------- helpers (used inside a scenario: $TD is its folder and the current directory) ----------
fail() { printf '%s\n' "$*" > "$TD/.reason"; exit 1; }
on_err() {   # ERR trap: remember the first command that stopped the scenario
  [ "$BASH_SUBSHELL" = "$TT_SUB" ] && [ ! -s "$TD/.reason" ] &&
    printf 'unexpected failure (exit %s) at line %s: %s\n' "$1" "$2" "$3" > "$TD/.reason"
  return 0
}
mk() {   # mk NAME: new repository $TD/NAME with editable revision properties; U = its URL
  svnadmin create "$TD/$1"
  printf '#!/bin/sh\nexit 0\n' > "$TD/$1/hooks/pre-revprop-change"
  chmod +x "$TD/$1/hooks/pre-revprop-change"
  U="file://$TD/$1"
}
mucc() {   # mucc USER MESSAGE ACTION...: one revision in $U
  local u=$1 m=$2; shift 2
  svnmucc -U "$U" --username "$u" -m "$m" "$@" > /dev/null
}
txt() { local f; f=$(mktemp -p "$TD/.files"); printf '%b' "$1" > "$f"; echo "$f"; }   # file with CONTENT
rnd() { local f; f=$(mktemp -p "$TD/.files"); head -c "$1" /dev/urandom > "$f"; echo "$f"; }   # BYTES random
layout() { mucc alice "Create layout" mkdir trunk mkdir branches mkdir tags; }
rlog() {   # rlog REV REGEX: the repository was built as intended (svn log -v of REV matches)
  svn log -v -r "$1" "$U" > "$TD/.rlog" 2>&1 || fail "setup: svn log -r$1 failed"
  grep -qE -- "$2" "$TD/.rlog" || fail "setup: r$1 does not match /$2/: $(tr '\n' ' ' < "$TD/.rlog")"
}
write_authors() {
  printf '%s\n' 'alice = Alice Able <alice@test.invalid>' 'bob = Bob Baker <bob@test.invalid>' \
    'carol = Carol Clark <carol@test.invalid>' '(no author) = Nobody Known <unknown@test.invalid>' > "$TD/authors.txt"
  A=$TD/authors.txt
}
s2g() {   # s2g LOGNAME ARGS...: run svn2github.sh; console in $TD/LOGNAME.log, exit status in RC
  LOG=$TD/$1.log; shift
  echo "$LOG" > "$TD/.log"
  RC=0; "$S2G" "$@" < /dev/null > "$LOG" 2>&1 || RC=$?
}
mgs() {   # mgs LOGNAME ARGS...: run migrate_gitsvn.sh the same way
  LOG=$TD/$1.log; shift
  echo "$LOG" > "$TD/.log"
  RC=0; "$HERE/migrate_gitsvn.sh" "$@" < /dev/null > "$LOG" 2>&1 || RC=$?
}
why() {   # the lines of the last run that explain its outcome, on one line
  local l
  l=$(grep -m4 -A1 -E '^\[FAIL\]|(ERROR|STOP): |NOT PUSHING|^FAILED|^Traceback|^[A-Za-z_.]*(Error|Exception)(:|$)|^(svn[a-z]*|git|fatal|error|numfmt): |cannot continue|Aborted' \
        "$LOG" 2> /dev/null | grep -v -e '^--$' -e '^[[:space:]]*$' | cut -c1-200)
  [ -n "$l" ] || l=$(grep -v '^[[:space:]]*$' "$LOG" 2> /dev/null | tail -n 2 | cut -c1-200)
  printf '%s' "$l" | tr '\n' '\036' | sed 's/\x1e/ | /g'
}
logged() {   # logged REGEX [MESSAGE]: the last run printed a line matching REGEX
  grep -qE -- "$1" "$LOG" || fail "${2:-no output line matches /$1/}; got: $(why)"
}
expect_pass() {   # expect_pass [WHAT]: the last run verified every project
  { [ "$RC" = 0 ] && grep -q 'ALL PROJECTS PASSED VERIFICATION' "$LOG"; } ||
    fail "${1:+$1: }expected ALL PROJECTS PASSED VERIFICATION, got exit $RC: $(why)"
}
expect_stop() {   # expect_stop REGEX: pre-flight refusal whose STOP line matches REGEX, nothing converted
  [ "$RC" != 0 ] || fail "expected a pre-flight 'STOP: .../$1/...' refusal, but the run exited 0"
  grep -qE -- "STOP: .*$1" "$LOG" || fail "expected 'STOP: .../$1/...', got exit $RC: $(why)"
  if grep -q 'ALL PROJECTS PASSED' "$LOG"; then fail "STOP printed but the run still reported success"; fi
}
expect_error() {   # expect_error REGEX: the run failed with an 'ERROR: ...' line matching REGEX
  [ "$RC" != 0 ] || fail "expected 'ERROR: .../$1/...' and a failure, but the run exited 0"
  grep -qE -- "ERROR: .*$1" "$LOG" || fail "expected 'ERROR: .../$1/...', got exit $RC: $(why)"
}
no_repo() {   # no_repo OUT: nothing was converted (a STOP must come before the conversion)
  local p; p=$(find "$1" -maxdepth 2 -name project -type d 2> /dev/null)
  [ -z "$p" ] || fail "a STOP must refuse before converting, but repositories exist: ${p//$TD\//}"
}
prj() {   # prj OUT [NAME]: G = migrated git repository of project NAME, D = its folder
  D=$1
  if [ -n "${2:-}" ] && [ -d "$1/$2/project" ]; then D=$1/$2; fi
  G=$D/project
  [ -d "$G" ] || fail "no migrated repository ${2:+for $2 }in ${1#$TD/}"
}
refs() { git -C "$1" for-each-ref --format='%(refname)' 2> /dev/null; }
need_ref() {   # need_ref REPO REGEX: a ref name matches REGEX
  [ -d "$1" ] || fail "missing repository ${1#$TD/}"
  grep -qE -- "^($2)\$" <<< "$(refs "$1")" || fail "no ref matching /$2/ in ${1#$TD/} (refs: $(refs "$1" | tr '\n' ' '))"
}
no_ref() {
  if grep -qE -- "^($2)\$" <<< "$(refs "$1")"; then fail "unexpected ref matching /$2/ in ${1#$TD/}"; fi
}
paths() { git -C "$1" ls-tree -r --name-only "$2" 2> /dev/null; }
need_file() {   # need_file REPO REV PATH [CONTENT]: PATH exists at REV (with CONTENT; trailing newlines ignored)
  [ -d "$1" ] || fail "missing repository ${1#$TD/}"
  git -C "$1" rev-parse -q --verify "$2^{commit}" > /dev/null ||
    fail "${1#$TD/}: no $2 (refs: $(refs "$1" | tr '\n' ' '))"
  git -C "$1" cat-file -e "$2:$3" 2> /dev/null ||
    fail "${1#$TD/}: $3 missing on $2 (has: $(paths "$1" "$2" | head -20 | tr '\n' ' '))"
  if [ $# -ge 4 ]; then
    local c; c=$(git -C "$1" cat-file blob "$2:$3")
    [ "$c" = "$4" ] || fail "${1#$TD/}: $2:$3 is $(printf %q "$c" | cut -c1-80), expected $(printf %q "$4")"
  fi
}
no_file() {
  if git -C "$1" cat-file -e "$2:$3" 2> /dev/null; then fail "${1#$TD/}: $3 must not exist on $2"; fi
}
is_pointer() {   # is_pointer REPO REV PATH: the blob is a Git LFS pointer
  local s; s=$(git -C "$1" cat-file -s "$2:$3" 2> /dev/null) || return 1
  [ "$s" -lt 1024 ] && [[ $(git -C "$1" cat-file blob "$2:$3") == "version https://git-lfs"* ]]
}
commit_of() {   # commit_of REPO REV: the commit whose refs/notes/svn note names rREV (empty if none)
  python3 -I - "$1" "$2" << 'EOF'
import subprocess, sys
out = subprocess.run(["git", "-C", sys.argv[1], "log", "--branches", "--tags", "--notes=svn",
                      "--format=%H%x00%N%x01"], capture_output=True).stdout.decode("utf-8", "replace")
for rec in out.split("\x01"):
    if "\x00" in rec:
        sha, note = rec.strip("\n").split("\x00", 1)
        if "SVN-Revision: r%s\n" % sys.argv[2] in note + "\n":
            print(sha)
            break
EOF
}
author_of() {   # author_of REPO REV: "Name <email>" of the commit whose svn note names rREV
  local c; c=$(commit_of "$1" "$2")
  [ -n "$c" ] || fail "no commit has an svn note naming r$2"
  git -C "$1" log -1 --format='%an <%ae>' "$c"
}
report_has() {   # report_has DIR REGEX [MESSAGE]: DIR/MIGRATION_REPORT.md matches REGEX
  [ -f "$1/MIGRATION_REPORT.md" ] || fail "no MIGRATION_REPORT.md in ${1#$TD/}"
  grep -qE -- "$2" "$1/MIGRATION_REPORT.md" || fail "${3:-MIGRATION_REPORT.md does not match /$2/} (${1#$TD/}/MIGRATION_REPORT.md)"
}
report_lacks() {
  if grep -qE -- "$2" "$1/MIGRATION_REPORT.md" 2> /dev/null; then fail "${3:-MIGRATION_REPORT.md must not match /$2/}"; fi
}
clone() {   # clone REMOTE DIR: fresh clone, LFS files smudged
  rm -rf "$2"
  git clone -q "$1" "$2" > "$TD/clone.log" 2>&1 || fail "git clone ${1#$TD/} failed: $(tail -3 "$TD/clone.log" | tr '\n' ' ')"
}
clean_status() {   # clean_status DIR [BRANCH]: the checkout shows no change once every file is compared by content
  if [ -n "${2:-}" ]; then
    git -C "$1" checkout -q "$2" > "$TD/checkout.log" 2>&1 || fail "checkout $2 failed: $(tail -2 "$TD/checkout.log" | tr '\n' ' ')"
  fi
  rm -f "$1/.git/index"
  git -C "$1" reset -q > /dev/null 2>&1 || true   # new index without stat data: no racy-clean shortcuts
  local s; s=$(git -C "$1" status --porcelain 2>&1)
  [ -z "$s" ] || fail "git status of a fresh clone${2:+ ($2)} is not clean: $(head -5 <<< "$s" | tr '\n' ' ')"
}
rep_verified() {   # rep_verified DIR: a MIGRATION_REPORT.md under DIR says Verification: PASS
  local f; f=$(find "$1" -maxdepth 2 -name MIGRATION_REPORT.md | head -1)
  [ -n "$f" ] || fail "no MIGRATION_REPORT.md under ${1#$TD/}: verify.py/report.py did not run"
  grep -q 'Verification: PASS' "$f" || fail "${f#$TD/} does not say Verification: PASS ($(grep -m1 Verification "$f"))"
}
multi() {   # two projects, a project-level file, a shared top-level folder and a root file
  mk multi
  mucc alice "Create projects" mkdir projA mkdir projA/trunk mkdir projA/branches mkdir projA/tags \
    mkdir projB mkdir projB/trunk mkdir projB/branches mkdir projB/tags mkdir shared
  mucc alice "projA code, a project-level build file and a root README" put "$(txt 'a\n')" projA/trunk/a.txt \
    put "$(txt '<project/>\n')" projA/build.xml put "$(txt 'readme\n')" README.txt
  mucc bob "projB code and shared code" put "$(txt 'b\n')" projB/trunk/b.txt put "$(txt 'int x;\n')" shared/lib.c
  mucc bob "Branch projA dev" cp HEAD projA/trunk projA/branches/dev
  mucc carol "Change outside the projects only" put "$(txt 'int x = 2;\n')" shared/lib.c \
    put "$(txt 'readme v2\n')" README.txt
  mucc carol "projB notes" mkdir projB/notes put "$(txt 'todo\n')" projB/notes/todo.txt
}

# ---------- scenarios: t_<finding>_<short> ------------------------------------------------------------

t_f00_basic() {   # baseline: standard layout with a branch, a tag and a full merge
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt put "$(txt 'int m;\n')" trunk/m.c
  mucc alice "Branch dev" cp 2 trunk branches/dev
  mucc bob "Dev work" put "$(txt 'dev\n')" branches/dev/d.txt
  svn co -q "$U/trunk" "$TD/wc"
  svn merge -q "$U/branches/dev" "$TD/wc"
  svn ci -q --username carol -m "Merge dev" "$TD/wc"
  mucc carol "Tag 1.0" cp HEAD trunk tags/1.0
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  need_file "$TD/remote.git" main d.txt dev
  need_ref "$TD/remote.git" refs/tags/1.0
  [ "$(git -C "$TD/remote.git" rev-list --merges main | wc -l)" = 1 ] || fail "the merge of dev is not a merge commit on main"
}

t_f01_auto() {   # F01: content outside the detected projects goes to repository <source>-other
  multi
  s2g run "$TD/multi" "$TD/out" --authors "$A" --push-remote "$TD/remotes/{name}.git"
  expect_pass
  logged 'goes to repository multi-other' "no 'content outside the projects (...) goes to repository multi-other' line"
  local O=$TD/remotes/multi-other.git
  need_file "$O" main shared/lib.c 'int x = 2;'
  need_file "$O" main README.txt 'readme v2'
  if paths "$O" main | grep -qE '^proj[AB]/'; then
    fail "multi-other holds files of the migrated projects: $(paths "$O" main | tr '\n' ' ')"
  fi
  need_file "$TD/remotes/projA.git" main a.txt
  need_ref "$TD/remotes/projA.git" refs/heads/dev
  need_file "$TD/remotes/projA.git" svn-outside-layout build.xml
  need_file "$TD/remotes/projB.git" svn-outside-layout notes/todo.txt
}

t_f01_explicit() {   # F01: with --project, outside content is not migrated, but WARNed and reported
  multi
  s2g run "$TD/multi" "$TD/out" --authors "$A" --project /projA --push-remote "$TD/remote.git"
  expect_pass
  logged 'WARN: not migrated \(outside the selected project\): /shared' \
    "no 'WARN: not migrated (outside the selected project): /shared' line"
  if [ -n "$(find "$TD/out" -maxdepth 1 -name '*-other')" ]; then
    fail "an OTHER repository was created although --project was given"
  fi
  need_file "$TD/remote.git" main a.txt
  need_file "$TD/remote.git" svn-outside-layout build.xml
  no_file "$TD/remote.git" svn-outside-layout shared/lib.c
  grep -q 'not migrated' "$TD/out/meta/preflight.txt" 2> /dev/null ||
    fail "meta/preflight.txt is missing or does not hold the 'not migrated' WARN lines"
  prj "$TD/out"
  report_has "$D" '/shared' "the report does not list /shared as not migrated"
}

t_f01_projcopy() {   # F01: a project folder created as a copy of another project: STOP, override continues
  mk repo
  mucc alice "Create projA" mkdir projA mkdir projA/trunk mkdir projA/branches mkdir projA/tags
  mucc alice "Add" put "$(txt 'a\n')" projA/trunk/a.txt
  mucc bob "Copy the whole project" cp 2 projA projC
  mucc bob "Work in the copy" put "$(txt 'c\n')" projC/trunk/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A"
  expect_stop 'project folder copied'
  logged 'Re-run with --allow-split-history'
  no_repo "$TD/out"
  s2g split "$TD/repo" "$TD/out2" --authors "$A" --allow-split-history --push-remote "$TD/remotes/{name}.git"
  if grep -q 'Re-run with --allow-split-history' "$LOG"; then fail "--allow-split-history did not override the STOP"; fi
  expect_pass "--allow-split-history"
  need_file "$TD/remotes/projA.git" main a.txt a
  need_file "$TD/remotes/projC.git" main a.txt c
}

t_f02_rootfiles() {   # F02/F59: files at the root, directly in branches/ and tags/, and a file copied into tags/
  mk repo; layout
  mucc alice "Root README with the first trunk files" put "$(txt 'readme\n')" README.txt \
    mkdir trunk/src put "$(txt 'int main;\n')" trunk/src/main.c
  mucc alice "Branch b1" cp 2 trunk branches/b1
  mucc bob "File directly in tags/" put "$(txt 'tag notes\n')" tags/NOTES.txt
  mucc bob "File directly in branches/" put "$(txt 'branch policy\n')" branches/POLICY.txt
  mucc bob "Tag 1.0" cp 2 trunk tags/1.0
  mucc carol "Edit the root README" put "$(txt 'readme v2\n')" README.txt
  mucc carol "Single-file tag" cp 2 trunk/src/main.c tags/main-1.0.c
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  local R=$TD/remote.git
  need_file "$R" svn-outside-layout README.txt 'readme v2'
  need_file "$R" svn-outside-layout tags/NOTES.txt 'tag notes'
  need_file "$R" svn-outside-layout branches/POLICY.txt 'branch policy'
  need_file "$R" svn-outside-layout tags/main-1.0.c 'int main;'
  need_ref "$R" refs/tags/1.0
  need_file "$R" b1 src/main.c
}

t_f03_later() {   # F03: project and container created by plain mkdir after r1; folder property change
  mk repo
  mucc alice "Create projA" mkdir projA mkdir projA/trunk mkdir projA/branches mkdir projA/tags
  mucc alice "projA code" put "$(txt 'a\n')" projA/trunk/a.txt
  mucc bob "Create projB later" mkdir projB mkdir projB/trunk mkdir projB/branches mkdir projB/tags
  mucc bob "projB code" put "$(txt 'b\n')" projB/trunk/b.txt
  mucc alice "Container for user branches" mkdir projA/branches/users
  mucc alice "User branch" cp 4 projA/trunk projA/branches/users/alice
  mucc carol "Property change on a container only" propset team blue projB/branches
  mucc carol "projA edit" put "$(txt 'a2\n')" projA/trunk/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remotes/{name}.git"
  expect_pass
  need_ref "$TD/remotes/projA.git" refs/heads/users/alice
  need_file "$TD/remotes/projA.git" main a.txt a2
  need_file "$TD/remotes/projB.git" main b.txt b
}

t_f04_trunkmove() {   # F04: trunk moved into a project folder: STOP; --allow-split-history migrates both parts
  mk repo; layout
  mucc alice "Add" put "$(txt 'v1\n')" trunk/a.txt
  mucc alice "Project folder" mkdir projA mkdir projA/branches mkdir projA/tags
  mucc bob "Move trunk into projA" mv trunk projA/trunk
  rlog 4 'A /projA/trunk \(from /trunk:3\)'
  mucc bob "Edit the moved trunk" put "$(txt 'v2\n')" projA/trunk/a.txt
  mucc carol "Branch after the move" cp 5 projA/trunk projA/branches/b1
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remotes/{name}.git"
  expect_stop 'trunk copied'
  logged 'Re-run with --allow-split-history'
  no_repo "$TD/out"
  s2g split "$TD/repo" "$TD/out2" --authors "$A" --allow-split-history --push-remote "$TD/remotes/{name}.git"
  expect_pass "--allow-split-history"
  need_file "$TD/remotes/projA.git" main a.txt v2
  need_ref "$TD/remotes/projA.git" refs/heads/b1
  need_file "$TD/remotes/repo.git" main a.txt v1
}

t_f05_deltag() {   # F05: deleted tag and deleted branch are kept as svn-deleted refs and reported
  mk repo; layout
  mucc alice "Add" put "$(txt 'v1\n')" trunk/a.txt
  mucc alice "Tag 1.0-rc1" cp 2 trunk tags/1.0-rc1
  mucc bob "Edit" put "$(txt 'v2\n')" trunk/a.txt
  mucc bob "Remove the release candidate tag" rm tags/1.0-rc1
  mucc bob "Tag 1.0" cp 4 trunk tags/1.0
  mucc carol "Branch feat" cp 4 trunk branches/feat
  mucc carol "Feature work" put "$(txt 'feat\n')" branches/feat/f.txt
  mucc carol "Delete feat" rm branches/feat
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  local R=$TD/remote.git
  need_ref "$R" 'refs/tags/svn-deleted/tags/1\.0-rc1@[0-9]+'
  need_ref "$R" refs/tags/1.0
  need_ref "$R" 'refs/tags/svn-deleted/feat@[0-9]+'
  prj "$TD/out"
  report_has "$D" '1\.0-rc1' "the report does not list the deleted tag 1.0-rc1"
}

t_f06a_assembled() {   # F06a: tag assembled from several copies plus a file added directly in it
  mk repo; layout
  mucc alice "Code and docs" mkdir trunk/src mkdir trunk/doc put "$(txt 'int main;\n')" trunk/src/main.c \
    put "$(txt 'doc\n')" trunk/doc/readme.txt
  mucc bob "Assemble tag 2.0" mkdir tags/2.0 cp 2 trunk/src tags/2.0/src cp 2 trunk/doc tags/2.0/doc \
    put "$(txt 'notes 2.0\n')" tags/2.0/RELEASE-NOTES.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  local R=$TD/remote.git
  need_file "$R" refs/tags/2.0/src main.c
  need_file "$R" refs/tags/2.0/doc readme.txt
  need_file "$R" svn-outside-layout tags/2.0/RELEASE-NOTES.txt 'notes 2.0'
}

t_f06b_mkdir() {   # F06b: branch created by mkdir and tag created by import (no copy) become refs
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc bob "Branch created by mkdir" mkdir branches/manual
  mucc bob "First file on it" put "$(txt 'm1\n')" branches/manual/m.txt
  mucc bob "Work on it" put "$(txt 'm2\n')" branches/manual/m.txt
  mucc carol "Tag imported, not copied" mkdir tags/1.0-bin put "$(txt 'bin\n')" tags/1.0-bin/app.bin
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  need_file "$TD/remote.git" refs/heads/manual m.txt m2
  need_file "$TD/remote.git" refs/tags/1.0-bin app.bin bin
}

t_f06c_nested() {   # F06c: branch name reused as a container holding a new branch: STOP
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc alice "Branch feature" cp 2 trunk branches/feature
  mucc bob "Feature work" put "$(txt 'f\n')" branches/feature/a.txt
  mucc bob "Delete feature" rm branches/feature
  mucc carol "Re-create feature as a folder holding a new branch" mkdir branches/feature cp 2 trunk branches/feature/login
  s2g run "$TD/repo" "$TD/out" --authors "$A"
  expect_stop 'nested'
  no_repo "$TD/out"
}

t_f07_sandbox() {   # F07: trunk copied into a sandbox: STOP in auto mode; --project / keeps it outside the layout
  mk repo; layout
  mucc alice "Add" put "$(txt 'int a;\n')" trunk/a.c
  mucc bob "Personal sandbox copy of trunk" mkdir sandbox mkdir sandbox/joe cp 2 trunk sandbox/joe/trunk
  mucc bob "Sandbox work" put "$(txt 'int a = 1;\n')" sandbox/joe/trunk/a.c
  mucc carol "Docs outside the layout" mkdir docs put "$(txt 'doc\n')" docs/x.txt
  mucc carol "Branch b1" cp 2 trunk branches/b1
  s2g auto "$TD/repo" "$TD/out" --authors "$A"
  expect_stop 'trunk copied'
  no_repo "$TD/out"
  s2g root "$TD/repo" "$TD/out2" --authors "$A" --project / --push-remote "$TD/remote.git"
  expect_pass "--project /"
  need_file "$TD/remote.git" svn-outside-layout sandbox/joe/trunk/a.c 'int a = 1;'
  need_file "$TD/remote.git" svn-outside-layout docs/x.txt doc
  need_ref "$TD/remote.git" refs/heads/b1
}

t_f08_containers() {   # F08: two-level containers, an emptied container, a container name with a space
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc alice "User branch two levels down" mkdir branches/users mkdir branches/users/alice \
    cp 2 trunk branches/users/alice/feat
  mucc alice "Work on it" put "$(txt 'feat\n')" branches/users/alice/feat/a.txt
  mucc bob "Release branch" mkdir branches/releases cp 2 trunk branches/releases/1.0
  mucc bob "Release branch deleted, container left empty" rm branches/releases/1.0
  mucc carol "Container with a space in its name" mkdir "branches/release 2.x" cp 2 trunk "branches/release 2.x/2.1"
  mucc carol "Grouped tag" mkdir tags/product mkdir tags/product/2020 cp 2 trunk tags/product/2020/v1
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  local R=$TD/remote.git
  need_file "$R" refs/heads/users/alice/feat a.txt feat
  need_ref "$R" refs/heads/release-2.x/2.1
  need_ref "$R" refs/tags/product/2020/v1
  need_ref "$R" 'refs/tags/svn-deleted/releases/1\.0@[0-9]+'
}

t_f09_retired() {   # F09: a project whose trunk is deleted at HEAD
  mk repo
  mucc alice "Create projects" mkdir projA mkdir projA/trunk mkdir projA/branches mkdir projA/tags \
    mkdir projOld mkdir projOld/trunk mkdir projOld/branches mkdir projOld/tags
  mucc alice "Code" put "$(txt 'a\n')" projA/trunk/a.txt put "$(txt 'old v1\n')" projOld/trunk/old.txt
  mucc bob "Last change to projOld" put "$(txt 'old v2\n')" projOld/trunk/old.txt
  mucc bob "Retire projOld" rm projOld/trunk
  mucc carol "projA goes on" put "$(txt 'a2\n')" projA/trunk/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remotes/{name}.git"
  expect_pass
  logged 'WARN: trunk was deleted at r[0-9]+' "no 'WARN: trunk was deleted at rN; main is its last state' line"
  need_file "$TD/remotes/projOld.git" main old.txt 'old v2'
  need_file "$TD/remotes/projA.git" main a.txt a2
}

t_f10_replace() {   # F10: trunk replaced by a branch in one commit; the old line survives as svn-deleted
  mk repo; layout
  mucc alice "Add" put "$(txt 'v1\n')" trunk/app.c
  mucc alice "Branch rewrite" cp 2 trunk branches/rewrite
  mucc bob "Old line change, never merged" put "$(txt 'old line\n')" trunk/app.c
  mucc carol "Rewrite" put "$(txt 'rewrite\n')" branches/rewrite/app.c
  mucc carol "Replace trunk by rewrite" rm trunk cp 5 branches/rewrite trunk rm branches/rewrite
  rlog 6 'R /trunk \(from /branches/rewrite:5\)'
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  local R=$TD/remote.git old
  need_file "$R" main app.c rewrite
  need_ref "$R" 'refs/tags/svn-deleted/(master|main)@[0-9]+'
  old=$(refs "$R" | grep -E '^refs/tags/svn-deleted/(master|main)@' | head -1)
  need_file "$R" "$old" app.c 'old line'
}

t_f11_hashnames() {   # F11: '#' and '${' in branch and tag names
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc alice "Branch with a hash" cp 2 trunk 'branches/bug#123'
  mucc alice "Branch sharing its prefix" cp 2 trunk branches/bugfix
  mucc bob 'Tag with ${x}' cp 2 trunk 'tags/v2.0${x}'
  mucc bob "Fix on bug#123" put "$(txt 'fixed\n')" 'branches/bug#123/a.txt'
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  local R=$TD/remote.git f bad
  need_file "$R" refs/heads/bug-123 a.txt fixed
  need_file "$R" refs/heads/bugfix a.txt a
  need_ref "$R" 'refs/tags/v2\.0-\{x\}'
  while IFS= read -r f; do
    bad=$(grep -vE '^[[:space:]]*#' "$f" | grep -E '#|\$\{' || true)
    [ -z "$bad" ] || fail "rules line with a literal '#' or '\${' in ${f#$TD/}: $(tr '\n' ' ' <<< "$bad")"
  done < <(find "$TD/out" -name rules.txt)
}

t_f12_caps() {   # F12: capitalised layout folders: WARN, everything kept on main
  mk repo
  mucc alice "Capitalised layout" mkdir Trunk mkdir Branches mkdir Tags
  mucc alice "Add" put "$(txt 'a\n')" Trunk/a.txt
  mucc bob "Tag" cp 2 Trunk Tags/1.0
  mucc bob "Branch" cp 2 Trunk Branches/dev
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  logged 'WARN: .*[Cc]ase' "no WARN about layout folders that differ only in case"
  need_file "$TD/remote.git" main Trunk/a.txt
  need_file "$TD/remote.git" main Tags/1.0/a.txt
  need_file "$TD/remote.git" main Branches/dev/a.txt
  prj "$TD/out"
  report_has "$D" 'Trunk' "the report does not mention the case-variant layout folder Trunk"
}

t_f13_startup() {   # F13/F21/F22/F29/F62: start-up checks refuse Windows shells, a stub python3, too old tools,
  # a host without a UTF-8 locale and an OUT where chmod has no effect
  mk repo; layout
  local real
  mkdir -p "$TD/win" "$TD/py" "$TD/oldgit" "$TD/oldsvn"
  printf '#!/bin/sh\necho MINGW64_NT-10.0-26100\n' > "$TD/win/uname"
  printf '#!/bin/sh\necho "Python was not found; run without arguments to install from the Microsoft Store" >&2\nexit 49\n' \
    > "$TD/py/python3"
  real=$(command -v git)
  printf '#!/bin/sh\ncase "$1" in --version|version) echo "git version 2.20.1"; exit 0;; esac\nexec %s "$@"\n' \
    "$real" > "$TD/oldgit/git"
  real=$(command -v svn)
  printf '#!/bin/sh\ncase " $* " in *" --version "*) case " $* " in *" --quiet "*|*" -q "*) echo 1.7.3;; *) echo "svn, version 1.7.3 (r1235301)";; esac; exit 0;; esac\nexec %s "$@"\n' \
    "$real" > "$TD/oldsvn/svn"
  mkdir -p "$TD/oldlfs" "$TD/oldsafe" "$TD/noutf8"
  real=$(command -v git-lfs)
  printf '#!/bin/sh\ncase "$1" in version|--version) echo "git-lfs/2.13.3 (GitHub; linux amd64; go 1.15.15)"; exit 0;; esac\nexec %s "$@"\n' \
    "$real" > "$TD/oldlfs/git-lfs"
  printf '#!/bin/sh\necho "Usage: svn-all-fast-export [Options]"\necho "  --identity-map <file>"\necho "  --rules <file>"\nexit 0\n' \
    > "$TD/oldsafe/svn-all-fast-export"
  printf '#!/bin/sh\nprintf "C\\nPOSIX\\n"\n' > "$TD/noutf8/locale"
  chmod +x "$TD/win/uname" "$TD/py/python3" "$TD/oldgit/git" "$TD/oldsvn/svn" "$TD/oldlfs/git-lfs" \
    "$TD/oldsafe/svn-all-fast-export" "$TD/noutf8/locale"
  PATH="$TD/oldlfs:$PATH" s2g oldlfs "$TD/repo" "$TD/out-lfs" --authors "$A"
  expect_error '[Ll][Ff][Ss].*(2.13|3.0)'
  PATH="$TD/oldsafe:$PATH" s2g oldsafe "$TD/repo" "$TD/out-safe" --authors "$A"
  expect_error 'svn-all-fast-export|--add-metadata-notes|--empty-dirs|--svn-ignore'
  PATH="$TD/noutf8:$PATH" s2g noutf8 "$TD/repo" "$TD/out-noutf8" --authors "$A"
  expect_error '[Ll]ocale|UTF-8'
  mkdir -p "$TD/nochmod"
  printf '#!/bin/sh\nexit 0\n' > "$TD/nochmod/chmod"; chmod +x "$TD/nochmod/chmod"
  PATH="$TD/nochmod:$PATH" s2g nochmod "$TD/repo" "$TD/out-nochmod" --authors "$A"
  expect_error 'POSIX permissions'
  PATH="$TD/win:$PATH" s2g windows "$TD/repo" "$TD/out-win" --authors "$A"
  expect_error 'Windows is not supported'
  PATH="$TD/py:$PATH" s2g python "$TD/repo" "$TD/out-py" --authors "$A"
  expect_error '[Pp]ython'
  PATH="$TD/oldgit:$PATH" s2g oldgit "$TD/repo" "$TD/out-git" --authors "$A"
  expect_error 'git.*2.(20|24)'
  PATH="$TD/oldsvn:$PATH" s2g oldsvn "$TD/repo" "$TD/out-svn" --authors "$A"
  expect_error '(svn|[Ss]ubversion).*1.(7|8)'
  PATH="$TD/win:$PATH" mgs gitsvn "$U" "$TD/out-gitsvn" --authors "$A"
  expect_error 'Windows is not supported'
}

t_f14_ctrlchars() {   # F14/F66: control characters in a log message and in a revision property
  mk repo; layout
  mucc alice "$(printf 'esc \033[31mred\033[0m ff \014 bs \010 end')" put "$(txt 'a\n')" trunk/a.txt
  svn propset -q --revprop -r 2 ci:result "$(printf 'build \033[32mOK\033[0m')" "$U"
  mucc bob "Plain message" put "$(txt 'a2\n')" trunk/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A"
  expect_pass
  prj "$TD/out"
  local c; c=$(commit_of "$G" 2)
  [ -n "$c" ] || fail "no commit carries an svn note for r2"
  git -C "$G" log -1 --format=%B "$c" | grep -qF "$(printf 'esc \033[31mred')" ||
    fail "the r2 commit message lost its control characters: $(git -C "$G" log -1 --format=%B "$c" | cat -v)"
  git -C "$G" notes --ref=svn show "$c" | grep -qF "$(printf 'ci:result: build \033[32mOK')" ||
    fail "the svn note of r2 does not carry the real ci:result value: $(git -C "$G" notes --ref=svn show "$c" | cat -v | tr '\n' ' ')"
}

t_f15_subfolder() {   # F15: SOURCE URL below the repository root
  mk repo
  mucc alice "projA" mkdir projA mkdir projA/trunk mkdir projA/branches mkdir projA/tags
  mucc alice "Add" put "$(txt 'a\n')" projA/trunk/a.txt
  s2g run "$U/projA" "$TD/out" --authors "$A"
  expect_error 'SOURCE must be the repository root URL'
}

t_f17_lock() {   # F17: URL source with a locked file
  mk repo; layout
  mucc alice "Add" put "$(txt 'bin\n')" trunk/a.bin put "$(txt 'x\n')" trunk/b.txt
  mucc bob "Edit" put "$(txt 'x2\n')" trunk/b.txt
  svn lock -q -m "editing" --username alice "$U/trunk/a.bin"
  [[ $(svn ls -v "$U/trunk") == *" O "* ]] || fail "setup: trunk/a.bin is not shown as locked"
  s2g run "$U" "$TD/out" --authors "$A"
  expect_pass
}

t_f18_dumps() {   # F18: gzip-compressed dump; a dump svnadmin rejects (hint); non-UTF-8 svn:log in a local repository
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  svnadmin dump -q "$TD/repo" > "$TD/repo.dump"
  gzip -c "$TD/repo.dump" > "$TD/repo.dump.gz"
  s2g gz "$TD/repo.dump.gz" "$TD/out-gz" --authors "$A"
  expect_pass "gzip-compressed dump"
  python3 -I - "$TD" << 'EOF'
import os, sys
def props(kv):
    return b"".join(b"K %d\n%s\nV %d\n%s\n" % (len(k), k, len(v), v) for k, v in kv) + b"PROPS-END\n"
def dump(name, log):
    out = [b"SVN-fs-dump-format-version: 2\n\n"]
    for n, kv in ((0, [(b"svn:date", b"2020-01-01T00:00:00.000000Z")]),
                  (1, [(b"svn:author", b"alice"), (b"svn:date", b"2020-01-02T00:00:00.000000Z"), (b"svn:log", log)])):
        p = props(kv)
        out.append(b"Revision-number: %d\nProp-content-length: %d\nContent-length: %d\n\n%s\n" % (n, len(p), len(p), p))
    e = props([])
    for d in (b"trunk", b"branches", b"tags"):
        out.append(b"Node-path: %s\nNode-kind: dir\nNode-action: add\nProp-content-length: %d\n"
                   b"Content-length: %d\n\n%s\n\n" % (d, len(e), len(e), e))
    t = b"a\n"
    out.append(b"Node-path: trunk/a.txt\nNode-kind: file\nNode-action: add\nProp-content-length: %d\n"
               b"Text-content-length: %d\nContent-length: %d\n\n%s%s\n\n" % (len(e), len(t), len(e) + len(t), e, t))
    open(os.path.join(sys.argv[1], name), "wb").write(b"".join(out))
dump("crlf.dump", b"line one\r\nline two\r\n")
dump("latin1.dump", b"gr\xfcn und sch\xf6n")
EOF
  s2g crlf "$TD/crlf.dump" "$TD/out-crlf" --authors "$A"
  [ "$RC" != 0 ] || fail "a dump that svnadmin load rejects (CRLF in svn:log) was accepted"
  logged 'ERROR: ' "no ERROR line for a dump svnadmin load rejects"
  logged '--bypass-prop-validation' "no hint to load the dump with svnadmin load --bypass-prop-validation"
  svnadmin create "$TD/legacy"
  svnadmin load -q --bypass-prop-validation "$TD/legacy" < "$TD/latin1.dump"
  U=file://$TD/legacy
  mucc bob "Normal commit" put "$(txt 'b\n')" trunk/b.txt
  s2g legacy "$TD/legacy" "$TD/out-legacy" --authors "$A"
  expect_pass "non-UTF-8 svn:log in a local repository"
}

t_f19_logins() {   # F19: SVN logins with a space (and one equal to its first word); '#' in an identity
  mk repo; layout
  mucc "John Smith" "Spaced login" put "$(txt 'a\n')" trunk/a.txt
  mucc John "First-word login" put "$(txt 'b\n')" trunk/b.txt
  printf '%s\n' 'alice = Alice Able <alice@test.invalid>' 'John Smith = John Smith <jsmith@test.invalid>' \
    'John = Johnny Walker <johnny@test.invalid>' > "$TD/authors-space.txt"
  s2g run "$TD/repo" "$TD/out" --authors "$TD/authors-space.txt"
  expect_pass
  prj "$TD/out"
  local who
  who=$(author_of "$G" 2)
  [ "$who" = "John Smith <jsmith@test.invalid>" ] || fail "r2 (login 'John Smith') has author '$who'"
  who=$(author_of "$G" 3)
  [ "$who" = "Johnny Walker <johnny@test.invalid>" ] || fail "r3 (login 'John') has author '$who'"
  printf '%s\n' 'alice = Alice #1 <alice@test.invalid>' 'John Smith = John Smith <jsmith@test.invalid>' \
    'John = Johnny Walker <johnny@test.invalid>' > "$TD/authors-hash.txt"
  s2g hash "$TD/repo" "$TD/out2" --authors "$TD/authors-hash.txt"
  expect_error '#'
}

t_f20_lfsunits() {   # F20: --lfs-above 50kb is 51200 bytes (1024-based); an invalid size is refused
  mk repo; layout
  mucc alice "Binaries" put "$(rnd 61440)" trunk/big.bin put "$(rnd 50500)" trunk/medium.bin put "$(rnd 10240)" trunk/small.bin
  s2g run "$TD/repo" "$TD/out" --authors "$A" --lfs-above 50kb --push-remote "$TD/remote.git"
  expect_pass "--lfs-above 50kb"
  local R=$TD/remote.git
  is_pointer "$R" main big.bin || fail "big.bin (61440 bytes) is not an LFS pointer with --lfs-above 50kb"
  if is_pointer "$R" main medium.bin; then fail "medium.bin (50500 bytes) became an LFS pointer: 50kb must mean 51200 bytes"; fi
  if is_pointer "$R" main small.bin; then fail "small.bin (10240 bytes) became an LFS pointer"; fi
  s2g bad "$TD/repo" "$TD/out2" --authors "$A" --lfs-above 12xyz
  expect_error '--lfs-above'
}

t_f23_relpaths() {   # F23: relative SOURCE (directory and dump file) and relative --push-remote
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  svnadmin dump -q "$TD/repo" > "$TD/repo.dump"
  cd "$TD"
  s2g dir repo out --authors authors.txt --push-remote remote.git
  expect_pass "relative repository directory"
  need_file "$TD/remote.git" main a.txt
  s2g dump repo.dump out2 --authors authors.txt
  expect_pass "relative dump file"
}

t_f24_bom() {   # F24: authors file with a UTF-8 BOM and CRLF line ends, and the same file as UTF-16
  mk repo; layout
  mucc bob "Add" put "$(txt 'a\n')" trunk/a.txt
  printf '\xef\xbb\xbf# login = Full Name <email>\r\nalice = Alice \xc3\x84ble <alice@test.invalid>\r\nbob = Bob B\xc3\xa4ker <bob@test.invalid>\r\n' \
    > "$TD/authors-bom.txt"
  python3 -I -c 'import sys; open(sys.argv[2], "w", encoding="utf-16").write(open(sys.argv[1], encoding="utf-8-sig").read().replace("\r\n", "\n"))' \
    "$TD/authors-bom.txt" "$TD/authors-utf16.txt"
  local f who
  for f in bom utf16; do
    s2g "$f" "$TD/repo" "$TD/out-$f" --authors "$TD/authors-$f.txt"
    expect_pass "authors file $f"
    prj "$TD/out-$f"
    who=$(author_of "$G" 2)
    [ "$who" = "Bob Bäker <bob@test.invalid>" ] || fail "authors file $f: r2 has author '$who'"
  done
}

t_f25_gitignore() {   # F25: versioned root .gitignore and .gitattributes (with a file moved to LFS)
  mk repo; layout
  mucc alice "Versioned .gitignore and .gitattributes" put "$(txt '*.o\n')" trunk/.gitignore \
    put "$(txt '*.txt text\n')" trunk/.gitattributes put "$(txt 'a\n')" trunk/a.txt put "$(rnd 61440)" trunk/big.bin
  mucc alice "Branch b1" cp 2 trunk branches/b1
  mucc bob "Edit" put "$(txt 'a2\n')" trunk/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A" --lfs-above 50KB --push-remote "$TD/remote.git"
  expect_pass
  need_file "$TD/remote.git" main .gitignore '*.o'
  grep -qx '\*\.txt text' <<< "$(git -C "$TD/remote.git" cat-file blob main:.gitattributes 2> /dev/null)" ||
    fail "main:.gitattributes lost the versioned '*.txt text' line"
}

t_f26_deloutside() {   # F26: folders outside the layout deleted, or moved into trunk
  mk repo; layout
  mucc alice "Trunk and a sandbox folder" put "$(txt 'a\n')" trunk/a.txt mkdir sandbox put "$(txt 'x\n')" sandbox/x.txt
  mucc alice "Sandbox work" put "$(txt 'x2\n')" sandbox/x.txt
  mucc bob "Delete the sandbox" rm sandbox
  mucc bob "Stray folder" mkdir stray put "$(txt 'int y;\n')" stray/y.c
  mucc carol "Move the stray folder into trunk" mv stray trunk/stray
  mucc carol "Edit" put "$(txt 'a2\n')" trunk/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  need_file "$TD/remote.git" main stray/y.c 'int y;'
  if paths "$TD/remote.git" svn-outside-layout | grep -qE '^(sandbox|stray)/'; then
    fail "svn-outside-layout still holds deleted folders: $(paths "$TD/remote.git" svn-outside-layout | tr '\n' ' ')"
  fi
}

t_f27_crash() {   # F27: verify.py dying without verify.json is reported as NOT VERIFIED; nothing pushed
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mkdir -p "$TD/kit"
  cp "$HERE"/*.sh "$HERE"/*.py "$TD/kit/"
  printf 'import sys\nsys.exit("simulated verify crash")\n' > "$TD/kit/verify.py"
  S2G=$TD/kit/svn2github.sh s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  [ "$RC" != 0 ] || fail "the run succeeded although verify.py crashed"
  prj "$TD/out"
  report_has "$D" 'NOT VERIFIED' "MIGRATION_REPORT.md does not say NOT VERIFIED after verify.py crashed"
  if [ -n "$(refs "$TD/remote.git")" ]; then fail "refs were pushed although verify.py crashed"; fi
}

t_f30_cherrypick() {   # F30: a cherry-pick that adds a file is not a git merge
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc alice "Branch 1.x" cp 2 trunk branches/1.x
  mucc bob "Trunk change that is not backported" put "$(txt 'a2\n')" trunk/a.txt
  mucc bob "Add fix.c" put "$(txt 'fix\n')" trunk/fix.c
  svn co -q "$U/branches/1.x" "$TD/wc"
  svn merge -q -c 5 "$U/trunk" "$TD/wc"
  svn ci -q --username carol -m "Backport r5 (adds fix.c)" "$TD/wc"
  rlog 6 'A /branches/1\.x/fix\.c \(from /trunk/fix\.c:5\)'
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  need_file "$TD/remote.git" refs/heads/1.x fix.c fix
  need_file "$TD/remote.git" refs/heads/1.x a.txt a
  [ -z "$(git -C "$TD/remote.git" rev-list --merges refs/heads/1.x)" ] ||
    fail "the cherry-pick of r5 became a merge commit on 1.x (it claims r4 was merged too)"
}

t_f31_gitattr() {   # F31/F34: --apply-gitattributes keeps the LFS rules, CRLF files get -text, clone is clean
  mk repo; layout
  local big; big=$(rnd 61440)
  mucc alice "Files with svn:eol-style and a large binary" \
    put "$big" trunk/big.bin propset svn:mime-type application/octet-stream trunk/big.bin \
    put "$(txt 'line1\r\nline2\r\n')" trunk/win.txt propset svn:eol-style CRLF trunk/win.txt \
    put "$(txt 'hello\n')" trunk/a.txt propset svn:eol-style native trunk/a.txt \
    put "$(txt '#!/bin/sh\n')" trunk/run.sh propset svn:eol-style LF trunk/run.sh
  mucc bob "Edit" put "$(txt 'hello again\n')" trunk/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A" --lfs-above 50KB --apply-gitattributes --push-remote "$TD/remote.git"
  expect_pass
  local ga
  ga=$(git -C "$TD/remote.git" cat-file blob main:.gitattributes 2> /dev/null) || fail "main has no .gitattributes"
  grep -q 'filter=lfs' <<< "$ga" || fail "main:.gitattributes lost the LFS rules: $(tr '\n' ';' <<< "$ga")"
  grep -vE '^[[:space:]]*(#|$)' <<< "$ga" | grep -vq 'filter=lfs' ||
    fail "main:.gitattributes has no rule from svn:eol-style/svn:mime-type: $(tr '\n' ';' <<< "$ga")"
  if grep -q 'eol=crlf' <<< "$ga"; then fail "svn:eol-style CRLF must map to -text, not eol=crlf: $(tr '\n' ';' <<< "$ga")"; fi
  clone "$TD/remote.git" "$TD/clone"
  cmp -s "$big" "$TD/clone/big.bin" || fail "big.bin in the clone is not the original content (LFS not resolved)"
  clean_status "$TD/clone"
}

t_f32_replace() {   # F32/F56: file replaced by a folder (and a file by a file) outside the layout; NONE layout too
  mk repo; layout
  mucc alice "Trunk and tools" put "$(txt 'a\n')" trunk/a.txt mkdir tools put "$(txt 'x=1\n')" tools/config \
    put "$(txt 'v1\n')" tools/x.txt
  mucc alice "Replace the config file by a folder" rm tools/config mkdir tools/config put "$(txt '[main]\n')" tools/config/app.ini
  rlog 3 'R /tools/config'
  mucc bob "Replace a file by a new file" rm tools/x.txt put "$(txt 'v2\n')" tools/x.txt
  rlog 4 'R /tools/x\.txt'
  mucc bob "Edit inside the new folder" put "$(txt '[main]\nx=2\n')" tools/config/app.ini
  mucc carol "Edit trunk" put "$(txt 'a2\n')" trunk/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass "outside the layout"
  local R=$TD/remote.git p
  need_file "$R" svn-outside-layout tools/config/app.ini "$(printf '[main]\nx=2')"
  need_file "$R" svn-outside-layout tools/x.txt v2
  for p in app.ini config config/app.ini x.txt; do no_file "$R" svn-outside-layout "$p"; done
  mk flat
  mucc alice "Flat import" mkdir src put "$(txt 'int m;\n')" src/main.c put "$(txt 'x=1\n')" config
  mucc alice "Replace config by a folder" rm config mkdir config put "$(txt '[main]\n')" config/app.ini
  rlog 2 'R /config'
  mucc bob "Edit" put "$(txt '[main]\nx=2\n')" config/app.ini
  s2g flat "$TD/flat" "$TD/out-flat" --authors "$A" --push-remote "$TD/flat.git"
  expect_pass "NONE layout"
  need_file "$TD/flat.git" main config/app.ini "$(printf '[main]\nx=2')"
  need_file "$TD/flat.git" main src/main.c
}

t_f33_reuse() {   # F33: branch name deleted and re-used, then fully merged: a real merge commit
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc alice "Branch feat" cp 2 trunk branches/feat
  mucc bob "Old feat work, never merged" put "$(txt 'old\n')" branches/feat/old.txt
  mucc bob "Delete feat" rm branches/feat
  mucc alice "Re-create feat" cp 5 trunk branches/feat
  # a modification, not an added file: an added file would get a converter-made merge parent (F30)
  mucc bob "New feat work" put "$(txt 'new\n')" branches/feat/a.txt
  svn co -q "$U/trunk" "$TD/wc"
  svn merge -q "$U/branches/feat" "$TD/wc"
  svn ci -q --username carol -m "Merge feat into trunk" "$TD/wc"
  [[ $(svn propget svn:mergeinfo "$U/trunk") == "/branches/feat:6-7" ]] ||
    fail "setup: trunk mergeinfo is '$(svn propget svn:mergeinfo "$U/trunk")', expected /branches/feat:6-7"
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  local R=$TD/remote.git
  [ "$(git -C "$R" rev-list --merges main | wc -l)" = 1 ] || fail "the full merge of the re-created feat is not a merge commit"
  [ "$(git -C "$R" rev-parse main^2)" = "$(git -C "$R" rev-parse refs/heads/feat)" ] ||
    fail "the merge commit's second parent is not the tip of feat"
}

t_f36_gitsvn() {   # F36/F64: migrate_gitsvn.sh with --authors; a re-run after a new commit advances main
  mk repo; layout
  mucc alice "Add" put "$(txt 'v1\n')" trunk/a.txt
  mucc bob "Branch b1" cp 2 trunk branches/b1
  mucc carol "Edit" put "$(txt 'v2\n')" trunk/a.txt
  mgs first "$U" "$TD/gs" --authors "$A"
  [ "$RC" = 0 ] || fail "migrate_gitsvn.sh --authors failed (exit $RC): $(why)"
  local R=$TD/gs/repo.git who
  need_file "$R" main a.txt v2
  who=$(git -C "$R" log --format='%an <%ae>' --branches --tags | sort -u | grep -v -e '@test\.invalid>$' -e '<migration@example\.com>$' || true)
  [ -z "$who" ] || fail "identities not taken from --authors in the git svn result: $(tr '\n' ' ' <<< "$who")"
  rep_verified "$TD/gs"
  mucc alice "Commit after the first run" put "$(txt 'v3\n')" trunk/a.txt
  mgs rerun "$U" "$TD/gs" --authors "$A"
  [ "$RC" = 0 ] || fail "re-run of migrate_gitsvn.sh failed (exit $RC): $(why)"
  need_file "$R" main a.txt v3
  rep_verified "$TD/gs"
}

t_f39_pushplan() {   # F39: pushes are split by size; with a small PUSH_LIMIT_BYTES every ref still arrives
  mk repo; layout
  local i l r plan
  for i in 1 2 3 4 5 6 7 8; do mucc alice "Trunk data $i" put "$(rnd 6000)" "trunk/d$i.bin"; done
  mucc bob "Branch b1" cp HEAD trunk branches/b1
  for i in 1 2 3; do mucc bob "Branch data $i" put "$(rnd 6000)" "branches/b1/e$i.bin"; done
  mucc carol "Tag 1.0" cp HEAD trunk tags/1.0
  PUSH_LIMIT_BYTES=20000 s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass "PUSH_LIMIT_BYTES=20000"
  prj "$TD/out"
  l=$(git -C "$G" for-each-ref --format='%(objectname) %(refname)' refs/heads refs/tags refs/notes)
  r=$(git -C "$TD/remote.git" for-each-ref --format='%(objectname) %(refname)' refs/heads refs/tags refs/notes)
  [ "$l" = "$r" ] || fail "remote refs differ from the migrated repository: $(diff <(echo "$l") <(echo "$r") | tr '\n' ' ')"
  no_ref "$TD/remote.git" refs/heads/svn-migration-tmp
  plan=$(python3 -I "$HERE/push_plan.py" "$G" --limit 20000 2> "$TD/plan.err") ||
    fail "push_plan.py --limit 20000 failed: $(tr '\n' ' ' < "$TD/plan.err")"
  [ "$(grep -c . <<< "$plan")" -ge 3 ] || fail "push_plan.py --limit 20000 planned fewer than 3 pushes: $(tr '\n' ';' <<< "$plan")"
  grep -q 'refs/heads/main:refs/heads/main' <<< "$plan" || fail "push plan has no final main push: $(tr '\n' ';' <<< "$plan")"
  if python3 -I "$HERE/push_plan.py" "$G" --limit 5000 > "$TD/plan2.out" 2> "$TD/plan2.err"; then
    fail "push_plan.py --limit 5000 accepted commits that each add 6000 bytes"
  fi
  grep -q 'ERROR: ' "$TD/plan2.err" || fail "push_plan.py --limit 5000: no ERROR line on stderr ($(tr '\n' ' ' < "$TD/plan2.err"))"
}

t_f40_dotgit() {   # F40: .git folders in SVN are not migrated, with a WARN
  mk repo; layout
  mucc alice "Vendor drop with its .git folder" put "$(txt 'a\n')" trunk/a.txt mkdir trunk/vendor mkdir trunk/vendor/lib \
    mkdir trunk/vendor/lib/.git put "$(txt '[core]\n')" trunk/vendor/lib/.git/config put "$(txt 'int v;\n')" trunk/vendor/lib/x.c \
    mkdir trunk/other mkdir trunk/other/.Git put "$(txt 'ref: refs/heads/master\n')" trunk/other/.Git/HEAD
  mucc bob "Change only inside .git" put "$(txt '[core]\nbare = false\n')" trunk/vendor/lib/.git/config
  mucc carol "Edit" put "$(txt 'a2\n')" trunk/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  logged 'WARN: \.git folder not migrated: .*vendor/lib/\.git' "no 'WARN: .git folder not migrated: /trunk/vendor/lib/.git (rN)' line"
  need_file "$TD/remote.git" main vendor/lib/x.c
  if paths "$TD/remote.git" main | grep -qiE '(^|/)\.git(/|$)'; then
    fail ".git paths reached git: $(paths "$TD/remote.git" main | grep -iE '(^|/)\.git(/|$)' | tr '\n' ' ')"
  fi
  prj "$TD/out"
  report_has "$D" 'vendor/lib/\.git' "the report does not list the .git folder that was not migrated"
}

t_f43_headrefs() {   # F43: refs whose names end in HEAD survive the post-push check
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc alice "Branch" cp 2 trunk branches/MERGED_TO_HEAD
  mucc bob "Work on it" put "$(txt 'm\n')" branches/MERGED_TO_HEAD/a.txt
  mucc bob "Tag" cp 2 trunk tags/v1-HEAD
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  need_file "$TD/remote.git" refs/heads/MERGED_TO_HEAD a.txt m
  need_ref "$TD/remote.git" refs/tags/v1-HEAD
}

t_f37_ghscope() {   # F37/F41: GitHub push of history with .github/workflows, token without workflow scope, GHES host
  mk repo; layout
  mucc alice "Add a workflow" mkdir trunk/.github mkdir trunk/.github/workflows \
    put "$(txt 'on: push\n')" trunk/.github/workflows/ci.yml put "$(txt 'a\n')" trunk/a.txt
  mkdir -p "$TD/gh"
  # stub gh: logged in to ghe.example.test with a token lacking the workflow scope; records every call
  cat > "$TD/gh/gh" << EOF
#!/bin/sh
echo "gh \$*" >> "$TD/gh.calls"
case "\$1 \$2" in
  "--version "*|"version "*) echo "gh version 2.62.0 (2024-11-14)"; exit 0;;
  "auth status")
    case " \$* " in *" ghe.example.test "*|*" --hostname=ghe.example.test "*) ;; *) echo "not logged in" >&2; exit 1;; esac
    printf '%s\n' "ghe.example.test" "  ✓ Logged in to ghe.example.test account alice (keyring)" \
      "  - Active account: true" "  - Git operations protocol: https" "  - Token: gho_************************************" \
      "  - Token scopes: 'gist', 'read:org', 'repo'"
    exit 0;;
  "repo view") echo "GraphQL: Could not resolve to a Repository with the name 'org/demo'. (repository)" >&2; exit 1;;
esac
echo "stub gh: unexpected call: \$*" >&2
exit 1
EOF
  chmod +x "$TD/gh/gh"
  PATH="$TD/gh:$PATH" s2g run "$TD/repo" "$TD/out" --authors "$A" --push ghe.example.test/org/demo
  expect_error 'workflow'
  logged 'gh auth refresh -h ghe\.example\.test -s workflow' "no 'gh auth refresh -h ghe.example.test -s workflow' advice"
  grep -q 'auth status.*ghe\.example\.test' "$TD/gh.calls" 2> /dev/null ||
    fail "gh auth status was not asked about host ghe.example.test (calls: $(tr '\n' ';' < "$TD/gh.calls" 2> /dev/null))"
  if grep -q 'repo create' "$TD/gh.calls" 2> /dev/null; then fail "the repository was created before the workflow-scope check"; fi
}

t_f47_externals() {   # F47: each project's report lists only its own svn:externals
  mk repo
  mucc alice "Projects" mkdir projA mkdir projA/trunk mkdir projA/branches mkdir projA/tags \
    mkdir projB mkdir projB/trunk mkdir projB/branches mkdir projB/tags
  mucc alice "Code and externals" put "$(txt 'a\n')" projA/trunk/a.txt put "$(txt 'b\n')" projB/trunk/b.txt \
    propset svn:externals 'libA https://svn.example.invalid/libA/trunk' projA/trunk \
    propset svn:externals 'libB https://svn.example.invalid/libB/trunk' projB/trunk
  s2g run "$TD/repo" "$TD/out" --authors "$A"
  expect_pass
  prj "$TD/out" projA
  report_has "$D" 'libA' "projA's report does not list its svn:externals"
  report_lacks "$D" 'libB' "projA's report lists projB's svn:externals"
  prj "$TD/out" projB
  report_has "$D" 'libB' "projB's report does not list its svn:externals"
  report_lacks "$D" 'libA' "projB's report lists projA's svn:externals"
  [ -f "$TD/out/meta/externals.json" ] || fail "meta/externals.json was not written"
}

t_f51_eol() {   # F51: svn:eol-style native file stored with CRLF (svnmucc does not normalise)
  mk repo; layout
  mucc alice "CRLF bytes under svn:eol-style native" put "$(txt 'one\r\ntwo\r\n')" trunk/notes.txt \
    propset svn:eol-style native trunk/notes.txt
  [[ $(svnlook cat "$TD/repo" trunk/notes.txt | od -c) == *'\r'* ]] || fail "setup: notes.txt is not stored with CRLF"
  mucc bob "Another file" put "$(txt 'x\n')" trunk/other.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A"
  expect_pass
  prj "$TD/out"
  python3 -I - "$D/verify.json" << 'EOF' || fail "verify.json does not list notes.txt under 'info: SVN files stored with line endings that differ from svn:eol-style'"
import json, sys
p = json.load(open(sys.argv[1], encoding="utf-8")).get("problems", {})
cats = [c for c in p if "line endings that differ from svn:eol-style" in c]
sys.exit(0 if any("notes.txt" in i for c in cats for i in p[c]) else 1)
EOF
  report_has "$D" 'notes\.txt' "the report does not list notes.txt (stored line endings differ from svn:eol-style)"
}

t_f52_gitsvnid() {   # F52: log messages quoting git-svn-id lines keep their own revision mapping
  mk repo; layout
  mucc alice "$(printf 'Port a fix from the git mirror\n\n    git-svn-id: https://svn.example.org/repo/trunk@1234 9a8b7c6d-0000-4000-8000-000000000001\nReviewed.')" \
    put "$(txt 'a\n')" trunk/a.txt
  mucc bob "$(printf 'Squashed import\n\ngit-svn-id: https://svn.example.org/repo/trunk@77 9a8b7c6d-0000-4000-8000-000000000002')" \
    put "$(txt 'a2\n')" trunk/a.txt
  mucc carol "Plain change" put "$(txt 'a3\n')" trunk/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A"
  expect_pass
  prj "$TD/out"
  local r
  for r in 2 3 4; do
    [ -n "$(commit_of "$G" "$r")" ] || fail "no commit has an svn note naming r$r"
  done
  for r in 1234 77; do
    [ -z "$(commit_of "$G" "$r")" ] || fail "a commit was mapped to r$r, a revision quoted in a log message"
  done
}

t_f53_lfsshrink() {   # F53: a file above the LFS threshold that shrinks again, and a branch with the small version
  mk repo; layout
  mucc alice "Small data" put "$(rnd 400)" trunk/data.bin
  mucc alice "Maintenance branch" cp 2 trunk branches/maint
  mucc bob "Data grows above the threshold" put "$(rnd 61440)" trunk/data.bin
  mucc bob "Data shrinks again" put "$(rnd 500)" trunk/data.bin
  mucc carol "Maintenance work" put "$(txt 'm\n')" branches/maint/other.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A" --lfs-above 50KB --push-remote "$TD/remote.git"
  expect_pass
  clone "$TD/remote.git" "$TD/clone"
  clean_status "$TD/clone"
  git -C "$TD/clone" lfs fsck --pointers > "$TD/fsck.log" 2>&1 ||
    fail "git lfs fsck --pointers on main: $(tail -2 "$TD/fsck.log" | tr '\n' ' ')"
  clean_status "$TD/clone" maint
  git -C "$TD/clone" lfs fsck --pointers > "$TD/fsck.log" 2>&1 ||
    fail "git lfs fsck --pointers on maint: $(tail -2 "$TD/fsck.log" | tr '\n' ' ')"
}

t_f54_tagger() {   # F54: tag created in a revision without svn:author gets the (no author) identity as tagger
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc bob "Tag 1.0" cp 2 trunk tags/1.0
  svn propdel -q --revprop -r 3 svn:author "$U"
  rlog 3 '^r3 \| \(no author\) \|'
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  local who; who=$(git -C "$TD/remote.git" for-each-ref --format='%(taggername) %(taggeremail)' refs/tags/1.0)
  [ "$who" = "Nobody Known <unknown@test.invalid>" ] ||
    fail "tag 1.0 has tagger '$who', expected the (no author) identity 'Nobody Known <unknown@test.invalid>'"
}

t_f55_revgap() {   # F55: dump with a revision gap: WARN, meta/revmap.json, SVN-Original-Revision in the note
  mk orig
  mucc alice "Layout" mkdir trunk mkdir branches mkdir tags mkdir other
  mucc alice "Add" put "$(txt 'v1\n')" trunk/a.txt
  mucc bob "Other project" put "$(txt 'o\n')" other/o.txt
  mucc bob "Edit" put "$(txt 'v2\n')" trunk/a.txt
  mucc carol "Edit again" put "$(txt 'v3\n')" trunk/a.txt
  svnadmin dump -q "$TD/orig" > "$TD/full.dump"
  svndumpfilter exclude /other --drop-empty-revs --quiet < "$TD/full.dump" > "$TD/gap.dump"
  [ "$(grep -a '^Revision-number: ' "$TD/gap.dump" | tr -d '\n')" = \
    "Revision-number: 0Revision-number: 1Revision-number: 2Revision-number: 4Revision-number: 5" ] ||
    fail "setup: gap.dump does not hold revisions 0 1 2 4 5"
  s2g run "$TD/gap.dump" "$TD/out" --authors "$A"
  expect_pass
  logged 'WARN: .*([Rr]evision|[Rr]enumber|original)' "no WARN about renumbered revisions"
  python3 -I -c 'import json, sys; d = json.load(open(sys.argv[1])); sys.exit(0 if {str(k): int(v) for k, v in d.items()} == {"3": 4, "4": 5} else 1)' \
    "$TD/out/meta/revmap.json" || fail "meta/revmap.json is not {\"3\": 4, \"4\": 5}: $(cat "$TD/out/meta/revmap.json" 2>&1)"
  prj "$TD/out"
  local c; c=$(commit_of "$G" 3)
  [ -n "$c" ] || fail "no commit has an svn note for mirror r3"
  git -C "$G" notes --ref=svn show "$c" | grep -qx 'SVN-Original-Revision: r4' ||
    fail "the note of mirror r3 has no 'SVN-Original-Revision: r4' line: $(git -C "$G" notes --ref=svn show "$c" | head -4 | tr '\n' ' ')"
}

t_f57_spaces() {   # F57: repository path and a file name with spaces: generated .gitattributes rules match
  mk "my repo"
  mucc alice "Layout" mkdir trunk mkdir branches mkdir tags
  mucc alice "Files with svn:eol-style" put "$(txt 'a\n')" trunk/a.txt propset svn:eol-style native trunk/a.txt \
    put "$(txt 'r1\r\nr2\r\n')" "trunk/Read Me.txt" propset svn:eol-style CRLF "trunk/Read Me.txt"
  s2g run "$TD/my repo" "$TD/out" --authors "$A" --apply-gitattributes --push-remote "$TD/remote.git"
  expect_pass
  prj "$TD/out"
  if grep -qE '^/ ' "$D/suggested.gitattributes"; then
    fail "suggested.gitattributes has a rule for '/' (propget target paths not decoded): $(tr '\n' ';' < "$D/suggested.gitattributes")"
  fi
  clone "$TD/remote.git" "$TD/clone"
  local a
  a=$(git -C "$TD/clone" check-attr text -- "Read Me.txt")
  [ "$a" = "Read Me.txt: text: unset" ] || fail "'Read Me.txt' (svn:eol-style CRLF) should be -text; git check-attr: $a"
  a=$(git -C "$TD/clone" check-attr text -- a.txt)
  [ "$a" = "a.txt: text: set" ] || fail "a.txt (svn:eol-style native) should be text; git check-attr: $a"
  clean_status "$TD/clone"
}

t_f60_renames() {   # F60: rename whose old and new names clean up to one git name; a branch named main
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc alice "Release branch" cp 2 trunk "branches/release 2.0"
  mucc bob "Release work" put "$(txt 'r1\n')" "branches/release 2.0/a.txt"
  mucc bob "Rename the release branch" mv "branches/release 2.0" branches/release-2.0
  mucc carol "More release work" put "$(txt 'r2\n')" branches/release-2.0/a.txt
  mucc carol "Branch named main" cp 2 trunk branches/main
  mucc alice "Work on it" put "$(txt 'm\n')" branches/main/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  local R=$TD/remote.git
  need_file "$R" refs/heads/release-2.0 a.txt r2
  need_ref "$R" 'refs/tags/svn-deleted/release-2\.0-svn-r3@[0-9]+'
  need_file "$R" refs/heads/main-svn-r7 a.txt m
  need_file "$R" refs/heads/main a.txt a
}

t_f61_hooks() {   # F61: the --apply-gitattributes commit ignores failing global hooks and commit.gpgSign
  mk repo; layout
  mucc alice "CRLF file" put "$(txt 'x\r\ny\r\n')" trunk/win.txt propset svn:eol-style CRLF trunk/win.txt \
    put "$(txt 'a\n')" trunk/a.txt
  mkdir -p "$TD/hooks"
  printf '#!/bin/sh\necho "global hook refuses every commit" >&2\nexit 1\n' > "$TD/hooks/pre-commit"
  cp "$TD/hooks/pre-commit" "$TD/hooks/commit-msg"
  chmod +x "$TD/hooks/pre-commit" "$TD/hooks/commit-msg"
  printf '[core]\n\thooksPath = %s\n[commit]\n\tgpgSign = true\n' "$TD/hooks" > "$TD/hooks.gitconfig"
  GIT_CONFIG_GLOBAL=$TD/hooks.gitconfig s2g run "$TD/repo" "$TD/out" --authors "$A" --apply-gitattributes \
    --push-remote "$TD/remote.git"
  expect_pass
  git -C "$TD/remote.git" cat-file -e main:.gitattributes 2> /dev/null ||
    fail "main has no .gitattributes although --apply-gitattributes was given and the run passed"
}

t_f63_live() {   # F16/F63: a commit made while the migration runs is not migrated (revision pinned)
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc alice "Branch b1" cp 2 trunk branches/b1
  local real; real=$(command -v svn-all-fast-export)
  mkdir -p "$TD/live"
  printf 'late\n' > "$TD/.files/late"
  cat > "$TD/live/svn-all-fast-export" << EOF
#!/bin/sh
# a developer commits to the live repository just as the conversion starts
case " \$* " in *" --rules "*)
  if [ ! -e "$TD/.committed" ]; then
    : > "$TD/.committed"
    svnmucc -U "$U" --username bob -m "Commit during the migration" put "$TD/.files/late" trunk/late.txt \
      cp HEAD trunk branches/late > /dev/null
  fi;;
esac
exec "$real" "\$@"
EOF
  chmod +x "$TD/live/svn-all-fast-export"
  PATH="$TD/live:$PATH" s2g run "$TD/repo" "$TD/out" --authors "$A"
  [ -e "$TD/.committed" ] || fail "setup: the converter wrapper never made the concurrent commit"
  expect_pass
  prj "$TD/out"
  no_file "$G" main late.txt
  no_ref "$G" refs/heads/late
}

t_f65_nfd() {   # F65: NFC/NFD twins and an NFD-only name are listed as macOS checkout risks
  mk repo; layout
  local nfc nfd cafe
  nfc=$(printf 'Sp\303\251cification.doc'); nfd=$(printf 'Spe\314\201cification.doc'); cafe=$(printf 'Cafe\314\201.txt')
  mucc alice "Unicode names" put "$(txt '1\n')" "trunk/$nfc" put "$(txt '2\n')" "trunk/$nfd" put "$(txt '3\n')" "trunk/$cafe"
  s2g run "$TD/repo" "$TD/out" --authors "$A"
  expect_pass
  prj "$TD/out"
  report_has "$D" "$cafe" "the report does not list the NFD (non-NFC) name $cafe"
  report_has "$D" "$nfd" "the report does not list the NFC/NFD twin $nfd"
}

t_f09_projdel() {   # F09: a retired project folder deleted as a whole while it held a README / build file
  mk repo
  mucc alice "Create" mkdir projB mkdir projB/trunk mkdir projB/branches mkdir projB/tags mkdir other
  mucc alice "Code" put "$(txt 'b\n')" projB/trunk/b.txt put "$(txt 'readme\n')" projB/README.txt
  mucc bob "Retire projB" rm projB
  mucc carol "Other work" put "$(txt 'o\n')" other/o.txt
  s2g explicit "$TD/repo" "$TD/out1" --authors "$A" --project /projB --push-remote "$TD/remote1.git"
  expect_pass "--project /projB"
  need_file "$TD/remote1.git" main b.txt b
  mk repo2
  mucc alice "Create projects" mkdir projA mkdir projA/trunk mkdir projA/branches mkdir projA/tags \
    mkdir projB mkdir projB/trunk mkdir projB/branches mkdir projB/tags
  mucc alice "Code" put "$(txt 'a\n')" projA/trunk/a.txt put "$(txt 'b\n')" projB/trunk/b.txt put "$(txt 'bx\n')" projB/build.xml
  mucc bob "Tag projB" cp 2 projB/trunk projB/tags/1.0
  mucc bob "Branch projB" cp 2 projB/trunk projB/branches/dev
  mucc bob "dev work" put "$(txt 'dev\n')" projB/branches/dev/b.txt
  mucc carol "Retire projB" rm projB
  mucc carol "projA goes on" put "$(txt 'a2\n')" projA/trunk/a.txt
  s2g auto "$TD/repo2" "$TD/out2" --authors "$A" --push-remote "$TD/remotes/{name}.git"
  expect_pass "auto mode"
  need_file "$TD/remotes/projB.git" main b.txt b
  need_ref "$TD/remotes/projB.git" 'refs/tags/svn-deleted/tags/1\.0@[0-9]+'
  need_ref "$TD/remotes/projB.git" 'refs/tags/svn-deleted/dev@[0-9]+'
}

t_f09_trunkempty() {   # F09: trunk deleted, then re-created empty with svn mkdir
  mk repo; layout
  mucc alice "Add" put "$(txt 'v1\n')" trunk/a.txt
  mucc alice "Delete trunk" rm trunk
  mucc bob "Re-create trunk" mkdir trunk
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  logged 'WARN: trunk was re-created empty at r4' "no 'WARN: trunk was re-created empty at r4' line"
  if grep -q 'main is its last state' "$LOG"; then fail "the run claims main is trunk's last state although trunk exists at r4"; fi
  [ -z "$(paths "$TD/remote.git" main)" ] || fail "main is not empty: $(paths "$TD/remote.git" main | tr '\n' ' ')"
  need_file "$TD/remote.git" main^ a.txt v1
  need_ref "$TD/remote.git" 'refs/tags/svn-deleted/(master|main)@3'
}

t_f05_containerr() {   # F05: branches/ replaced without a copy (rm + mkdir in one commit) deletes its branches
  mk repo; layout
  mucc alice "Add" put "$(txt 'v1\n')" trunk/a.txt
  mucc alice "Branch y" cp 2 trunk branches/y
  mucc bob "y work" put "$(txt 'y\n')" branches/y/y.txt
  mucc bob "Reset branches" rm branches mkdir branches cp 4 trunk branches/x
  rlog 5 'R /branches$'
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  no_ref "$TD/remote.git" 'refs/heads/y'
  need_ref "$TD/remote.git" 'refs/tags/svn-deleted/y@5'
  need_ref "$TD/remote.git" 'refs/heads/x'
  need_file "$TD/remote.git" refs/tags/svn-deleted/y@5 y.txt y
}

t_f05_tagsr() {   # F05: tags/ replaced without a copy deletes its tags
  mk repo; layout
  mucc alice "Add" put "$(txt 'v1\n')" trunk/a.txt
  mucc alice "Tag 1.0" cp 2 trunk tags/1.0
  mucc bob "Reset tags" rm tags mkdir tags
  rlog 4 'R /tags$'
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  no_ref "$TD/remote.git" 'refs/tags/1\.0'
  need_ref "$TD/remote.git" 'refs/tags/svn-deleted/tags/1\.0@4'
}

t_f05_projr() {   # F05/F09: a project folder replaced by an empty folder: its trunk and branches are deleted
  mk repo
  mucc alice "Create" mkdir projA mkdir projA/trunk mkdir projA/branches mkdir projA/tags \
    mkdir projB mkdir projB/trunk mkdir projB/branches mkdir projB/tags
  mucc alice "Code" put "$(txt 'a\n')" projA/trunk/a.txt put "$(txt 'b\n')" projB/trunk/b.txt
  mucc bob "Branch projB" cp 2 projB/trunk projB/branches/dev
  mucc bob "Wipe projB" rm projB mkdir projB
  rlog 4 'R /projB$'
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remotes/{name}.git"
  expect_pass
  logged 'WARN: trunk was deleted at r4' "no 'WARN: trunk was deleted at r4' line for projB"
  no_ref "$TD/remotes/projB.git" 'refs/heads/dev'
  need_ref "$TD/remotes/projB.git" 'refs/tags/svn-deleted/dev@4'
  need_file "$TD/remotes/projB.git" main b.txt b
}

t_f14_ctrlauthor() {   # F14: an svn:author holding an ESC byte (svn info/ls --xml write it raw)
  mk repo
  mucc alice "Layout" mkdir trunk mkdir branches mkdir tags put "$(txt 'a\n')" trunk/a.txt
  mucc bob "Branch b1" cp 1 trunk branches/b1
  svn propset -q --revprop -r 2 svn:author "$(printf 'bo\033b')" "$U"
  mucc alice "Edit" put "$(txt 'a2\n')" trunk/a.txt
  printf '%s\n' "$(printf 'bo\033b') = Bob Escape <bobe@test.invalid>" >> "$A"
  s2g local "$TD/repo" "$TD/out1" --authors "$A"
  expect_pass "local repository"
  prj "$TD/out1"
  [ "$(author_of "$G" 2)" = "Bob Escape <bobe@test.invalid>" ] || fail "r2 author is $(author_of "$G" 2 | cat -v)"
  s2g url "$U" "$TD/out2" --authors "$A"
  expect_pass "file:// URL source"
  svn propset -q --revprop -r 3 svn:author "$(printf 'bo\033b')" "$U"
  s2g young "$TD/repo" "$TD/out3" --authors "$A"
  expect_pass "youngest revision by that author"
}

t_f21_propmods() {   # F21: a server older than 1.7 sends no prop-mods: svn_meta.py still records svn:mergeinfo
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc bob "Branch b" cp 2 trunk branches/b
  mucc alice "Edit" put "$(txt 'a2\n')" trunk/a.txt
  svn co -q "$U/branches/b" "$TD/wc"
  svn merge -q "$U/trunk" "$TD/wc"
  svn ci -q --username bob -m "Merge trunk" "$TD/wc"
  mkdir -p "$TD/old"
  cat > "$TD/old/svn" << EOF
#!/bin/sh
case " \$* " in *" log "*) "$(command -v svn)" "\$@" | sed -E 's/[[:space:]]*(prop|text)-mods="[^"]*"//g'; exit;; esac
exec "$(command -v svn)" "\$@"
EOF
  chmod +x "$TD/old/svn"
  PATH="$TD/old:$PATH" svn log --xml -v -r 5 "$U" > "$TD/old.xml"
  if grep -q 'prop-mods' "$TD/old.xml"; then fail "setup: the svn wrapper does not hide prop-mods"; fi
  PATH="$TD/old:$PATH" python3 -I "$HERE/svn_meta.py" "$U" "$TD/meta" --rev 5 > "$TD/meta.log" 2>&1 ||
    fail "svn_meta.py failed: $(tail -3 "$TD/meta.log" | tr '\n' ' ')"
  python3 -I - "$TD/meta/log.json" 2> "$TD/check.err" << 'EOF' || fail "log.json without prop-mods: $(cat "$TD/check.err")"
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
p5 = {p["path"]: p for p in d["5"]["paths"]}
p4 = {p["path"]: p for p in d["4"]["paths"]}
msg = []
if "/trunk" not in p5.get("/branches/b", {}).get("props", {}).get("svn:mergeinfo", ""):
    msg.append("r5 /branches/b has no svn:mergeinfo: %r" % p5.get("/branches/b"))
if "props" in p4.get("/trunk/a.txt", {}):
    msg.append("r4 /trunk/a.txt (content change only) got props: %r" % p4["/trunk/a.txt"])
if msg:
    sys.stderr.write("; ".join(msg)); sys.exit(1)
EOF
  grep -q 'WARN: .*prop-mods' "$TD/meta.log" || fail "no WARN that the server log has no prop-mods: $(tr '\n' ' ' < "$TD/meta.log")"
}

t_f25_merge() {   # F25: versioned trunk/.gitignore, then a merge into trunk (svn:mergeinfo set on trunk)
  mk repo; layout
  mucc alice "Versioned .gitignore" put "$(txt '*.o\n')" trunk/.gitignore put "$(txt 'a\n')" trunk/a.txt
  mucc alice "Branch b1" cp 2 trunk branches/b1
  mucc bob "Work on b1" put "$(txt 'b\n')" branches/b1/b.txt
  mucc bob "Merge b1" cp 4 branches/b1/b.txt trunk/b.txt propset svn:mergeinfo '/branches/b1:3-4' trunk
  mucc carol "Edit" put "$(txt 'a2\n')" trunk/a.txt
  mucc carol "Branch b2" cp 6 trunk branches/b2
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  need_file "$TD/remote.git" main .gitignore '*.o'
  need_file "$TD/remote.git" refs/heads/b2 .gitignore '*.o'
}

t_f25_ignoreprop() {   # F25: versioned .gitignore plus svn:ignore on the same folder, at once and later
  mk repo; layout
  mucc alice "Versioned .gitignore and svn:ignore" put "$(txt '*.o\n')" trunk/.gitignore put "$(txt 'a\n')" trunk/a.txt \
    mkdir trunk/sub put "$(txt '*.log\n')" trunk/sub/.gitignore propset svn:ignore '*.tmp' trunk
  mucc bob "Ignore in sub later" propset svn:ignore '*.bak' trunk/sub
  mucc carol "Drop svn:ignore" propdel svn:ignore trunk
  mucc carol "Edit" put "$(txt 'a2\n')" trunk/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A"
  expect_pass
  prj "$TD/out"
  need_file "$G" main .gitignore '*.o'
  need_file "$G" main sub/.gitignore '*.log'
}

t_f25_subdir() {   # F25: versioned .gitignore in a trunk subfolder and in a folder outside the layout, then folder properties
  mk repo; layout
  mucc alice "Sub .gitignore" mkdir trunk/src put "$(txt '*.o\n')" trunk/src/.gitignore put "$(txt 'a\n')" trunk/src/a.c \
    mkdir sandbox put "$(txt '*.pyc\n')" sandbox/.gitignore put "$(txt 'x\n')" sandbox/x.py
  mucc bob "Props on folders" propset owner bob trunk/src propset owner bob sandbox
  mucc carol "Edit" put "$(txt 'a2\n')" trunk/src/a.c
  s2g run "$TD/repo" "$TD/out" --authors "$A"
  expect_pass
  prj "$TD/out"
  need_file "$G" main src/.gitignore '*.o'
  need_file "$G" svn-outside-layout sandbox/.gitignore '*.pyc'
}

t_f51_lfs() {   # F51 + LFS: eol-style native text file stored with CRLF, above --lfs-above
  mk repo; layout
  local f i; f=$(mktemp -p "$TD/.files"); for i in $(seq 1 400); do printf 'line %d of the log\r\n' "$i"; done > "$f"
  mucc alice "Big CRLF text under eol-style native" put "$f" trunk/big.log propset svn:eol-style native trunk/big.log \
    put "$(txt 'a\n')" trunk/a.txt
  mucc bob "Edit" put "$(txt 'a2\n')" trunk/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A" --lfs-above 4KB
  expect_pass
  prj "$TD/out"
  is_pointer "$G" main big.log || fail "big.log did not go to LFS"
  grep -q 'big\.log' "$D/verify.json" || fail "verify.json does not list big.log under the EOL info category"
}

t_f30_region() {   # F30: a "branch" outside the layout (copy of trunk to /my-feature) merged back into trunk
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc alice "Feature copy in the wrong place" cp 2 trunk my-feature
  mucc bob "feature adds a file" put "$(txt 'f\n')" my-feature/f.txt
  mucc bob "feature edits" put "$(txt 'a-feat\n')" my-feature/a.txt
  svn co -q "$U/trunk" "$TD/wc"
  svn merge -q "$U/my-feature" "$TD/wc"
  svn ci -q --username carol -m "Merge my-feature into trunk" "$TD/wc"
  rlog 6 'A /trunk/f\.txt \(from /my-feature/f\.txt:5\)'
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  need_file "$TD/remote.git" main f.txt f
  logged 'SVN merges outside trunk/branches/tags' "verify does not list the merge from /my-feature as not representable"
}

t_f39_nested() {   # F39: feature -> release -> main merges whose merged history is over the limit are split at every depth
  export GIT_AUTHOR_NAME=a GIT_AUTHOR_EMAIL=a@test.invalid GIT_COMMITTER_NAME=a GIT_COMMITTER_EMAIL=a@test.invalid
  local n=0 i line plan l r
  c() { head -c 6000 /dev/urandom > "$TD/w/$1"; git -C "$TD/w" add "$1"; n=$((n + 1))
        GIT_COMMITTER_DATE="$((1600000000 + n)) +0000" git -C "$TD/w" commit -qm "c$n $1"; }
  m() { n=$((n + 1)); GIT_COMMITTER_DATE="$((1600000000 + n)) +0000" git -C "$TD/w" merge -q --no-ff --no-edit "$1"; }
  git init -q -b main "$TD/w"
  for i in 1 2 3; do c "m$i"; done
  git -C "$TD/w" checkout -qb release; c r1
  git -C "$TD/w" checkout -qb feature; for i in 1 2 3 4 5 6; do c "f$i"; done
  git -C "$TD/w" checkout -qb sub release; for i in 1 2 3 4; do c "s$i"; done
  git -C "$TD/w" checkout -q feature; m sub; c f7
  git -C "$TD/w" checkout -q release; m feature
  git -C "$TD/w" checkout -q main; c m4; m release; c m5
  git -C "$TD/w" branch -q -D feature sub
  git clone -q --bare "$TD/w" "$TD/repo.git"
  git -C "$TD/repo.git" tag t1 main~1
  plan=$(python3 -I "$HERE/push_plan.py" "$TD/repo.git" --limit 20000 2> "$TD/plan.err") ||
    fail "push_plan.py --limit 20000 refused a history whose commits are all far under the limit: $(tr '\n' ' ' < "$TD/plan.err")"
  echo "$plan" > "$TD/plan.txt"
  git init -q --bare "$TD/remote.git"
  git -C "$TD/remote.git" config receive.maxInputSize 23000
  while read -r line; do
    # shellcheck disable=SC2086
    git -C "$TD/repo.git" push -q "$TD/remote.git" $line > "$TD/push.log" 2>&1 ||
      fail "push of '$line' failed (over receive.maxInputSize?): $(tail -2 "$TD/push.log" | tr '\n' ' ')"
  done <<< "$plan"
  l=$(git -C "$TD/repo.git" for-each-ref --format='%(objectname) %(refname)' refs/heads refs/tags)
  r=$(git -C "$TD/remote.git" for-each-ref --format='%(objectname) %(refname)' refs/heads refs/tags)
  [ "$l" = "$r" ] || fail "remote refs differ: $(diff <(echo "$l") <(echo "$r") | tr '\n' ' ')"
  PUSH_LIMIT_BYTES=20000 "$HERE/push_github.sh" "$TD/repo.git" --remote "$TD/remote2.git" > "$TD/pg.log" 2>&1 ||
    fail "push_github.sh failed: $(grep -m2 -E 'ERROR|fatal' "$TD/pg.log" | tr '\n' ' ')"
  grep -q '^OK: all' "$TD/pg.log" || fail "push_github.sh did not confirm the remote refs"
}

t_r01_samename() {   # a branch and a tag with the same name: the tag moves with the rewritten history
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc alice "Branch feat" cp 2 trunk branches/feat
  mucc bob "Feat work" put "$(txt 'a2\n')" branches/feat/a.txt
  svn co -q "$U/trunk" "$TD/wc"
  svn merge -q "$U/branches/feat" "$TD/wc"
  svn ci -q --username carol -m "Merge feat into trunk" "$TD/wc"
  mucc alice "Branch 1.0" cp 5 trunk branches/1.0
  mucc alice "Tag 1.0" cp 5 trunk tags/1.0
  mucc bob "Trunk after the tag" put "$(txt 'a3\n')" trunk/a.txt
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  local R=$TD/remote.git
  [ "$(git -C "$R" cat-file -t refs/tags/1.0)" = tag ] || fail "refs/tags/1.0 is not an annotated tag"
  [ "$(git -C "$R" rev-list --merges main | wc -l)" = 1 ] || fail "the merge of feat into trunk is not a merge commit"
  local c
  for c in $(git -C "$R" rev-list refs/tags/1.0); do   # a stale pre-rewrite copy has no notes
    git -C "$R" notes --ref=svn show "$c" > /dev/null 2>&1 ||
      fail "tag 1.0 reaches commit ${c:0:9} that has no SVN note: a stale copy of the history from before the rewrite"
  done
  [ "$(git -C "$R" rev-parse 'refs/tags/1.0^{commit}^')" = "$(git -C "$R" rev-parse main~1)" ] ||
    fail "tag 1.0 is not based on trunk's rewritten r5 commit"
}

t_r02_semicolon() {   # ';' in a branch name: notes name the right SVN path and the full merge is a git merge
  mk repo; layout
  mucc alice "Add" put "$(txt 'a\n')" trunk/a.txt
  mucc alice "Branch fix;1" cp 2 trunk 'branches/fix;1'
  mucc bob "Fix work" put "$(txt 'a2\n')" 'branches/fix;1/a.txt'
  svn co -q "$U/trunk" "$TD/wc"
  svn merge -q "$U/branches/fix;1" "$TD/wc"
  svn ci -q --username carol -m "Merge fix;1 into trunk" "$TD/wc"
  s2g run "$TD/repo" "$TD/out" --authors "$A" --push-remote "$TD/remote.git"
  expect_pass
  local R=$TD/remote.git
  [ "$(git -C "$R" rev-list --merges main | wc -l)" = 1 ] || fail "the full merge of fix;1 is not a merge commit"
  git -C "$R" fetch -q "$R" 'refs/notes/*:refs/notes/*' 2> /dev/null || true
  git -C "$R" notes --ref=svn show 'refs/heads/fix;1' | grep -qx 'SVN-Path: /branches/fix;1/\?' ||
    fail "the note of fix;1's tip does not name /branches/fix;1: $(git -C "$R" notes --ref=svn show 'refs/heads/fix;1' | grep SVN-Path)"
}

# ---------- runner -------------------------------------------------------------------------------------
run_one() {   # run_one NAME: scenario NAME in $W/NAME; leaves .done ("status seconds") and .reason there
  local t=$1 TD=$W/$1 start=$SECONDS
  rm -rf "$TD"; mkdir -p "$TD/.files"
  ( cd "$TD" || exit 1
    TT_SUB=$BASH_SUBSHELL
    set -eE; set +o pipefail
    trap 'on_err $? $LINENO "$BASH_COMMAND"' ERR
    write_authors
    "$t"
  ) > "$TD/scenario.log" 2>&1
  echo "$? $((SECONDS - start))" > "$TD/.done"
}
show() {   # show NAME: print the result of a finished scenario
  local t=$1 TD=$W/$1 rc secs reason log
  read -r rc secs < "$TD/.done"
  if [ "$rc" = 0 ]; then echo "PASS $t"; PASSED=$((PASSED + 1)); return; fi
  reason="exit status $rc"
  [ -s "$TD/.reason" ] && reason=$(head -c 2000 "$TD/.reason" | tr '\n' ' ')
  echo "FAIL $t: $reason"
  log=$(cat "$TD/.log" 2> /dev/null || true)
  [ -f "$log" ] || log=$TD/scenario.log
  echo "    log: $log (${secs}s)"
  tail -n 12 "$log" | cut -c1-200 | sed 's/^/    | /'
  FAILS+=("$t")
}

mapfile -t ALL < <(declare -F | awk '$3 ~ /^t_/ {print $3}' | sort)
if [ $# -gt 0 ] && [[ $1 == t_* ]] && declare -F "$1" > /dev/null; then W=$(mktemp -d)
else W=${1:-$(mktemp -d)}; [ $# -gt 0 ] && shift; fi
SEL=()
for n in "$@"; do
  n=${n#t_}; hit=0
  for t in "${ALL[@]}"; do
    if [[ $t == "t_$n"* ]] && [[ " ${SEL[*]} " != *" $t "* ]]; then SEL+=("$t"); hit=1; fi
  done
  [ $hit = 1 ] || { echo "ERROR: no scenario matches '$n'; available: ${ALL[*]}"; exit 2; }
done
[ ${#SEL[@]} -gt 0 ] || SEL=("${ALL[@]}")
for t in svn svnadmin svnmucc svnlook svndumpfilter git git-lfs python3 gzip; do
  command -v "$t" > /dev/null || { echo "ERROR: missing tool: $t"; exit 2; }
done
mkdir -p "$W"; W=$(realpath "$W")
export GIT_CONFIG_GLOBAL=$W/global.gitconfig
: > "$GIT_CONFIG_GLOBAL"
# clones are checked with the LFS filters (smudge on checkout, clean in git status)
[ -n "$(git config --get filter.lfs.process)" ] ||
  { echo "ERROR: the git-lfs filters are not in the system git configuration: run 'git lfs install --system'"; exit 2; }
JOBS=${TT_JOBS:-4}
echo "trigger tests: ${#SEL[@]} scenarios in $W, $JOBS at a time"
for t in "${SEL[@]}"; do rm -rf "${W:?}/$t"; done
PASSED=0; FAILS=(); next=0; running=0
flush() {
  while [ $next -lt ${#SEL[@]} ] && [ -f "$W/${SEL[$next]}/.done" ]; do show "${SEL[$next]}"; next=$((next + 1)); done
}
for t in "${SEL[@]}"; do
  if [ $running -ge "$JOBS" ]; then wait -n; running=$((running - 1)); flush; fi
  run_one "$t" &
  running=$((running + 1))
done
while [ $running -gt 0 ]; do wait -n; running=$((running - 1)); flush; done
wait; flush
echo "TRIGGER TESTS: $PASSED passed, ${#FAILS[@]} failed${FAILS[*]:+ (${FAILS[*]})}"
[ ${#FAILS[@]} -eq 0 ]
