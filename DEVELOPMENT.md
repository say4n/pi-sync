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
