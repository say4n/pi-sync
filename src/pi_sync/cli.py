"""pi-sync — push or pull pi agent config between hosts over rsync.

Only the declarative parts of the agent dir are syncable:

    config      models.json, settings.json   (hand-written, portable)
    extensions  extensions/                  (your extension code)
    auth        auth.json                    (secrets, opt-in)

Host-local state (sessions/, npm/, models-store.json, ayu/, bin/, trust.json)
is deliberately never touched.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

import click
from click.shell_completion import CompletionItem

DEFAULT_AGENT_DIR = "~/.pi/agent"
SSH_CONFIG = "~/.ssh/config"
PI_INSTALL_CMD = "curl -fsSL https://pi.dev/install.sh | sh"
PI_PACKAGE = "@earendil-works/pi-coding-agent"
# One ssh round trip answers both "is it reachable" and "where is pi".
PI_PROBE = """\
found="$(command -v pi 2>/dev/null)"
if [ -z "$found" ]; then
  for candidate in "$HOME/.local/bin/pi" "$HOME/.pi/bin/pi" \\
      "$HOME/.linuxbrew/bin/pi" /home/linuxbrew/.linuxbrew/bin/pi \\
      "$HOME/.pi/agent/bin/pi" /opt/homebrew/bin/pi /usr/local/bin/pi; do
    if [ -x "$candidate" ]; then found="$candidate"; break; fi
  done
fi
if [ -n "$found" ]; then echo "PI:$found"; fi
exit 0
"""
DIRECTORY_ITEMS = frozenset({"extensions"})
GROUPS: dict[str, tuple[str, ...]] = {
    "config": ("models.json", "settings.json"),
    "extensions": ("extensions",),
    "auth": ("auth.json",),
}
# Order groups are synced in, so output is stable.
GROUP_ORDER = ("config", "extensions", "auth")


def local_agent_dir(override: str | None = None) -> Path:
    """Local agent dir: explicit flag, else $PI_CODING_AGENT_DIR, else ~/.pi/agent."""
    raw = override or os.environ.get("PI_CODING_AGENT_DIR") or DEFAULT_AGENT_DIR
    return Path(raw).expanduser()


@dataclass(frozen=True)
class SshHost:
    """A host alias, plus the details worth showing in shell completions."""

    name: str
    hostname: str | None = None
    user: str | None = None

    @property
    def detail(self) -> str:
        if self.hostname and self.user:
            return f"{self.user}@{self.hostname}"
        return self.hostname or self.user or ""


def ssh_config_hosts(
    config: str = SSH_CONFIG, _seen: frozenset[str] = frozenset()
) -> list[SshHost]:
    """Hosts from an ssh config, following Include and skipping wildcards.

    Directives bind to the Host line that precedes them, so the current block is
    flushed when the next Host or Include appears.
    """
    path = Path(config).expanduser()
    if not path.is_file():
        return []
    resolved = str(path.resolve())
    if resolved in _seen:
        return []
    seen = _seen | {resolved}
    hosts: list[SshHost] = []
    names: set[str] = set()
    pending: list[str] = []
    hostname: str | None = None
    user: str | None = None

    def add(entries: list[SshHost]) -> None:
        for entry in entries:
            if entry.name not in names:
                names.add(entry.name)
                hosts.append(entry)

    def flush() -> None:
        add([SshHost(name, hostname, user) for name in pending])
        pending.clear()

    for raw_line in path.read_text(errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        keyword, rest = parts[0].lower(), parts[1].strip()
        if keyword == "host":
            flush()
            hostname = user = None
            pending += [t for t in rest.split() if not any(c in t for c in "*?!")]
        elif keyword == "hostname":
            hostname = rest.split()[0]
        elif keyword == "user":
            user = rest.split()[0]
        elif keyword == "include":
            flush()
            for pattern in rest.split():
                target = Path(pattern).expanduser()
                if not target.is_absolute():
                    target = Path.home() / ".ssh" / target
                for included in sorted(target.parent.glob(target.name)):
                    add(ssh_config_hosts(str(included), seen))
    flush()
    return hosts


def ssh_hosts(
    config: str = SSH_CONFIG, _seen: frozenset[str] = frozenset()
) -> list[str]:
    """Host alias names from an ssh config (see ssh_config_hosts)."""
    return [host.name for host in ssh_config_hosts(config, _seen)]


def host_completions(incomplete: str) -> list[CompletionItem]:
    """Hosts from the ssh config, for the part of the word after any user@."""
    prefix = incomplete.rpartition("@")[2]
    lead = incomplete[: len(incomplete) - len(prefix)] if prefix else incomplete
    return [
        CompletionItem(f"{lead}{host.name}", help=host.detail)
        for host in ssh_config_hosts()
        if host.name.startswith(prefix)
    ]


def complete_target(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete the host part of a target from the user's ssh config."""
    return host_completions(incomplete)


def select_items(groups: set[str]) -> list[str]:
    return [item for group in GROUP_ORDER if group in groups for item in GROUPS[group]]


def rsync_argv(
    rel: str,
    target: str,
    local_dir: Path,
    remote_dir: str,
    *,
    pull: bool = False,
    delete: bool = False,
    dry_run: bool = False,
    excludes: Sequence[str] = (),
) -> list[str]:
    """One rsync invocation for one item, in the requested direction."""
    remote = f"{target}:{remote_dir.rstrip('/')}/{rel}"
    local = str(local_dir / rel)
    if rel in DIRECTORY_ITEMS:
        remote += "/"  # copy contents, not the directory itself
        local += "/"
    argv = ["rsync", "-az", "-i"]
    argv += [f"--exclude={pattern}" for pattern in excludes]
    if delete and rel in DIRECTORY_ITEMS:
        # Mirroring wins over backups here: the destination is meant to match the
        # source exactly, and openrsync (the rsync macOS ships) errors out when it
        # has to back up a file it deletes.
        argv.append("--delete-during")
    else:
        # Keep the destination's copy of whatever we replace, as <file>.backup.
        argv += ["--backup", "--suffix=.backup"]
    argv.append("--exclude=*.backup")  # backups stay host-local
    if dry_run:
        argv.append("-n")
    argv += [remote, local] if pull else [local, remote]
    return argv


def run_cmd(
    argv: list[str], input_text: str | None = None, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=capture, text=True, input=input_text)


def summarize(output: str) -> tuple[int, int]:
    """Count transferred files and deletions from rsync --itemize-changes output."""
    transferred = deleted = 0
    for line in output.splitlines():
        if line.startswith("*deleting"):
            deleted += 1
        elif line[:1] in ("<", ">"):
            transferred += 1
    return transferred, deleted


def probe_host(target: str) -> tuple[str | None, str | None]:
    """Return (error, pi path): error is set when the host is unreachable."""
    proc = run_cmd(["ssh", "-o", "ConnectTimeout=10", target, "sh -s"], PI_PROBE)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "ssh failed").strip().splitlines()
        return (detail[0] if detail else "ssh failed"), None
    for line in proc.stdout.splitlines():
        if line.startswith("PI:"):
            return None, line.removeprefix("PI:").strip()
    return None, None


def install_pi(target: str) -> str | None:
    """Run pi's installer on the host. Returns an error message, or None on success.

    Attached to a terminal the installer streams straight through and gets a
    remote tty, so its prompts are visible and answerable: capturing them would
    leave the user staring at a silent, unanswerable hang. Unattended, output is
    captured for the failure summary and stdin is closed so a prompt fails fast
    instead of blocking forever.

    The installer's exit status is not proof of anything: its "do nothing" menu
    choice exits 0 without installing, so the host is re-probed afterwards.
    """
    interactive = sys.stdin.isatty()
    argv = ["ssh", *(["-t"] if interactive else []), target, PI_INSTALL_CMD]
    if interactive:
        proc = run_cmd(argv, capture=False)
        failure = f"installer exited {proc.returncode}" if proc.returncode else None
    else:
        proc = run_cmd(argv, input_text="")
        if proc.returncode:
            detail = ((proc.stderr or proc.stdout) or "").strip().splitlines()
            failure = detail[-1] if detail else f"installer exited {proc.returncode}"
        else:
            failure = None
    if failure:
        return failure
    _, pi_path = probe_host(target)
    if pi_path is None:
        return "installer exited without installing pi (its menu needs 'y')"
    return None


def uninstall_pi(target: str, pi_path: str) -> str | None:
    """Remove pi's npm install. Returns an error message, or None on success.

    Mirrors the installer's own npm path — the prefix comes from where pi
    actually lives — because its unattended mode can only install or reinstall;
    uninstall is reachable only through its interactive menu. Like the
    installer, the result is verified rather than trusted, and only the CLI is
    removed: the agent directory is left alone.
    """
    prefix = Path(pi_path).parent.parent
    cmd = (
        f"npm uninstall -g --prefix {prefix} --no-fund --no-audit "
        f"--loglevel=error --progress=false {PI_PACKAGE}"
    )
    proc = run_cmd(["ssh", target, cmd])
    if proc.returncode:
        detail = ((proc.stderr or proc.stdout) or "").strip().splitlines()
        return detail[-1] if detail else f"npm uninstall exited {proc.returncode}"
    _, remaining = probe_host(target)
    if remaining:
        return f"npm uninstall finished, but pi is still present at {remaining}"
    return None


def ensure_pi(target: str, assume_yes: bool, remote_dir: str) -> bool:
    """Make sure the host has pi. Returns False when the host must be skipped.

    Copying into a host without pi is not useful and usually fails outright,
    since the agent directory does not exist there yet.

    Interactively the installer owns the decision: it has its own
    install/uninstall/do-nothing menu, so prompting a second time here would
    only add a redundant question before the one that counts.
    """
    interactive = sys.stdin.isatty()
    if not interactive and not assume_yes:
        click.secho(
            "  pi not installed — skipping (pass --install to add it)", fg="yellow"
        )
        return False
    if interactive:
        click.secho(
            "  handing over to pi's installer — the sync continues when it exits"
        )
    else:
        click.secho("  running pi's installer (unattended)")
    error = install_pi(target)
    if error:
        click.secho(f"  {error} — skipping host", fg="red")
        return False
    click.secho("  pi installed; continuing with the sync", fg="green")
    # A fresh install has never run, so the agent dir may not exist yet and
    # rsync will not create intermediate directories for us.
    run_cmd(["ssh", target, f"mkdir -p {remote_dir}"])
    return True


def sync_host(
    target: str,
    items: list[str],
    local_dir: Path,
    remote_dir: str,
    *,
    pull: bool,
    delete: bool,
    dry_run: bool,
    verbose: bool,
    excludes: Sequence[str] = (),
) -> bool:
    ok = True
    for rel in items:
        if not pull and not (local_dir / rel).exists():
            click.secho(f"  {rel:<14} skipped (not found locally)", fg="yellow")
            continue
        argv = rsync_argv(
            rel,
            target,
            local_dir,
            remote_dir,
            pull=pull,
            delete=delete,
            dry_run=dry_run,
            excludes=excludes,
        )
        if verbose:
            click.echo(f"  $ {' '.join(argv)}")
        proc = run_cmd(argv)
        if verbose and proc.stdout:
            click.echo(
                "".join(f"    {line}\n" for line in proc.stdout.splitlines()), nl=False
            )
        if proc.returncode != 0:
            ok = False
            click.secho(f"  {rel:<14} FAILED", fg="red")
            for line in (proc.stderr or proc.stdout).strip().splitlines()[:5]:
                click.echo(f"    {line}")
            continue
        transferred, deleted = summarize(proc.stdout)
        if not transferred and not deleted:
            click.secho(f"  {rel:<14} already in sync")
            continue
        parts = []
        if transferred:
            parts.append(f"{transferred} file{'s' if transferred != 1 else ''}")
        if deleted:
            parts.append(f"{deleted} deleted")
        click.secho(
            f"  {rel:<14} {'would copy' if dry_run else 'copied'} {', '.join(parts)}"
        )
    return ok


PYPI_JSON = "https://pypi.org/pypi/{dist}/json"
EPHEMERAL_MARKERS = ("/uv/archive-", "/uv/environments-")


@dataclass(frozen=True)
class Install:
    """How the running copy of pi-sync got onto this machine."""

    dist: str
    version: str
    kind: str
    detail: str = ""


def install_kind(direct_url: str, prefix: str) -> tuple[str, str]:
    """Classify an environment from its PEP 610 payload and its prefix.

    The payload is parsed rather than pattern-matched: uv and pip disagree about
    whitespace in `direct_url.json`, so a substring test silently misses one.

    A payload means the package was installed from a *direct reference*, which
    cannot be upgraded from an index: a directory is rebuilt from that directory,
    a VCS install is re-fetched from that repo, and a distribution file can only
    ever be reinstalled. No payload at all means an ordinary index install, which
    is the only kind `pipx upgrade`/`uv tool upgrade` can move to a new release.
    """
    try:
        info = json.loads(direct_url) if direct_url else {}
    except ValueError:
        info = {}
    if not isinstance(info, dict):
        info = {}
    url = str(info.get("url", "")).removeprefix("file://")
    dir_info = info.get("dir_info")
    if isinstance(dir_info, dict) and dir_info.get("editable"):
        return "editable", url
    if isinstance(dir_info, dict):
        return "dir", url
    if isinstance(info.get("vcs_info"), dict):
        return "vcs", str(info.get("url", ""))
    if isinstance(info.get("archive_info"), dict):
        return "archive", url
    if "/pipx/venvs/" in prefix:
        return "pipx", prefix
    if "/uv/tools/" in prefix:
        return "uv-tool", prefix
    if any(marker in prefix for marker in EPHEMERAL_MARKERS):
        return "ephemeral", prefix
    return "pip", prefix


def running_install() -> Install:
    """Identify the install we are running *from*, not the one merely on PATH.

    Two installs of this tool can coexist (an older distribution name and the
    current one), so the distribution is resolved from this process's own
    environment: a hardcoded package name would happily upgrade somebody else's
    virtualenv, or none at all.
    """
    found = metadata.packages_distributions().get("pi_sync") or ["pi-sync-cli"]
    dist = metadata.distribution(found[0])
    kind, detail = install_kind(dist.read_text("direct_url.json") or "", sys.prefix)
    return Install(dist.metadata["Name"], dist.version, kind, detail)


def upgrade_argv(install: Install) -> list[str] | None:
    """The command that upgrades this install, or None when it is not managed."""
    if install.kind == "pipx":
        return ["pipx", "upgrade", install.dist]
    if install.kind == "uv-tool":
        return ["uv", "tool", "upgrade", install.dist]
    if install.kind == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", install.dist]
    return None


def latest_version(dist: str) -> str | None:
    """Newest release on PyPI, or None when it cannot be determined."""
    url = PYPI_JSON.format(dist=dist)
    if urlparse(url).scheme != "https":  # never fetch over anything else
        return None
    try:
        with urlopen(url, timeout=10) as response:  # noqa: S310 - scheme checked
            return json.load(response)["info"]["version"]
    except Exception:  # offline, renamed project, unexpected payload
        return None


def version_tuple(value: str) -> tuple[int, ...]:
    """The leading numeric release, ignoring any pre-release suffix."""
    match = re.match(r"\d+(?:\.\d+)*", value.strip())
    if not match:
        return ()
    try:
        return tuple(int(part) for part in match.group(0).split("."))
    except ValueError:  # unreachable: the pattern only matches digits
        return ()


def pi_version_on(target: str) -> str | None:
    """The host's pi version, or None when it cannot be read."""
    proc = run_cmd(["ssh", target, "pi --version"])
    text = (proc.stdout or "").strip()
    return text if proc.returncode == 0 and text else None


def update_pi_on(target: str) -> str | None:
    """Run pi's own updater on the host. Returns an error message, or None."""
    proc = run_cmd(["ssh", target, "pi update --self"])
    if proc.returncode == 0:
        return None
    detail = ((proc.stderr or proc.stdout) or "").strip().splitlines()
    return detail[-1] if detail else f"pi update exited {proc.returncode}"


class SyncCommand(click.Command):
    """`sync` is the default command, so its usage should not read `pi-sync sync`."""

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        pieces = " ".join(self.collect_usage_pieces(ctx))
        program = ctx.find_root().info_name or "pi-sync"
        formatter.write_usage(program, pieces, prefix=None)


class DefaultGroup(click.Group):
    """Group that treats the absence of a subcommand as `sync`.

    This keeps `pi-sync host`, `pi-sync --config host` and `pi-sync update` all
    working. The cost is that "update" is now a reserved word: a host with that
    alias has to be reached as `user@update` or by renaming the alias.

    The prepend happens before click parses the group's options, because that
    parser rejects sync's flags (`No such option: --config`) long before
    command resolution would get a chance to fall back.
    """

    def parse_args(  # type: ignore[override]
        self, ctx: click.Context, args: list[str]
    ) -> list[str]:
        if args and args[0] not in self.commands and args[0] != "--version":
            args = ["sync", *args]
        return super().parse_args(ctx, args)

    def shell_complete(  # type: ignore[override]
        self, ctx: click.Context, incomplete: str
    ) -> list[CompletionItem]:
        """Complete as if `sync` had been typed.

        Without this, click completes a group by listing its subcommands, so
        `pi-sync <TAB>` offers `sync` and `update` and every host name becomes
        unreachable (`pi-sync tin<TAB>` matches no subcommand and returns
        nothing). `sync` itself is left out of the suggestions because it is the
        default: naming it is never necessary.
        """
        if incomplete.startswith("-"):
            return self.commands["sync"].shell_complete(ctx, incomplete)
        items = host_completions(incomplete)
        if incomplete:
            items += [
                CompletionItem(name, help=self.commands[name].get_short_help_str())
                for name in self.commands
                if name != "sync" and name.startswith(incomplete)
            ]
        return items


@click.group(cls=DefaultGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="pi-sync-cli")
def app() -> None:
    """Sync pi agent config across hosts, and manage pi itself."""


@app.command("update", context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--check", is_flag=True, help="Report what would happen; change nothing.")
def update_cmd(check: bool) -> None:
    """Update pi-sync itself to the latest release.

    \b
    pi-sync update           # upgrades via pipx, uv, or pip — whichever installed it
    pi-sync update --check   # just report
    """
    install = running_install()
    click.echo(f"pi-sync {install.version}  ({install.kind})")
    if install.kind == "editable":
        click.echo(
            f"running from a checkout; update it with:\n  git -C {install.detail} pull"
        )
        return
    if install.kind in {"dir", "vcs", "archive"}:
        # Rebuilding the same source would not move this install to a release, and
        # saying "upgrading x → y" here would be a lie about what just happened.
        source = {
            "dir": "a local directory",
            "vcs": "a git repository",
            "archive": "a local distribution file",
        }[install.kind]
        click.echo(f"installed from {source}: {install.detail}")
        click.echo("this copy follows that source, so there is no release to fetch.")
        click.echo(
            "to follow PyPI releases instead:\n"
            f"  pipx install --force {install.dist}\n"
            f"  uv tool install --force {install.dist}"
        )
        return
    if install.kind == "ephemeral":
        click.echo(
            "running from an ephemeral uvx environment — nothing to update.\n"
            "for a durable install: uv tool install pi-sync-cli"
        )
        return
    argv = upgrade_argv(install)
    if argv is None:  # pragma: no cover - every kind above is handled earlier
        click.secho("do not know how this copy was installed", fg="red")
        raise SystemExit(1)
    latest = latest_version(install.dist)
    if latest is None:
        click.secho(f"could not reach PyPI for {install.dist}", fg="red")
        raise SystemExit(1)
    if version_tuple(latest) <= version_tuple(install.version):
        click.secho(f"already up to date (latest is {latest})", fg="green")
        return
    if check:
        click.echo(f"would run: {' '.join(argv)}\n{install.version} → {latest}")
        return
    click.echo(f"upgrading {install.version} → {latest}...")
    proc = run_cmd(argv, capture=False)  # streamed: pipx/uv show their own progress
    if proc.returncode:
        click.secho(f"upgrade failed (exit {proc.returncode})", fg="red")
        raise SystemExit(1)
    click.secho(f"pi-sync {latest} installed", fg="green")


@app.command(
    "sync",
    cls=SyncCommand,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.argument(
    "targets",
    nargs=-1,
    required=True,
    metavar="[USER@]HOST...",
    shell_complete=complete_target,
)
@click.option(
    "--all", "all_", is_flag=True, help="Sync config and extensions (the default)."
)
@click.option(
    "--config", "config_", is_flag=True, help="Sync models.json and settings.json."
)
@click.option(
    "--extensions", "extensions_", is_flag=True, help="Sync the extensions/ directory."
)
@click.option(
    "--auth", "auth_", is_flag=True, help="Sync auth.json (contains API keys)."
)
@click.option("--pull", is_flag=True, help="Copy host → local instead of local → host.")
@click.option(
    "--delete",
    "delete_",
    is_flag=True,
    help="Mirror extensions/ exactly, deleting files absent from the source.",
)
@click.option(
    "--dry-run", is_flag=True, help="Report changes without copying anything."
)
@click.option(
    "-x",
    "--exclude",
    "excludes",
    multiple=True,
    metavar="PATTERN",
    help="rsync exclude pattern, e.g. '*/logs/*' (repeatable).",
)
@click.option(
    "--install",
    "install_",
    is_flag=True,
    help="Install pi on hosts that lack it, without prompting.",
)
@click.option(
    "--uninstall",
    "uninstall_",
    is_flag=True,
    help="Uninstall pi from the host instead of syncing (keeps ~/.pi/agent).",
)
@click.option(
    "--update-pi",
    "update_pi",
    is_flag=True,
    help="Update pi on each host before syncing.",
)
@click.option(
    "--local-dir",
    default=None,
    help="Local agent dir (default: $PI_CODING_AGENT_DIR or ~/.pi/agent).",
)
@click.option(
    "--remote-dir",
    default=DEFAULT_AGENT_DIR,
    show_default=True,
    help="Agent dir on the host.",
)
@click.option(
    "-v", "--verbose", is_flag=True, help="Print each rsync command and its output."
)
def main(
    targets: tuple[str, ...],
    all_: bool,
    config_: bool,
    extensions_: bool,
    auth_: bool,
    pull: bool,
    delete_: bool,
    dry_run: bool,
    excludes: tuple[str, ...],
    install_: bool,
    uninstall_: bool,
    update_pi: bool,
    local_dir: str | None,
    remote_dir: str,
    verbose: bool,
) -> None:
    """Sync pi agent config to one or more hosts, using your ssh config for routing.

    \b
    pi-sync tinfoil                 # push config + extensions
    pi-sync --config laptop         # just models.json and settings.json
    pi-sync --pull --all tinfoil    # fetch the host's config back
    pi-sync update                  # update pi-sync itself
    """
    groups = {
        group
        for group, enabled in (
            ("config", config_ or all_),
            ("extensions", extensions_ or all_),
            ("auth", auth_),
        )
        if enabled
    }
    if not groups:
        groups = {"config", "extensions"}
    items = select_items(groups)

    if auth_ and not pull:
        click.secho(
            "! auth.json contains API keys and will be copied to the host", fg="yellow"
        )

    agent_dir = local_agent_dir(local_dir)
    click.echo(
        f"{'pulling' if pull else 'pushing'} {', '.join(items)} "
        f"{'from' if pull else 'to'} {len(targets)} host(s)\n"
        f"local: {agent_dir}\nremote: {remote_dir}\n"
    )

    failed = False
    for raw_target in targets:
        target = raw_target.rstrip(":")
        error, pi_path = probe_host(target)
        if error:
            failed = True
            click.secho(f"→ {target}\n  unreachable: {error}", fg="red")
            continue
        click.secho(f"→ {target}", bold=True)
        if uninstall_:
            if pi_path is None:
                click.secho("  pi is not installed — nothing to uninstall")
                continue
            prefix = Path(pi_path).parent.parent
            if dry_run:
                click.secho(
                    f"  would run: npm uninstall -g --prefix {prefix} {PI_PACKAGE}"
                )
                continue
            click.secho(f"  uninstalling pi from {pi_path}...")
            error = uninstall_pi(target, pi_path)
            if error:
                failed = True
                click.secho(f"  {error}", fg="red")
                click.secho(
                    "  for a managed install, run the installer and choose 'u'",
                    fg="yellow",
                )
            else:
                click.secho("  pi uninstalled — ~/.pi/agent was left alone", fg="green")
            continue
        if pi_path is None:
            if dry_run:
                click.secho("  pi not installed — nothing would be synced", fg="yellow")
                failed = True
                continue
            if not ensure_pi(target, install_, remote_dir):
                failed = True
                continue
        if update_pi:
            if dry_run:
                click.secho("  would run: pi update --self")
            else:
                before = pi_version_on(target)
                error = update_pi_on(target)
                after = pi_version_on(target)
                if error:
                    failed = True
                    click.secho(f"  pi update failed: {error}", fg="red")
                elif before and after and before != after:
                    click.secho(f"  pi {before} → {after}", fg="green")
                else:
                    click.secho(f"  pi {after or 'unknown'} (already current)")
        if not sync_host(
            target,
            items,
            agent_dir,
            remote_dir,
            pull=pull,
            delete=delete_,
            dry_run=dry_run,
            verbose=verbose,
            excludes=excludes,
        ):
            failed = True

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    app()
