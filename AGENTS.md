# AGENTS.md

Working notes for AI agents (and humans) changing this repo. User-facing
documentation lives in [README.md](README.md); layout, tooling and the release
process live in [DEVELOPMENT.md](DEVELOPMENT.md).

## What this is

pi-sync: a single click module that rsyncs pi agent config between hosts.
`src/pi_sync/cli.py` is the whole program and `tests/test_cli.py` the whole
suite, so a change is one file plus its tests.

## Commands

```bash
uv sync                 # create/refresh .venv and uv.lock
uv run pytest           # the suite (fast, offline)
uv run pi-sync --help
```

## Invariants — please don't "fix" these

- **Entry point is `pi_sync.cli:app`**, a click group with a default-command
  fallback: `DefaultGroup.parse_args` prepends `sync` when the first token is not
  a subcommand, and `SyncCommand.format_usage` keeps help reading
  `pi-sync [OPTIONS] ...`. The sync function stays named `main` because the tests
  invoke `cli.main` directly.
- **No `__version__`.** The version comes from distribution metadata; adding one
  back reintroduces the drift that removal fixed.
- **Tests never spawn anything.** `FakeRun` records argv and replays output;
  extend it instead of calling `subprocess`. That guarantee is deliberate.
- **Host probes are POSIX sh piped to `ssh host sh -s`**, because some hosts run
  fish and `for …; do … done` is a syntax error there.
- **Never trust pi's installer exit status.** Its "do nothing" menu choice exits
  0, so `install_pi` re-probes the host. Keep new install/uninstall paths
  verification-based.
- **`update` resolves the distribution from its own environment**; do not
  hardcode `pi-sync-cli`, since an older install can coexist under `pi-sync` and
  a hardcoded name would upgrade the wrong virtualenv.
- **`--dry-run` must not mutate anything**, including installing or uninstalling
  pi on a host.

## Conventions

- Commit messages: all lowercase, conventional prefix (`fix:`, `feat:`, `chore:`,
  `ci:`, `docs:`).
- Run `uv run pytest` and `lens_diagnostics mode=all` before declaring work done;
  fix blockers rather than reporting around them.
- pi-lens reformats files on write and sometimes *after* a commit. Check
  `git status` before finishing and commit formatter churn separately.
- `.pi/tasks/` is gitignored local task output — never commit it.
- Never commit secrets. `auth.json` is host-local and only travels with an
  explicit `--auth`.

## Working against real hosts

- This Mac drives everything; `tinfoil` and the Raspberry Pi (`pi`) are real
  hosts and can be offline.
- Prefer `--dry-run` when demonstrating. A real push overwrites host config — a
  `.backup` is kept, but don't be cavalier about it.
- `~/.ssh/config` is protected: read it through code that extracts host aliases,
  and don't dump its contents into output or a commit.
