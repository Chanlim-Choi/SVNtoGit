# Review of the delivered toolkit

Two rounds. Round 1 (2026-11-05) reviewed the delivered code and fixed what a read-through found.
Round 2 (2026-11-06) hunted for failure scenarios systematically, fixed them, and tested everything
on Linux. Where the two disagree, round 2 is current.

# Round 2: failure hunt, fixes and Linux tests (2026-11-06)

## How

1. **Failure hunt.** Six reviewers looked for scenarios where the toolkit fails, each from a different
   angle: layout and rules, inputs and environment, verification, history rewriting, push, scale.
   A completeness critic then looked for whole classes they missed. Two independent verifiers
   checked every claim against the toolkit's code, the svn-all-fast-export source and small local
   SVN repositories. **66 scenarios were confirmed and 1 refuted.**
2. **Fixes.** A written spec set out each decision and the interfaces between scripts. Seven
   implementers then worked on separate files.
3. **Tests.** Everything ran in a Debian container: Subversion 1.14.5, git 2.47.3, git-lfs 3.6.1,
   Python 3.13 and svn-all-fast-export 1.0.18+git20221225. `trigger_tests.sh` is new: each
   scenario builds a small SVN repository with one failure trigger, migrates it and checks the
   result.
4. **Adversarial review.** Five reviewers tried to break the fixes. They found 9 problems, 4 of
   them major. All 9 were fixed and each now has a test.

## The rule now

Every confirmed scenario is either fixed, or stops the run with a message that names the SVN paths
and revisions involved and says what to do. Nothing in the list fails silently any more. Two items
are deferred (see the end).

## What changed for users

| Area | Before | Now |
|---|---|---|
| Platform | Started anywhere, then failed in confusing ways | Linux or WSL2 only; Windows is refused at start-up. Versions are checked: Python 3.7, git 2.24, git-lfs 3.0, Subversion 1.9, bash 4.4. A UTF-8 locale is required, and OUT must have POSIX permissions |
| Content outside the projects (F01, F02, F59) | Dropped silently in multi-project repositories. A file at the repository root aborted the conversion | Root files, files next to `trunk`, and files in `branches/` or `tags/` that belong to no branch go to `svn-outside-layout`. Content outside all projects goes to `<source>-other` when projects are detected automatically. With `--project`, it is printed as a WARN and listed in the report |
| History that crosses project borders (F04, F06c, F07) | Split silently | Pre-flight `STOP` naming the paths. `--allow-split-history` goes ahead anyway |
| Cherry-picks that add files (F30) | Recorded as full merges: git believed the whole source branch was merged | No merge commit. Merges come only from complete `svn:mergeinfo` ranges |
| Deleted or replaced tags and branches (F05, F10) | Left in `refs/backups/…`, never reported or pushed | Kept as `refs/tags/svn-deleted/tags/<t>@<rev>` and `refs/tags/svn-deleted/<b>@<rev>` |
| Retired project, trunk deleted (F09) | Could never pass verification | `main` is trunk's last state; verify compares it with that revision |
| Name collisions (F60) | Stopped with advice (`svn mv`) that could not help | Renamed automatically (`<name>-svn-r<rev>`), and every rename is printed |
| Verification (F03, F08, F25, F26, F51 and others) | Blocked correct migrations in common layouts | Layout-aware. Folders created later, nested and empty containers, versioned `.gitignore`, deleted outside folders and EOL-only differences now pass. `verify.json` is always written, and the report shows NOT VERIFIED when verify did not finish |
| Inputs (F14, F18, F19, F23, F24, F55, F66) | Several aborts | Control characters in messages, non-UTF-8 logs, logins with spaces, authors files with a BOM or in UTF-16, relative paths, compressed dumps and renumbered dumps all work. Renumbered dumps keep the original revision in each note |
| URL sources (F15, F16, F17, F58) | Aborted on locks, concurrent commits, old servers or a subfolder URL, with no reason given | Pinned to one revision; locks and old-server differences are ignored; a subfolder URL gives a clear ERROR |
| `--lfs-above` (F20, F53) | Inconsistent units; small versions of LFS files stayed raw, so clones showed them as modified | One parse, 1024-based, in bytes everywhere. Every version of a path that ever went to LFS is a pointer, checked with `git lfs fsck --pointers` |
| `--apply-gitattributes` (F31, F34, F61) | Overwrote the LFS rules; CRLF files showed as modified; failures were ignored | Merged with the LFS rules. `svn:eol-style` CRLF/CR become `-text`. Every step is checked |
| Push (F37, F38, F39, F41, F42, F43) | Batches counted refs and commits, not bytes; github.com hard-coded; false "refs differ" errors | `push_plan.py` keeps every push under 1 GiB of new data. Also: `HOST/OWNER/NAME` targets, `--resume`, an LFS size check, a workflow-scope check and GH013 guidance |
| git-svn fallback (F36, F64) | Placeholder authors, no verification, re-runs did not advance `main` | `--authors`, verify and report run, and `main` is rebuilt on every run |
| Scale (F28, F35, F45, F46, F47, F49, F50) | Slow steps that could fail outright | Streamed hashes, in-memory merge ancestry, parallel HEAD checks, bounded `gc` memory, one batched `svn proplist` per revision, cached externals, `--fast-import-timeout 0` |

## Found after the automated round, and fixed

- **A tag named like a branch kept the old history.** `git fast-export --branches --tags` silently
  drops an annotated tag whose name matches a branch, so `add_merges.py` never rewrote it. The tag
  kept a duplicate chain of pre-rewrite commits with no notes: 437 commits in the 5,000-revision
  test (the test data has a branch and a tag both named `UPPER`). This predates both rounds. Fixed
  by exporting full ref names. Test: `t_r01_samename`.
- **A `;` in a branch name** made `svn_notes.py` and `add_merges.py` read the wrong SVN path. The
  run failed loudly (missing notes, merge not represented). Fixed. Test: `t_r02_semicolon`.
- **Belt and braces for `.git` paths.** verify now fails if any path in the history has a `.git`
  component. The rules already skip them, but GitHub rejects the whole push if one slips through.

## Test results (final code)

Debian trixie container (versions above):

| Suite | Result |
|---|---|
| `py_compile` / `bash -n` on every file | clean |
| `layout_tests.sh` (two projects in one repository, no-layout repository) | PASS |
| `trigger_tests.sh` (64 scenarios: every fixable finding, the review issues, the two later fixes) | 64/64 PASS |
| `run_tests.sh 5000` (synthetic repository with edge cases, push, clone, `git lfs fsck`, `git fsck`) | PASS: 4,974/4,974 commit trees identical to SVN, 4,974/4,974 git commits mapped to an SVN revision, 125 SVN merges as git ancestry, 4,974 notes |

Not covered by tests: real GitHub (see below), performance at 100k+ revisions, F29 and F62 on real
hosts (checked through command stubs only).

## Still open

- **Deferred:**
  - F44: with `--samples all`, verify exports the full tree for every commit, so very large
    repositories take days. Use `--samples N`.
  - F48: the whole history is held in memory. This becomes a problem at roughly 10 million
    changed paths.
- **Not tested against real GitHub.** No access from here. The `gh` code paths ran only against a
  stub: auth status, repo view/create/edit and the credential helper. Organization rulesets are
  not pre-checked; a GH013 rejection prints guidance and the `--resume` command.
- **`push_plan.py`** estimates only blob sizes, with a 1 GiB target against GitHub's 2 GiB limit.
  Lower `PUSH_LIMIT_BYTES` if a push is still rejected.
- **`svn merge --record-only`** (mergeinfo but no file change) produces no converter commit, so it
  gets no merge commit. Unchanged.
- **Authors that cannot be mapped.** A non-UTF-8 `svn:author` cannot be mapped: verify fails and a
  WARN says to fix it in SVN. Logins containing `<`, `>` or ` = ` are not handled.
- **Untested edge case:** a property change on a `branches/` or `tags/` container in the same
  revision as a container delete or copy.

# Round 1: review of the delivered code (2026-11-05)

What was reviewed: every script and the test results in `test-results/`. The first commit in this
repository is the toolkit exactly as delivered. The second commit holds the fixes below. Fixes 1 and 3
were later replaced (round 2: automatic renames, and byte-exact `--lfs-above`), and the three
"Not changed" items were fixed in round 2 (F35, F39, F49).

## The test results

- The logs match `RESULTS.md`: 4,974 commits mapped, 4,973 of 4,974 trees identical, 123 merges
  as Git ancestry, 4,974 of 4,974 notes. Ref counts add up: 77 branches + 108 tags + 2 notes refs = 187.
- The single `FAIL` (`r1250`, `sandbox/experiment.txt`) is a false alarm from the verifier, not a
  migration error. The `prefix` handling now in `verify.py` (`compare(..., prefix=...)`) fixes it correctly.
- Overstated claim, now reworded: "every check passes on every commit" was never shown by one
  complete run. The shipped logs still end in `FAIL`.

## Fixes made

| # | File | Problem | Fix |
|---|---|---|---|
| 1 | `make_rules.py` | Two SVN branches that become the same Git name after cleanup (for example `release 2.0` and `release-2.0`) were silently merged into one Git branch, mixing their histories. An SVN branch named `master`/`main` would merge into or collide with trunk. Names like `a` and `a/b` cannot both be Git refs. | Detect all three cases before conversion and stop with a clear message naming the SVN paths to rename. |
| 2 | `make_rules.py`, `verify.py`, `migrate_gitsvn.sh` | Name cleanup missed some Git ref rules (`.lock` suffix, control characters, a component ending in `.`, `//`, a bare `@`). The `git svn` copy also lacked the `/.` rule, so it differed from the other two. | One cleanup function, identical in all three places. Output for every name in the test repository is unchanged. |
| 3 | `verify.py` + `svn2github.sh` | Files were only checked as LFS pointers above 1 MiB, so `--lfs-above` below 1 MB would give false `FAIL`s. | `svn2github.sh` passes the threshold to `verify.py` as `--lfs-above BYTES`. |
| 4 | `verify.py` | The GitHub size check built a `sh -c` string with the repository path in single quotes, which breaks on a path containing `'`. | Pipe between two `git` calls directly. |
| 5 | `push_github.sh` | "Repo is empty" was read from the API's `size` field, which lags behind pushes (a repo with a fresh README can report 0). | Ask the remote with `git ls-remote`. |
| 6 | `push_github.sh` | An HTTPS push relied on the user's own git credential setup, even though only `gh auth` was checked. | Repository-local credential helper `gh auth git-credential` (no global config change). |
| 7 | `svn2github.sh` | `--apply-gitattributes` committed a file with only comments when the suggestion had blank lines but no rules. | Ignore blank lines as well as comments. |
| 8 | `svn2github.sh` | For dump or URL sources, a root-layout project was named `svn-mirror`. | Name it after the source directory, dump file or URL. |
| 9 | `run_tests.sh` | A relative `WORKDIR` produced an invalid `file://` URL. | Make it absolute (as `layout_tests.sh` already did). |
| 10 | all Python | Files were read and written in the locale's encoding. Non-ASCII author names or messages could crash outside a UTF-8 locale. | Explicit UTF-8. |

## Not changed (noted for later)

- `add_merges.py` runs one `git merge-base` per unmerged source commit for each merge. This is
  correct but slow on very large repositories.
- `push_github.sh` batches by commit count and ref count, not by pack size. One branch with more
  than 2 GB of history that `main` doesn't share could still go over GitHub's per-push limit.
- `svn_meta.py` runs `svn proplist` once per path with property changes. Expect it to be slow
  against a remote server with 100k+ revisions.

## Verification of this review

- `py_compile` on every `.py` file and `bash -n` on every `.sh` file pass.
- Tested directly: name cleanup (same output from all three copies for the test names) and the
  collision check (exits for duplicate, reserved and `a`/`a/b` names; passes the test repo's names).
- **Not re-run:** `run_tests.sh` and `layout_tests.sh`. They need Linux with `svnadmin`,
  `svnrdump` and `svn-all-fast-export`, which this Windows machine does not have. Run both before
  relying on the fixes:

```bash
./run_tests.sh 5000
./layout_tests.sh
```
