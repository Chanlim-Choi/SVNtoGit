# Review of the delivered toolkit (2026-11-05)

What was reviewed: every script and the test results in `test-results/`. The first commit in this
repository is the toolkit exactly as delivered. The second commit holds the fixes below.

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
