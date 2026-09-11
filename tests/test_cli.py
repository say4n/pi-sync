"""Tests for pi-sync. No ssh or rsync process is ever executed."""

from __future__ import annotations

import subprocess
from pathlib import Path

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
    ):
        self.calls: list[list[str]] = []
        self.rsync_stdout = rsync_stdout
        self.rsync_rc = rsync_rc
        self.ssh_rc = ssh_rc

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[0] == "ssh":
            return subprocess.CompletedProcess(
                argv, self.ssh_rc, "", "" if self.ssh_rc == 0 else "ssh: connect failed"
            )
        return subprocess.CompletedProcess(argv, self.rsync_rc, self.rsync_stdout, "")


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


def test_ssh_preflight_runs_before_rsync(fake: FakeRun, agent_dir: Path) -> None:
    CliRunner().invoke(cli.main, ["--local-dir", str(agent_dir), "host"])
    assert fake.calls[0] == ["ssh", "-o", "ConnectTimeout=10", "host", "true"]
    assert fake.calls[1][0] == "rsync"


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
