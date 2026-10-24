"""Shared CLI options retain their semantics at every command level."""
from pathlib import Path

import pytest

from tkn_codex_chat_note.cli import build_parser


@pytest.mark.parametrize("command", [
    ["config", "init"], ["config", "list"], ["clone"], ["pull"], ["build-report"],
    ["scheduled-run"], ["review-notes", "due"],
    ["review-notes", "build", "--period", "weekly"],
    ["review-notes", "validate", "review.json"],
    ["storage", "migrate", "--from-config", "old.yaml"],
    ["status"], ["provenance", "validate"], ["raw", "ingest"],
])
@pytest.mark.parametrize("options", [
    ["--source", "my-pc"], ["--config", "settings with spaces.yaml"], ["--profile", "local"],
    ["--provider", "codex"], ["--model", "test"], ["--reasoning-effort", "high"],
    ["--runtime-minutes", "30"], ["--model-timeout-seconds", "60"],
    ["--codex-executable", "codex.cmd"], ["--claude-executable", "claude.cmd"],
    ["--copilot-executable", "copilot.cmd"], ["--ollama-base-url", "http://localhost:11434"],
    ["-q"], ["--verbose"],
])
def test_common_options_before_between_and_after(command: list[str], options: list[str]) -> None:
    parser = build_parser()
    expected = vars(parser.parse_args(options + command))
    assert vars(parser.parse_args(command + options)) == expected
    assert vars(parser.parse_args(command[:1] + options + command[1:])) == expected


def test_mixed_positions_and_last_value_wins() -> None:
    args = build_parser().parse_args([
        "--source", "old", "--config", "settings.yaml", "review-notes", "--model", "test",
        "due", "--source=new", "--dry-run", "-q",
    ])
    assert args.source == "new" and args.config == Path("settings.yaml")
    assert args.model == "test" and args.quiet and args.dry_run


@pytest.mark.parametrize("first,second", [
    (["--profile", "local"], ["--provider", "codex"]),
    (["--provider", "codex"], ["--profile", "local"]),
    (["-q"], ["-v"]), (["--verbose"], ["--quiet"]),
])
@pytest.mark.parametrize("split", [0, 1])
def test_mutual_exclusion_across_command_levels(first: list[str], second: list[str], split: int) -> None:
    command = ["review-notes", "due"]
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(command[:split] + first + command[split:] + second)
    assert exc.value.code == 2


def test_subcommand_help_lists_common_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["review-notes", "due", "--help"])
    assert exc.value.code == 0
    assert "--source" in capsys.readouterr().out


def test_command_specific_defaults_are_preserved() -> None:
    args = build_parser().parse_args(["review-notes", "due", "--source", "my-pc"])
    assert args.dry_run is False and args.force is False and args.full_output is False
    assert args.allow_edited is False and args.as_of is None
    assert args.quiet is False and args.verbose is False
