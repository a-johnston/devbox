# Phase 1: Secrets from the Environment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harness secrets come from the host environment instead of the macOS Keychain.

**Architecture:** `App.new` keeps passing declared secrets only to the harness process (`exec --env NAME`), but reads their values from `os.environ` instead of `keychain.load`. `keychain.py` and the `agentbox secret` subcommand are deleted. The Claude profile stops copying `.credentials.json`.

**Tech Stack:** Python 3.12+, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-10-21-linux-backend-design.md` (Phase 1)

## Global Constraints

- Python 3.12 compatible; run `uv run pytest` after every source change and `uv run prek run --all-files` before asking to land.
- Work on a new branch in `.worktrees/env-secrets` (`git worktree add .worktrees/env-secrets -b env-secrets`, then `uv sync --locked`).
- Secret values never appear in command arguments, container configuration, images or saved state.
- `--no-config` forwards no secrets.
- A declared secret that is unset or empty is skipped silently.

## Review Focus

- A declared secret set to the empty string: not forwarded, no notice for it (Task 1 test).
- Undeclared host variables (e.g. `AWS_SECRET_ACCESS_KEY`) are not forwarded just because they exist (Task 1 test).
- `--no-config` with the variable set: nothing forwarded, no notice (Task 1 test).
- The value appears in no `engine.calls` argument and no file under the instance directory (Task 1 test, kept from the old suite).
- `agentbox secret` is gone from the parser, so old muscle memory fails with argparse's "invalid choice" rather than a traceback (Task 1 test).

---

### Task 1: Forward declared secrets from the environment

**Files:**
- Modify: `src/agentbox/app.py` (import at line 14; secrets block in `App.new`, lines ~173-176)
- Modify: `src/agentbox/cli.py` (import at line 20, `secret` parser lines ~123-127, `secret` dispatch lines ~157-171, `getpass` import, its only user)
- Delete: `src/agentbox/keychain.py`
- Modify: `tests/test_secrets.py` (rewrite)
- Modify: `README.md` (usage list line 43, "Harness config and credentials" section, `secrets` bullet under "Additional harnesses")

**Interfaces:**
- Produces: `environment_secrets(harness: Harness) -> dict[str, str]` in `src/agentbox/app.py`, which returns `{name: os.environ[name]}` for each name in `harness.secrets` that is set and nonempty, in declaration order.

- [ ] **Step 1: Rewrite `tests/test_secrets.py`**

Drop every test that uses `keychain` or the `secret` command (`test_store_passes_value_on_stdin`, `test_store_rejects_values_that_could_escape_the_command`, `test_secret_command_reads_stdin_without_a_terminal`, `test_secret_command_requires_declared_secrets`). Keep `test_invalid_secret_names` and `test_claude_declares_its_oauth_token` as they are. Replace the `keychain` import with `from agentbox.app import App, environment_secrets` and remove unused imports (`io`, `subprocess`, `Any`, `dispatch`). New or changed tests, all using the existing `secretive` fixture (`secrets=("HARNESS_TOKEN",)`):

```python
def test_secrets_reach_only_the_harness_process(project, secretive, tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_TOKEN", "s3cret")
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    id = app.new(project, secretive, no_config=False)
    startup = engine.calls[-1]
    assert startup[:5] == ("exec", "--user", "agent", "--env", "HARNESS_TOKEN")
    assert engine.env == {"HARNESS_TOKEN": "s3cret"}
    assert not any("s3cret" in arg for call in engine.calls for arg in call)
    home = app.store.directory(id)
    assert not any("s3cret" in p.read_text() for p in home.rglob("*") if p.is_file())

def test_no_config_skips_secrets(project, secretive, tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_TOKEN", "s3cret")
    engine = FakeEngine()
    App(engine, Store(tmp_path / "state")).new(project, secretive, no_config=True)
    assert "--env" not in engine.calls[-1][:5]

@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_secrets_are_skipped(secretive, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("HARNESS_TOKEN", value)
    assert environment_secrets(secretive) == {}

def test_only_declared_secrets_are_forwarded(secretive, monkeypatch):
    monkeypatch.setenv("HARNESS_TOKEN", "s3cret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "other")
    assert environment_secrets(secretive) == {"HARNESS_TOKEN": "s3cret"}

def test_secret_command_is_gone():
    with pytest.raises(SystemExit):
        parser().parse_args(["secret", "claude"])
```

(Add type annotations matching the file's existing style.)

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_secrets.py -v`
Expected: collection error, `ImportError: cannot import name 'environment_secrets'`

- [ ] **Step 3: Implement**

In `app.py`, add `environment_secrets` next to `git_identity`, and in `App.new` replace `keychain.load(harness)` with `environment_secrets(harness)`. Keep the existing comment ("Only the harness process receives these…"), and change the notice to `"Forwarding credentials from environment: " + ", ".join(secrets)`. Remove the `keychain` import. In `cli.py`, remove the `secret` subparser, its dispatch branch, the `keychain` import and the `getpass` import. Delete `src/agentbox/keychain.py`.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest`
Expected: all pass. `grep -rn keychain src tests` prints nothing.

- [ ] **Step 5: Update the README**

- Usage block: remove the `agentbox secret claude` line.
- "Harness config and credentials": replace the `claude setup-token` / `agentbox secret claude` block with exporting the token, e.g.

  ```sh
  claude setup-token                      # prints a token valid for a year
  set -Ux CLAUDE_CODE_OAUTH_TOKEN …       # fish; or export it from your shell profile
  ```

  Keep the sentence about the token reaching only the harness process's environment. Add that wrapping the launch with a password-manager CLI (e.g. `op run -- agentbox new claude`) avoids keeping it in the shell environment, and that `--no-config` skips forwarding. Remove "`agentbox secret claude --remove` deletes it".
- "Additional harnesses" `secrets` bullet: "environment variable names forwarded from the host environment to the harness process when set".

- [ ] **Step 6: Lint, then commit**

Run: `uv run prek run --all-files` (expected: all hooks pass)

```bash
git add -A src tests README.md
git commit -m "Forward harness secrets from the host environment

Drop the macOS Keychain integration and the secret subcommand.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2: Stop copying Claude's host login

**Files:**
- Modify: `src/agentbox/assets/harnesses/claude/profile.toml` (lines 11-15)
- Modify: `README.md` (Claude paragraph in "Harness config and credentials")
- Test: `tests/test_claude.py`

**Interfaces:**
- Consumes: nothing from Task 1, apart from the README wording it settled.

- [ ] **Step 1: Write the failing test**

```python
def test_claude_never_copies_the_host_login() -> None:
    assert ".credentials.json" not in Harness.load(ASSETS / "harnesses/claude").seed_files
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_claude.py::test_claude_never_copies_the_host_login -v`
Expected: FAIL (`assert '.credentials.json' not in (...)`)

- [ ] **Step 3: Implement**

Set `seed_files = ["settings.json", "keybindings.json", "CLAUDE.md"]`. Replace the "macOS keeps credentials…" comment with one line explaining that the host login (Keychain on macOS, `.credentials.json` on Linux) is never copied because a shared login would rotate tokens away from the host. Change the `secrets` comment to "A long-lived token from `claude setup-token`, forwarded from the host environment." In the README, remove "and, on Linux hosts, `.credentials.json`" from the list of copied files, and reword "The host's Claude login lives in the macOS Keychain and is never copied" so it covers both hosts.

- [ ] **Step 4: Run the full suite and the hooks**

Run: `uv run pytest && uv run prek run --all-files`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agentbox/assets/harnesses/claude/profile.toml tests/test_claude.py README.md
git commit -m "Never copy Claude's host login into containers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
