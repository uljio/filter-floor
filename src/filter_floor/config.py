"""Load tunable yaml from the repo config/ directory."""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = _REPO_ROOT / "config"


def repo_root() -> Path:
    return _REPO_ROOT


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


@lru_cache(maxsize=1)
def load_vetoes() -> dict[str, Any]:
    return _load_yaml("vetoes.yaml")


@lru_cache(maxsize=1)
def load_scoring() -> dict[str, Any]:
    return _load_yaml("scoring.yaml")


@lru_cache(maxsize=1)
def load_chains() -> dict[str, Any]:
    return _load_yaml("chains.yaml")


@lru_cache(maxsize=1)
def load_outcomes() -> dict[str, Any]:
    return _load_yaml("outcomes.yaml")


@lru_cache(maxsize=1)
def load_explain() -> dict[str, Any]:
    return _load_yaml("explain.yaml")
