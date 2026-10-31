# Encrypted local-work preservation — 2026-10-31

This branch is an archive. It preserves local branches, tags, worktree heads,
stash snapshots (including index/untracked parents), original changes and Git
metadata without publishing private credentials or applying unfinished changes.
It is not a production release or evidence of runtime acceptance.

The 32-byte recovery key is kept outside Git in the protected local preservation
directory. Retain that directory independently of the original checkout.
The key is never stored in this branch. Losing the key prevents recovery.

## Restore

Use Node.js 22 or newer and a new empty output directory:

```sh
node restore.mjs restore . /absolute/path/to/restore-key.bin /absolute/path/to/restored
tar -xzf /absolute/path/to/restored/history.tar.gz -C /absolute/path/to/restored
```

The helper verifies every part, the full encrypted checksum, authenticated
decryption and the restored checksum. It refuses to overwrite existing files.
Keep `private-local-files.tar.gz`, if present, private and extract it only to a
protected directory; it contains local configuration and diagnostic data.

`history.bundle`, when present, is incremental relative to the upstream branches
recorded in `refs.json`. Start with a clone of the original repository, fetch its
remote branches, and verify/import the bundle into a separate namespace:

```sh
git -C /absolute/path/to/repository bundle verify /absolute/path/to/restored/history.bundle
git -C /absolute/path/to/repository fetch /absolute/path/to/restored/history.bundle 'refs/*:refs/preserved/20261031/*'
```

The encrypted `refs.json` records original ref names, commit IDs, stash IDs and
linked worktree locations. Recover selected branches using those commit IDs;
restore a selected stash with `git stash store COMMIT_ID` only when needed.
If no bundle is present, the original commits are already covered by the upstream
branches and the encrypted inventory provides their exact names and IDs.

Linked worktrees still require their original common Git directory until they
are explicitly relocated or reconnected. This archive does not move them.
