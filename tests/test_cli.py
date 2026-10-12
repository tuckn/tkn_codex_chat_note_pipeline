from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pytest import CaptureFixture

from tkn_codex_chat_note.chat_logs import read_thread_source
from tkn_codex_chat_note.cli import LOGGER, _configure_logging, _progress, build_parser, main
from tkn_codex_chat_note.config import CONFIG_SCHEMA_VERSION
from tkn_codex_chat_note.console_logging import SUCCESS, ColorFormatter


@pytest.mark.parametrize(
    "args",
    [
        ["init"],
        ["projects", "fetch"],
        ["session-notes", "pull"],
        ["session-notes", "rebuild"],
        ["pull", "--backfill"],
        ["decisions", "build", "--write"],
        ["working-context", "build", "--write"],
        ["config", "show"],
    ],
)
def test_retired_commands_and_compatibility_flags_are_removed(args: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(args)


@pytest.mark.parametrize("args", [["clone"], ["pull"], ["review-notes", "due"], ["scheduled-run"]])
def test_builds_write_by_default_and_have_explicit_dry_run(args: list[str]) -> None:
    assert not build_parser().parse_args(args).dry_run
    assert build_parser().parse_args([*args, "--dry-run"]).dry_run


def test_clone_dry_run_and_pull_initialization_boundary(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    from chat_fixtures import write_chat
    from test_pipeline_workflow import config_for

    from tkn_codex_chat_note.config import write_config

    config = config_for(tmp_path)
    target = tmp_path / "config.yaml"
    write_config(config, target)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    assert main(["--config", str(target), "pull", "--dry-run"]) == 1
    assert "clone" in json.loads(capsys.readouterr().out)["error"]
    assert main(["--config", str(target), "clone", "--dry-run", "--full-output"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["threads"][0]["status"] == "current"
    assert not config.data_root.exists() and result["reportPath"] is None


@pytest.mark.parametrize("complete,failed,expected", [(True, False, 0), (False, False, 2), (False, True, 1)])
def test_pipeline_exit_codes_and_summary_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
    complete: bool,
    failed: bool,
    expected: int,
) -> None:
    import tkn_codex_chat_note.pipeline as pipeline

    def fake_run(*args, **kwargs):
        return {
            "complete": complete,
            "ok": complete,
            "failed": ["failure"] if failed else [],
            "threadCounts": {},
            "scopeCounts": {},
            "threads": [{"private": "detail"}],
            "scopeResults": [],
            "rawIngest": {},
            "reportPath": "C:/reports/run.json",
        }

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run)
    assert main(["clone"]) == expected
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Run report: C:/reports/run.json" in captured.err
    assert "clone: normalized 0 conversations;" in captured.err
    assert "private" not in captured.err
    assert main(["clone", "--full-output"]) == expected
    result = json.loads(capsys.readouterr().out)
    assert result["threads"] == [{"private": "detail"}]


def test_logging_uses_readable_stderr_prefixes(
    capsys: CaptureFixture[str],
) -> None:
    args = build_parser().parse_args(["config", "list", "--json"])

    _configure_logging(args)
    LOGGER.info("Readable progress")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "[INFO] Readable progress\n"
    assert logging.getLogger().level == logging.INFO


@pytest.mark.parametrize("flags", [[], ["--verbose"], ["--quiet"]])
def test_console_suppresses_sdk_details_but_preserves_errors(
    flags: list[str], capsys: CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_logging(build_parser().parse_args([*flags, "config", "list", "--json"]))
    for name in ("azure.core.pipeline.policies.http_logging_policy", "azure.identity", "msal", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "level", logging.DEBUG)
        logger.debug("Request headers: debug details")
        logger.info("Response headers: info details")
        logger.warning("Transport warning")
        logger.error("Transport failed")
    LOGGER.info("Azure authentication succeeded: access token acquired")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "headers" not in captured.err
    assert captured.err.count("[ERROR] Transport failed\n") == 5
    assert captured.err.count("[WARNING] Transport warning\n") == (0 if "--quiet" in flags else 5)
    assert ("Azure authentication succeeded" in captured.err) == ("--quiet" not in flags)


def test_quiet_and_verbose_logging_levels() -> None:
    quiet = build_parser().parse_args(["-q", "config", "list", "--json"])
    _configure_logging(quiet)
    assert logging.getLogger().level == logging.ERROR

    verbose = build_parser().parse_args(["-v", "config", "list", "--json"])
    _configure_logging(verbose)
    assert logging.getLogger().level == logging.DEBUG


def test_jsonl_parse_warning_uses_configured_logging(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    source = tmp_path / "invalid.jsonl"
    source.write_text("{invalid}\n", encoding="utf-8")

    normal = build_parser().parse_args(["config", "list", "--json"])
    _configure_logging(normal)
    read_thread_source(source)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"[WARNING] {source}:1:" in captured.err

    quiet = build_parser().parse_args(["--quiet", "config", "list", "--json"])
    _configure_logging(quiet)
    read_thread_source(source)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_source_progress_is_human_readable(capsys: CaptureFixture[str]) -> None:
    _configure_logging(build_parser().parse_args(["config", "list", "--json"]))
    _progress({"type": "source-start", "sourceId": "pc"})
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "[INFO] Source: pc\n"


@pytest.mark.parametrize(
    ("level", "name", "color"),
    [
        (SUCCESS, "SUCCESS", "\x1b[32m"),
        (logging.WARNING, "WARNING", "\x1b[33m"),
        (logging.ERROR, "ERROR", "\x1b[31m"),
    ],
)
def test_console_formatter_colors_success_warning_and_error(
    level: int,
    name: str,
    color: str,
) -> None:
    formatter = ColorFormatter("[%(levelname)s] %(message)s", use_color=True)
    record = logging.LogRecord("test", level, __file__, 1, "message", (), None)

    assert formatter.format(record) == f"{color}[{name}] message\x1b[0m"


@pytest.mark.parametrize("level,name", [(SUCCESS, "SUCCESS"), (logging.WARNING, "WARNING")])
def test_console_formatter_keeps_redirected_output_plain(level: int, name: str) -> None:
    formatter = ColorFormatter("[%(levelname)s] %(message)s", use_color=False)
    record = logging.LogRecord("test", level, __file__, 1, "message", (), None)

    assert formatter.format(record) == f"[{name}] message"


def test_config_list_reports_source_reviews_without_summary_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert main(["-q", "config", "list", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert "summaryProfile" not in output
    assert output["config"]["sources"]["windows"]["reviews"]["language"] == "ja"
    assert "reviews" not in output["config"]
    assert output["config"]["schema_version"] == CONFIG_SCHEMA_VERSION
    assert output["configSchema"] == {
        "effectiveVersion": CONFIG_SCHEMA_VERSION, "hasInMemoryMigrations": False,
    }
    assert output["sources"]["generation.profiles.codex.bridge_profile"] == "built-in defaults"
    assert [layer["kind"] for layer in output["layers"]] == ["built-in", "global", "project"]
    assert output["layers"][0]["schemaVersion"] == CONFIG_SCHEMA_VERSION


def test_config_init_cli_creates_then_keeps_the_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn/codex_chat_note_pipeline/config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    assert main(["config", "init"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert main(["config", "init"]) == 0
    unchanged = json.loads(capsys.readouterr().out)

    assert created == {
        "command": "config init",
        "status": "created",
        "configPath": str(target),
        "backupPath": None,
    }
    assert unchanged["status"] == "unchanged"
    assert target.is_file()
    assert target.read_text(encoding="utf-8").splitlines()[0] == (f'schema_version: "{CONFIG_SCHEMA_VERSION}"')


def test_config_init_rejects_runtime_overrides(capsys: CaptureFixture[str]) -> None:
    result = main(["--model", "temporary", "config", "init"])

    assert result == 1
    output = json.loads(capsys.readouterr().out)
    assert "cannot be used with config init" in output["error"]


def test_user_summary_prompt_commands_and_override_are_not_public() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as prompt_command:
        parser.parse_args(["prompt", "init"])
    assert prompt_command.value.code == 2

    with pytest.raises(SystemExit) as prompt_override:
        parser.parse_args(["--summary-prompt", "custom.md", "config", "list", "--json"])
    assert prompt_override.value.code == 2


def test_invalid_config_returns_machine_readable_error(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("unknown_key: true\n", encoding="utf-8")

    result = main(["--config", str(target), "config", "list", "--json"])

    assert result == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False


@pytest.mark.parametrize("provider", ["claude-code", "github-copilot"])
def test_other_acquisition_providers_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str], provider: str
) -> None:
    from test_config import write_yaml

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    target = tmp_path / "config.yaml"
    write_yaml(
        target,
        {"chat": {"providers": {provider: {"sources": {"windows": {"enabled": True, "source_root": "~/source"}}}}}},
    )
    assert main(["--config", str(target), "config", "list", "--json"]) == 1
    assert "unknown configuration key: chat" in json.loads(capsys.readouterr().out)["error"]
    assert main(["--config", str(target), "raw", "ingest", "--dry-run"]) == 1
    assert "unknown configuration key: chat" in json.loads(capsys.readouterr().out)["error"]
    assert not (tmp_path / "user").exists()


def test_cli_name_and_version_match_package(capsys: CaptureFixture[str]) -> None:
    from tkn_codex_chat_note import __version__

    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"tkn-codex-chat-note {__version__}"
    help_text = build_parser().format_help()
    assert "chat.providers" not in help_text
    assert "--profile" in help_text
