from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Roots(BaseModel):
    data: Path = Path("data")
    cache: Path = Path("cache")
    output: Path = Path("output")
    checkpoints: Path = Path("checkpoints")


class Network(BaseModel):
    user_agent: str
    contact_is_configured: bool = False
    max_concurrency: int = Field(default=2, ge=1, le=4)
    requests_per_second: float = Field(default=1.0, gt=0, le=5)
    maxlag: int = Field(default=5, ge=1, le=30)
    timeout_seconds: int = Field(default=60, ge=10, le=300)
    max_attempts: int = Field(default=7, ge=1, le=12)

    @model_validator(mode="after")
    def identifying_agent(self) -> Network:
        if "wikidisputesssot" not in self.user_agent.lower():
            raise ValueError("network.user_agent must identify WikiDisputesSSOT")
        return self


class WikiConv(BaseModel):
    language: str = "english"
    years: list[int]
    base_url: str


class MediaWiki(BaseModel):
    endpoint: str
    parser: str = "default"


class RunOptions(BaseModel):
    batch_size: int = Field(default=1000, ge=1)
    pilot_case_limit: int = Field(default=5, ge=1)
    review_seed: int = 20260818


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    identity_algorithm_version: str
    representation_version: str
    join_contract_version: str
    dv_definition_version: str
    canonical_serialization_version: str
    roots: Roots
    network: Network
    wikiconv: WikiConv
    mediawiki: MediaWiki
    run: RunOptions

    def resolved(self, repository_root: Path) -> Settings:
        copy = self.model_copy(deep=True)
        for name in ("data", "cache", "output", "checkpoints"):
            value = getattr(copy.roots, name)
            if not value.is_absolute():
                setattr(copy.roots, name, (repository_root / value).resolve())
        return copy

    def canonical_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        # Operational roots and retrieval-only settings do not define canonical content.
        payload.pop("roots", None)
        payload.pop("network", None)
        return payload


def load_settings(path: Path, repository_root: Path) -> Settings:
    with path.open("rb") as handle:
        raw = yaml.safe_load(handle)
    return Settings.model_validate(raw).resolved(repository_root)
