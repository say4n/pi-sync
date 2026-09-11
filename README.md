# pi-sync

Sync pi agent config between hosts over rsync, using your existing ssh config
for routing (so `~/.ssh/config` aliases just work).

```bash
pi-sync tinfoil                  # push config + extensions
pi-sync --config laptop          # only models.json and settings.json
pi-sync --pull --all tinfoil     # fetch the host's config back
pi-sync --dry-run --all a b      # preview against two hosts
pi-sync update                   # update pi-sync itself
```

## Install

```bash
# the PyPI package is pi-sync-cli; it installs the `pi-sync` command
pipx install pi-sync-cli
pipx install git+ssh://git@github.com/say4n/pi-sync   # from source (needs access)
```

Requires Python 3.10+. `uv tool install` works in place of `pipx install`.

## What syncs

| Group | Files |
| --- | --- |
| `--config` | `models.json`, `settings.json` |
| `--extensions` | `extensions/` |
| `--auth` | `auth.json` — secrets, opt-in, warns on push |

`--all` is `--config` + `--extensions` (also the default when no flag is given).

Your host-local state is never touched: `sessions/`, `npm/`,
`models-store.json`, `ayu/`, `bin/`, `trust.json`.

## Flags

| Flag | Effect |
| --- | --- |
| `--all` / `--config` / `--extensions` / `--auth` | what to sync |
| `--pull` | host → local instead of local → host |
| `--delete` | mirror `extensions/` exactly (deletes extras on the destination) |
| `--dry-run` | report changes, copy nothing |
| `-x, --exclude PATTERN` | skip matching files (repeatable) |
| `--install` | install pi on hosts that lack it, without prompting |
| `--uninstall` | remove pi from the host instead of syncing (config is kept) |
| `--update-pi` | update pi on each host before syncing |
| `--local-dir` | default `$PI_CODING_AGENT_DIR` or `~/.pi/agent` |
| `--remote-dir` | default `~/.pi/agent` |
| `-v` / `--verbose` | print each rsync command and its output |

Several hosts at once: `pi-sync a b c`. The run exits non-zero when a host cannot
be synced. Anything overwritten on the destination is kept beside it as
`<name>.backup`.

## Developing

See [DEVELOPMENT.md](DEVELOPMENT.md).
