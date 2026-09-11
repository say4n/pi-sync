# Developing pi-sync

## Layout

```text
src/pi_sync/cli.py    the whole CLI: probing, rsync, completions, self-update
tests/test_cli.py     unit tests (no ssh or rsync process is ever executed)
pyproject.toml        click dependency, console script, uv dev group
pyrightconfig.json    points pyright at .venv (see "Tooling" below)
```

Three design anchors worth understanding before editing:

- **The console script points at `pi_sync.cli:app`**, a click *group*.
  `DefaultGroup.parse_args` prepends `sync` when the first argument is not a
  subcommand, which is what lets `pi-sync tinfoil`, `pi-sync --config tinfoil`
  and `pi-sync update` all work. `SyncCommand.format_usage` exists so help reads
  `pi-sync [OPTIONS] [USER@]HOST...` instead of `pi-sync sync [OPTIONS] ...`.
  `main` is the sync subcommand and keeps that name because the tests drive it
  directly.
- **The version comes from distribution metadata** (`@click.version_option`), so
  there is no `__version__` to fall out of step with the release.
- **`update` resolves the distribution from its own environment**
  (`importlib.metadata.packages_distributions()`) rather than hardcoding a name.
  An older install can coexist under the pre-rename name `pi-sync`, and a
  hardcoded name would upgrade the wrong virtualenv — or nothing.

## How it works

**One probe per host.** `probe_host` pipes a POSIX sh script through
`ssh host sh -s`, so reachability and pi's location cost a single round trip. It
checks `command -v pi` and then the usual install locations (`~/.local/bin`,
`~/.pi/bin`, `~/.pi/agent/bin`, linuxbrew, homebrew, `/usr/local/bin`) because a
non-interactive ssh session does not source the host's shell init — on a
linuxbrew host `command -v pi` alone finds nothing.

**Installing pi.** Interactively the terminal is handed to pi's own installer and
the sync resumes when it exits. Unattended (`--install` with no tty) the output is
captured and stdin is closed, so a prompt fails fast instead of hanging. The
installer's exit status is not trusted — its "do nothing" menu choice exits 0 —
so `install_pi` re-probes and reports honestly. A host that still lacks pi is
skipped, which makes the run exit non-zero. After a successful install,
`mkdir -p` creates the agent directory, because rsync will not create
intermediate directories itself.

**Uninstalling.** The official installer can only uninstall through its
interactive menu (its unattended mode always installs or reinstalls), so pi-sync
issues the npm command that menu would have: `npm uninstall -g --prefix <prefix>
@earendil-works/pi-coding-agent`, with the prefix derived from where pi actually
lives. It re-probes afterwards, since npm can exit 0 having removed nothing. Only
the CLI goes; the agent directory is never touched.

**Self-update.** `update` classifies the running environment and delegates. Only
an *index* install can move to a newer release; a `direct_url.json` payload means
the install follows a direct reference, so updating it would just rebuild that
reference — the command says so rather than reporting a version change it never
made.

| Detected | Action |
| --- | --- |
| index install via pipx | `pipx upgrade <dist>` |
| index install via uv tool | `uv tool upgrade <dist>` |
| index install in a plain venv | `python -m pip install --upgrade <dist>` |
| `dir_info.editable` | prints `git -C <path> pull` |
| `dir_info` (a directory build) | reports the directory, suggests a `--force` reinstall from PyPI |
| `vcs_info` | reports the repo; there is no release to fetch |
| `archive_info` | reports the file; there is no release to fetch |
| uvx ephemeral | nothing to do; suggests a durable install |

It resolves the distribution from its own environment instead of hardcoding a
name, and refuses to downgrade when the local version is ahead of PyPI.

**Completions.** Read from `~/.ssh/config` at completion time, following
`Include` and skipping wildcard entries; `CompletionItem.help` carries
`user@hostname` for the shells that render it (bash cannot).

**rsync invocation.** `-az -i`, plus `--backup --suffix=.backup` and
`--exclude=*.backup` so replaced files are kept but backups never travel, and
`--delete-during` only for `extensions/` when `--delete` is given — which also
suppresses backups for that directory.

## Commands

```bash
uv sync                 # create/refresh .venv and uv.lock
uv run pytest           # the suite
uv run pi-sync --help
uv build                # wheel + sdist into dist/
```

## Tests

`FakeRun` records every command and replays canned output, so the suite needs no
network, no hosts, and no real transfers. When you add a command, extend
`FakeRun` rather than reaching for `subprocess` — the guarantee that nothing is
executed is what keeps the suite fast and safe.

The one path that cannot be unit-tested is the interactive installer handover,
because it depends on a real terminal. It was verified out-of-band with a PTY
harness that attaches a pseudo-terminal, feeds a keystroke, and asserts the
installer's `/dev/tty` menu reached the terminal and that the keypress reached
the remote host.

## Tooling

`pi-lens` runs ruff format/lint as files are written, and sometimes reformats
*after* a commit — check `git status` before finishing, and commit formatter
churn on its own.

`pyrightconfig.json` sets `venvPath`/`venv` because pyright does not pick up a
uv-created venv on its own here. Measured: without it, `Import "pytest" could
not be resolved`; with it, zero errors. A long-lived pyright language server
that started before the venv existed will keep reporting the stale result until
it is restarted.

## Releasing

```bash
uv version --bump minor            # or patch; also updates uv.lock
git commit -am "chore: release X.Y.Z"
git push origin main
git tag -a vX.Y.Z -m vX.Y.Z
git push origin vX.Y.Z             # publishing happens on the tag
```

`.github/workflows/publish.yml` then builds, smoke-tests both artifacts *through
the console script*, and publishes with OIDC trusted publishing — no token is
stored anywhere. It refuses to publish when the tag does not match the version in
`pyproject.toml`.

Verify a release:

```bash
gh run view <run-id> --log | grep "Uploading pi_"
curl -s https://pypi.org/pypi/pi-sync-cli/X.Y.Z/json   # per-version: immediate
```

Expect **CDN lag** on the aggregate endpoints. Measured on 0.4.0: the
per-version endpoint answered immediately, `/pypi/pi-sync-cli/json` took ~45s,
and the simple index took ~40s. Until the simple index flips,
`uvx --from pi-sync-cli==X.Y.Z` fails with "no version … requirements are
unsatisfiable", which looks like a broken release but is not. Re-tagging never
helps (PyPI rejects duplicate versions); `workflow_dispatch` re-runs the job
without a new tag.

## PyPI notes

The distribution is `pi-sync-cli`; the command is `pi-sync`. Plain `pi-sync` is
permanently unavailable: PyPI compares names with punctuation stripped, and
`pisync` already exists (an unrelated rsync backup script).

Trusted publisher fields: project `pi-sync-cli`, owner `say4n`, repository
`pi-sync`, workflow `publish.yml`, environment `pypi`. A pending publisher
reserves nothing until first use — it is invalidated if someone else registers
the name in the meantime.

## Platform gotchas found the hard way

- macOS ships **openrsync**, which rejects rsync 3 flags. Only flags verified
  against it are used; `--ignore-missing-args`, `--human-readable` and
  `--mkpath` are not available.
- `--backup` with `--delete` fails on openrsync acting as *receiver*
  (`fchownat: Operation not permitted`, exit 23) but works fine with rsync 3.x,
  so backups are suppressed whenever `--delete` is in play.
- rsync's default check compares size and mtime at 1-second granularity: two
  files created in the same second with equal sizes look "already in sync". Use
  distinct mtimes when testing backups, or the test proves nothing.
- Remote shells are **fish** on some hosts, so probes are POSIX sh piped over
  stdin (`ssh host sh -s`), never a shell loop in the command string
  (`for …; do … done` is a syntax error there).
- pi's installer reads `/dev/tty`, not stdin, so capturing its output makes its
  prompts invisible while it waits for a keypress. Interactive installs must
  stream; unattended ones must close stdin so a prompt fails fast.
