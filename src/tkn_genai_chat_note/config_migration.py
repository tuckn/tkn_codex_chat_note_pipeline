"""Convert a schema-9 configuration into a self-contained schema-10 file.

Schema 9 named profiles in the shared tkn_genai_bridge configuration
(``generation.profiles.<name>.bridge_profile``). Schema 10 defines every connection
in this application's file under ``genai_profiles``. The conversion reads the old
files once, never modifies them, and writes only the requested target.
"""

from __future__ import annotations

import os
import shutil
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from tkn_genai_bridge import GenAIError, Profile, load_config

from .api_settings import AzurePricing
from .config import (
    CONFIG_SCHEMA_VERSION,
    _read_layer,
    global_config_path,
    legacy_app_root,
    legacy_global_config_path,
    validate_config_file_document,
)
from .runtime import DEFAULT_MODEL_TIMEOUT_SECONDS, DEFAULT_SOURCE_ID, PipelineError, atomic_write_text

CLI_EXECUTABLE_DEFAULTS = {"codex": "codex", "claude-code": "claude", "github-copilot": "copilot",
                           "antigravity": "agy"}


def default_bridge_config_path() -> Path:
    return Path.home() / ".tkn" / "genai_bridge" / "config.yaml"


def _compact_profile(profile: Profile) -> dict[str, Any]:
    """Keep the identity fields and every non-default Bridge setting."""
    values = profile.model_dump(mode="json", exclude_defaults=True)
    result = {"provider": profile.provider, "model": profile.model, **values}
    if Profile.model_validate(result).model_dump() != profile.model_dump():
        raise PipelineError(f"cannot copy Bridge profile {profile.profile_name!r} without changing it")
    return result


def _bridge_pricing(entries: dict[str, Any]) -> dict[str, Any]:
    return {model: AzurePricing.model_validate(price).bridge_pricing().model_dump(mode="json")
            for model, price in entries.items()}


def _inline_profile(settings: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    """Translate an inline schema-9 application profile (provider/model/executable/endpoint)."""
    provider = settings.get("provider")
    if not isinstance(provider, str):
        raise PipelineError("inline generation profiles need a provider")
    effort = settings.get("reasoning_effort", "high")
    profile: dict[str, Any] = {"provider": provider, "model": settings.get("model")}
    if provider != "ollama":  # Ollama expresses reasoning through ollama.think.
        profile["reasoning_effort"] = effort
    # Schema 9 ran inline profiles with model_timeout_seconds (default 1800) as the Bridge timeout.
    profile["timeout_seconds"] = float(timeout_seconds)
    if provider in CLI_EXECUTABLE_DEFAULTS:
        executable = settings.get("executable")
        if executable and executable != CLI_EXECUTABLE_DEFAULTS[provider]:
            profile["cli"] = {"executable": executable}
    elif provider == "ollama":
        model = str(settings.get("model") or "")
        think: Any = effort if model.casefold().split(":", 1)[0] == "gpt-oss" else effort != "low"
        profile["ollama"] = {"base_url": settings.get("endpoint") or "http://127.0.0.1:11434", "think": think}
    elif provider == "azure-openai":
        authentication = settings.get("authentication") or {}
        profile["azure"] = {"endpoint": settings.get("endpoint"), "auth": "interactive_browser",
                            "token_scope": "https://ai.azure.com/.default",
                            **({"tenant_id": authentication["tenant_id"]}
                               if isinstance(authentication, dict) and authentication.get("tenant_id") else {})}
    if settings.get("pricing"):
        profile["pricing"] = _bridge_pricing(settings["pricing"])
    return profile


def _shared_profile(name: str, overrides: dict[str, Any] | None, bridge_config: Path) -> dict[str, Any]:
    """Resolve a profile exactly as schema 9 did: Bridge's built-in profiles plus one user file."""
    try:
        resolved = load_config(user_file=bridge_config,
                               overrides={"profiles": {name: overrides}} if overrides else None)
        profile = resolved.profile(name)
    except GenAIError as exc:
        where = str(bridge_config)
        if not bridge_config.is_file():
            where += " (not found; built-in profiles only)"
        raise PipelineError(
            f"cannot resolve Bridge profile {name!r} from {where}: {exc}; pass --bridge-config <path>"
        ) from None
    return _compact_profile(profile)


def _legacy_generation(generation: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Schema-9 profiles and active name, including its defaults and provider-keyed legacy form."""
    providers = generation.get("providers")
    if not isinstance(providers, dict):
        profiles = generation.get("profiles")
        if profiles is None:
            profiles = {"codex": {"bridge_profile": "codex-default"}}  # The schema-9 built-in default.
        if not isinstance(profiles, dict):
            raise PipelineError("generation.profiles must be a YAML mapping")
        return deepcopy(profiles), str(generation.get("active_profile", "codex"))
    entries: dict[str, Any] = {}
    for name, raw in providers.items():
        settings = deepcopy(raw) if isinstance(raw, dict) else {}
        settings["provider"] = name
        if "base_url" in settings:
            settings["endpoint"] = settings.pop("base_url")
        azure = settings.pop("azure", None)
        if isinstance(azure, dict):
            deployment = azure.pop("deployment", None)
            if deployment is not None:
                settings["model"] = deployment
            azure.pop("model_version", None)
            azure.pop("subscription_id", None)
            rates = {key: azure.pop(key) for key in ("input_jpy_per_million", "output_jpy_per_million",
                                                     "pricing_date") if key in azure}
            if rates and settings.get("model"):
                azure.setdefault("pricing", {}).setdefault(settings["model"], rates)
            tenant = azure.pop("tenant_id", None)
            if tenant is not None:
                azure["authentication"] = {"tenant_id": tenant}
            settings.update(azure)
        entries[str(name)] = settings
    active = str(generation.get("active_provider", generation.get("active_profile", "codex")))
    if "codex" not in entries:
        # Provider-keyed files did not replace schema 9's built-in profile, unlike `profiles`.
        entries["codex"] = {"bridge_profile": "codex-default"}
    return entries, active


def _unused_name(base: str, taken: dict[str, Any]) -> str:
    candidates = [base, f"{base}-pricing", *(f"{base}-pricing-{n}" for n in range(2, 1000))]
    return next(name for name in candidates if name not in taken)


PATH_KEYS = ("cache_root", "report_path")
SOURCE_PATH_KEYS = ("source_root", "raw_root", "data_root", "state_root")


def _rebase(document: dict[str, Any], source_dir: Path) -> list[str]:
    """Make relative paths absolute against the old file's folder, so moving the file keeps their targets."""
    changed: list[str] = []

    def fix(container: Any, key: str, label: str) -> None:
        value = container.get(key) if isinstance(container, dict) else None
        if not isinstance(value, str) or not value.strip() or value.startswith("~"):
            return
        if Path(os.path.expandvars(value)).is_absolute():
            return
        container[key] = str((source_dir / value).absolute())
        changed.append(label)

    for key in PATH_KEYS:
        fix(document, key, key)
    for source_id, settings in (document.get("sources") or {}).items():
        for key in SOURCE_PATH_KEYS:
            fix(settings, key, f"sources.{source_id}.{key}")
    for name, settings in (document.get("archives") or {}).items():
        fix(settings, "root", f"archives.{name}.root")
    fix(document.get("vault"), "root", "vault.root")
    for key in ("data_root", "state_root"):
        fix(document.get("processes"), key, f"processes.{key}")
    for name, settings in (document.get("genai_profiles") or {}).items():
        cli = settings.get("cli") if isinstance(settings, dict) else None
        executable = cli.get("executable") if isinstance(cli, dict) else None
        if isinstance(executable, str) and ("/" in executable or "\\" in executable):
            fix(cli, "executable", f"genai_profiles.{name}.cli.executable")
    return changed


def convert_schema9(document: dict[str, Any], *, bridge_config: Path) -> tuple[dict[str, Any], list[str]]:
    """Return a schema-10 document and human-readable notes about what changed."""
    result = deepcopy(document)
    notes: list[str] = []
    generation = result.pop("generation", None) or {}
    if not isinstance(generation, dict):
        raise PipelineError("generation must be a YAML mapping")
    profiles, active = _legacy_generation(generation)
    timeout = result.get("model_timeout_seconds", DEFAULT_MODEL_TIMEOUT_SECONDS)
    genai_profiles: dict[str, Any] = {}
    unchanged_copy: dict[str, str] = {}  # Bridge profile name -> a migrated entry equal to the shared profile.
    for name, entry in profiles.items():
        if not isinstance(entry, dict):
            raise PipelineError(f"generation.profiles.{name} must be a YAML mapping")
        if "bridge_profile" in entry:
            bridge_name = str(entry["bridge_profile"])
            converted = _shared_profile(bridge_name, entry.get("overrides"), bridge_config)
            if entry.get("pricing"):
                converted["pricing"] = {**converted.get("pricing", {}), **_bridge_pricing(entry["pricing"])}
            if not entry.get("overrides") and not entry.get("pricing"):
                unchanged_copy.setdefault(bridge_name, str(name))
            notes.append(f"genai_profiles.{name}: copied from Bridge profile {bridge_name!r}")
        else:
            converted = _inline_profile(entry, timeout)
            notes.append(f"genai_profiles.{name}: converted from the inline generation profile")
        for key in ("limits", "model_digest"):
            if entry.get(key) is not None:
                converted[key] = deepcopy(entry[key])
        genai_profiles[str(name)] = converted
    scenarios = (result.get("usage_report") or {}).get("price_scenarios") or {}
    for scenario_name, scenario in scenarios.items():
        if not isinstance(scenario, dict) or "bridge_profile" not in scenario:
            continue
        bridge_name = str(scenario.pop("bridge_profile"))
        target = unchanged_copy.get(bridge_name)
        if target is None:
            # Reference prices came from the shared profile itself, not from a generation override.
            target = _unused_name(bridge_name, genai_profiles)
            genai_profiles[target] = _shared_profile(bridge_name, None, bridge_config)
            unchanged_copy[bridge_name] = target
            notes.append(f"genai_profiles.{target}: copied from Bridge profile {bridge_name!r} for price scenarios")
        scenario["genai_profile"] = target
        notes.append(f"usage_report.price_scenarios.{scenario_name}: now references genai_profiles.{target}")
    explicit_sources = isinstance(result.get("sources"), dict)
    # Without a sources map, schema 9 used one built-in source with default roots.
    sources = result["sources"] if explicit_sources else {DEFAULT_SOURCE_ID: {}}
    pinned = False
    for source_id, settings in sources.items():
        if not isinstance(settings, dict):
            continue
        for kind in ("raw", "data", "state"):
            legacy = legacy_app_root() / kind / "codex" / str(source_id)
            if settings.get(kind + "_root") is None and legacy.exists():
                # Omitted roots resolved under the previous application directory.
                settings[kind + "_root"] = str(legacy)
                pinned = True
                notes.append(f"sources.{source_id}.{kind}_root: pinned to the previous default {legacy}")
    if pinned and not explicit_sources:
        result["sources"] = sources
    ordered: dict[str, Any] = {"schema_version": CONFIG_SCHEMA_VERSION}
    for key, value in result.items():
        if key != "schema_version":
            ordered[key] = value
    if genai_profiles:
        ordered["genai_profiles"] = genai_profiles
    ordered["generation"] = {"genai_profile": active}
    return ordered, notes


def _render(document: dict[str, Any], source: Path) -> str:
    body = dict(document)
    version = body.pop("schema_version")
    header = (f"# Converted by `config migrate` from {source.name} on "
              f"{datetime.now().astimezone().date().isoformat()}. Comments of the original are not kept.\n")
    text: str = yaml.safe_dump(body, allow_unicode=True, sort_keys=False)
    return header + f'schema_version: "{version}"\n' + text


def migrate_config(
    *,
    from_config: Path | None,
    target: Path | None,
    bridge_config: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Convert (schema 9) or copy (schema 10) an old configuration into the target path."""
    destination = (target or global_config_path()).expanduser().absolute()
    if from_config is not None:
        source = from_config.expanduser().absolute()
    else:
        legacy = legacy_global_config_path()
        source = legacy if legacy.is_file() else global_config_path()
    if not source.is_file():
        raise PipelineError(f"configuration to migrate is not found: {source}; pass --from-config <path>")
    bridge = (bridge_config or default_bridge_config_path()).expanduser().absolute()
    raw = _read_layer(source)
    version = raw.get("schema_version")
    major = version.partition(".")[0] if isinstance(version, str) else None
    if major == "9":
        document, notes = convert_schema9(raw, bridge_config=bridge)
    elif version == CONFIG_SCHEMA_VERSION:
        document, notes = deepcopy(raw), ["already schema " + CONFIG_SCHEMA_VERSION]
    else:
        raise PipelineError(
            f"config migrate converts schema 9 only; {source} has schema_version {version!r}. "
            "Update older files to schema 9 first (see the error shown by any ordinary command)"
        )
    rebased = _rebase(document, source.parent) if source.parent != destination.parent else []
    notes.extend(f"{label}: relative path made absolute against {source.parent}" for label in rebased)
    if major != "9" and not rebased:
        text = source.read_text(encoding="utf-8-sig")  # Copied as is, comments included.
    else:
        text = _render(document, source)
    validate_config_file_document(yaml.safe_load(text), destination)
    same = source == destination
    exists = destination.is_file()
    if exists and destination.read_text(encoding="utf-8-sig") == text:
        return {"status": "unchanged", "sourcePath": str(source), "configPath": str(destination),
                "backupPath": None, "dryRun": dry_run, "notes": notes, "sourceModified": False}
    if exists and not force and not dry_run:
        raise PipelineError(
            f"target configuration already exists: {destination}; review it, or pass --force to back it up "
            "and replace it" + (" (the source is the same file)" if same else "")
        )
    backup: Path | None = None
    if not dry_run:
        if exists:
            stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
            backup = destination.with_name(f"{destination.name}.{stamp}.bak")
            shutil.copy2(destination, backup)
        atomic_write_text(destination, text)
    return {
        "status": "planned" if dry_run else ("replaced" if exists else "created"),
        "sourcePath": str(source),
        "configPath": str(destination),
        "bridgeConfigPath": str(bridge) if major == "9" else None,
        "backupPath": str(backup) if backup else None,
        "dryRun": dry_run,
        "notes": notes,
        "targetExists": exists,
        "requiresForce": exists and not force,
        "document": text if dry_run else None,
        "sourceModified": False,
    }
