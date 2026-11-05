# svn2github

Migrate Subversion repositories to GitHub with complete history and metadata,
and prove it with an automatic comparison against SVN.

```bash
./svn2github.sh /path/to/svnrepo out --authors authors.txt --push myorg/myrepo
```

`SOURCE` can be a local repository, an `svnadmin dump` file, or a URL. A URL is
mirrored with `svnrdump`, and the mirror's full log, revision properties and file
listing are compared with the server before anything else runs.

Nothing is pushed unless verification passes and every SVN user has a real identity.

## Options

| Option | |
|---|---|
| `--authors FILE` | `login = Full Name <email>` per SVN user; every user must be listed. `(no author) = …` covers revisions without an author |
| `--project PREFIX` | migrate one project: `/` (root layout), `/projA`, or `NONE` (no trunk/branches/tags). By default every project found is migrated into its own repository |
| `--push OWNER/NAME` | create a private GitHub repo (via `gh`) and push. With several projects: `OWNER/{name}` |
| `--push-remote URL` | push to any git remote (`{name}` allowed) |
| `--lfs-above SIZE` | files over this size go to Git LFS across all history (default `50MB`, `off`) |
| `--apply-gitattributes` | commit `.gitattributes` generated from `svn:eol-style` / `svn:mime-type` |
| `--samples N\|all` | how many historical commits to compare with SVN (default `all`) |

## What it does

| Step | Tool | Result |
|---|---|---|
| Metadata | `svn_meta.py` | authors, messages, revision properties, file properties |
| Layout | `make_rules.py` | projects detected; branch/tag roots found from copy history (nested, deleted, illegal names) |
| Convert | `svn-all-fast-export` | history; `svn:ignore` → `.gitignore`; empty dirs kept |
| Notes | `svn_notes.py` | every commit gets `refs/notes/svn`: revision, path, author, date, revprops, changed paths, properties |
| Merges | `add_merges.py` | merge commits rebuilt from `svn:mergeinfo`; placeholder authors fixed |
| Large files | `lfs_fix.py` | big files moved to LFS in all history; notes re-keyed |
| Verify | `verify.py` | **every** commit's tree (content, exec bit, symlinks), message, author, date, note, every merge, every branch and tag, GitHub size limits |
| Report | `report.py` | `MIGRATION_REPORT.md`, `suggested.gitattributes`, paths that break Windows/macOS checkouts |
| Push | `push_github.sh` | LFS first, refs in batches (under GitHub's 2 GB/push), notes, default branch; then every remote ref is compared with the local one |

Requirements: `svn`, `svnadmin`, `svnrdump`, `git`, `git-lfs`, `svn-all-fast-export`,
Python 3, coreutils. `migrate_gitsvn.sh` is a slower `git svn`-based fallback for when
`svn-all-fast-export` is not available.

## What no tool can migrate

These SVN features have no Git equivalent. Each one is listed by name in the
report, and its data is kept in the notes:

- `svn:externals`: re-create as submodules or dependencies
- `svn:keywords`: not expanded by Git
- partial merges (cherry-picks): content is correct, but no merge is recorded
- path-based read permissions: use separate repositories (write rules map to rulesets/CODEOWNERS)
- SVN revision numbers: Git commit IDs differ; the revision number is in each note
- a single SVN commit to several branches becomes one Git commit per branch

## Cut-over

Every run is a full conversion that takes minutes, so the usual procedure is:
1. Do trial runs and fix what the report flags: authors, path conflicts, externals.
2. Freeze SVN commits (make it read-only).
3. Do the final run with `--push`.
4. Keep SVN read-only as an archive.

## Testing

```bash
./run_tests.sh 5000     # synthetic repo with edge cases, push, clone back, LFS fsck
./layout_tests.sh       # multi-project and non-standard layouts
```

## After cloning from GitHub

`git clone` does not fetch notes. To see the SVN metadata:

```bash
git fetch origin 'refs/notes/*:refs/notes/*'
git log --notes=svn
```
