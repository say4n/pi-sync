# pi-sync

pi-sync keeps your pi agent config the same across hosts. It copies the
declarative parts of `~/.pi/agent` to another machine over rsync, and it reads
your ssh config, so the aliases in `~/.ssh/config` work as host names.

```bash
pi-sync tinfoil                  # copy config and extensions to tinfoil
pi-sync --config laptop          # copy only models.json and settings.json
pi-sync --pull --all tinfoil     # copy tinfoil's config back to this machine
pi-sync --dry-run --all a b      # report what would change, without copying
pi-sync update                   # update pi-sync itself
```

## install

```bash
pipx install pi-sync-cli
```

The PyPI package is `pi-sync-cli`, and it installs the `pi-sync` command.
`uv tool install pi-sync-cli` works the same way.

To install from source, run:

```bash
pipx install git+ssh://git@github.com/say4n/pi-sync
```

pi-sync requires Python 3.10 or later.

## what syncs

pi-sync copies the declarative parts of the agent directory, and nothing else.

| group | what it copies |
| --- | --- |
| `--config` | `models.json` and `settings.json` |
| `--extensions` | the `extensions/` directory |
| `--auth` | `auth.json`, which holds API keys. This group is opt-in, and pi-sync warns you before it copies secrets. |

`--all` copies `--config` and `--extensions`. It is the default when you pass no
group flag.

pi-sync leaves host-local state alone: `sessions/`, `npm/`, `models-store.json`,
`ayu/`, `bin/`, and `trust.json` stay where they are.

## flags

| flag | effect |
| --- | --- |
| `--all`, `--config`, `--extensions`, `--auth` | set what pi-sync copies |
| `--pull` | copy from the host to this machine instead of the other way |
| `--delete` | make `extensions/` match the source exactly, including deletions |
| `--dry-run` | report changes without copying anything |
| `-x, --exclude PATTERN` | skip files that match the pattern. Repeat the flag to add more patterns. |
| `--install` | install pi on hosts that do not have it, without prompting |
| `--uninstall` | remove pi from the host instead of copying config. Your config stays. |
| `--update-pi` | update pi on each host before copying config |
| `--local-dir` | set the local agent directory. The default is `$PI_CODING_AGENT_DIR`, or `~/.pi/agent`. |
| `--remote-dir` | set the agent directory on the host. The default is `~/.pi/agent`. |
| `-v, --verbose` | print each rsync command and its output |

To copy to more than one host, pass more than one name: `pi-sync a b c`. The
command exits with a non-zero status when it cannot copy to a host. Each file
pi-sync overwrites on the destination is kept beside the new one as
`<name>.backup`.

## developing

For the layout, the tests, and the release process, see
[DEVELOPMENT.md](DEVELOPMENT.md).
