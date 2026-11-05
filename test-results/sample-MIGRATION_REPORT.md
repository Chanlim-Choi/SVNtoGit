# SVN → Git migration report

**Verification: FAIL** — failing checks: tree

| | |
|---|---|
| SVN revisions | 5000 |
| Git commits | 5411 |
| Branches | 77 |
| Tags | 108 |
| Merge commits rebuilt from svn:mergeinfo | 123 |
| Notes refs | refs/notes/commits, refs/notes/svn |

- git commits=5411  mapped to SVN=4974  svn revisions=5000  revisions represented=4973
- historical commits: 4973/4974 trees identical to SVN (ALL commits)
- merges: 123 SVN merges are git ancestry, 0 partial (cherry-pick) merges
- notes: 4974/4974 commits have an svn note (483 with custom revprops)

Every commit carries a note in `refs/notes/svn` with its SVN revision, path, original author, date, custom revision properties, changed paths and SVN properties. View with `git log --notes=svn`; push with `git push origin 'refs/notes/*'`.

## Needs a human decision

### svn:externals (157) — no git equivalent

Re-create as git submodules, subtrees, or a package dependency. Definitions at SVN HEAD:

- `/branches/UPPER`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-10`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-13`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-15`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-17`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-18`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-2`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-20`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-21`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-24`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-25`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-26`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-28`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-29`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-32`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-33`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-34`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-35`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-37`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-38`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-39`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-40`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-41`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-44`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-46`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-47`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-48`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-49`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-50`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-51`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-53`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-54`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-55`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-56`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-57`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-58`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-59`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-6`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-61`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-62`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-64`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-67`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-68`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-69`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-7`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-70`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-71`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-72`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-73`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-74`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-75`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-76`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-77`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-78`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-79`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-80`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-81`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-82`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-83`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-84`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-85`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-86`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-87`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-88`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-89`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-9`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-90`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature-91`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/feature..bad`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/hotfix/1.x`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/maint-old`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/recreated`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/release%202.0`: `vendor https://example.com/svn/vendor/trunk`
- `/branches/sub-of-hotfix`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/UPPER`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/release-1.0`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v1.0~rc1`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v1.9`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v10.7`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v11.9`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v12.5`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v13.8`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v14.6`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v18.8`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v19.4`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v2.3`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v20.9`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v21.6`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v22.8`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v23.5`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v24.7`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v25.6`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v26.8`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v27.0`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v28.9`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v29.6`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v3.6`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v30.6`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v31.2`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v32.2`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v33.6`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v34.1`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v35.7`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v37.8`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v38.2`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v39.5`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v4.7`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v40.4`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v41.7`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v42.5`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v43.9`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v44.1`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v45.6`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v46.0`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v47.5`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v48.9`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v49.4`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v5.3`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v50.1`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v51.1`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v52.9`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v53.0`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v54.2`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v55.5`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v56.4`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v57.7`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v58.9`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v59.4`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v6.8`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v60.3`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v61.0`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v62.4`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v63.9`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v64.7`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v65.2`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v66.5`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v67.2`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v68.9`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v69.0`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v7.8`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v70.4`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v71.3`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v72.5`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v73.3`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v74.8`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v75.3`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v76.0`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v77.8`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v78.5`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v79.6`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v8.7`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v80.6`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v81.5`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v82.1`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v83.7`: `vendor https://example.com/svn/vendor/trunk`
- `/tags/v9.5`: `vendor https://example.com/svn/vendor/trunk`
- `/trunk`: `vendor https://example.com/svn/vendor/trunk`

### svn:keywords (0 files on trunk) — not expanded by git

Files contain literal `$Id$`/`$Revision$` text. Usually fine; otherwise drop the keywords or use a `filter` in `.gitattributes`.


### Partial merges / cherry-picks (0) — git cannot represent

Content is correct; git just does not record them as merges. The SVN merge record stays in the notes.

- none

### Paths that break checkouts on Windows/macOS (0)

Git stores these fine, but a clone on a case-insensitive (macOS/Windows) or Windows file system will fail or silently lose files. Rename them in SVN before the final run, or on main after.

- none

## Done automatically — for your information

### Branch/tag names changed (illegal in git)

- `branches/feature..bad -> feature.bad`
- `branches/release 2.0 -> release-2.0`
- `tags/v1.0~rc1 -> v1.0-rc1`

### Deleted SVN branches/tags kept (25)

Their full history is preserved under `svn-deleted/<name>@<last revision>`.

- `refs/tags/svn-deleted/feature-11@1901`
- `refs/tags/svn-deleted/feature-12@925`
- `refs/tags/svn-deleted/feature-14@3501`
- `refs/tags/svn-deleted/feature-16@1986`
- `refs/tags/svn-deleted/feature-19@1647`
- `refs/tags/svn-deleted/feature-1@1617`
- `refs/tags/svn-deleted/feature-22@1355`
- `refs/tags/svn-deleted/feature-23@1498`
- `refs/tags/svn-deleted/feature-27@4842`
- `refs/tags/svn-deleted/feature-30@3471`
- `refs/tags/svn-deleted/feature-31@3467`
- `refs/tags/svn-deleted/feature-36@2357`
- `refs/tags/svn-deleted/feature-3@1046`
- `refs/tags/svn-deleted/feature-42@4411`
- `refs/tags/svn-deleted/feature-43@2884`
- `refs/tags/svn-deleted/feature-45@3433`
- `refs/tags/svn-deleted/feature-4@495`
- `refs/tags/svn-deleted/feature-52@3373`
- `refs/tags/svn-deleted/feature-5@412`
- `refs/tags/svn-deleted/feature-60@4503`
- `refs/tags/svn-deleted/feature-63@4010`
- `refs/tags/svn-deleted/feature-65@4324`
- `refs/tags/svn-deleted/feature-66@4114`
- `refs/tags/svn-deleted/feature-8@697`
- `refs/tags/svn-deleted/recreated@3252`

### Paths outside trunk/branches/tags

Kept on branch `svn-outside-layout`.

### Large files moved to Git LFS

- see lfs.log

Empty SVN directories are kept with an empty `.gitignore` placeholder (`--empty-dirs`); `svn:ignore` is converted to `.gitignore` throughout history (`--svn-ignore`).

### Line endings / binary flags

2 files had `svn:eol-style` or a binary `svn:mime-type`. Git does not enforce these by itself; review and commit `suggested.gitattributes` as `.gitattributes`.

## Cannot be migrated (by design of git)

- **Path-based read permissions (SVN authz)**: anyone who can read a GitHub repo reads all of it. Read-restricted paths must become separate repositories. Write restrictions map to GitHub rulesets / CODEOWNERS.
- **Revision numbers**: git commits get new IDs; the SVN revision is in each commit's note.
- **One SVN commit touching several branches** becomes one git commit per branch.
