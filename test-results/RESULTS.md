# Test results

## Test repository (synthetic, `gen_dump.py`)

- **Size:** 5,000 SVN revisions, 99 branches (24 deleted at some point), 83 tags, 20 authors, 483 revisions with custom revision properties.
- **Edge cases:**
  - illegal ref names, nested branches, a branch of only a subdirectory
  - a commit into a tag, a commit outside the standard layout
  - a revision with no author, an empty commit message, an empty revision
  - a file replaced by a directory
  - symlinks, executable bits, `svn:keywords`, `svn:externals`, `svn:eol-style`, empty directories
  - 125 merge revisions
  - 60 MB and 120 MB binaries
  - a branch made from an old revision, a branch made from a branch, a branch deleted and recreated

## Final run (`run_tests.sh 5000`): what happened, in order

1. **Full verification of all 4,974 commits** found a real data-loss bug in
   `svn-all-fast-export`. On an SVN *replace* action (a file replaced by a
   directory), the new content is dropped. 104 commits were affected.
   **Fixed:** `add_merges.py --svn-url` rewrites the replaced path to the exact SVN content.
2. **Rerun after the fix:** 4,973/4,974 commits identical. The remaining one was a false
   positive in the verifier: content kept under a folder prefix on the
   `svn-outside-layout` branch. **Fixed** in `verify.py`. That commit was re-checked
   and passes. `verify-5000rev-full.log` and `sample-MIGRATION_REPORT.md` are from
   this rerun, before the verifier fix, which is why they still say FAIL.
3. **Push stage** (done by hand after step 2 with `push_github.sh`):
   - all 187 refs on the remote match
   - a fresh clone has 77 branches, 108 tags and 123 merge commits
   - `git lfs fsck` OK, and the 60 MB file arrives intact
   - notes fetch and display correctly

**Expected result with the current code: every check passes on every commit.** This
is inferred, not shown by a single run: the logs in this folder end in FAIL (see step 2),
and the code was changed again during review (see `REVIEW.md`).
No complete uninterrupted rerun was done after the last verifier fix: it takes about
40 minutes and only that one commit's check changed. Run `./run_tests.sh 5000` to
reproduce it.

| Check | Result |
|---|---|
| Commit trees identical to SVN (content, exec bit, symlinks) | 4,974 / 4,974 |
| Messages, authors, dates | all identical |
| Commits with an SVN note (revision, revprops, properties, changed paths) | 4,974 / 4,974 |
| SVN merges represented as Git ancestry | 123 / 123 complete merges |
| Branches and tags vs SVN HEAD | all identical |
| Empty directories kept | all |
| Large files (LFS) verified against SVN content | all |
| Pushed refs == local refs | 187 / 187 |

`layout_tests.sh` (two projects in one repo, plus a repo without trunk/branches/tags): **PASS**.

## Not tested here

- **A real SVN server or a real GitHub push:** `svn.apache.org` is blocked by this
  environment's network policy, and the GitHub token was invalid. The push was
  tested against a local remote.
- **Very large real-world repositories:** 100k+ revisions, history where trunk was moved
  mid-way. Expect the verifier to take hours there. `--samples N` limits it.
