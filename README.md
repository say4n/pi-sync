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
pipx install .        # or: uv tool install .
```

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
| `--local-dir` | default `$PI_CODING_AGENT_DIR` or `~/.pi/agent` |
| `--remote-dir` | default `~/.pi/agent` |
| `-v` / `--verbose` | print each rsync command and its output |

Multiple hosts are accepted: `pi-sync a b c`. Exits non-zero if any host is
unreachable or any transfer fails.

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
