"""Per-repository settings read from `.ai-review.yml` on the PR's head commit.

Example:
    enabled: true
    min_severity: medium        # low | medium | high | critical
    max_comments: 15
    llm_enabled: true
    fail_on: high               # PR check fails at/above this severity (or "never")
    ignored_paths:
      - "docs/*"
      - "migrations/*"
    instructions: |
      We use SQLAlchemy 2.0; flag any raw SQL string building.
"""
from __future__ import annotations

import logging
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings
from app.models import Severity

log = logging.getLogger(__name__)

CONFIG_PATH = ".ai-review.yml"


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    min_severity: Severity | None = None
    max_comments: int | None = Field(None, ge=1, le=100)
    llm_enabled: bool | None = None
    fail_on: Severity | Literal["never"] | None = None
    ignored_paths: list[str] = Field(default_factory=list)
    instructions: str = Field("", max_length=2000)


def parse_repo_config(text: str | None) -> tuple[RepoConfig, str | None]:
    """Returns (config, error message). Invalid config falls back to defaults."""
    if not text:
        return RepoConfig(), None
    try:
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("top level must be a mapping")
        return RepoConfig.model_validate(data), None
    except (yaml.YAMLError, ValidationError, ValueError) as e:
        log.warning("Invalid %s: %s", CONFIG_PATH, e)
        first_line = str(e).splitlines()[0][:200]
        return RepoConfig(), f"`{CONFIG_PATH}` is invalid ({first_line}); using defaults."


def apply_repo_config(settings: Settings, cfg: RepoConfig) -> Settings:
    """Return a copy of settings with the repo's overrides applied."""
    update: dict = {"ignored_paths": settings.ignored_paths + cfg.ignored_paths,
                    "review_instructions": cfg.instructions.strip()}
    if cfg.min_severity is not None:
        update["min_severity"] = cfg.min_severity.value
    if cfg.max_comments is not None:
        update["max_comments"] = cfg.max_comments
    if cfg.llm_enabled is not None:
        update["llm_enabled"] = cfg.llm_enabled
    if cfg.fail_on is not None:
        update["fail_on"] = cfg.fail_on.value if isinstance(cfg.fail_on, Severity) else cfg.fail_on
    return settings.model_copy(update=update)
