"""Tests for pi-sync.

Covers flag→item selection, rsync argv construction, ssh-config host
completion, and the per-host pi probe/install path. No ssh or rsync process is
ever executed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from pi_sync import cli


class FakeRun:
    """Records every command; replays canned itemize output for rsync."""

    def __init__(
        self,
        rsync_stdout: str = ">f+++++++ models.json\n",
        rsync_rc: int = 0,
        ssh_rc: int = 0,
        pi_path: str | None = "/usr/bin/pi",
        install_rc: int = 0,
        install_installs: bool = True,
        uninstall_rc: int = 0,
        uninstall_removes: bool = True,
    ):
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.captures: list[bool] = []
        self.rsync_stdout = rsync_stdout
        self.rsync_rc = rsync_rc
        self.ssh_rc = ssh_rc
        self.pi_path = pi_path
        self.install_rc = install_rc
        self.install_installs = install_installs
        self.uninstall_rc = uninstall_rc
        self.uninstall_removes = uninstall_removes
        self.installed = False

    def __call__(
        self, argv: list[str], input_text: str | None = None, capture: bool = True
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        self.inputs.append(input_text)
        self.captures.append(capture)
        if argv[0] != "ssh":
            return subprocess.CompletedProcess(
                argv, self.rsync_rc, self.rsync_stdout, ""
            )
        if any("npm uninstall" in arg for arg in argv):
            if self.uninstall_rc == 0 and self.uninstall_removes:
                self.pi_path = None
            stderr = "" if self.uninstall_rc == 0 else "npm ERR! code E404"
            return subprocess.CompletedProcess(
                argv, self.uninstall_rc, "removed\n", stderr
            )
        if any("install.sh" in arg for arg in argv):
            if self.install_rc == 0:
                # a real installer exits 0 even when the user picks "do nothing"
                self.installed = self.install_installs
            stderr = "" if self.install_rc == 0 else "curl: (22) installer failed"
            return subprocess.CompletedProcess(
                argv, self.install_rc, "installed\n", stderr
            )
        if self.ssh_rc:
            return subprocess.CompletedProcess(
                argv, self.ssh_rc, "", "ssh: connect failed"
            )
        path = self.pi_path or ("/usr/local/bin/pi" if self.installed else None)
        probe = f"PI:{path}\n" if path else ""
        return subprocess.CompletedProcess(argv, 0, probe, "")


class _TtyStdin:
    """Stand-in for an interactive terminal."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    runner = FakeRun()
    monkeypatch.setattr(cli, "run_cmd", runner)
    return runner


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    """A fake local agent dir with every syncable item present."""
    (tmp_path / "models.json").write_text("{}")
    (tmp_path / "settings.json").write_text("{}")
    (tmp_path / "auth.json").write_text("{}")
    (tmp_path / "extensions").mkdir()
    return tmp_path


def rsync_calls(fake: FakeRun) -> list[list[str]]:
    return [call for call in fake.calls if call[0] == "rsync"]


def sources(fake: FakeRun) -> list[str]:
    """The local-side path of each rsync call, for push invocations."""
    return [Path(call[-2]).name for call in rsync_calls(fake)]


@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        ({"config"}, ["models.json", "settings.json"]),
        ({"extensions"}, ["extensions"]),
        ({"auth"}, ["auth.json"]),
        ({"config", "extensions"}, ["models.json", "settings.json", "extensions"]),
        ({"config", "auth"}, ["models.json", "settings.json", "auth.json"]),
        (set(), []),
    ],
)
def test_select_items(groups: set[str], expected: list[str]) -> None:
    assert cli.select_items(groups) == expected


@pytest.mark.parametrize(
    ("argv_kwargs", "expected"),
    [
        ({}, ["rsync", "-az", "-i", "/a/models.json", "host:~/.pi/agent/models.json"]),
        (
            {"pull": True},
            ["rsync", "-az", "-i", "host:~/.pi/agent/models.json", "/a/models.json"],
        ),
        (
            {"dry_run": True},
            [
                "rsync",
                "-az",
                "-i",
                "-n",
                "/a/models.json",
                "host:~/.pi/agent/models.json",
            ],
        ),
        (
            {"delete": True},
            ["rsync", "-az", "-i", "/a/models.json", "host:~/.pi/agent/models.json"],
        ),
    ],
)
def test_rsync_argv_files(argv_kwargs: dict, expected: list[str]) -> None:
    assert (
        cli.rsync_argv("models.json", "host", Path("/a"), "~/.pi/agent", **argv_kwargs)
        == expected
    )


def test_rsync_argv_directory_gets_trailing_slashes() -> None:
    argv = cli.rsync_argv("extensions", "host", Path("/a"), "~/.pi/agent")
    assert argv[-2:] == ["/a/extensions/", "host:~/.pi/agent/extensions/"]


def test_rsync_argv_delete_only_for_directories() -> None:
    assert "--delete-during" in cli.rsync_argv(
        "extensions", "host", Path("/a"), "~/.pi/agent", delete=True
    )
    assert "--delete-during" not in cli.rsync_argv(
        "models.json", "host", Path("/a"), "~/.pi/agent", delete=True
    )


def test_rsync_argv_excludes() -> None:
    argv = cli.rsync_argv(
        "extensions", "host", Path("/a"), "~/.pi/agent", excludes=("*/logs/*",)
    )
    assert "--exclude=*/logs/*" in argv


def test_exclude_flag_passes_through(fake: FakeRun, agent_dir: Path) -> None:
    CliRunner().invoke(
        cli.main,
        ["--extensions", "-x", "*/logs/*", "--local-dir", str(agent_dir), "host"],
    )
    calls = rsync_calls(fake)
    assert calls and all("--exclude=*/logs/*" in call for call in calls)


def test_remote_dir_trailing_slash_is_not_doubled() -> None:
    argv = cli.rsync_argv("models.json", "host", Path("/a"), "~/.pi/agent/")
    assert argv[-1] == "host:~/.pi/agent/models.json"


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ([], ["models.json", "settings.json", "extensions"]),
        (["--all"], ["models.json", "settings.json", "extensions"]),
        (["--config"], ["models.json", "settings.json"]),
        (["--extensions"], ["extensions"]),
        (["--config", "--auth"], ["models.json", "settings.json", "auth.json"]),
        (
            ["--all", "--auth"],
            ["models.json", "settings.json", "extensions", "auth.json"],
        ),
    ],
)
def test_flags_select_items(
    fake: FakeRun, agent_dir: Path, flags: list[str], expected: list[str]
) -> None:
    result = CliRunner().invoke(
        cli.main, [*flags, "--local-dir", str(agent_dir), "host"]
    )
    assert result.exit_code == 0, result.output
    assert sources(fake) == expected


def test_ssh_probe_runs_before_rsync(fake: FakeRun, agent_dir: Path) -> None:
    CliRunner().invoke(cli.main, ["--local-dir", str(agent_dir), "host"])
    assert fake.calls[0] == ["ssh", "-o", "ConnectTimeout=10", "host", "sh -s"]
    assert fake.calls[1][0] == "rsync"
    # the probe script is piped over stdin so it works under fish/csh too
    assert "command -v pi" in (fake.inputs[0] or "")


def test_missing_local_item_is_skipped(fake: FakeRun, tmp_path: Path) -> None:
    (tmp_path / "models.json").write_text("{}")
    result = CliRunner().invoke(
        cli.main, ["--all", "--local-dir", str(tmp_path), "host"]
    )
    assert result.exit_code == 0
    assert sources(fake) == ["models.json"]
    assert "skipped" in result.output


def test_pull_does_not_require_local_files(fake: FakeRun, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli.main, ["--all", "--pull", "--local-dir", str(tmp_path), "host"]
    )
    assert result.exit_code == 0
    assert sources(fake) == [
        "models.json",
        "settings.json",
        "extensions",
    ]  # remote paths, pulled from


def test_default_remote_dir(fake: FakeRun, agent_dir: Path) -> None:
    CliRunner().invoke(cli.main, ["--config", "--local-dir", str(agent_dir), "host"])
    assert all("host:~/.pi/agent/" in call[-1] for call in rsync_calls(fake))


def test_remote_dir_override(fake: FakeRun, agent_dir: Path) -> None:
    CliRunner().invoke(
        cli.main,
        ["--config", "--local-dir", str(agent_dir), "--remote-dir", "/srv/pi", "host"],
    )
    assert all("host:/srv/pi/" in call[-1] for call in rsync_calls(fake))


def test_trailing_colon_is_stripped(fake: FakeRun, agent_dir: Path) -> None:
    CliRunner().invoke(
        cli.main, ["--config", "--local-dir", str(agent_dir), "sayan@host:"]
    )
    assert all(call[-1].startswith("sayan@host:") for call in rsync_calls(fake))


def test_multiple_hosts(fake: FakeRun, agent_dir: Path) -> None:
    CliRunner().invoke(cli.main, ["--config", "--local-dir", str(agent_dir), "a", "b"])
    targets = {call[-1].split(":")[0] for call in rsync_calls(fake)}
    assert targets == {"a", "b"}


def test_auth_warns_about_secrets(fake: FakeRun, agent_dir: Path) -> None:
    result = CliRunner().invoke(
        cli.main, ["--auth", "--local-dir", str(agent_dir), "host"]
    )
    assert "API keys" in result.output


def test_unreachable_host_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, agent_dir: Path
) -> None:
    monkeypatch.setattr(cli, "run_cmd", FakeRun(ssh_rc=255))
    result = CliRunner().invoke(
        cli.main, ["--config", "--local-dir", str(agent_dir), "host"]
    )
    assert result.exit_code == 1
    assert "unreachable" in result.output


def test_rsync_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, agent_dir: Path
) -> None:
    monkeypatch.setattr(cli, "run_cmd", FakeRun(rsync_rc=23))
    result = CliRunner().invoke(
        cli.main, ["--config", "--local-dir", str(agent_dir), "host"]
    )
    assert result.exit_code == 1
    assert "FAILED" in result.output


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (">f+++++++ a\n>f+++++++ b\n", (2, 0)),
        ("*deleting stale.txt\n>f+++++++++ new.txt\n", (1, 1)),
        (".d..t...... extensions/\n", (0, 0)),
        ("", (0, 0)),
    ],
)
def test_summarize(output: str, expected: tuple[int, int]) -> None:
    assert cli.summarize(output) == expected


def test_local_dir_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    assert cli.local_agent_dir() == tmp_path
    assert cli.local_agent_dir("/explicit") == Path("/explicit")


def ssh_probe_calls(fake: FakeRun) -> list[list[str]]:
    """ssh calls that are the pi probe (not the installer)."""
    return [
        c for c in fake.calls if c[0] == "ssh" and not any("install.sh" in a for a in c)
    ]


def install_calls(fake: FakeRun) -> list[list[str]]:
    return [c for c in fake.calls if any("install.sh" in a for a in c)]


class TestSshHosts:
    def test_single_host(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config"
        cfg.write_text("Host tinfoil\n  HostName tinfoil.example\n")
        assert cli.ssh_hosts(str(cfg)) == ["tinfoil"]

    def test_multiple_tokens_and_trailing_whitespace(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config"
        cfg.write_text("Host svalbard \nHost a b\n")
        assert cli.ssh_hosts(str(cfg)) == ["svalbard", "a", "b"]

    def test_wildcards_and_negations_skipped(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host *.orb.local\nHost api.example.com !api.internal\nHost real\n"
        )
        assert cli.ssh_hosts(str(cfg)) == ["api.example.com", "real"]

    def test_directives_are_case_insensitive(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config"
        cfg.write_text("HOST upper\nhost lower\n")
        assert cli.ssh_hosts(str(cfg)) == ["upper", "lower"]

    def test_comments_and_blank_lines_ignored(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config"
        cfg.write_text("# a comment\n\n   # indented comment\nHost real\n")
        assert cli.ssh_hosts(str(cfg)) == ["real"]

    def test_non_host_directives_ignored(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config"
        cfg.write_text("Host real\n  HostName example.com\n  User bob\n  Port 2222\n")
        assert cli.ssh_hosts(str(cfg)) == ["real"]

    def test_include_is_followed_and_deduped(self, tmp_path: Path) -> None:
        extra = tmp_path / "extra_config"
        extra.write_text("Host orb\nHost tinfoil\n")
        cfg = tmp_path / "config"
        cfg.write_text(f"Host tinfoil\nInclude {extra}\n")
        assert cli.ssh_hosts(str(cfg)) == ["tinfoil", "orb"]

    def test_include_glob(self, tmp_path: Path) -> None:
        (tmp_path / "conf.d").mkdir()
        (tmp_path / "conf.d" / "a").write_text("Host alpha\n")
        (tmp_path / "conf.d" / "b").write_text("Host beta\n")
        cfg = tmp_path / "config"
        cfg.write_text(f"Include {tmp_path}/conf.d/*\n")
        assert cli.ssh_hosts(str(cfg)) == ["alpha", "beta"]

    def test_include_cycle_terminates(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config"
        other = tmp_path / "other"
        cfg.write_text(f"Host one\nInclude {other}\n")
        other.write_text(f"Host two\nInclude {cfg}\n")
        assert cli.ssh_hosts(str(cfg)) == ["one", "two"]

    def test_missing_config_is_empty(self, tmp_path: Path) -> None:
        assert cli.ssh_hosts(str(tmp_path / "nope")) == []


class TestCompletion:
    @staticmethod
    def complete(incomplete: str) -> list[str]:
        """Run the completion callback the way click would."""
        ctx = click.Context(cli.main)
        items = cli.complete_target(ctx, cli.main.params[0], incomplete)
        return [item.value for item in items]

    def test_completes_matching_hosts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cli, "ssh_hosts", lambda *a, **k: ["tinfoil", "zero-frame", "phatboi"]
        )
        assert self.complete("t") == ["tinfoil"]

    def test_empty_incomplete_lists_everything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "ssh_hosts", lambda *a, **k: ["a", "b"])
        assert self.complete("") == ["a", "b"]

    def test_user_at_prefix_is_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "ssh_hosts", lambda *a, **k: ["tinfoil", "zero-frame"])
        assert self.complete("sayan@zer") == ["sayan@zero-frame"]


class TestProbe:
    def test_reports_pi_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cli, "run_cmd", FakeRun(pi_path="/home/linuxbrew/.linuxbrew/bin/pi")
        )
        assert cli.probe_host("host") == (None, "/home/linuxbrew/.linuxbrew/bin/pi")

    def test_reports_missing_pi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "run_cmd", FakeRun(pi_path=None))
        assert cli.probe_host("host") == (None, None)

    def test_reports_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "run_cmd", FakeRun(ssh_rc=255))
        error, pi_path = cli.probe_host("host")
        assert error == "ssh: connect failed"
        assert pi_path is None


class TestInstallOffering:
    def test_no_install_when_pi_present(
        self, monkeypatch: pytest.MonkeyPatch, agent_dir: Path
    ) -> None:
        fake = FakeRun(pi_path="/usr/bin/pi")
        monkeypatch.setattr(cli, "run_cmd", fake)
        result = CliRunner().invoke(cli.main, ["--local-dir", str(agent_dir), "host"])
        assert result.exit_code == 0
        assert install_calls(fake) == []

    def test_missing_pi_without_tty_skips_host(
        self, monkeypatch: pytest.MonkeyPatch, agent_dir: Path
    ) -> None:
        fake = FakeRun(pi_path=None)
        monkeypatch.setattr(cli, "run_cmd", fake)
        result = CliRunner().invoke(cli.main, ["--local-dir", str(agent_dir), "host"])
        assert result.exit_code == 1
        assert install_calls(fake) == []
        assert not any(c[0] == "rsync" for c in fake.calls)
        assert "--install" in result.output

    def test_interactive_install_needs_no_second_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The installer owns the decision; pi-sync must not pre-ask on top of it."""
        fake = FakeRun(pi_path=None)
        monkeypatch.setattr(cli, "run_cmd", fake)
        monkeypatch.setattr(sys, "stdin", _TtyStdin())
        asked: list[str] = []
        monkeypatch.setattr(
            click, "confirm", lambda *a, **k: asked.append("confirm") or False
        )
        assert cli.ensure_pi("host", assume_yes=False, remote_dir="~/.pi/agent") is True
        assert asked == []
        assert len(install_calls(fake)) == 1

    def test_install_flag_installs_then_syncs(
        self, monkeypatch: pytest.MonkeyPatch, agent_dir: Path
    ) -> None:
        fake = FakeRun(pi_path=None)
        monkeypatch.setattr(cli, "run_cmd", fake)
        result = CliRunner().invoke(
            cli.main, ["--install", "--local-dir", str(agent_dir), "host"]
        )
        assert result.exit_code == 0
        (call,) = install_calls(fake)
        assert call[-1] == cli.PI_INSTALL_CMD
        assert "pi installed" in result.output
        # a fresh install has no agent dir yet, so it is created before rsync
        assert any("mkdir -p" in arg for c in fake.calls for arg in c)
        assert any(c[0] == "rsync" for c in fake.calls)

    def test_install_failure_skips_host(
        self, monkeypatch: pytest.MonkeyPatch, agent_dir: Path
    ) -> None:
        fake = FakeRun(pi_path=None, install_rc=22)
        monkeypatch.setattr(cli, "run_cmd", fake)
        result = CliRunner().invoke(
            cli.main, ["--install", "--local-dir", str(agent_dir), "host"]
        )
        assert result.exit_code == 1
        assert "skipping host" in result.output
        assert not any(c[0] == "rsync" for c in fake.calls)

    def test_installer_that_installs_nothing_skips_host(
        self, monkeypatch: pytest.MonkeyPatch, agent_dir: Path
    ) -> None:
        """The installer exits 0 for its "do nothing" choice; that is not success."""
        fake = FakeRun(pi_path=None, install_installs=False)
        monkeypatch.setattr(cli, "run_cmd", fake)
        result = CliRunner().invoke(
            cli.main, ["--install", "--local-dir", str(agent_dir), "host"]
        )
        assert result.exit_code == 1
        assert "without installing pi" in result.output
        assert not any(c[0] == "rsync" for c in fake.calls)

    def test_dry_run_never_installs_anything(
        self, monkeypatch: pytest.MonkeyPatch, agent_dir: Path
    ) -> None:
        fake = FakeRun(pi_path=None)
        monkeypatch.setattr(cli, "run_cmd", fake)
        result = CliRunner().invoke(
            cli.main,
            ["--install", "--dry-run", "--local-dir", str(agent_dir), "host"],
        )
        assert result.exit_code == 1
        assert install_calls(fake) == []
        assert not any(c[0] == "rsync" for c in fake.calls)

    def test_interactive_install_streams_and_gets_a_tty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The installer prompts on /dev/tty, so its output must not be captured."""
        fake = FakeRun()
        monkeypatch.setattr(cli, "run_cmd", fake)
        monkeypatch.setattr(sys, "stdin", _TtyStdin())
        assert cli.install_pi("host") is None
        (call,) = install_calls(fake)
        assert "-t" in call
        assert fake.captures[0] is False  # install call streams; the re-probe captures

    def test_unattended_install_captures_and_closes_stdin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a terminal the installer gets EOF instead of blocking on a prompt."""
        fake = FakeRun()
        monkeypatch.setattr(cli, "run_cmd", fake)
        assert cli.install_pi("host") is None
        (call,) = install_calls(fake)
        assert "-t" not in call
        assert fake.inputs[0] == ""
        assert fake.captures[0] is True

    def test_no_install_attempt_when_unreachable(
        self, monkeypatch: pytest.MonkeyPatch, agent_dir: Path
    ) -> None:
        fake = FakeRun(pi_path=None, ssh_rc=255)
        monkeypatch.setattr(cli, "run_cmd", fake)
        result = CliRunner().invoke(
            cli.main, ["--install", "--local-dir", str(agent_dir), "host"]
        )
        assert result.exit_code == 1
        assert install_calls(fake) == []


class TestUninstall:
    def uninstall_calls(self, fake: FakeRun) -> list[list[str]]:
        return [c for c in fake.calls if any("npm uninstall" in a for a in c)]

    def test_uninstalls_with_the_prefix_pi_lives_in(
        self, monkeypatch: pytest.MonkeyPatch, agent_dir: Path
    ) -> None:
        fake = FakeRun(pi_path="/home/linuxbrew/.linuxbrew/bin/pi")
        monkeypatch.setattr(cli, "run_cmd", fake)
        result = CliRunner().invoke(cli.main, ["--uninstall", "host"])
        assert result.exit_code == 0, result.output
        (call,) = self.uninstall_calls(fake)
        cmd = call[-1]
        assert "npm uninstall -g --prefix /home/linuxbrew/.linuxbrew" in cmd
        assert cli.PI_PACKAGE in cmd
        assert "~/.pi/agent was left alone" in result.output
        # uninstalling never syncs
        assert not any(c[0] == "rsync" for c in fake.calls)

    def test_absent_pi_is_a_noop(
        self, monkeypatch: pytest.MonkeyPatch, agent_dir: Path
    ) -> None:
        fake = FakeRun(pi_path=None)
        monkeypatch.setattr(cli, "run_cmd", fake)
        result = CliRunner().invoke(cli.main, ["--uninstall", "host"])
        assert result.exit_code == 0
        assert "nothing to uninstall" in result.output
        assert self.uninstall_calls(fake) == []

    def test_npm_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, agent_dir: Path
    ) -> None:
        fake = FakeRun(pi_path="/usr/bin/pi", uninstall_rc=1)
        monkeypatch.setattr(cli, "run_cmd", fake)
        result = CliRunner().invoke(cli.main, ["--uninstall", "host"])
        assert result.exit_code == 1
        assert "npm ERR!" in result.output

    def test_still_present_after_uninstall_is_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, agent_dir: Path
    ) -> None:
        """npm can exit 0 having removed nothing; the re-probe has the last word."""
        fake = FakeRun(pi_path="/usr/bin/pi", uninstall_removes=False)
        monkeypatch.setattr(cli, "run_cmd", fake)
        result = CliRunner().invoke(cli.main, ["--uninstall", "host"])
        assert result.exit_code == 1
        assert "still present at /usr/bin/pi" in result.output

    def test_dry_run_uninstall_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, agent_dir: Path
    ) -> None:
        fake = FakeRun(pi_path="/usr/bin/pi")
        monkeypatch.setattr(cli, "run_cmd", fake)
        result = CliRunner().invoke(cli.main, ["--uninstall", "--dry-run", "host"])
        assert result.exit_code == 0
        assert "would run: npm uninstall" in result.output
        assert self.uninstall_calls(fake) == []

    def test_unreachable_host_fails(
        self, monkeypatch: pytest.MonkeyPatch, agent_dir: Path
    ) -> None:
        fake = FakeRun(ssh_rc=255)
        monkeypatch.setattr(cli, "run_cmd", fake)
        result = CliRunner().invoke(cli.main, ["--uninstall", "host"])
        assert result.exit_code == 1
        assert "unreachable" in result.output
        assert self.uninstall_calls(fake) == []
