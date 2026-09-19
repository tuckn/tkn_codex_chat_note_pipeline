"""Strict layered YAML configuration for the standalone pipeline."""

from __future__ import annotations

import os
import re
import shutil
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from .api_settings import ApiLimits, AzurePricing, AzureSettings, normalize_azure_generation
from .config_validation import validate_config_layer
from .inference import InferenceProvider, validate_ollama_base_url
from .report_settings import UsageReportSettings
from .session_notes import (
    DEFAULT_IDLE_MINUTES,
    DEFAULT_MODEL,
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_RUNTIME_MINUTES,
    DEFAULT_SOURCE_ID,
    PipelineConfig,
    PipelineError,
    atomic_write_text,
)

CONFIG_SCHEMA_VERSION: Literal["8.1.0"] = "8.1.0"
_CONFIG_SCHEMA_VERSION_PARTS = (8, 1, 0)
_CONFIG_SCHEMA_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
APP_DIRECTORY_NAME = "codex_chat_note_pipeline"
CONFIG_EXAMPLE_RESOURCE = "resources/config.example.yaml"
ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max", "ultra"]
LEGACY_GENERATION_KEYS = frozenset(
    {
        "provider",
        "model",
        "reasoning_effort",
        "codex_executable",
        "claude_executable",
        "copilot_executable",
        "ollama_base_url",
    }
)
PROVIDER_TRANSPORT_DEFAULTS: dict[InferenceProvider, tuple[str, str]] = {
    "codex": ("executable", "codex"),
    "claude-code": ("executable", "claude"),
    "github-copilot": ("executable", "copilot"),
    "ollama": ("endpoint", "http://127.0.0.1:11434"),
    "azure-openai": ("endpoint", ""),
}


@dataclass(frozen=True)
class ConfigResolution:
    """Resolved configuration together with its inspectable provenance."""

    config: AppConfig
    sources: dict[str, str]
    layers: tuple[dict[str, Any], ...]
    effective_schema_version: str
    has_in_memory_migrations: bool


def default_app_root() -> Path:
    return Path.home() / ".tkn" / APP_DIRECTORY_NAME


def default_user_cache_root() -> Path:
    configured = os.getenv("XDG_CACHE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return base / APP_DIRECTORY_NAME


class AuthenticationSettings(BaseModel):
    """Optional browser authentication account selection; never credentials."""

    model_config = ConfigDict(extra="forbid")
    tenant_id: str | None = None

    @field_validator("tenant_id")
    @classmethod
    def tenant_guid(cls, value: str | None) -> str | None:
        return AzureSettings.guid(value)


def normalize_generation(value: Any) -> Any:
    """Normalize old provider-keyed layers before merging; never rewrite input files."""
    if not isinstance(value, dict):
        return value
    result = normalize_azure_generation(value)
    if {"active_provider", "providers"}.intersection(result) and {"active_profile", "profiles"}.intersection(result):
        raise ValueError("generation cannot contain both legacy provider keys and named profile keys")
    if "active_provider" in result:
        result["active_profile"] = result.pop("active_provider")
    if "providers" in result:
        entries = result.pop("providers")
        if not isinstance(entries, dict):
            raise ValueError("generation.providers must be a mapping")
        for name, settings in entries.items():
            if name not in PROVIDER_TRANSPORT_DEFAULTS:
                raise ValueError(f"unsupported legacy inference provider: {name}")
            if not isinstance(settings, dict):
                continue
            if "provider" in settings:
                raise ValueError("legacy providers entries cannot override their provider identity")
            settings["provider"] = name
            if "base_url" in settings:
                if "endpoint" in settings:
                    raise ValueError("cannot contain both base_url and endpoint")
                settings["endpoint"] = settings.pop("base_url")
            if "azure" in settings:
                azure = settings.pop("azure")
                if not isinstance(azure, dict):
                    raise ValueError("legacy azure must be a mapping")
                tenant = azure.pop("tenant_id", None)
                if tenant is not None:
                    azure["authentication"] = {"tenant_id": tenant}
                if set(azure).intersection(settings):
                    raise ValueError("conflicting legacy azure and profile settings")
                settings.update(azure)
        result["profiles"] = entries
    return result


class ProviderConfig(BaseModel):
    """One named generation profile with an explicit transport provider."""

    model_config = ConfigDict(extra="forbid")

    provider: InferenceProvider
    model: str
    reasoning_effort: ReasoningEffort = "high"
    executable: str | None = None
    endpoint: str | None = None
    authentication: AuthenticationSettings | None = None
    pricing: dict[str, AzurePricing] = Field(default_factory=dict)
    limits: ApiLimits | None = None
    model_digest: str | None = None

    @field_validator("model")
    @classmethod
    def require_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be empty")
        return value.strip()

    @field_validator("executable")
    @classmethod
    def require_executable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("provider executable must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def validate_transport(self) -> Self:
        if self.provider == "azure-openai":
            if self.endpoint is None:
                raise ValueError("azure-openai requires endpoint")
            if self.executable is not None or self.model_digest is not None:
                raise ValueError("azure-openai does not support executable/model_digest")
            self.endpoint = AzureSettings.endpoint_url(self.endpoint)
            if self.limits is None:
                self.limits = ApiLimits()
        else:
            if self.authentication is not None or self.pricing:
                raise ValueError("authentication/pricing require azure-openai provider")
            if self.provider == "ollama":
                if self.executable is not None:
                    raise ValueError("ollama does not support executable")
                self.endpoint = validate_ollama_base_url(
                    self.endpoint if self.endpoint is not None else "http://127.0.0.1:11434")
            else:
                if self.endpoint is not None or self.limits is not None or self.model_digest is not None:
                    raise ValueError("endpoint/limits/model_digest are only supported for API providers")
                if self.executable is None:
                    self.executable = PROVIDER_TRANSPORT_DEFAULTS[self.provider][1]
        return self

    @property
    def azure(self) -> AzureSettings | None:
        # Preserve the internal API/cache contract across this configuration-only migration.
        if self.provider != "azure-openai":
            return None
        assert self.endpoint is not None
        return AzureSettings(endpoint=self.endpoint, pricing=self.pricing,
                             tenant_id=self.authentication.tenant_id if self.authentication else None)

    def inference_options(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.azure is not None:
            result["azure"] = self.azure.model_dump(mode="json")
        if self.limits is not None:
            result["limits"] = self.limits.model_dump(mode="json")
        if self.model_digest is not None:
            result["model_digest"] = self.model_digest
        return result


def _default_profiles() -> dict[str, ProviderConfig]:
    return {"codex": ProviderConfig(provider="codex", model=DEFAULT_MODEL)}


class GenerationConfig(BaseModel):
    """Named execution profiles, independent of the Session Note language profile."""

    model_config = ConfigDict(extra="forbid")

    session_note_profile: Literal["default-jp", "default-en"] = "default-jp"
    active_profile: str = "codex"
    profiles: dict[str, ProviderConfig] = Field(default_factory=_default_profiles)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy(cls, value: Any) -> Any:
        return normalize_generation(value)

    @model_validator(mode="after")
    def validate_profiles(self) -> Self:
        if any(not name.strip() or name != name.strip() for name in self.profiles):
            raise ValueError("profile names must be nonempty with no surrounding whitespace")
        if self.active_profile not in self.profiles:
            raise ValueError(f"active_profile {self.active_profile!r} must have a matching entry under profiles")
        return self


def validate_source_id(value: str) -> str:
    """Preserve exact identity while requiring a portable path component."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError(
            "source_id must use only ASCII letters, digits, dot, underscore, or hyphen; "
            "start with a letter or digit; lowercase kebab-case is recommended"
        )
    if value.endswith(".") or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", value.split(".")[0]):
        raise ValueError("source_id must not end with a dot or use a Windows reserved device name")
    return value


SourceId = Annotated[str, AfterValidator(validate_source_id)]


class CodexSourceConfig(BaseModel):
    """One local Codex input and its owned output roots; the map key is its ID."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    source_root: Path = Field(default_factory=lambda: Path.home() / ".codex")
    include_archived: bool = True
    raw_root: Path | None = None
    data_root: Path | None = None
    state_root: Path | None = None

    @field_validator("source_root", "raw_root", "data_root", "state_root", mode="before")
    @classmethod
    def require_path(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("source paths must not be empty")
        return value


class AppConfig(BaseModel):
    """Codex acquisition sources and independent inference backend selection."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["8.1.0"] = CONFIG_SCHEMA_VERSION
    installed_at: datetime | None = None
    sources: dict[SourceId, CodexSourceConfig] = Field(default_factory=lambda: {DEFAULT_SOURCE_ID: CodexSourceConfig()})
    cache_root: Path = Field(default_factory=default_user_cache_root)
    report_path: Path = Field(default_factory=lambda: default_app_root() / "reports")
    usage_report: UsageReportSettings = Field(default_factory=UsageReportSettings)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    idle_minutes: int = Field(default=DEFAULT_IDLE_MINUTES, ge=0)
    runtime_minutes: int = Field(default=DEFAULT_RUNTIME_MINUTES, gt=0)
    model_timeout_seconds: int = Field(default=DEFAULT_MODEL_TIMEOUT_SECONDS, gt=0)

    _selected_source: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def unique_source_ids(self) -> Self:
        if len({name.casefold() for name in self.sources}) != len(self.sources):
            raise ValueError("source_id keys must be unique ignoring case")
        return self

    def for_source(self, source_id: str) -> Self:
        if source_id not in self.sources:
            raise PipelineError(f"unknown source_id: {source_id}")
        selected = self.model_copy()
        selected._selected_source = source_id
        return selected

    def enabled_source_configs(self, source_id: str | None = None) -> list[Self]:
        if self._selected_source is not None:
            if source_id is not None and source_id != self._selected_source:
                raise PipelineError(f"unknown source_id: {source_id}")
            source_id = self._selected_source
        if source_id is not None and source_id not in self.sources:
            raise PipelineError(f"unknown source_id: {source_id}")
        enabled = [
            self.for_source(name)
            for name, source in self.sources.items()
            if source.enabled and (source_id is None or name == source_id)
        ]
        if not enabled:
            raise PipelineError("no enabled Codex source selected; set sources.<source_id>.enabled to true")
        return enabled

    @property
    def source_identity(self) -> tuple[str, str]:
        # Published storage/evidence identities retain the acquisition application.
        return ("codex", self.source_id)

    @property
    def source_settings(self) -> CodexSourceConfig:
        return self.sources[self.source_id]

    @property
    def source_root(self) -> Path:
        return self.source_settings.source_root

    @property
    def codex_home(self) -> Path:
        """Internal adapter convenience; the public configuration uses source_root."""
        return self.source_root

    @property
    def source_id(self) -> str:
        if self._selected_source is not None:
            return self._selected_source
        names = [name for name, source in self.sources.items() if source.enabled]
        if len(names) != 1:
            raise PipelineError("select exactly one enabled source with --source <source_id>")
        return names[0]

    @property
    def source_provider(self) -> str:
        return "codex"

    def source_storage_paths(self, source_id: str | None = None) -> dict[str, Path]:
        source_id = source_id if source_id is not None else self.source_id
        settings = self.sources[source_id]
        paths = {
            kind: (getattr(settings, kind + "_root") or default_app_root() / kind / "codex" / source_id)
            .expanduser()
            .absolute()
            for kind in ("raw", "data", "state")
        }
        paths["cache"] = self.cache_root.expanduser().absolute() / "codex" / source_id
        return paths

    @property
    def raw_root(self) -> Path:
        return self.source_storage_paths()["raw"]

    @property
    def data_root(self) -> Path:
        return self.source_storage_paths()["data"]

    @property
    def state_root(self) -> Path:
        return self.source_storage_paths()["state"]

    @property
    def source_raw_root(self) -> Path:
        return self.raw_root

    @property
    def source_data_root(self) -> Path:
        return self.data_root

    @property
    def source_state_root(self) -> Path:
        return self.state_root

    @property
    def source_cache_root(self) -> Path:
        return self.source_storage_paths()["cache"]

    @property
    def include_archived(self) -> bool:
        return self.source_settings.include_archived

    def require_supported_chat_sources(self) -> None:
        self.enabled_source_configs()

    @property
    def sessions_root(self) -> Path:
        return self.codex_home / "sessions"

    @property
    def app_state_path(self) -> Path:
        return self.codex_home / ".codex-global-state.json"

    @property
    def registry_path(self) -> Path:
        return self.source_data_root / "project-registry.jsonl"

    @property
    def projects_data_root(self) -> Path:
        return self.source_data_root / "projects"

    @property
    def projects_state_root(self) -> Path:
        return self.source_state_root / "projects"

    @property
    def reports_root(self) -> Path:
        return self.source_state_root / "reports"

    @property
    def provider(self) -> InferenceProvider:
        return self.active_provider_config.provider

    @property
    def active_provider_config(self) -> ProviderConfig:
        return self.generation.profiles[self.generation.active_profile]

    @property
    def model(self) -> str:
        return self.active_provider_config.model

    @property
    def reasoning_effort(self) -> ReasoningEffort:
        return self.active_provider_config.reasoning_effort

    def _provider_executable(self, provider: InferenceProvider, default: str) -> str:
        settings = self.active_provider_config if self.provider == provider else None
        return settings.executable if settings is not None and settings.executable is not None else default

    @property
    def codex_executable(self) -> str:
        return self._provider_executable("codex", "codex")

    @property
    def claude_executable(self) -> str:
        return self._provider_executable("claude-code", "claude")

    @property
    def copilot_executable(self) -> str:
        return self._provider_executable("github-copilot", "copilot")

    @property
    def ollama_base_url(self) -> str:
        settings = self.active_provider_config if self.provider == "ollama" else None
        if settings is not None and settings.endpoint is not None:
            return settings.endpoint
        return "http://127.0.0.1:11434"

    def session_note_pipeline_config(self, *, allow_missing_watermark: bool = False) -> PipelineConfig:
        installed_at = self.installed_at
        if installed_at is None:
            if not allow_missing_watermark:
                raise PipelineError("installed_at is missing; run `tkn-codex-chat-note init` first")
            installed_at = datetime.now().astimezone()
        return PipelineConfig(
            installed_at=installed_at.astimezone().isoformat(timespec="seconds"),
            sessions_root=self.sessions_root,
            raw_root=self.raw_root,
            source_id=self.source_id,
            session_note_profile=self.generation.session_note_profile,
            provider=self.provider,
            generation_profile=self.generation.active_profile,
            codex_bin=self.codex_executable,
            claude_bin=self.claude_executable,
            copilot_bin=self.copilot_executable,
            ollama_base_url=self.ollama_base_url,
            inference_options=self.active_provider_config.inference_options(),
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            idle_minutes=self.idle_minutes,
            runtime_minutes=self.runtime_minutes,
            model_timeout_seconds=self.model_timeout_seconds,
        )


def global_config_path() -> Path:
    return default_app_root() / "config.yaml"


def project_config_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / ".tkn" / "config.yaml"


class _UniqueKeyLoader(yaml.SafeLoader):  # type: ignore[misc]  # PyYAML has no bundled type stubs.
    """Reject duplicate YAML keys instead of silently discarding a source."""


def _unique_mapping(loader: _UniqueKeyLoader, node: Any) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise PipelineError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _read_layer(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8-sig"), Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise PipelineError(f"cannot read config: {path}: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PipelineError(f"config must contain a YAML mapping: {path}")
    return {str(key): item for key, item in value.items()}


def _leaf_paths(value: Any, prefix: str = "") -> tuple[str, ...]:
    if isinstance(value, dict) and value:
        return tuple(
            child
            for key, item in value.items()
            for child in _leaf_paths(item, f"{prefix}.{key}" if prefix else str(key))
        )
    return (prefix,)


def _mark_sources(sources: dict[str, str], value: dict[str, Any], label: str) -> None:
    for path in _leaf_paths(value):
        if path:
            sources[path] = label


def _deep_merge(target: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        current = target.get(key)
        if (key == "profiles" and isinstance(value, dict) and isinstance(current, dict)):
            for name, settings in value.items():
                previous = current.get(name)
                if (isinstance(settings, dict) and isinstance(previous, dict)
                        and "provider" in settings and settings["provider"] != previous.get("provider")):
                    current[name] = deepcopy(settings)
                elif isinstance(settings, dict) and isinstance(previous, dict):
                    _deep_merge(previous, settings)
                else:
                    current[name] = deepcopy(settings)
        elif key == "sources" and value == {}:
            target[key] = {}
        elif isinstance(current, dict) and isinstance(value, dict):
            _deep_merge(current, value)
        else:
            target[key] = value


def _without_null_provider_settings(value: dict[str, Any]) -> dict[str, Any]:
    generation = value.get("generation")
    providers = generation.get("profiles") if isinstance(generation, dict) else None
    if isinstance(providers, dict):
        for settings in providers.values():
            if isinstance(settings, dict):
                for key in tuple(settings):
                    if settings[key] is None:
                        settings.pop(key)
    return value


def _default_config_document() -> dict[str, Any]:
    document = _without_null_provider_settings(AppConfig().model_dump(mode="python", by_alias=True))
    # Defer source defaults until validation: an explicit map must not acquire a
    # hidden built-in source. Later layers merge declared sources by stable ID.
    document.pop("sources")
    return document


def _reject_legacy_generation_config(value: dict[str, Any], path: Path) -> None:
    legacy_keys = sorted(LEGACY_GENERATION_KEYS.intersection(value))
    schema_version = value.get("schema_version")
    schema_v1 = schema_version == 1 or (isinstance(schema_version, str) and schema_version.partition(".")[0] == "1")
    if schema_v1 or legacy_keys:
        detail = f"; retired keys: {', '.join(legacy_keys)}" if legacy_keys else ""
        raise PipelineError(
            f"configuration schema v1 is no longer supported: {path}{detail}; "
            f'set schema_version: "{CONFIG_SCHEMA_VERSION}" and move generation settings under '
            "generation.active_profile and generation.profiles"
        )


def _inspect_config_schema(value: dict[str, Any], path: Path) -> dict[str, Any]:
    if "schema_version" not in value:
        raise PipelineError(
            f'configuration schema_version is required: {path}; set schema_version: "{CONFIG_SCHEMA_VERSION}"'
        )
    raw_version = value["schema_version"]
    if raw_version == 2 or isinstance(raw_version, str) and re.fullmatch(r"[234]\.[0-9]+\.[0-9]+", raw_version):
        raise PipelineError(
            "legacy configuration requires a new schema-7 config with source-local roots; "
            "use `storage migrate --from-config <old-config> --dry-run` with the new --config; "
            "the old configuration and data are not modified"
        )

    if isinstance(raw_version, str) and re.fullmatch(r"[56]\.[0-9]+\.[0-9]+", raw_version):
        raise PipelineError(
            "configuration schema 5/6 requires an explicit config-only update: "
            "move chat.providers.codex.sources to top-level sources (for schema 5, "
            "key the Codex entry by its source_id and rename home to source_root); "
            'remove chat and set schema_version to "7.0.0"; retain IDs and final raw/data/state roots, '
            "including old default paths; storage 5 needs no data migration. "
            "Generation providers remain under generation.providers"
        )
    if not isinstance(raw_version, str) or not _CONFIG_SCHEMA_VERSION_PATTERN.fullmatch(raw_version):
        raise PipelineError(
            f"invalid configuration schema_version {raw_version!r}: {path}; "
            f'expected a quoted MAJOR.MINOR.PATCH value such as "{CONFIG_SCHEMA_VERSION}"'
        )
    parts = tuple(int(part) for part in raw_version.split("."))
    current_major, current_minor, _current_patch = _CONFIG_SCHEMA_VERSION_PARTS
    major, minor, _patch = parts
    if major == 7 and minor > 2:
        raise PipelineError(f"unsupported newer configuration schema_version {raw_version!r}; "
                            "only schema 7.0-7.2 can migrate to schema 8")
    if major == 7 and minor <= 2:
        return {
            "schemaVersion": raw_version,
            "effectiveSchemaVersion": CONFIG_SCHEMA_VERSION,
            "migration": {"kind": "named-generation-profiles", "fromVersion": raw_version,
                          "toVersion": CONFIG_SCHEMA_VERSION, "persistentConfigUpdated": False},
        }
    if major != current_major:
        direction = "newer" if major > current_major else "older"
        action = (
            "upgrade tkn-codex-chat-note"
            if major > current_major
            else "migrate the configuration explicitly; no migration path is available"
        )
        raise PipelineError(
            f"unsupported {direction} configuration schema_version {raw_version!r}: {path}; "
            f"this application supports schema versions through {CONFIG_SCHEMA_VERSION} "
            f"within major {current_major}; {action}"
        )
    if minor > current_minor:
        raise PipelineError(
            f"unsupported newer configuration schema_version {raw_version!r}: {path}; "
            f"this application supports schema versions through {CONFIG_SCHEMA_VERSION}; "
            "upgrade tkn-codex-chat-note"
        )
    migration: dict[str, Any] | None = None
    if minor < current_minor:
        migration = {
            "kind": "compatible-version-normalization",
            "fromVersion": raw_version,
            "toVersion": CONFIG_SCHEMA_VERSION,
            "persistentConfigUpdated": False,
        }
    return {
        "schemaVersion": raw_version,
        "effectiveSchemaVersion": CONFIG_SCHEMA_VERSION,
        "migration": migration,
    }


def _config_properties(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("schema_version", None)
    return result


def _resolve_paths(value: dict[str, Any], base: Path) -> dict[str, Any]:
    result = deepcopy(value)

    def resolve(raw: Any) -> Path:
        expanded_text = os.path.expandvars(str(raw))
        if expanded_text == "~":
            expanded = Path.home()
        elif expanded_text.startswith(("~/", "~\\")):
            expanded = Path.home() / expanded_text[2:]
        else:
            expanded = Path(expanded_text).expanduser()
        return expanded if expanded.is_absolute() else (base / expanded).absolute()

    for key in ("raw_root", "data_root", "state_root", "cache_root", "report_path"):
        if result.get(key) is not None:
            result[key] = resolve(result[key])
    source_map = result.get("sources")
    if isinstance(source_map, dict):
        if len({str(key).casefold() for key in source_map}) != len(source_map):
            raise PipelineError("source_id keys must be unique ignoring case")
        for settings in source_map.values():
            if isinstance(settings, dict):
                for key in ("source_root", "raw_root", "data_root", "state_root"):
                    if settings.get(key) is not None:
                        if isinstance(settings[key], str) and not settings[key].strip():
                            raise PipelineError("source paths must not be empty")
                        settings[key] = resolve(settings[key])
    chat = result.get("chat")
    providers = chat.get("providers") if isinstance(chat, dict) else None
    if isinstance(providers, dict):
        for provider in providers.values():
            if isinstance(provider, dict):
                # Legacy standalone migration readers still resolve home here.
                entries = provider.get("sources", {"legacy": provider})
                if not isinstance(entries, dict):
                    continue
                if "sources" in provider and len({str(key).casefold() for key in entries}) != len(entries):
                    raise PipelineError("source_id keys must be unique ignoring case within a provider")
                for settings in entries.values():
                    if not isinstance(settings, dict):
                        continue
                    for key in ("source_root", "home", "raw_root", "data_root", "state_root"):
                        if settings.get(key) is not None:
                            if isinstance(settings[key], str) and not settings[key].strip():
                                raise PipelineError("chat source paths must not be empty")
                            settings[key] = resolve(settings[key])
    return result


def _without_retired_user_prompt(
    value: dict[str, Any],
    *,
    remove_configured: bool,
) -> tuple[dict[str, Any], bool]:
    result = dict(value)
    if "summary_prompt" not in result:
        return result, False
    configured = result.pop("summary_prompt")
    if configured is not None and not remove_configured:
        raise PipelineError(
            "summary_prompt is no longer supported; remove it from config because "
            "summary profiles are application-owned"
        )
    return result, True


def _new_provider_override(provider: InferenceProvider, model: str, effort: str | None) -> dict[str, Any]:
    if provider == "azure-openai":
        raise PipelineError("configure azure-openai model (deployment) and endpoint in a config file first")
    transport_key, transport_value = PROVIDER_TRANSPORT_DEFAULTS[provider]
    return {
        "provider": provider,
        "model": model,
        "reasoning_effort": effort or "high",
        transport_key: transport_value,
    }


def _apply_runtime_overrides(
    merged: dict[str, Any],
    overrides: dict[str, Any],
    *,
    working: Path,
    sources: dict[str, str],
) -> None:
    if "schema_version" in overrides:
        raise PipelineError("schema_version is configuration-source metadata and cannot be a CLI override")
    resolved = _resolve_paths(overrides, working)
    generation_options = {key: resolved.pop(key) for key in tuple(resolved)
                          if key in LEGACY_GENERATION_KEYS or key == "profile"}
    if "generation" in resolved:
        resolved["generation"] = normalize_generation(resolved["generation"])
    _deep_merge(merged, resolved)
    _mark_sources(sources, resolved, "CLI option")
    if not generation_options:
        return
    generation = merged.get("generation")
    if not isinstance(generation, dict) or not isinstance(generation.get("profiles"), dict):
        raise PipelineError("generation.profiles must be a YAML mapping")
    profiles = generation["profiles"]
    requested = generation_options.get("profile")
    provider = generation_options.get("provider")
    if requested is not None and provider is not None:
        raise PipelineError("use either --profile or --provider, not both")
    name = str(requested or generation.get("active_profile", "codex"))
    model = generation_options.get("model")
    effort = generation_options.get("reasoning_effort")
    if provider is not None:
        if provider not in PROVIDER_TRANSPORT_DEFAULTS:
            raise PipelineError(f"unsupported inference provider: {provider}")
        matches = [key for key, item in profiles.items()
                   if isinstance(item, dict) and item.get("provider") == provider]
        if len(matches) > 1:
            raise PipelineError(f"multiple profiles use {provider!r}: {', '.join(matches)}; select --profile")
        if matches:
            name = matches[0]
        else:
            if model is None:
                raise PipelineError(f"provider {provider!r} is not configured; add a profile or pass --model")
            name = str(provider)
            if name in profiles:
                raise PipelineError(f"profile name {name!r} already belongs to another provider; configure --profile")
            profiles[name] = _new_provider_override(provider, str(model), str(effort) if effort else None)
            _mark_sources(sources, {"generation": {"profiles": {name: profiles[name]}}}, "CLI option")
    if name not in profiles:
        raise PipelineError(f"unknown generation profile: {name}; configure generation.profiles.{name}")
    settings = profiles[name]
    if not isinstance(settings, dict):
        raise PipelineError(f"generation.profiles.{name} must be a YAML mapping")
    if requested is not None or provider is not None:
        generation["active_profile"] = name
        sources["generation.active_profile"] = "CLI option"
    for field, value in (("model", model), ("reasoning_effort", effort)):
        if value is not None:
            settings[field] = value
            sources[f"generation.profiles.{name}.{field}"] = "CLI option"
    transport_options = {
        "codex_executable": ("codex", "executable"),
        "claude_executable": ("claude-code", "executable"),
        "copilot_executable": ("github-copilot", "executable"),
        "ollama_base_url": ("ollama", "endpoint"),
    }
    for option, (kind, field) in transport_options.items():
        if option not in generation_options:
            continue
        if settings.get("provider") != kind:
            raise PipelineError(f"--{option.replace('_', '-')} requires an active {kind} profile")
        settings[field] = generation_options[option]
        sources[f"generation.profiles.{name}.{field}"] = "CLI option"


def resolve_app_config(
    *,
    explicit_path: Path | None = None,
    cwd: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> ConfigResolution:
    """Load defaults, global, project, explicit, then CLI overrides."""

    working = (cwd or Path.cwd()).absolute()
    legacy_global = Path.home() / ".tkn" / "genai_chat_note_pipeline" / "config.yaml"
    if (
        not global_config_path().exists()
        and legacy_global.is_file()
        and explicit_path is None
        and not project_config_path(working).exists()
    ):
        raise PipelineError(
            f"legacy user configuration found: {legacy_global}; copy it to {global_config_path()} "
            "and update to schema 7, preserving source IDs and final storage paths, "
            "or pass an explicit schema-7 --config; no legacy config or data is modified"
        )
    merged = _default_config_document()
    sources: dict[str, str] = {}
    _mark_sources(sources, _config_properties(merged), "built-in defaults")
    layer_specs = [
        ("global", global_config_path()),
        ("project", project_config_path(working)),
        (
            "explicit",
            explicit_path.expanduser().absolute() if explicit_path else None,
        ),
    ]
    layer_report: list[dict[str, Any]] = [
        {
            "kind": "built-in",
            "path": None,
            "exists": True,
            "schemaVersion": CONFIG_SCHEMA_VERSION,
            "effectiveSchemaVersion": CONFIG_SCHEMA_VERSION,
            "migration": None,
        }
    ]
    declared_profiles = False
    for kind, path in layer_specs:
        if path is None:
            continue
        report: dict[str, Any] = {
            "kind": kind,
            "path": str(path),
            "exists": path.is_file(),
            "schemaVersion": None,
            "effectiveSchemaVersion": None,
            "migration": None,
        }
        layer_report.append(report)
        if kind == "explicit" and not report["exists"]:
            raise PipelineError(f"explicit configuration file not found: {path}")
        if report["exists"]:
            raw_layer = _read_layer(path)
            _reject_legacy_generation_config(raw_layer, path)
            schema_report = _inspect_config_schema(raw_layer, path)
            report.update(schema_report)
            layer, _removed = _without_retired_user_prompt(
                _config_properties(raw_layer),
                remove_configured=False,
            )
            try:
                if "generation" in layer:
                    layer["generation"] = normalize_generation(layer["generation"])
                validate_config_layer(AppConfig, layer)
            except ValueError as exc:
                raise PipelineError(f"invalid configuration layer {path}: {exc}") from exc
            raw_generation = raw_layer.get("generation")
            if isinstance(raw_generation, dict) and ("profiles" in raw_generation or "providers" in raw_generation):
                if not declared_profiles and "profiles" in raw_generation:
                    merged["generation"]["profiles"] = {}
                declared_profiles = True
            resolved_layer = _resolve_paths(layer, path.parent)
            _deep_merge(merged, resolved_layer)
            _mark_sources(sources, resolved_layer, f"{kind}: {path}")
    _apply_runtime_overrides(
        merged,
        overrides or {},
        working=working,
        sources=sources,
    )
    try:
        config = AppConfig.model_validate(merged)
    except Exception as exc:
        raise PipelineError(f"invalid configuration: {exc}") from exc
    resolved = config.model_copy(
        update={
            "cache_root": config.cache_root.expanduser().absolute(),
        }
    )
    effective_leaves = _leaf_paths(_config_properties(resolved.model_dump(mode="python", by_alias=True)))
    sources = {key: sources.get(key, "built-in defaults") for key in effective_leaves}
    return ConfigResolution(
        config=resolved,
        sources=sources,
        layers=tuple(layer_report),
        effective_schema_version=CONFIG_SCHEMA_VERSION,
        has_in_memory_migrations=any(layer["migration"] is not None for layer in layer_report),
    )


def load_app_config(
    *,
    explicit_path: Path | None = None,
    cwd: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """Load and return the effective application configuration."""

    return resolve_app_config(
        explicit_path=explicit_path,
        cwd=cwd,
        overrides=overrides,
    ).config


def config_example_text() -> str:
    """Read the application-owned example distributed in the package."""

    try:
        return files("tkn_codex_chat_note").joinpath(CONFIG_EXAMPLE_RESOURCE).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise PipelineError(f"packaged config example is unavailable: {exc}") from exc


def initialize_user_config(
    path: Path | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Create the user config safely from the packaged example."""

    target = (path or global_config_path()).expanduser().absolute()
    legacy_global = Path.home() / ".tkn" / "genai_chat_note_pipeline" / "config.yaml"
    if path is None and not target.exists() and legacy_global.is_file():
        raise PipelineError(
            f"legacy user configuration found: {legacy_global}; copy it to {target} "
            "and update to schema 7 with top-level sources and existing final storage paths; "
            "use --config <new-path> config init only to create a separate configuration"
        )
    example = config_example_text()
    expected = example.encode("utf-8")
    backup_path: Path | None = None

    if target.exists() and not target.is_file():
        raise PipelineError(f"config target is not a file: {target}")
    if target.is_file():
        try:
            current = target.read_bytes()
        except OSError as exc:
            raise PipelineError(f"cannot read config: {target}: {exc}") from exc
        if current == expected:
            return {
                "status": "unchanged",
                "configPath": str(target),
                "backupPath": None,
            }
        if not force:
            raise PipelineError(
                f"config already exists with different content: {target}; "
                "review it or use `config init --force` to back it up and replace it"
            )
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
        backup_path = target.with_name(f"{target.name}.{stamp}.bak")
        try:
            shutil.copy2(target, backup_path)
        except OSError as exc:
            raise PipelineError(f"cannot back up config: {target}: {exc}") from exc

    try:
        atomic_write_text(target, example)
    except OSError as exc:
        raise PipelineError(f"cannot write config: {target}: {exc}") from exc
    return {
        "status": "replaced" if backup_path else "created",
        "configPath": str(target),
        "backupPath": str(backup_path) if backup_path else None,
    }


def config_document(config: AppConfig) -> dict[str, Any]:
    document = _without_null_provider_settings(config.model_dump(mode="json", by_alias=True))
    for source_id, settings in document["sources"].items():
        for kind, path in config.source_storage_paths(source_id).items():
            if kind != "cache":
                settings[kind + "_root"] = str(path)
    return document


def initialization_config(
    path: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    refresh_installed_at: bool = True,
) -> tuple[AppConfig, Path, tuple[str, ...]]:
    """Load the target config for init, tolerating only retired init-owned keys."""

    target = (path or global_config_path()).expanduser().absolute()
    if not target.is_file():
        raise PipelineError(f"config not found: {target}; run `tkn-codex-chat-note config init` first")
    raw: dict[str, Any] = {}
    removed: list[str] = []
    if target.is_file():
        raw = _read_layer(target)
        _reject_legacy_generation_config(raw, target)
        _inspect_config_schema(raw, target)
        raw = _config_properties(raw)
        raw, removed_summary_prompt = _without_retired_user_prompt(
            raw,
            remove_configured=True,
        )
        if removed_summary_prompt:
            removed.append("summary_prompt")
        for key in ("context_store_root",):
            if key in raw:
                raw.pop(key)
                removed.append(key)
        if "generation" in raw:
            raw["generation"] = normalize_generation(raw["generation"])
        raw = _resolve_paths(raw, target.parent)
    merged = _default_config_document()
    original_generation = _read_layer(target).get("generation")
    if isinstance(original_generation, dict) and "profiles" in original_generation:
        merged["generation"]["profiles"] = {}
    _deep_merge(merged, raw)
    _apply_runtime_overrides(
        merged,
        overrides or {},
        working=Path.cwd().absolute(),
        sources={},
    )
    if refresh_installed_at:
        merged["installed_at"] = datetime.now().astimezone()
    try:
        config = AppConfig.model_validate(merged)
    except Exception as exc:
        raise PipelineError(f"invalid configuration: {exc}") from exc
    return (
        config.model_copy(
            update={
                "cache_root": config.cache_root.expanduser().absolute(),
            }
        ),
        target,
        tuple(removed),
    )


def write_config(config: AppConfig, target: Path) -> None:
    document = config_document(config)
    schema_version = document.pop("schema_version")
    text = yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
    )
    atomic_write_text(target, f'schema_version: "{schema_version}"\n{text}')
