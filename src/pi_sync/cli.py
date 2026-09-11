"""pi-sync — push or pull pi agent config between hosts over rsync.

Only the declarative parts of the agent dir are syncable:

    config      models.json, settings.json   (hand-written, portable)
    extensions  extensions/                  (your extension code)
    auth        auth.json                    (secrets, opt-in)

Host-local state (sessions/, npm/, models-store.json, ayu/, bin/, trust.json)
is deliberately never touched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import click
from click.shell_completion import CompletionItem

DEFAULT_AGENT_DIR = "~/.pi/agent"
SSH_CONFIG = "~/.ssh/config"
PI_INSTALL_CMD = "curl -fsSL https://pi.dev/install.sh | sh"
# One ssh round trip answers both "is it reachable" and "where is pi".
PI_PROBE = """\
found="$(command -v pi 2>/dev/null)"
if [ -z "$found" ]; then
  for candidate in "$HOME/.local/bin/pi" "$HOME/.pi/bin/pi" \\
      "$HOME/.linuxbrew/bin/pi" /home/linuxbrew/.linuxbrew/bin/pi \\
      /opt/homebrew/bin/pi /usr/local/bin/pi; do
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


def ssh_hosts(config: str = SSH_CONFIG, _seen: frozenset[str] = frozenset()) -> list[str]:
    """Host aliases from an ssh config, following Include and skipping wildcards."""
    path = Path(config).expanduser()
    if not path.is_file():
        return []
    resolved = str(path.resolve())
    if resolved in _seen:
        return []
    seen = _seen | {resolved}
    hosts: list[str] = []
    for raw_line in path.read_text(errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        keyword, rest = parts[0].lower(), parts[1].strip()
        if keyword == "host":
            for token in rest.split():
                wildcard = "*" in token or "?" in token or token.startswith("!")
                if not wildcard and token not in hosts:
                    hosts.append(token)
        elif keyword == "include":
            for pattern in rest.split():
                target = Path(pattern).expanduser()
                if not target.is_absolute():
                    target = Path.home() / ".ssh" / target
                for included in sorted(target.parent.glob(target.name)):
                    hosts += [h for h in ssh_hosts(str(included), seen) if h not in hosts]
    return hosts


def complete_target(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete the host part of a target from the user's ssh config."""
    prefix = incomplete.rpartition("@")[2]
    lead = incomplete[: len(incomplete) - len(prefix)] if prefix else incomplete
    return [CompletionItem(f"{lead}{host}") for host in ssh_hosts() if host.startswith(prefix)]


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
        argv.append("--delete-during")
    if dry_run:
        argv.append("-n")
    argv += [remote, local] if pull else [local, remote]
    return argv


def run_cmd(argv: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, input=input_text)


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
    """Run pi's installer on the host. Returns an error message, or None."""
    argv = ["ssh", *(["-t"] if sys.stdin.isatty() else []), target, PI_INSTALL_CMD]
    proc = run_cmd(argv)
    if proc.returncode == 0:
        return None
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    return detail[-1] if detail else f"installer exited {proc.returncode}"


def ensure_pi(target: str, assume_yes: bool, remote_dir: str) -> bool:
    """Make sure the host has pi. Returns False when the host must be skipped.

    Copying into a host without pi is not useful and usually fails outright,
    since the agent directory does not exist there yet.
    """
    if not assume_yes:
        if not sys.stdin.isatty():
            click.secho("  pi not installed — skipping (pass --install to add it)", fg="yellow")
            return False
        prompt = f"  pi is not installed on {target}. install it?"
        if not click.confirm(prompt, default=False):
            click.secho("  skipping host", fg="yellow")
            return False
    click.secho("  installing pi...")
    error = install_pi(target)
    if error:
        click.secho(f"  pi install failed: {error} — skipping host", fg="red")
        return False
    click.secho("  installed pi", fg="green")
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
            click.echo("".join(f"    {line}\n" for line in proc.stdout.splitlines()), nl=False)
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
        click.secho(f"  {rel:<14} {'would copy' if dry_run else 'copied'} {', '.join(parts)}")
    return ok


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument(
    "targets",
    nargs=-1,
    required=True,
    metavar="[USER@]HOST...",
    shell_complete=complete_target,
)
@click.option("--all", "all_", is_flag=True, help="Sync config and extensions (the default).")
@click.option("--config", "config_", is_flag=True, help="Sync models.json and settings.json.")
@click.option("--extensions", "extensions_", is_flag=True, help="Sync the extensions/ directory.")
@click.option("--auth", "auth_", is_flag=True, help="Sync auth.json (contains API keys).")
@click.option("--pull", is_flag=True, help="Copy host → local instead of local → host.")
@click.option(
    "--delete",
    "delete_",
    is_flag=True,
    help="Mirror extensions/ exactly, deleting files absent from the source.",
)
@click.option("--dry-run", is_flag=True, help="Report changes without copying anything.")
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
@click.option("-v", "--verbose", is_flag=True, help="Print each rsync command and its output.")
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
    local_dir: str | None,
    remote_dir: str,
    verbose: bool,
) -> None:
    """Sync pi agent config to one or more hosts, using your ssh config for routing.

    \b
    pi-sync tinfoil                 # push config + extensions
    pi-sync --config laptop         # just models.json and settings.json
    pi-sync --pull --all tinfoil    # fetch the host's config back
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
        click.secho("! auth.json contains API keys and will be copied to the host", fg="yellow")

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
        if pi_path is None and not ensure_pi(target, install_, remote_dir):
            failed = True
            continue
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
    main()
