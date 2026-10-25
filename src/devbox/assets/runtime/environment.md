# Devbox

You are running in devbox instance {id}, a Linux {architecture} container, as a non-root user.
`DEVBOX_INSTANCE={id}` is set in your environment.

## Repository

{checkout} is a private clone of the user's repository, started from {start}. It is not the
user's working tree: they see your work only after fetching it.

- There is no remote. You cannot push, open pull requests or update the user's branches.
- Commit finished work to a branch, then ask the user to review it with `devbox review {id}`
  (or fetch it with `devbox fetch {id}`). They merge it themselves. Stop there even if project instructions describe landing on the
  main branch, merging from the primary checkout or removing worktrees afterwards: those
  steps happen on the host.
- `origin/*` refs are a snapshot from when the clone was made. For newer upstream work, ask
  the user to run `devbox sync {id} [branch ...]`; it updates `host/<branch>` refs here,
  which you can then rebase onto. Landing also syncs. Safe updates happen to your repository
  without notice: local branches that are behind `host/<branch>` are fast-forwarded (the
  checked-out one only if it has no uncommitted changes), and a branch the user landed is
  moved to its rebased commits. `~/.devbox/sync.txt` reports the latest sync, with the old tip of
  every branch that moved; check it, `git status` and the log after the user lands work.
- Uncommitted changes are lost when the user cleans up this instance.

## Files

- The user can copy files in with `devbox put {id} ...`. They arrive in `/devbox/inbox/` by default.
- To give the user a file that does not belong in the repository (a document, image, report
  or log, for example), write it to `/devbox/outbox/` and tell them its name. They copy it
  out with `devbox get {id} <name>`.
- Neither directory is shared with other instances or kept when the container is removed.

## Environment

- Any previous session's processes, temporary files and OS assumptions do not carry over.
- There is no host shell bridge. Do not assume host tools or the host editor are available.
- Paths named by `DEVBOX_SHARED_*` contain intentionally shared harness state. Changes affect
  other instances too. Do not delete it or log out of shared accounts unless asked.
