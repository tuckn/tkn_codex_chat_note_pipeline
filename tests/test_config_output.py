from __future__ import annotations

import json
from pathlib import Path

import pytest

from tkn_codex_chat_note.cli import build_parser, main
from tkn_codex_chat_note.config import CONFIG_SCHEMA_VERSION
from tkn_codex_chat_note.config_output import config_lines


def test_config_lines_copyable_paths_containers_and_scalars() -> None:
    assert config_lines({
        "path": r"C:\Users\ExampleUser\notes",
        "nested": {"items": [{"enabled": True}, False, None, 3, 1.5]},
        "empty_list": [], "empty_map": {}, "text": "日本語 = value", "empty": "",
    }) == [
        r"path=C:\Users\ExampleUser\notes",
        "nested.items[0].enabled=true", "nested.items[1]=false", "nested.items[2]=null",
        "nested.items[3]=3", "nested.items[4]=1.5", "empty_list=[]", "empty_map={}",
        "text=日本語 = value", "empty=",
    ]


def test_config_lines_escape_controls_in_keys_and_values() -> None:
    assert config_lines({"key\n": "a\r\n\t\x00\x1b\x7f\x85\u2028\u2029b"}) == [
        r"key\n=a\r\n\t\u0000\u001b\u007f\u0085\u2028\u2029b",
    ]


@pytest.mark.parametrize("flags", [[], ["--quiet"], ["--verbose"]])
def test_config_list_text_and_json_preserve_layers_and_are_readonly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flags: list[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    global_config = Path.home() / ".tkn/codex_chat_note_pipeline/config.yaml"
    project_config = tmp_path / ".tkn/config.yaml"
    explicit_config = tmp_path / "explicit.yaml"
    for path, settings in (
        (global_config, {"idle_minutes": 31}),
        (project_config, {"runtime_minutes": 32}),
        (explicit_config, {"idle_minutes": 33}),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": CONFIG_SCHEMA_VERSION, **settings}), encoding="utf-8")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-secret-never-display")

    def snapshot() -> dict[Path, bytes | None]:
        return {path: path.read_bytes() if path.is_file() else None for path in tmp_path.rglob("*")}

    before = snapshot()
    args = [*flags, "--config", str(explicit_config), "--idle-minutes", "34", "config", "list"]
    assert main(args) == 0
    text = capsys.readouterr()
    assert main([*args, "--json"]) == 0
    machine = capsys.readouterr()
    report = json.loads(machine.out)
    lines = text.out.splitlines()
    assert "command=config list" in lines
    assert "config.idle_minutes=34" in lines
    assert "config.runtime_minutes=32" in lines
    assert "sources.idle_minutes=CLI option" in lines
    assert f"sources.runtime_minutes=project: {project_config}" in lines
    assert f"layers[1].schemaVersion={CONFIG_SCHEMA_VERSION}" in lines
    assert f"configSchema.effectiveVersion={CONFIG_SCHEMA_VERSION}" in lines
    assert "configSchema.hasInMemoryMigrations=false" in lines
    assert "generationResolved.profile=codex" in lines
    assert "summaryProfile.name=default-jp" in lines
    assert all("=" in line for line in lines)
    assert report["command"] == "config list"
    assert report["config"]["idle_minutes"] == 34
    assert [layer["kind"] for layer in report["layers"]] == ["built-in", "global", "project", "explicit"]
    assert "test-secret-never-display" not in text.out + text.err + machine.out + machine.err
    if "--quiet" in flags:
        assert text.err == machine.err == ""
    else:
        assert text.err == machine.err == "[INFO] Showing resolved configuration\n"
    assert snapshot() == before


def test_config_list_help_describes_output_and_old_name_is_removed(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["config", "list", "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "key=value" in help_text and "--json" in help_text and "Read-only" in help_text
    with pytest.raises(SystemExit) as old_exit:
        main(["config", "show"])
    assert old_exit.value.code == 2
    assert capsys.readouterr().out == ""
    assert not build_parser().parse_args(["config", "list"]).json
