# Repository workflow

- Before starting work, inspect the working tree and synchronize `main` with `origin/main` using a safe fast-forward-only pull. Preserve any existing uncommitted user changes; never reset or overwrite them to synchronize.
- After completing and verifying requested changes, commit the task's changes and push `main` to `origin/main` automatically.
- Never use force push, including `--force` or `--force-with-lease`.
- If synchronization, authentication, commit, or push fails, stop and report the complete error instead of rewriting history or discarding changes.
