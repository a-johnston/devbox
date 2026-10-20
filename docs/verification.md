# Verification

## Implemented scope

Pi-first host wrapper, with two new declarative contracts: project manifests and harness profiles.
The wrapper owns image composition, build/create locking, managed-container lifecycle and private
per-instance storage. The guest runtime owns tmux, command preparation and VM-local output masks.
No project-specific paths or Godot behavior are baked into the host wrapper.

## Verified on Apple silicon, 2026-10-20

- Python 3.15.0 development environment: 59 pytest tests, ruff lint/format checks and ty pass.
- Python 3.12 isolated environment: the same 59 tests pass without changing the development venv.
- Git fixture operations are isolated from commit-hook environment variables and personal Git config;
  explicit project discovery also ignores inherited repository-local Git scope.
- Wheel/sdist builds include runtime scripts, hidden build-context ignore file and Pi profile assets.
- The editable `uv tool` installation runs independently of the checkout's development venv.
- Local prek hooks explicitly invoke uv-managed ruff, ty and pytest, not managed hook environments.
- Apple's container 1.5 service/kernel initialized; real project/runtime/Pi images build through it.
- Guest identity is non-root; the preparation helper works, arbitrary sudo is denied, and control
  configuration is read-only. Host checkout paths are preserved.
- Real in-container Git worktree creation succeeds with Git LFS. Generated-directory mounts cover
  the new worktree, a guest-written cache sentinel is absent on the host, and Git sees no false LFS
  modifications. Only Git author identity is imported; specific worktree paths are trusted.
- Actual PTY attach/detach succeeds twice with Ctrl-a/d and preserves the same live Pi pane process.
- Actual fzf harness/container cancellation returns 130; attach and shell selection work; leaving
  either returns to the host. The inspection shell has the expected non-root identity.
- Selected host Pi config and credentials are copied, not shared read-write; context-mode and
  pi-subagents load inside Linux. No model requests were issued during verification.
- A Pi-exported local-shell session survives original-container deletion; resuming restores its
  content in a new, forked session. Both source and fork remain catalogued after kill-all.
- Managed-only kill-all leaves Apple's unrelated buildkit container alone. All test containers and
  the temporary source worktree were subsequently removed. Test histories/credential copies were
  explicitly purged separately, and the builder VM was stopped to release memory. Cached images and
  the container service remain available.

## Project integration verification

Rouge Legacy's .NET suite passes on macOS and inside Linux: 3,995 passed on each, no compiler warnings.
The complete project import finishes without native errors after provisioning Blender, NumPy and
its noninteractive editor preferences. The exact final base image also imports a representative
`.blend` scene in a fresh non-root VM with no preexisting editor settings or import cache; the scene's
imported resource exists and the native error output is clean.

Linux ARM import generates platform-dependent tracked `.import` changes. These were reviewed and
removed from the tooling change; no game texture-format policy was changed. The project docs explain
this distinction and that native import errors can accompany a zero process exit code.

## Still manual

- Confirm modified Enter keys, clipboard behavior, accent readability and outer/inner prefix behavior
  in the user's actual terminal and outer tmux setup; PTY checks cannot prove terminal integration.
- Keep the host Godot editor open during container work, then run a normal graphical playtest. These
  project checks also live in its `docs/tasks/playtests.md`.
- Provider-specific OAuth refresh/revocation behavior is not covered by local smoke tests; copied
  refresh credentials do not imply independently renewable logins. Verify normal model use on first
  use, and authenticate separately in the container when needed.
