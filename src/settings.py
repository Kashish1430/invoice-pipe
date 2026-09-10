"""Config loading for the pipeline.

Not in the Stage-5 layout in docs/plan.md, but every module there reads from
config/*.yaml and something has to own that read. Kept deliberately thin: a
cached YAML load plus dotted-path access, so no module hardcodes a region, a
table name or a threshold.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

_MISSING = object()


@lru_cache(maxsize=None)
def _load_yaml(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def settings_path() -> Path:
    """Active settings file. INVOICE_PIPE_SETTINGS overrides it (tests, prod)."""
    override = os.environ.get("INVOICE_PIPE_SETTINGS")
    return Path(override) if override else CONFIG_DIR / "settings.yaml"


def fx_rates_path() -> Path:
    override = os.environ.get("INVOICE_PIPE_FX_RATES")
    return Path(override) if override else CONFIG_DIR / "fx_rates.yaml"


def canonical_schema_path() -> Path:
    override = os.environ.get("INVOICE_PIPE_CANONICAL_SCHEMA")
    return Path(override) if override else CONFIG_DIR / "canonical_schema.yaml"


def settings() -> dict[str, Any]:
    return _load_yaml(str(settings_path()))


def get(dotted: str, default: Any = _MISSING) -> Any:
    """Read `aws.region`-style paths out of settings.yaml."""
    node: Any = settings()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            if default is _MISSING:
                raise KeyError(f"missing setting: {dotted}")
            return default
        node = node[part]
    return node


def data_path(kind: str) -> Path:
    """Absolute path for one of the medallion layers: raw/silver/gold/quarantine."""
    rel = get(f"paths.{kind}")
    p = Path(rel)
    return p if p.is_absolute() else PROJECT_ROOT / p


def reset_cache() -> None:
    """Drop the YAML cache. Tests call this after pointing env vars elsewhere."""
    _load_yaml.cache_clear()
