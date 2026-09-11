# pi-sync

Sync pi agent config between hosts over rsync, using your existing ssh config
for routing (so `~/.ssh/config` aliases just work).

```bash
pi-sync tinfoil                  # push config + extensions
pi-sync --config laptop          # only models.json and settings.json
pi-sync --pull --all tinfoil     # fetch the host's config back
pi-sync --dry-run --all a b      # preview against two hosts
```

## Install

```bash
# the PyPI package is pi-sync-cli; it installs the `pi-sync` command
pipx install pi-sync-cli
pipx install git+ssh://git@github.com/say4n/pi-sync   # from source (needs access)
```

Requires Python 3.10+. `uv tool install` works in place of `pipx install`.

## What syncs

Only the declarative parts of the agent dir:

| Group | Files |
| --- | --- |
| `--config` | `models.json`, `settings.json` |
| `--extensions` | `extensions/` |
| `--auth` | `auth.json` — secrets, opt-in, warns on push |

`--all` is `--config` + `--extensions` (also the default when no flag is given).

Host-local state is deliberately never touched: `sessions/`, `npm/`,
`models-store.json` (regenerated from the pi.dev catalog), `ayu/`, `bin/`,
`trust.json`.

## Flags

| Flag | Effect |
| --- | --- |
| `--all` / `--config` / `--extensions` / `--auth` | what to sync |
| `--pull` | host → local instead of local → host |
| `--delete` | mirror `extensions/` exactly (deletes extras on the destination) |
| `--dry-run` | report changes, copy nothing |
| `-x, --exclude PATTERN` | skip matching files (repeatable) |
| `--install` | install pi on hosts that lack it, without prompting |
| `--local-dir` | default `$PI_CODING_AGENT_DIR` or `~/.pi/agent` |
| `--remote-dir` | default `~/.pi/agent` |
| `-v` / `--verbose` | print each rsync command and its output |

Multiple hosts are accepted: `pi-sync a b c`. Exits non-zero if any host is
unreachable or any transfer fails.

## Host preflight

Each host gets one ssh probe that reports reachability and pi's location in the
same round trip. If pi is missing, pi-sync offers to install it with
`curl -fsSL https://pi.dev/install.sh | sh`: it asks first when running
interactively, stays quiet without a tty, and installs unattended with
`--install`.

**A host without pi is skipped** — copying into a host that has never run pi is
not useful and usually fails anyway, since there is no agent directory to copy
into. That covers a declined prompt, a failed install, and a non-interactive run
without `--install`; a skipped host makes the run exit non-zero. After a
successful install pi-sync creates the agent directory, because rsync will not
create intermediate directories on its own.

The probe checks `command -v pi` plus the usual install locations
(`~/.local/bin`, `~/.pi/bin`, linuxbrew, homebrew, `/usr/local/bin`), because a
non-interactive ssh session does not source the host's shell init — on a
linuxbrew host `command -v pi` alone misses it.

## Shell completions

Host arguments complete from `~/.ssh/config`, following `Include` directives and
skipping wildcard entries:

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

Completions are read from the config at completion time, so new hosts appear
without regenerating anything.

## Caveats

- `settings.json` is machine-written by pi (`lastChangelogVersion` bumps, UI
  toggles), so two hosts pushing it will overwrite each other's local
  preferences. Sync it when you change `packages`, not reflexively.
- Extensions that write runtime files inside their own directory (logs,
  checkpoints) get those files synced too, and each host's copy is overwritten by
  whichever side pushed last — exclude them with `-x '*/logs/*'`.
- Extension versions are whatever each host has installed; pin them in
  `settings.json` (`npm:pi-lens@1.2.3`) if you need hosts identical.
- `--auth` copies API keys in the clear. Prefer `OPENCODE_API_KEY` (and friends)
  in the environment where you can.
- Remote paths go through the host's shell, so `~` expands there as usual.

## Development

```bash
uv sync
uv run pytest
uv run pi-sync --help
```
