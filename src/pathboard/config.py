"""Strict configuration loading.

Every experimental parameter must be present in the config file.  Missing keys
and unknown keys are both errors, so a config file is a complete and exact
record of what ran.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from pathlib import Path
from typing import Any, TypeVar

import yaml

T = TypeVar("T")


class ConfigError(ValueError):
    pass


def load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _is_dataclass_type(tp: Any) -> bool:
    return isinstance(tp, type) and dataclasses.is_dataclass(tp)


def _convert(value: Any, tp: Any, where: str) -> Any:  # noqa: C901  one branch per supported type
    origin = typing.get_origin(tp)
    if _is_dataclass_type(tp):
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected a mapping for {tp.__name__}")
        return from_dict(tp, value, where)
    if origin in (list, tuple):
        (inner, *_rest) = typing.get_args(tp)
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{where}: expected a list")
        items = [_convert(v, inner, f"{where}[{i}]") for i, v in enumerate(value)]
        return tuple(items) if origin is tuple else items
    if origin is dict:
        _kt, vt = typing.get_args(tp)
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected a mapping")
        return {k: _convert(v, vt, f"{where}.{k}") for k, v in value.items()}
    if origin in (typing.Union, types.UnionType):
        args = typing.get_args(tp)
        if value is None and type(None) in args:
            return None
        errors = []
        for a in args:
            if a is type(None):
                continue
            try:
                return _convert(value, a, where)
            except ConfigError as exc:
                errors.append(str(exc))
        raise ConfigError(f"{where}: no union member matched ({'; '.join(errors)})")
    if origin is typing.Literal:
        if value not in typing.get_args(tp):
            raise ConfigError(f"{where}: {value!r} not in {typing.get_args(tp)}")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
        return float(value)
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: expected an integer, got {value!r}")
        return value
    if tp is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true/false, got {value!r}")
        return value
    if tp is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a string, got {value!r}")
        return value
    if tp is Any:
        return value
    raise ConfigError(f"{where}: unsupported config type {tp!r}")


def from_dict(cls: type[T], data: dict[str, Any], where: str = "config") -> T:
    """Build dataclass ``cls`` from ``data``; missing or extra keys are errors."""
    hints = typing.get_type_hints(cls)
    names = [f.name for f in dataclasses.fields(cls)]
    missing = [n for n in names if n not in data]
    extra = [k for k in data if k not in names]
    if missing:
        raise ConfigError(f"{where}: missing required keys {missing}")
    if extra:
        raise ConfigError(f"{where}: unknown keys {extra}")
    return cls(**{n: _convert(data[n], hints[n], f"{where}.{n}") for n in names})


def load(cls: type[T], path: str | Path) -> T:
    return from_dict(cls, load_yaml(path), str(path))
