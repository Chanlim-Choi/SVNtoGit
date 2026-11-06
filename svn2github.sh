#!/usr/bin/env bash
# SVN -> GitHub migration, one command. Linux (or WSL2 on the Linux file system) only.
#
# usage: svn2github.sh SOURCE OUTDIR [options]
#   SOURCE   local SVN repository directory, an svnadmin dump file (also .gz/.bz2/.xz/.zst),
#            or the repository root URL (mirrored with svnrdump, then checked against the server)
# options:
#   --authors FILE          "login = Full Name <email>" per SVN user (required to push;
#                           may contain "(no author) = ..." for revisions without an author)
#   --project ID            migrate one project ("/" = root layout, "/projA", or NONE for a
#                           repository without trunk/branches/tags). Default: every project
#                           found, each into its own git repository OUTDIR/<name>, and content
#                           outside them into OUTDIR/<source>-other
#   --lfs-above SIZE        move files of SIZE or more to Git LFS (default 50MB; K/M/G/T with
#                           optional B or iB, any case, 1024-based; "off" to skip)
#   --apply-gitattributes   commit the generated .gitattributes (line endings, binaries) on main
#   --push OWNER/NAME       push when verification passes (HOST/OWNER/NAME for another GitHub
#                           host); with several projects use OWNER/{name} ({name} = project name)
#   --push-remote URL       push to any git remote instead ({name} allowed as above)
#   --samples N|all         historical commits to verify against SVN (default all)
#   --allow-split-history   migrate even when the pre-flight finds history that the projects
#                           split apart (a trunk or project folder copied to another project)
if [ -z "${BASH_VERSION:-}" ]; then echo "ERROR: run this script with bash 4.4 or newer"; exit 1; fi
set -euo pipefail
case $(uname -s) in
  MINGW*|MSYS*|CYGWIN*)
    echo "ERROR: Windows is not supported: run under Linux or WSL2 (svn-all-fast-export has no Windows build)"; exit 1;;
  Linux) ;;
  *) echo "WARN: $(uname -s) is not tested; this toolkit is made for Linux";;
esac
if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4) )); then
  echo "ERROR: bash 4.4 or newer is required (this is bash $BASH_VERSION)"; exit 1
fi
HERE=$(cd "$(dirname "$0")" && pwd)
[ $# -ge 2 ] || { echo "usage: svn2github.sh SOURCE OUTDIR [options]   (options: see the top of this script)"; exit 2; }
SRC=$1; OUT=$2; shift 2
AUTHORS=""; LFS_ABOVE=50MB; PUSH=""; PUSH_REMOTE=""; SAMPLES=all; PROJECT=""; GITATTR=0; SPLIT=0
while [ $# -gt 0 ]; do
  case $1 in
    --apply-gitattributes) GITATTR=1; shift;;
    --allow-split-history) SPLIT=1; shift;;
    --authors|--project|--lfs-above|--push|--push-remote|--samples)
      [ $# -ge 2 ] || { echo "ERROR: $1 needs a value"; exit 1; }
      case $1 in
        --authors) AUTHORS=$2;; --project) PROJECT=$2;; --lfs-above) LFS_ABOVE=$2;;
        --push) PUSH=$2;; --push-remote) PUSH_REMOTE=$2;; --samples) SAMPLES=$2;;
      esac
      shift 2;;
    *) echo "ERROR: unknown option $1"; exit 1;;
  esac
done

# ---------- start-up checks: platform, tools, versions, converter options, locale ----------
for t in svn svnadmin svnrdump svnlook git git-lfs svn-all-fast-export python3 realpath cmp diff locale; do
  command -v $t >/dev/null || { echo "ERROR: missing tool: $t"; exit 1; }
done
ver_ok() {   # $1 = text holding a version, $2 = minimum X.Y
  [[ $1 =~ ([0-9]+)\.([0-9]+) ]] || return 1
  local a=$((10#${BASH_REMATCH[1]})) b=$((10#${BASH_REMATCH[2]}))
  [[ $2 =~ ([0-9]+)\.([0-9]+) ]]
  (( a > BASH_REMATCH[1] || (a == BASH_REMATCH[1] && b >= BASH_REMATCH[2]) ))
}
V=$(python3 -I -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null) || V="that does not run"
ver_ok "$V" 3.7 || { echo "ERROR: Python 3.7 or newer is required as python3 (found: $V)"; exit 1; }
V=$(git --version 2>&1) || V=""
ver_ok "$V" 2.24 || { echo "ERROR: git 2.24 or newer is required (found: ${V:-none})"; exit 1; }
V=$(git lfs version 2>&1) || V=""
ver_ok "$V" 3.0 || { echo "ERROR: git-lfs 3.0 or newer is required (found: ${V:-none})"; exit 1; }
V=$(svn --version --quiet 2>&1) || V=""
ver_ok "$V" 1.9 || { echo "ERROR: Subversion 1.9 or newer is required (found: ${V:-none})"; exit 1; }
V=$(svn-all-fast-export --help 2>&1) || true
for o in --empty-dirs --svn-ignore --add-metadata-notes --max-rev; do
  [[ $V =~ (^|[[:space:]])$o([[:space:]]|$) ]] ||
    { echo "ERROR: this svn-all-fast-export has no $o option; install a current build"; exit 1; }
done
FIT=()   # without it, svn-all-fast-export stops git fast-import after 300 s at the end of a run
if [[ $V =~ (^|[[:space:]])--fast-import-timeout([[:space:]]|$) ]]; then FIT=(--fast-import-timeout 0)
else echo "WARN: this svn-all-fast-export has no --fast-import-timeout option; on very large repositories the last refs can be lost"; fi
V=$(locale -a 2>/dev/null) || V=""
LOC=""
for l in C.UTF-8 C.utf8 en_US.UTF-8 en_US.utf8; do
  if grep -qxF "$l" <<< "$V"; then LOC=$l; break; fi
done
[ -n "$LOC" ] || LOC=$(grep -m1 -iE '\.utf-?8$' <<< "$V") ||
  { echo "ERROR: no UTF-8 locale is installed (locale -a): install C.UTF-8 or en_US.UTF-8, otherwise svn fails on non-ASCII paths"; exit 1; }
export LC_ALL=$LOC

# ---------- arguments: absolute paths (before cd), sizes, project id ----------
if [ -e "$SRC" ]; then SRC=$(realpath "$SRC")
elif [[ $SRC != *://* ]]; then echo "ERROR: SOURCE $SRC: no such repository directory, dump file or URL"; exit 1; fi
if [ -n "$AUTHORS" ]; then
  [ -f "$AUTHORS" ] || { echo "ERROR: --authors $AUTHORS: no such file"; exit 1; }
  AUTHORS=$(realpath "$AUTHORS")
fi
# a local --push-remote (no scheme://, not scp-like host:path) is made absolute
if [ -n "$PUSH_REMOTE" ] && [[ $PUSH_REMOTE != *://* && ${PUSH_REMOTE%%/*} != *:* && $PUSH_REMOTE != /* ]]; then
  PUSH_REMOTE=$PWD/$PUSH_REMOTE
fi
lfs_bytes() {   # N, NK, NMB, NGiB ... (any case, 1024-based) -> bytes
  local s=${1^^} n m=1
  [[ $s =~ ^([0-9]{1,15})(([KMGT])(I?B)?|B)?$ ]] || return 1
  n=$((10#${BASH_REMATCH[1]}))
  case ${BASH_REMATCH[3]} in K) m=$((1 << 10));; M) m=$((1 << 20));; G) m=$((1 << 30));; T) m=$((1 << 40));; esac
  (( n > 0 && n <= (1 << 62) / m )) || return 1
  echo $((n * m))
}
if [ "${LFS_ABOVE,,}" = off ]; then LFS_BYTES=off
else LFS_BYTES=$(lfs_bytes "$LFS_ABOVE") ||
  { echo "ERROR: --lfs-above $LFS_ABOVE: use off or a size such as 50MB, 500K, 2GiB, 1048576 (K/M/G/T are 1024-based)"; exit 1; }
fi
[[ $SAMPLES =~ ^([0-9]+|all)$ ]] || { echo "ERROR: --samples $SAMPLES: use a number or all"; exit 1; }
EXPLICIT=0
if [ -n "$PROJECT" ]; then
  EXPLICIT=1
  case $PROJECT in
    NONE) ;;
    OTHER) echo "ERROR: --project OTHER: content outside the projects is migrated automatically when no --project is given"; exit 1;;
    *) PROJECT=/$(sed 's#^/*##; s#/*$##' <<< "$PROJECT");;   # "projA/" -> "/projA", "/" stays
  esac
fi

[ -e "$OUT" ] && { echo "ERROR: $OUT exists; use a new output directory"; exit 1; }
mkdir -p "$OUT"; OUT=$(realpath "$OUT"); cd "$OUT"
posix_problems() {   # verify.py reads exec bits and symlinks from files exported under OUT
  local d=$OUT/.posix-check p=""
  mkdir "$d" && : > "$d/f" || { echo "cannot create files"; return; }
  { chmod 644 "$d/f" && [ -n "$(find "$d/f" -perm 644)" ]; } 2>/dev/null || p+="chmod 644 leaves the file executable; "
  { chmod 755 "$d/f" && [ -n "$(find "$d/f" -perm 755)" ]; } 2>/dev/null || p+="chmod 755 does not make it executable; "
  { ln -s f "$d/l" && [ "$(readlink "$d/l")" = f ]; } 2>/dev/null || p+="symbolic links do not work; "
  rm -rf "$d"
  echo "${p%; }"
}
V=$(posix_problems)
if [ -n "$V" ]; then
  rm -rf "$OUT/.posix-check"; rmdir "$OUT" 2>/dev/null || true
  echo "ERROR: OUT is on a file system without POSIX permissions ($V): use a directory on a Linux file system (under WSL2 not /mnt/c)"
  exit 1
fi
step() { echo; echo "=== $* ==="; }
mkdir meta

# svn --xml output in a stable form: attribute order is random per run (APR hash order),
# text-mods/prop-mods are missing from pre-1.7 servers, and locks are not part of a dump.
# Side file (2nd argument): log = revisions without svn:date or svn:log (what path-based read
# restrictions hide), ls = locked paths.
CANON=$(cat <<'EOF'
import io, json, re, sys, xml.etree.ElementTree as ET
mode, side = sys.argv[1], (open(sys.argv[2], "w", encoding="utf-8") if len(sys.argv) > 2 else None)
class Src:   # svn ls --xml writes svn:author raw: control characters would make the XML invalid
    def __init__(self, f):
        self.f = f
    def read(self, n=-1):
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "?", self.f.read(n))
for _, e in ET.iterparse(Src(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"))):
    if mode == "log" and e.tag == "logentry":
        rec = [e.get("revision"), e.findtext("author"), e.findtext("date"), e.findtext("msg")]
        rec += sorted([p.get("name") or "", p.get("encoding") or "", p.text or ""] for p in e.findall("revprops/property"))
        rec += sorted([p.text or ""] + sorted("%s=%s" % a for a in p.attrib.items() if a[0] not in ("text-mods", "prop-mods"))
                      for p in e.findall("paths/path"))
        if side and (e.find("date") is None or e.find("msg") is None):
            side.write("r%s\n" % e.get("revision"))
    elif mode == "ls" and e.tag == "entry":
        rec = [e.get("kind"), e.findtext("name"), e.findtext("size"), [c.get("revision") for c in e.findall("commit")],
               e.findtext("commit/author"), e.findtext("commit/date")]
        if side and e.find("lock") is not None:
            side.write(e.findtext("name") + "\n")
    else:
        continue
    print(json.dumps(rec, ensure_ascii=False))
    e.clear()
EOF
)
canon_xml() { python3 -I -c "$CANON" "$@"; }
load_mirror() {   # stdin: dump stream; meta/load.log keeps the revision numbering (English: C locale)
  LC_ALL=C svnadmin load svn-mirror | awk '/based on original revision|------- Committed/' > meta/load.log
}
LOAD_HINT="If svnadmin rejected a property value (E125005: non-LF line endings or non-UTF-8 text in svn:log or
another svn: property), load the history yourself and pass the repository directory as SOURCE:
  svnadmin create REPO && svnadmin load --bypass-prop-validation REPO < DUMP     (or --normalize-props)
(for a URL, make DUMP first with: svnrdump dump URL > DUMP)"

step "mirror: local copy of the SVN repository"
if [ -f "$SRC/format" ] && [ -d "$SRC/db" ]; then
  SVNREPO=$SRC
  REV=$(svnlook youngest "$SVNREPO")
elif [ -d "$SRC" ]; then
  echo "ERROR: SOURCE $SRC is a directory but not an SVN repository (no format file and db/)"; exit 1
else
  svnadmin create svn-mirror; SVNREPO=$OUT/svn-mirror
  printf '#!/bin/sh\nexit 0\n' > svn-mirror/hooks/pre-revprop-change; chmod +x svn-mirror/hooks/pre-revprop-change
  if [ -f "$SRC" ]; then
    case $SRC in
      *.gz) DEC=(gzip -dc);; *.bz2) DEC=(bzip2 -dc);; *.xz) DEC=(xz -dc);; *.zst) DEC=(zstd -dcq);; *) DEC=(cat);;
    esac
    command -v "${DEC[0]}" >/dev/null || { echo "ERROR: $SRC is compressed: install ${DEC[0]} to read it"; exit 1; }
    "${DEC[@]}" "$SRC" | load_mirror || { echo "ERROR: loading $SRC into the mirror failed (see above)"; echo "$LOAD_HINT"; exit 1; }
    REV=$(svnlook youngest svn-mirror)
    # svnadmin load renumbers dumps with gaps or a late first revision: keep the original numbers
    python3 -I - meta/load.log meta/revmap.json <<'EOF' || { echo "ERROR: reading meta/load.log failed"; exit 1; }
import json, re, sys
m = {}
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    x = re.search(r"Committed new rev (\d+) \(loaded from original rev (\d+)\)", line)
    if x:
        m[x.group(1)] = int(x.group(2))
if m:
    json.dump(m, open(sys.argv[2], "w", encoding="utf-8"), indent=0)
    k = min(m, key=int)
    print(f"WARN: the dump's revision numbers have gaps or do not start at r1, so {len(m)} revisions got new "
          f"numbers in the mirror (e.g. original r{m[k]} is now r{k}); every note names the original revision "
          f"(SVN-Original-Revision) and meta/revmap.json lists them all")
EOF
  else
    ROOT=$(svn info --show-item repos-root-url "$SRC") && V=$(svn info --show-item url "$SRC") ||
      { echo "ERROR: cannot read $SRC (svn info failed, see above)"; exit 1; }
    [ "$V" = "$ROOT" ] || { echo "ERROR: SOURCE must be the repository root URL ($ROOT); use --project to migrate part of it"; exit 1; }
    REV=$(svn info --show-item revision "$ROOT@HEAD") || { echo "ERROR: cannot read the youngest revision of $ROOT"; exit 1; }
    [ "$REV" -gt 0 ] || { echo "ERROR: $ROOT has no revisions (r0); nothing to migrate"; exit 1; }
    echo "dumping $ROOT r0:$REV with svnrdump"
    svnrdump dump -q -r "0:$REV" "$ROOT" | load_mirror || { echo "ERROR: mirroring $ROOT failed (see above)"; echo "$LOAD_HINT"; exit 1; }
    [ "$(svnlook youngest svn-mirror)" = "$REV" ] || { echo "ERROR: the mirror does not end at r$REV"; exit 1; }
    echo "checking the mirror against $ROOT at r$REV (log with revision properties, file listing)"
    mkdir meta/mirror-check
    for side in server mirror; do
      u=$ROOT; [ $side = server ] || u=file://$SVNREPO
      { svn log --xml -v --with-all-revprops -r "$REV:1" "$u@$REV" | canon_xml log "meta/mirror-check/$side-hidden.txt" > "meta/mirror-check/$side-log.txt" &&
        svn ls -R --xml -r "$REV" "$u@$REV" | canon_xml ls "meta/mirror-check/$side-locks.txt" > "meta/mirror-check/$side-ls.txt"; } ||
        { echo "ERROR: reading the $side log or file listing failed (see above)"; exit 1; }
    done
    # the server hides paths an account may not read, consistently in the log, the listing and
    # the dump; only the missing svn:log (partly readable revision) or svn:date (unreadable) show it
    if [ -s meta/mirror-check/server-hidden.txt ]; then
      echo "ERROR: revisions without log message or date as this account sees them" \
           "($(head -n 5 meta/mirror-check/server-hidden.txt | tr '\n' ' ')- $(wc -l < meta/mirror-check/server-hidden.txt) in all):" \
           "the account cannot read every path of the repository (path-based read restrictions), so the migration" \
           "would silently miss content. Use an account that can read the whole repository, or migrate from a dump" \
           "made on the server (svnadmin dump) or from the repository directory. If those revisions really have no" \
           "log message or date, pass a dump made with 'svnrdump dump $ROOT > FILE' as SOURCE."
      exit 1
    fi
    for x in log ls; do
      if ! cmp -s "meta/mirror-check/server-$x.txt" "meta/mirror-check/mirror-$x.txt"; then
        diff "meta/mirror-check/server-$x.txt" "meta/mirror-check/mirror-$x.txt" | head -n 20 || true
        echo "ERROR: the mirror's $([ $x = log ] && echo log || echo file listing) differs from the server at r$REV" \
             "(first 20 differences above: < server, > mirror; full files in $OUT/meta/mirror-check)." \
             "Possible causes: path-based read restrictions (the account must be able to read the whole repository)," \
             "or revision properties changed during the run."
        exit 1
      fi
    done
    [ ! -s meta/mirror-check/server-locks.txt ] || echo "WARN: SVN locks are not migrated; locked on the server:" \
      "$(head -n 3 meta/mirror-check/server-locks.txt | tr '\n' ' ')- $(wc -l < meta/mirror-check/server-locks.txt) in all"
    rm -rf meta/mirror-check
    echo "mirror identical to the server at r$REV"
  fi
fi
[ "$REV" -gt 0 ] || { echo "ERROR: the repository has no revisions (r0); nothing to migrate"; exit 1; }
URL="file://$SVNREPO"
echo "SVN: $SVNREPO at r$REV (every step reads r$REV; later commits are not migrated)"

step "metadata: authors, messages, revision properties, file properties"
python3 -I "$HERE/svn_meta.py" "$URL" meta --rev "$REV" || { echo "ERROR: reading the SVN metadata failed (svn_meta.py, see above)"; exit 1; }
NOAUTHOR="Unknown <unknown@example.com>"
if [ -n "$AUTHORS" ]; then
  python3 -I "$HERE/authors_check.py" "$AUTHORS" meta || exit 1
  NOAUTHOR=$(cat meta/noauthor)
else
  echo "WARN: no --authors file; using PLACEHOLDER identities (login@example.com). Pushing is disabled."
fi

step "projects and pre-flight checks"
# name of the source repository: the directory, dump file (without extensions) or last URL segment,
# never the internal "svn-mirror" directory used for dumps and URLs
SRCNAME=$(basename "${SRC%/}")
for x in .gz .bz2 .xz .zst .dump .svndump; do SRCNAME=${SRCNAME%"$x"}; done
pname() { case $1 in /|NONE) echo "$SRCNAME";; OTHER) echo "$SRCNAME-other";; *) echo "${1#/}";; esac | sed 's#/#-#g; s/[^A-Za-z0-9._-]/-/g'; }
if [ $EXPLICIT = 1 ]; then PROJECTS=("$PROJECT")
else
  V=$(python3 -I "$HERE/make_rules.py" meta/log.json --detect) && [ -n "$V" ] ||
    { echo "ERROR: project detection failed (make_rules.py --detect, see above)"; exit 1; }
  mapfile -t PROJECTS <<< "$V"
fi
echo "projects: ${PROJECTS[*]}"
PF=(--preflight --migrate "$(IFS=,; echo "${PROJECTS[*]}")")
[ $EXPLICIT = 0 ] || PF+=(--explicit)
V=0; python3 -I "$HERE/make_rules.py" meta/log.json "${PF[@]}" > meta/preflight.txt || V=$?
[ $V = 0 ] || [ $V = 3 ] || { echo "ERROR: pre-flight check failed (make_rules.py --preflight, exit $V)"; exit 1; }
awk '/^STOP: /; /^WARN: / && ++w <= 30; END {if (w > 30) print "(" w - 30 " more WARN lines in meta/preflight.txt)"}' meta/preflight.txt
if [ $V = 3 ]; then
  if [ $SPLIT = 0 ]; then echo "Re-run with --allow-split-history to migrate anyway, or choose projects with --project."; exit 1; fi
  echo "WARN: --allow-split-history: continuing; the history named in the STOP lines above stays split between the repositories"
fi
if [ $EXPLICIT = 0 ] && grep -q '^OUTSIDE: ' meta/preflight.txt && [[ " ${PROJECTS[*]} " != *" / "* && " ${PROJECTS[*]} " != *" NONE "* ]]; then
  PROJECTS+=(OTHER)
  V=$(awk '/^OUTSIDE: / {sub(/^OUTSIDE: /, ""); if (++n <= 3) s = s (n > 1 ? ", " : "") $0}
           END {print s (n > 3 ? ", ... " n " paths" : "")}' meta/preflight.txt)
  echo "content outside the projects ($V) goes to repository $(pname OTHER)"
fi
declare -A SEEN=()
for P in "${PROJECTS[@]}"; do
  x=$(pname "$P")
  if [ ${#PROJECTS[@]} -gt 1 ] && [[ $x == meta || $x == svn-mirror ]]; then
    echo "ERROR: project $P would get the folder OUTDIR/$x, which this script uses itself; migrate it alone with --project $P (into its own OUTDIR)"; exit 1
  fi
  [ -z "${SEEN[$x]:-}" ] || { echo "ERROR: projects ${SEEN[$x]} and $P both get the name $x; migrate them one at a time with --project into different OUTDIRs"; exit 1; }
  SEEN[$x]=$P
done
if [ ${#PROJECTS[@]} -gt 1 ] && [ -n "$PUSH$PUSH_REMOTE" ] && [[ "$PUSH$PUSH_REMOTE" != *"{name}"* ]]; then
  echo "ERROR: ${#PROJECTS[@]} repositories (${PROJECTS[*]}); use OWNER/{name} (or a URL with {name}) to push each to its own repository, or choose one project with --project"
  exit 1
fi

apply_gitattributes() {   # $1 = git repo, $2 = new worktree dir; run in the project dir
  local GIT=$1 WT=$2 ga=$2/.gitattributes add x
  GIT_LFS_SKIP_SMUDGE=1 git -C "$GIT" -c core.autocrlf=false -c core.hooksPath=/dev/null worktree add -q "$WT" main || return 1
  add=$(grep -vE '^(#|[[:space:]]*$)' suggested.gitattributes | while IFS= read -r x; do
          [ -f "$ga" ] && grep -qxF -- "$x" "$ga" || printf '%s\n' "$x"; done)
  if [ -z "$add" ]; then echo "main's .gitattributes already has every suggested rule"; return 0; fi
  # suggested rules first: later lines win, so the per-file filter=lfs lines keep precedence
  { echo "# generated from svn:eol-style / svn:mime-type (see suggested.gitattributes)"; printf '%s\n' "$add"
    if [ -s "$ga" ]; then echo; cat "$ga"; fi; } > "$WT/.gitattributes.new" && mv "$WT/.gitattributes.new" "$ga" || return 1
  git -C "$WT" -c core.autocrlf=false add .gitattributes || return 1
  GIT_AUTHOR_NAME="SVN Migration" GIT_AUTHOR_EMAIL=migration@example.com \
  GIT_COMMITTER_NAME="SVN Migration" GIT_COMMITTER_EMAIL=migration@example.com \
    git -C "$WT" -c commit.gpgsign=false -c core.hooksPath=/dev/null commit -q --no-verify \
      -m "Add .gitattributes generated from svn:eol-style / svn:mime-type" || return 1
  git -C "$GIT" cat-file -e main:.gitattributes || return 1
  x=$(git -C "$WT" ls-files --eol | awk -F '\t' '$1 ~ /^i\/(crlf|mixed) / && $1 ~ /attr\/text/ && $1 !~ /attr\/text=auto/ {print $2}')
  [ -z "$x" ] || echo "WARN: files on main stored with CRLF line endings are now text in .gitattributes, so clones show them as modified until 'git add --renormalize .' is committed ($(wc -l <<< "$x")): $(head -n 5 <<< "$x" | tr '\n' ' ')"
  echo "committed .gitattributes on main"
}

migrate_project() {   # $1 = project id
  # NOTE: errexit does not apply inside a function used with ||, so every step checks itself
  local P=$1 NAME; NAME=$(pname "$P")
  local D="$OUT/$NAME"; [ ${#PROJECTS[@]} -gt 1 ] || D="$OUT"
  mkdir -p "$D" && cd "$D" || return 1
  if [ "$D" != "$OUT" ]; then ln -sfn "$OUT/meta" meta || return 1; fi
  local GIT="$D/project" EXCL=() x
  if [ $EXPLICIT = 0 ] && { [ "$P" = / ] || [ "$P" = OTHER ]; }; then   # subtrees migrated by other projects of this run
    for x in "${PROJECTS[@]}"; do [ "$x" = "$P" ] || [ "$x" = OTHER ] || EXCL+=("$x"); done
  fi
  step "[$NAME] rules from copy history (project $P)"
  local RA=(--project "$P" --layout-out "$D/layout.json")
  [ ${#EXCL[@]} = 0 ] || RA+=(--exclude "$(IFS=,; echo "${EXCL[*]}")")
  python3 -I "$HERE/make_rules.py" "$OUT/meta/log.json" project "${RA[@]}" > rules.txt 2> renames.txt ||
    { cat renames.txt; echo "ERROR: [$NAME] generating the conversion rules failed (make_rules.py, see above)"; return 1; }
  cat renames.txt

  step "[$NAME] convert (svn-all-fast-export, r1-r$REV)"
  svn-all-fast-export --identity-map "$OUT/meta/identity.map" --identity-domain example.com \
    --rules rules.txt --add-metadata-notes --svn-ignore --empty-dirs --stats --max-rev "$REV" "${FIT[@]}" \
    "$SVNREPO" > convert.log 2>&1 || { tail -30 convert.log; echo "ERROR: [$NAME] svn-all-fast-export failed (see $D/convert.log)"; return 1; }
  # layout facts: trunk's last lifetime and the SVN branches/tags that are deleted at r$REV
  local lay ta ts te tdate; lay=$(python3 -I - "$D/layout.json" "$OUT/meta/log.json" <<'PY'
import datetime, json, sys
l, log = json.load(open(sys.argv[1], encoding="utf-8")), json.load(open(sys.argv[2], encoding="utf-8"))
t = (l.get("trunk") or {}).get("lifetimes") or []
d = (log.get(str(t[-1][0])) or {}).get("date") or "" if t else ""
try:
    e = int(datetime.datetime.strptime(d[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp())
except ValueError:
    e = 0
print("trunk", int(bool(t) and t[-1][1] is None), t[-1][0] if t else 0, (t[-1][1] or 0) if t else 0, e)
for r in l.get("roots") or []:
    lt = r.get("lifetimes") or []
    if lt and lt[-1][1] is not None:
        print("dead", lt[-1][1], r["ref"])
PY
) || { echo "ERROR: [$NAME] reading $D/layout.json failed"; return 1; }
  read -r _ ta ts te tdate <<< "$(grep '^trunk ' <<< "$lay")"
  [ "$P" != NONE ] && [ "$P" != OTHER ] || ta=x
  if git -C "$GIT" rev-parse -q --verify refs/heads/master >/dev/null; then
    git -C "$GIT" branch -m master main || return 1
    # trunk gone through a replace of a folder above it without a copy: the converter keeps the ref
    [ "$ta" != 0 ] || [ "$te" = 0 ] || echo "WARN: trunk was deleted at r$te; main is its last state ([$NAME])"
  else
    x=$(git -C "$GIT" for-each-ref --format='%(refname)' refs/tags/backups/ refs/backups/ |
        sed -n -e 's#^refs/tags/backups/master@\([0-9][0-9]*\)$#\1 &#p' -e 's#^refs/backups/r\([0-9][0-9]*\)/heads/master$#\1 &#p' |
        sort -n | tail -n 1)
    if [ "$ta" = 1 ]; then   # trunk re-created by svn mkdir and still empty: the converter writes no commit
      sha=$(git -C "$GIT" mktree < /dev/null) &&
      sha=$(GIT_AUTHOR_NAME="SVN Migration" GIT_AUTHOR_EMAIL=migration@example.com GIT_AUTHOR_DATE="$tdate +0000" \
            GIT_COMMITTER_NAME="SVN Migration" GIT_COMMITTER_EMAIL=migration@example.com GIT_COMMITTER_DATE="$tdate +0000" \
            git -C "$GIT" commit-tree --no-gpg-sign ${x:+-p "${x#* }"} -m "trunk re-created empty in SVN r$ts" "$sha") &&
      git -C "$GIT" update-ref refs/heads/main "$sha" || return 1
      echo "WARN: trunk was re-created empty at r$ts (no files): main is an empty commit${x:+ on top of the last trunk state before r${x%% *}} ([$NAME])"
    else   # trunk deleted at the end: its last state (newest delete backup) becomes main
      [ -n "$x" ] || { echo "ERROR: [$NAME] the conversion produced no trunk history (no refs/heads/master; does $P/trunk exist?); see $D/convert.log"; return 1; }
      git -C "$GIT" update-ref refs/heads/main "${x#* }" || return 1
      echo "WARN: trunk was deleted at r${x%% *}; main is its last state ([$NAME])"
    fi
  fi
  git -C "$GIT" symbolic-ref HEAD refs/heads/main || return 1
  # refs the converter set aside when a branch/tag was deleted or replaced: keep them as tags
  local refs ref sha new; refs=$(git -C "$GIT" for-each-ref --format='%(objectname) %(refname)' refs/tags/backups/ refs/backups/) || return 1
  while read -r sha ref; do
    [ -n "$ref" ] || continue
    if [[ $ref =~ ^refs/tags/backups/(.+)$ ]]; then new=refs/tags/svn-deleted/${BASH_REMATCH[1]}
    elif [[ $ref =~ ^refs/backups/r([0-9]+)/heads/(.+)$ ]]; then new="refs/tags/svn-deleted/${BASH_REMATCH[2]}@${BASH_REMATCH[1]}"
    elif [[ $ref =~ ^refs/backups/r([0-9]+)/tags/(.+)$ ]]; then new="refs/tags/svn-deleted/tags/${BASH_REMATCH[2]}@${BASH_REMATCH[1]}"
    else echo "ERROR: [$NAME] unexpected converter ref $ref (not migrated; see $D/convert.log)"; return 1
    fi
    git -C "$GIT" update-ref "$new" "$sha" "" || { echo "ERROR: [$NAME] cannot keep $ref as $new (that ref exists)"; return 1; }
    git -C "$GIT" update-ref -d "$ref" || return 1
  done <<< "$refs"
  # branches/tags deleted by a replace (without a copy) of a folder above them: the converter only
  # looks at the new, empty folder and leaves their refs live (F05)
  while read -r x te ref; do
    [ "$x" = dead ] && sha=$(git -C "$GIT" rev-parse -q --verify "$ref") || continue
    case $ref in refs/heads/*) new="refs/tags/svn-deleted/${ref#refs/heads/}@$te";; *) new="refs/tags/svn-deleted/tags/${ref#refs/tags/}@$te";; esac
    if [ "$(git -C "$GIT" rev-parse -q --verify "$new")" != "$sha" ]; then
      git -C "$GIT" update-ref "$new" "$sha" "" || { echo "ERROR: [$NAME] cannot keep $ref (deleted in SVN at r$te) as $new (that ref exists)"; return 1; }
    fi
    git -C "$GIT" update-ref -d "$ref" || return 1
    echo "[$NAME] $ref: deleted in SVN at r$te (a folder above it was replaced), kept as $new"
  done <<< "$lay"

  step "[$NAME] SVN metadata as git notes (refs/notes/svn)"
  local NA=(); [ ! -f "$OUT/meta/revmap.json" ] || NA=(--revmap "$OUT/meta/revmap.json")
  python3 -I "$HERE/svn_notes.py" "$GIT" "$OUT/meta/log.json" "${NA[@]}" || { echo "ERROR: [$NAME] svn_notes.py failed"; return 1; }

  step "[$NAME] merge commits from svn:mergeinfo"
  TMPDIR=$D python3 -I "$HERE/add_merges.py" "$GIT" "$OUT/meta/log.json" merges.json --no-author "$NOAUTHOR" \
    --svn-url "$URL" --layout "$D/layout.json" || { echo "ERROR: [$NAME] add_merges.py failed"; return 1; }

  step "[$NAME] large files -> Git LFS"
  if [ "$LFS_BYTES" = off ]; then echo "LFS off" | tee lfs.log
  else
    x=$(git -C "$GIT" rev-list --objects --branches --tags | git -C "$GIT" cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' |
        awk -v lim="$LFS_BYTES" '$1 == "blob" && $2 + 0 >= lim + 0 {n++} END {print n + 0}') || { echo "ERROR: [$NAME] listing blobs failed"; return 1; }
    if [ "$x" -gt 0 ]; then
      python3 -I "$HERE/lfs_fix.py" "$GIT" "$LFS_BYTES" --paths-out "$D/lfs-paths.txt" > lfs.log 2>&1 ||
        { cat lfs.log; echo "ERROR: [$NAME] moving files to Git LFS failed"; return 1; }
      cat lfs.log
    else
      echo "no files of $LFS_ABOVE ($LFS_BYTES bytes) or more" | tee lfs.log
    fi
  fi
  x=$(nproc 2>/dev/null) || x=4; [ "$x" -le 8 ] || x=8
  git -C "$GIT" -c pack.windowMemory=256m -c pack.threads="$x" gc -q --aggressive --prune=now ||
    { echo "ERROR: [$NAME] git gc failed"; return 1; }

  step "[$NAME] verify against SVN r$REV"
  local VA=(--layout "$D/layout.json" --rev "$REV" --samples "$SAMPLES" --authors "$OUT/meta/authors.txt" --json verify.json)
  [ "$LFS_BYTES" = off ] || VA+=(--lfs-above "$LFS_BYTES")
  [ ! -f "$D/lfs-paths.txt" ] || VA+=(--lfs-paths "$D/lfs-paths.txt")
  local VERIFY=0
  python3 -I "$HERE/verify.py" "$URL" "$GIT" "$OUT/meta/log.json" "${VA[@]}" > verify.log 2>&1 || VERIFY=$?
  sed -n '/^git commits=/,$p' verify.log
  grep -q '^RESULT: ' verify.log || { tail -n 20 verify.log; echo "ERROR: [$NAME] verify.py stopped before its result (see $D/verify.log)"; VERIFY=1; }

  step "[$NAME] report"
  python3 -I "$HERE/report.py" "$URL" "$D" "$GIT" "$P" --rev "$REV" --layout "$D/layout.json" ||
    { echo "ERROR: [$NAME] report.py failed"; return 1; }
  if [ $GITATTR = 1 ]; then
    if [ $VERIFY != 0 ]; then echo "[$NAME] .gitattributes not committed: verification failed"
    elif ! grep -qvE '^(#|[[:space:]]*$)' suggested.gitattributes; then echo "[$NAME] no .gitattributes rules to commit"
    else
      local WT; WT=$(mktemp -d "$D/gitattributes.XXXXXX") || return 1
      if ! apply_gitattributes "$GIT" "$WT"; then
        git -C "$GIT" worktree remove --force "$WT" 2>/dev/null || true
        echo "ERROR: [$NAME] committing .gitattributes on main failed (see above)"; return 1
      fi
      git -C "$GIT" worktree remove --force "$WT" || return 1
    fi
  fi

  if [ -n "$PUSH$PUSH_REMOTE" ]; then
    [ $VERIFY = 0 ] || { echo "ERROR: [$NAME] NOT PUSHING: verification failed (see $D/verify.log)"; return 1; }
    [ -n "$AUTHORS" ] || { echo "ERROR: [$NAME] NOT PUSHING: real author identities required (--authors)"; return 1; }
    step "[$NAME] push"
    if [ -n "$PUSH_REMOTE" ]; then "$HERE/push_github.sh" "$GIT" --remote "${PUSH_REMOTE//\{name\}/$NAME}" || { echo "ERROR: [$NAME] push failed"; return 1; }
    else "$HERE/push_github.sh" "$GIT" "${PUSH//\{name\}/$NAME}" || { echo "ERROR: [$NAME] push failed"; return 1; }; fi
  fi
  echo "[$NAME] git repo: $GIT   report: $D/MIGRATION_REPORT.md"
  return $VERIFY
}

FAILED=()
for P in "${PROJECTS[@]}"; do
  migrate_project "$P" || FAILED+=("$P")
  cd "$OUT"
done
echo
if [ ${#FAILED[@]} -eq 0 ]; then echo "ALL PROJECTS PASSED VERIFICATION (${PROJECTS[*]})"
else echo "FAILED: ${FAILED[*]}"; exit 1; fi
