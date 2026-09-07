"""Typer commands for the staged Method-B workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from wikidisputes_ssot.config import Settings

from .workflow import (
    MethodBPaths,
    final_invariants,
    hydrate_population,
    profile_population,
    recover_population,
    select_combined,
)

app = typer.Typer(
    help="Independent cache-first revision-diff recovery (Method B).",
    no_args_is_help=True,
)

_settings_loader: Any = None


def configure(settings_loader: Any) -> None:
    """Bind the repository CLI's settings loader without duplicating config logic."""

    global _settings_loader
    _settings_loader = settings_loader


def _settings(config: Path) -> Settings:
    if _settings_loader is None:
        raise RuntimeError("revision-diff CLI is not configured")
    return _settings_loader(config)


def _emit(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def _population(settings: Settings) -> Path:
    return MethodBPaths.from_settings(settings).source_population


@app.command("profile")
def profile(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Stage 1: profile current A-fallback plus A-review occurrences."""

    _emit(profile_population(_settings(config)))


@app.command("hydrate")
def hydrate(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    allow_network: Annotated[
        bool,
        typer.Option(
            "--allow-network",
            help="Explicitly permit missing exact MediaWiki responses to be fetched.",
        ),
    ] = False,
    max_revisions: Annotated[int | None, typer.Option(min=1)] = None,
    batch_size: Annotated[int, typer.Option(min=1, max=50)] = 50,
    history_depth: Annotated[int, typer.Option(min=0, max=20)] = 0,
) -> None:
    """Cache-profile exact target/true-parent pairs; network is opt-in."""

    settings = _settings(config)
    _emit(
        hydrate_population(
            settings,
            population_path=_population(settings),
            allow_network=allow_network,
            max_revisions=max_revisions,
            batch_size=batch_size,
            history_depth=history_depth,
            include_controls=False,
        )
    )


@app.command("recover")
def recover(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    baseline_evidence: Annotated[
        Path | None,
        typer.Option(
            "--baseline-evidence",
            exists=True,
            dir_okay=False,
            help="Reuse prior b_safe/b_usable evidence as immutable controls.",
        ),
    ] = None,
    max_revisions: Annotated[int | None, typer.Option(min=1)] = None,
    checkpoint_every: Annotated[int, typer.Option(min=1)] = 250,
    resume: Annotated[bool, typer.Option("--resume/--no-resume")] = True,
    max_trace_cells: Annotated[int, typer.Option(min=1)] = 2_000_000,
    ambiguity_tolerance: Annotated[int, typer.Option(min=0)] = 1,
    max_assignment_actions: Annotated[int, typer.Option(min=1)] = 10,
    max_assignment_candidates: Annotated[int, typer.Option(min=1)] = 30,
    max_assignment_edges: Annotated[int, typer.Option(min=1)] = 200,
    max_assignment_states: Annotated[int, typer.Option(min=1)] = 100_000,
) -> None:
    """Run deterministic local recovery only; this command never uses network."""

    settings = _settings(config)
    _emit(
        recover_population(
            settings,
            population_path=_population(settings),
            baseline_evidence_path=baseline_evidence,
            checkpoint_every=checkpoint_every,
            resume=resume,
            max_revisions=max_revisions,
            max_trace_cells=max_trace_cells,
            include_controls=False,
            ambiguity_tolerance=ambiguity_tolerance,
            max_assignment_actions=max_assignment_actions,
            max_assignment_candidates=max_assignment_candidates,
            max_assignment_edges=max_assignment_edges,
            max_assignment_states=max_assignment_states,
        )
    )


@app.command("select")
def select(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Stage 5: write monotonic A/B selection artifacts only."""

    _emit(select_combined(_settings(config)))


@app.command("invariants")
def invariants(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    staged_annotation: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
) -> None:
    """Stage 7: verify local populations, immutable structure, and selection safety."""

    _emit(final_invariants(_settings(config), staged_annotation=staged_annotation))
