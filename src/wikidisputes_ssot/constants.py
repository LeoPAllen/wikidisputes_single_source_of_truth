from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourcePin:
    name: str
    repository: str
    commit: str
    archive: str
    sha256: str
    authoritative: bool


REPOSITORY = "https://github.com/christinedekock11/wikidisputes"
CURRENT = SourcePin(
    name="current",
    repository=REPOSITORY,
    commit="b6f5906179724969a883ebf3012064765edf133e",
    archive="data.tar.gz",
    sha256="52544dd6ae28963cf77c39069a8e043d89e3cfa297acae584ad82e81c881ad4d",
    authoritative=True,
)
HISTORICAL = SourcePin(
    name="historical_pre_edit_removal",
    repository=REPOSITORY,
    commit="9a82336b4a493f6aeddd89ca2edf56dd2dd41ca5",
    archive="data.tar.gz",
    sha256="dd3b56a68d1d1766e8e5bf3f5f510362ca5d1a5eff2c34f8cbc560f6378e5252",
    authoritative=True,
)
SAMPLED = SourcePin(
    name="sampled_derivative",
    repository=REPOSITORY,
    commit=CURRENT.commit,
    archive="sampled_data.json.zip",
    sha256="f3db3613680ff79d0a7bdd453a58a193da0a73a4c69cb073ba69ddc9cefd081b",
    authoritative=False,
)

EXPECTED_COUNTS = {
    "discussions": {"escalated": 217, "non_escalated": 9006, "total": 9223},
    "rows": {"escalated": 4441, "non_escalated": 133019, "total": 137460},
    "types": {"original": 99502, "modification": 30722, "restoration": 7236},
}

CROSS_LABEL_DISCUSSION_IDS = (
    "514025327.9787.9787",
    "545620272.7408.7408",
    "509266297.83325.83325",
    "512260994.112001.112001",
    "502277435.16708.16708",
)

SCHEMA_VERSION = "1.0.1"
IDENTITY_VERSION = "1.0.2"
JOIN_CONTRACT_VERSION = "1.0.3"
REPRESENTATION_VERSION = "1.0.0"
DV_VERSION = "1.0.0"
PROJECTION_SERIALIZATION_VERSION = "source-projection-v1"
