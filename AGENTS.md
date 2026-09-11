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

Kept out of the README: these describe *using* pi-sync rather than changing
it, and are collected here so the README stays a short front page.

## Updating

- `pi-sync update` upgrades pi-sync itself, through whichever installer owns it
  (`--check` reports without changing anything).
- `pi-sync --update-pi <hosts>` updates pi on the hosts before syncing.

## Uninstalling pi from a host

`pi-sync --uninstall <hosts>` removes pi and leaves your config in place. If it
fails, run `curl -fsSL https://pi.dev/install.sh | sh` on the host and choose
`u` instead.

## Shell completions

Host arguments complete from `~/.ssh/config`. zsh and fish also show where each
alias points:

```console
$ pi-sync t<TAB>
tinfoil          tinfoil@tinfoil.sayan.page
tinfoil-proxy    notdebian@100.98.241.11
```

```bash
# bash
_PI_SYNC_COMPLETE=bash_source pi-sync > ~/.pi-sync-complete.bash
echo 'source ~/.pi-sync-complete.bash' >> ~/.bashrc

# zsh
_PI_SYNC_COMPLETE=zsh_source pi-sync > ~/.pi-sync-complete.zsh
echo 'source ~/.pi-sync-complete.zsh' >> ~/.zshrc

# fish (config.fish)
_PI_SYNC_COMPLETE=fish_source pi-sync | source
```

PowerShell uses `powershell_source` the same way.

## Caveats

- `settings.json` is written by pi itself (`lastChangelogVersion` bumps, UI
  toggles), so pushing it overwrites the host's local preferences — the previous
  copy is kept as `settings.json.backup`. Sync it when you change `packages`.
- Extensions that keep runtime files in their own directory (logs, checkpoints)
  get those files synced too, and whichever side pushes last wins. Exclude them
  with `-x '*/logs/*'`.
- Extension versions are whatever each host has installed; pin them in
  `settings.json` (`npm:pi-lens@1.2.3`) if you need hosts identical.
- `--auth` copies API keys in the clear. Prefer `OPENCODE_API_KEY` (and friends)
  in the environment where you can.
- `--delete` disables backups for the mirrored directory.
