"""Configuration loading.

Precedence, lowest to highest:

    config/default.yaml  <  config/local.yaml  <  ANTAR__* environment variables

Secrets are never read from YAML. They come from the environment only
(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import os
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ENV_PREFIX = "ANTAR__"
_MISSING = object()


def repo_root() -> Path:
    """The repository root, i.e. the directory holding config/."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "default.yaml").exists():
            return parent
    raise RuntimeError("could not locate repo root (no config/default.yaml above antar/)")


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _coerce(raw: str) -> Any:
    """Parse an environment override with YAML semantics (so ints stay ints)."""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def _apply_env(data: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
    out = deepcopy(data)
    for name, raw in environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        path = name[len(ENV_PREFIX) :].lower().split("__")
        cursor: dict[str, Any] = out
        for part in path[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[path[-1]] = _coerce(raw)
    return out


class Config:
    """Read-only nested configuration with dotted-path access."""

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, path: str, default: Any = _MISSING) -> Any:
        cursor: Any = self._data
        for part in path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                if default is _MISSING:
                    raise KeyError(f"config key not found: {path!r}")
                return default
            cursor = cursor[part]
        return deepcopy(cursor) if isinstance(cursor, (dict, list)) else cursor

    def section(self, path: str) -> Config:
        value = self.get(path)
        if not isinstance(value, dict):
            raise TypeError(f"config path {path!r} is not a section")
        return Config(value)

    def as_dict(self) -> dict[str, Any]:
        return deepcopy(self._data)

    def with_overrides(self, overrides: dict[str, Any]) -> Config:
        """Return a new Config with dotted-path overrides applied.

        Used by the robustness sweeps (docs/EVALUATION.md section 9.3), which must
        vary one knob at a time without mutating global state.
        """
        data = deepcopy(self._data)
        for path, value in overrides.items():
            cursor = data
            parts = path.split(".")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = value
        return Config(data)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Config(top_level={sorted(self._data)})"


def load_config(
    *,
    root: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Config:
    """Load configuration from disk. Not cached; see `get_config` for the cached view."""
    base_dir = (root or repo_root()) / "config"
    with (base_dir / "default.yaml").open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh) or {}

    local = base_dir / "local.yaml"
    if local.exists():
        with local.open("r", encoding="utf-8") as fh:
            data = _deep_merge(data, yaml.safe_load(fh) or {})

    data = _apply_env(data, dict(os.environ if environ is None else environ))
    return Config(data)


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


def artifacts_dir() -> Path:
    """Directory for generated artifacts. Created on demand."""
    path = repo_root() / get_config().get("storage.artifacts_dir", "artifacts")
    path.mkdir(parents=True, exist_ok=True)
    return path


def secret(name: str, default: str | None = None) -> str | None:
    """Read a secret from the environment. Never from YAML."""
    return os.environ.get(name, default)
