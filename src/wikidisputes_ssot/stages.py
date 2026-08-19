from __future__ import annotations

import datetime as dt
import json
import os
import platform
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hashing import canonical_json_hash
from .io import atomic_write_json


@dataclass(frozen=True)
class StageResult:
    stage: str
    status: str
    marker: Path
    payload: dict[str, Any]


def code_identifier(repository_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return result.stdout.strip() + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


class StageRunner:
    """Dependency/config keyed success markers with atomic completion."""

    def __init__(self, root: Path, repository_root: Path) -> None:
        self.root = root
        self.repository_root = repository_root
        self.root.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        name: str,
        canonical_inputs: dict[str, Any],
        function: Callable[[], dict[str, Any]],
        *,
        force: bool = False,
    ) -> StageResult:
        input_hash = canonical_json_hash(canonical_inputs)
        stage_root = self.root / name / input_hash
        marker = stage_root / "SUCCESS.json"
        if marker.exists() and not force:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            return StageResult(name, "cached", marker, payload)
        stage_root.mkdir(parents=True, exist_ok=True)
        payload = function()
        output = {
            "stage": name,
            "status": "success",
            "canonical_input_hash": input_hash,
            "canonical_inputs": canonical_inputs,
            "canonical_outputs": payload,
            # Provenance-only values below are deliberately excluded from input/content hashes.
            "run_provenance": {
                "completed_at_utc": dt.datetime.now(dt.UTC).isoformat(),
                "code_identifier": code_identifier(self.repository_root),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "pid": os.getpid(),
            },
        }
        atomic_write_json(marker, output)
        return StageResult(name, "success", marker, output)

    def checkpoint(self, stage: str, key: str, payload: dict[str, Any]) -> Path:
        target = self.root / stage / "checkpoints" / f"{key}.json"
        atomic_write_json(target, payload)
        return target


def deliberate_interrupt_marker(root: Path, stage: str, batch: int) -> None:
    """Test helper: write durable progress, then simulate interruption."""
    target = root / stage / "checkpoints" / f"batch-{batch:08d}.json"
    atomic_write_json(target, {"stage": stage, "completed_batch": batch})
    descriptor, temporary = tempfile.mkstemp(prefix="interrupted-", dir=target.parent)
    os.close(descriptor)
    os.unlink(temporary)
    raise InterruptedError(f"deliberate interruption after batch {batch}")
