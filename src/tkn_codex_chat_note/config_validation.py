"""Validate every supplied configuration field before layered merging."""

from __future__ import annotations

from typing import Any, get_args, get_origin

from pydantic import BaseModel, TypeAdapter
from tkn_genai_bridge import Profile


def validate_config_layer(model: type[BaseModel], value: dict[str, Any], prefix: str = "") -> None:
    fields = {field.alias or name: field for name, field in model.model_fields.items()}
    for key, item in value.items():
        field = fields.get(key)
        location = f"{prefix}.{key}" if prefix else key
        if field is None:
            raise ValueError(f"unknown configuration key: {location}")
        annotation = field.annotation
        if key == "overrides" and "bridge_profile" in fields:
            if not isinstance(item, dict):
                raise ValueError(f"{location} must be a mapping")
            validate_config_layer(Profile, item, location)
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if not isinstance(item, dict):
                raise ValueError(f"{location} must be a mapping")
            validate_config_layer(annotation, item, location)
        elif get_origin(annotation) is dict:
            key_type, item_type = get_args(annotation)
            if not isinstance(item, dict):
                raise ValueError(f"{location} must be a mapping")
            if isinstance(item_type, type) and issubclass(item_type, BaseModel):
                for nested_key, nested_value in item.items():
                    TypeAdapter(key_type).validate_python(nested_key)
                    if not isinstance(nested_value, dict):
                        raise ValueError(f"{location}.{nested_key} must be a mapping")
                    validate_config_layer(item_type, nested_value, f"{location}.{nested_key}")
            else:
                candidates = [t for t in get_args(item_type)
                              if isinstance(t, type) and issubclass(t, BaseModel)]
                if candidates:
                    for nested_key, nested_value in item.items():
                        TypeAdapter(key_type).validate_python(nested_key)
                        if not isinstance(nested_value, dict):
                            raise ValueError(f"{location}.{nested_key} must be a mapping")
                        # Validate fragments without loading shared files before layering.
                        shared = "bridge_profile" in nested_value or "overrides" in nested_value
                        selected = candidates[0] if shared else candidates[-1]
                        validate_config_layer(selected, nested_value, f"{location}.{nested_key}")
                else:
                    TypeAdapter(field.rebuild_annotation()).validate_python(item)
        else:
            TypeAdapter(field.rebuild_annotation()).validate_python(item)
