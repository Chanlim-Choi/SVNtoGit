# svn2github

Migrate Subversion repositories to GitHub with complete history and metadata,
and prove it with an automatic comparison against SVN.

```bash
./svn2github.sh /path/to/svnrepo out --authors authors.txt --push myorg/myrepo
```

`SOURCE` can be a local repository directory, an `svnadmin dump` file (also `.gz`, `.bz2`,
`.xz`, `.zst`), or the repository **root** URL. A URL is mirrored with `svnrdump`, and the
mirror's full log, revision properties and file listing are compared with the server before
anything else runs. The account must be able to read the whole repository: the run stops when
the server hides paths from it (path-based read restrictions). Every step reads one fixed
revision, the youngest when the run starts, so commits made during a run are not migrated and
do not disturb it.

Nothing is pushed unless verification passes and every SVN user has a real identity.

## Requirements

Linux, or WSL2 with `OUTDIR` on the Linux file system (not under `/mnt/c`: verification needs
POSIX permission bits and symbolic links). Windows itself (Git Bash, Cygwin) is not supported,
because `svn-all-fast-export` has no Windows build. The script checks all of this at start-up
and stops with `ERROR:` and the reason:

| Tool | Minimum |
|---|---|
| bash | 4.4 |
| Python (`python3`) | 3.7 |
| git | 2.24 |
| git-lfs | 3.0 |
| Subversion (`svn`, `svnadmin`, `svnrdump`, `svnlook`) | 1.9 |
| `svn-all-fast-export` | a build with `--empty-dirs`, `--svn-ignore`, `--add-metadata-notes`, `--max-rev` (with `--fast-import-timeout` it never stops `git fast-import` early) |
| locale | a UTF-8 locale (`C.UTF-8`, `en_US.UTF-8` or any `*.utf8` in `locale -a`) |
| `gh` | only for `--push OWNER/NAME` |

`gzip`, `bzip2`, `xz` or `zstd` are needed only for compressed dump files.

## Options

| Option | |
|---|---|
| `--authors FILE` | `login = Full Name <email>` per SVN user; every user must be listed. `(no author) = …` covers revisions without an author. UTF-8 (with or without BOM) or UTF-16 with BOM; logins may contain spaces; `#` starts a comment line and is not allowed anywhere else |
| `--project ID` | migrate one project: `/` (root layout), `/projA`, or `NONE` (no trunk/branches/tags: the whole repository is `main`). By default every project found is migrated into its own repository |
| `--push OWNER/NAME` | create a private GitHub repo (via `gh`) and push. `HOST/OWNER/NAME` for another GitHub host (GitHub Enterprise); otherwise `$GH_HOST` or github.com. With several repositories: `OWNER/{name}` |
| `--push-remote URL` | push to any git remote (`{name}` allowed; a relative path is taken from the current directory) |
| `--lfs-above SIZE` | files of this size or more go to Git LFS across all history (default `50MB`; `off` disables it) |
| `--apply-gitattributes` | commit `.gitattributes` generated from `svn:eol-style` / `svn:mime-type` on `main` (only when verification passed) |
| `--samples N\|all` | how many historical commits to compare with SVN (default `all`) |
| `--allow-split-history` | migrate even when the pre-flight check stops because history crosses project borders (see below) |

**Sizes** are a whole number with an optional unit `K`, `M`, `G` or `T` and an optional `B` or
`iB`, in any case. All units are 1024-based: `50MB` = `50mb` = `50MiB` = 52,428,800 bytes,
`500K` = 512,000 bytes, a plain number is bytes. Anything else (`1.5G`, `50Mi`) is refused.

## Projects and content outside them

A project is a folder that holds `trunk` (with `branches` and `tags` next to it): `/` for a root
layout, `/projA`, `/libs/projB`. Before converting, a pre-flight check reads the whole history
(`OUTDIR/meta/preflight.txt`):

- **Content outside every project** (top-level folders and files, `/libs/notes.txt`, …) goes
  to an extra repository `OUTDIR/<source>-other` when the projects are detected automatically.
  The console names it (`content outside the projects (…) goes to repository …`). With
  `--project`, outside content is **not** migrated: each path is printed as `WARN:` and listed
  in the report.
- Inside a project, everything that is not trunk and not a branch or tag (files next to
  `trunk`, other folders, files directly in `branches/` or `tags/`) is kept on branch
  `svn-outside-layout`, with its paths relative to the project folder.
- **History across projects**: a `trunk` copied or moved into another project, a project folder
  created as a copy of another project, or a branch or tag created inside the folder of another
  branch or tag at a time when that one did not exist stop the run with `STOP:` lines naming the
  paths and revisions: the history would be cut apart. Re-run with `--allow-split-history` to
  migrate anyway (the history stays split), or choose projects with `--project`.
- `trunk`/`branches`/`tags` spelled in another case (`Trunk`, `TAGS`) are not a layout; they are
  reported with `WARN:` and their content is kept as ordinary folders.
- `.git` folders committed to SVN are never migrated (GitHub refuses them); each is printed
  with `WARN:` and listed in the report.

Several repositories need `OWNER/{name}` (or `{name}` in `--push-remote`), where the name is the
project path with `/` replaced by `-` (`/libs/projB` → `libs-projB`), the source name for `/`
and `NONE`, and `<source>-other` for content outside the projects.

## Branches and tags

Branch and tag roots come from the copy history (also nested, deleted, created by `svn mkdir`).
Names that git cannot store get a legal name; names that would collide (`release 2.0` and
`release-2.0`, a branch named `main`, `a` next to `a/b`) get a unique one with `-svn-r<rev>`.
Every rename is printed and listed in the report.

Deleted or replaced branches and tags are kept as tags, so their history is pushed too:

| SVN | Git |
|---|---|
| branch `x` deleted (or replaced) in r123 | `refs/tags/svn-deleted/x@123` |
| tag `v1.0` deleted (or replaced) in r123 | `refs/tags/svn-deleted/tags/v1.0@123` |
| `branches/`, `tags/` or the project folder replaced by an empty folder in r123 | the same for every branch/tag in it |
| `trunk` deleted at the end of the history | `main` is its last state (`WARN: trunk was deleted at r…`), plus `svn-deleted/master@<rev>` |
| `trunk` deleted, then re-created empty (`svn mkdir`) | `main` is an empty commit on top of its last state (`WARN: trunk was re-created empty at r…`) |

## What it does

| Step | Tool | Result |
|---|---|---|
| Mirror | `svnadmin`, `svnrdump` | local copy of a dump or URL; revision numbers of a dump with gaps are recorded |
| Metadata | `svn_meta.py` | authors, messages, revision properties, file properties |
| Authors | `authors_check.py` | `--authors` checked against every SVN login |
| Layout | `make_rules.py` | projects detected, pre-flight check, branch/tag roots found from copy history |
| Convert | `svn-all-fast-export` | history; `svn:ignore` → `.gitignore` (a `.gitignore` versioned in SVN keeps its SVN content); empty dirs kept |
| Notes | `svn_notes.py` | every commit gets `refs/notes/svn`: revision, path, author, date, revprops, changed paths, properties |
| Merges | `add_merges.py` | merge commits rebuilt from `svn:mergeinfo`; placeholder authors fixed |
| Large files | `lfs_fix.py` | big files moved to LFS in all history; notes re-keyed |
| Verify | `verify.py` | **every** commit's tree (content, exec bit, symlinks), message, author, date, note, every merge, every branch and tag, GitHub size limits |
| Report | `report.py` | `MIGRATION_REPORT.md`, `suggested.gitattributes`, paths that break Windows/macOS checkouts |
| Push | `push_github.sh` | LFS first, refs in pushes under GitHub's 2 GB/push (`push_plan.py`), notes, default branch; then every remote ref is compared with the local one |

**Dump files** must keep their original revision numbers. If they do not (gaps from
`svndumpfilter --drop-empty-revs` without `--renumber-revs`, or a dump that starts late),
`svnadmin load` renumbers them: the run prints `WARN:`, every note names the original number
(`SVN-Original-Revision`), and `OUTDIR/meta/revmap.json` lists them all. If `svnadmin load`
rejects old property values (non-LF line endings or non-UTF-8 text in `svn:log`), load the dump
yourself with `svnadmin load --bypass-prop-validation` (or `--normalize-props`) and pass the
repository directory as `SOURCE`.

**`--apply-gitattributes`** adds the suggested rules to the `.gitattributes` that `main` already
has (the per-file `filter=lfs` lines stay and keep precedence). The commit ignores the
operator's hooks and signing settings, and the run stops if it fails.

## What no tool can migrate

These SVN features have no Git equivalent. Each one is listed by name in the
report, and its data is kept in the notes:

- `svn:externals`: re-create as submodules or dependencies
- `svn:keywords`: not expanded by Git
- partial merges (cherry-picks), and merges from or into folders outside trunk/branches/tags:
  content is correct, but no merge is recorded
- path-based read permissions: use separate repositories (write rules map to rulesets/CODEOWNERS)
- SVN revision numbers: Git commit IDs differ; the revision number is in each note
- a single SVN commit to several branches becomes one Git commit per branch
- SVN locks

## Cut-over

Every run is a full conversion into a new `OUTDIR`, so the usual procedure is:
1. Do trial runs and fix what the report flags: authors, path conflicts, externals, pre-flight stops.
2. Freeze SVN commits (make it read-only).
3. Do the final run with `--push`.
4. Keep SVN read-only as an archive.

If a push fails (network, GitHub rules, token scopes), the converted repository stays in
`OUTDIR/<name>/project` (`OUTDIR/project` for a single repository). Fix the cause and continue
without converting again:

```bash
./push_github.sh out/project myorg/myrepo --resume
```

`migrate_gitsvn.sh SVN_URL OUTDIR [--authors FILE]` is a slower `git svn`-based fallback for when
`svn-all-fast-export` is not available. It checks `--authors` the same way, verifies and reports
like `svn2github.sh` (without LFS and without `svn-outside-layout`), and resumes with
`git svn fetch` when run again.

## Not handled yet

- **Very large repositories**: verification exports the whole tree of every commit, which can
  take days for 100,000 commits of a large tree. Use `--samples N` to check a random sample of
  historical commits (the branch and tag heads are always compared in full).
- The whole history is held in memory by the metadata and rewrite steps; tens of millions of
  changed paths need a host with a lot of RAM.
- Organization rulesets on GitHub are not checked before pushing: the migrating account needs
  to be allowed to bypass them for the duration of the push.

## Testing

```bash
./run_tests.sh 5000     # synthetic repo with edge cases, push, clone back, LFS fsck
./layout_tests.sh       # multi-project and non-standard layouts
./trigger_tests.sh      # one small repository per known failure scenario
```

## After cloning from GitHub

`git clone` does not fetch notes. To see the SVN metadata:

```bash
git fetch origin 'refs/notes/*:refs/notes/*'
git log --notes=svn
```
