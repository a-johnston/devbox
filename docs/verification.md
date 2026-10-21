# Verification

## Implemented scope

Pi-first host wrapper, with two new declarative contracts: project manifests and harness profiles.
The wrapper owns image composition, build/create locking, managed-container lifecycle and private
per-instance storage. The guest runtime owns tmux, command preparation and VM-local output masks.
No project-specific paths or Godot behavior are baked into the host wrapper.

## Verified on Apple silicon, 2026-10-20

- Python 3.15.0 development environment: 91 pytest tests, ruff lint/format checks and ty pass.
- Python 3.12 isolated environment: the same 91 tests pass without changing the development venv.
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

## Prepared launches and builder lifetime

The prepared-image path was checked with Apple's container 1.5, the real Rouge Legacy image and
personal `context-mode`/`pi-subagents` declarations. No model requests were issued.

| Operation | Observed wall time |
| --- | --- |
| First preparation plus launch, existing project layers cached | 171 seconds (169 preparing) |
| Explicit rebuild/refresh of final image | 68 seconds |
| Fresh agent from prepared image, builder stopped | 1.8–2.1 seconds through VM/runtime creation |
| Fresh agent through first rendered Pi UI, including extensions | 3.1 seconds total |

These are local smoke measurements, not a broad benchmark. Normal launch does not perform any of the
three builds/exports; the builder remains stopped throughout. Final images provide `fd`, with no
first-launch download message. A build with separate 4G builder capacity completed successfully and
stopped its builder afterward. A deliberately failing real builder lease also stopped it and preserved
the original exception. Unit tests cover preexisting builders, `--keep-builder`, startup cancellation,
cleanup failure, cross-project/store locking, failed preparation and image lookup errors.

Prepared npm/git trees are cloned inside each guest, not through the host mount. Both extensions load;
the 79 MiB package tree is writable as the agent. A mutation in one VM was absent in the other VM and
the original image template. Copied settings/auth and transcripts remain host-backed, not image inputs.
A fresh guest passed all 3,995 .NET tests without compiler warnings. Actual PTY attach and Ctrl-a/d
returned exit 0 to the host while preserving Pi's pane PID. The PTY harness must set a real TERM and
terminal size; a headless tool shell's defaults are not a representative terminal.

Idle host samples still showed approximately 1.24–1.26 GiB physical footprint per agent VM plus
15–20 MiB for its runtime helper; Pi itself was about 170 MiB RSS inside Linux. This change does **not**
eliminate VM overhead. The important steady-state difference is the absence of the multi-gigabyte
builder VM. Do not substitute host RSS or guest-available memory for host physical-footprint accounting.
All test VMs and their copied credentials/history are removed after checks; prepared images remain.

Image freshness is explicit after the first preparation. Changes to Containerfiles, build contexts,
Agentbox runtime/harness code or base tags require `agentbox build`; source edits do not. Unpinned
personal package sources refresh on an explicit build, not on each agent launch. Project-scoped Pi
packages remain behind Pi's trust flow. Native Linux backends are a separate follow-up.

## Still manual

- Confirm modified Enter keys, clipboard behavior, accent readability and outer/inner prefix behavior
  in the user's actual terminal and outer tmux setup; PTY checks cannot prove terminal integration.
- Keep the host Godot editor open during container work, then run a normal graphical playtest. These
  project checks also live in its `docs/tasks/playtests.md`.
- Provider-specific OAuth refresh/revocation behavior is not covered by local smoke tests; copied
  refresh credentials do not imply independently renewable logins. Verify normal model use on first
  use, and authenticate separately in the container when needed.
