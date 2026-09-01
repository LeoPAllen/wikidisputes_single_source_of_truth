"""Typer commands for the staged Method-B workflow."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from wikidisputes_ssot.config import Settings

from .discussiontools_runner import DEFAULT_IMAGE, run_feasibility, write_feasibility_sample
from .llm_audit_bundle import build_llm_audit_bundle
from .residual_ceiling import SAMPLE_SIZE, SEED
from .residual_ceiling_audit import label_audit_row
from .residual_ceiling_workflow import (
    ResidualCeilingPaths,
    build_residual_ceiling_packet,
    summarize_residual_ceiling,
)
from .workflow import (
    MethodBPaths,
    build_human_audit,
    final_invariants,
    hydrate_population,
    profile_population,
    rebuild_annotation_export,
    recover_population,
    select_combined,
    select_pilot,
    validate_pilot,
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


def _population(settings: Settings, pilot: bool) -> Path:
    paths = MethodBPaths.from_settings(settings)
    return paths.pilot_population if pilot else paths.source_population


@app.command("profile")
def profile(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Stage 1: profile current A-fallback plus A-review occurrences."""

    _emit(profile_population(_settings(config)))


@app.command("pilot")
def pilot(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    seed: Annotated[int, typer.Option()] = 20260818,
    per_stratum: Annotated[int, typer.Option(min=1)] = 25,
) -> None:
    """Stage 2: write a deterministic overlapping stratified pilot sample."""

    _emit(select_pilot(_settings(config), seed=seed, per_stratum=per_stratum))


@app.command("hydrate")
def hydrate(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    pilot_population: Annotated[
        bool, typer.Option("--pilot", help="Use the deterministic pilot artifact.")
    ] = False,
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
            population_path=_population(settings, pilot_population),
            allow_network=allow_network,
            max_revisions=max_revisions,
            batch_size=batch_size,
            history_depth=history_depth,
            include_controls=pilot_population,
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
    pilot_population: Annotated[
        bool, typer.Option("--pilot", help="Use the deterministic pilot artifact.")
    ] = False,
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
            population_path=_population(settings, pilot_population),
            baseline_evidence_path=baseline_evidence,
            checkpoint_every=checkpoint_every,
            resume=resume,
            max_revisions=max_revisions,
            max_trace_cells=max_trace_cells,
            include_controls=pilot_population,
            ambiguity_tolerance=ambiguity_tolerance,
            max_assignment_actions=max_assignment_actions,
            max_assignment_candidates=max_assignment_candidates,
            max_assignment_edges=max_assignment_edges,
            max_assignment_states=max_assignment_states,
        )
    )


@app.command("validate-pilot")
def pilot_validation(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Stage 3: compare pilot A/B evidence without treating A as ground truth."""

    _emit(validate_pilot(_settings(config)))


@app.command("select")
def select(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Stage 5: write monotonic A/B selection artifacts only."""

    _emit(select_combined(_settings(config)))


@app.command("audit-packet")
def audit_packet(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    seed: Annotated[int, typer.Option()] = 20260818,
    excerpt_limit: Annotated[int, typer.Option(min=100)] = 500,
    per_stratum: Annotated[int, typer.Option(min=1)] = 25,
    pilot_population: Annotated[
        bool, typer.Option("--pilot", help="Use pilot evidence and pilot-only outputs.")
    ] = False,
) -> None:
    """Write blinded reviewer evidence plus a separate unblinding key."""

    _emit(
        build_human_audit(
            _settings(config),
            seed=seed,
            excerpt_limit=excerpt_limit,
            per_stratum=per_stratum,
            pilot=pilot_population,
        )
    )


@app.command("rebuild-annotation")
def rebuild_annotation(
    validation_decision: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    input_csv: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
) -> None:
    """Stage 6: explicitly rebuild a new annotation export after acceptance."""

    _emit(
        rebuild_annotation_export(
            _settings(config), validation_decision=validation_decision, input_csv=input_csv
        )
    )


@app.command("invariants")
def invariants(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    staged_annotation: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
) -> None:
    """Stage 7: verify local populations, immutable structure, and selection safety."""

    _emit(final_invariants(_settings(config), staged_annotation=staged_annotation))


@app.command("residual-ceiling-packet")
def residual_ceiling_packet(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    seed: Annotated[str, typer.Option()] = SEED,
    sample_size: Annotated[int, typer.Option(min=1)] = SAMPLE_SIZE,
    excerpt_limit: Annotated[int, typer.Option(min=200)] = 900,
) -> None:
    """Write the deterministic weighted residual-ceiling audit bundle."""

    _emit(
        build_residual_ceiling_packet(
            _settings(config),
            seed=seed,
            sample_size=sample_size,
            excerpt_limit=excerpt_limit,
        )
    )


@app.command("residual-ceiling-label")
def residual_ceiling_label(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    seed: Annotated[str, typer.Option()] = SEED,
) -> None:
    """Resume terminal labeling; atomically save every completed row."""

    settings = _settings(config)
    paths = ResidualCeilingPaths.from_settings(settings, seed)
    csv.field_size_limit(sys.maxsize)
    while True:
        with paths.csv.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        pending = [row for row in rows if not row.get("recoverability")]
        if not pending:
            typer.echo(f"complete: {len(rows)}/{len(rows)} labels; {paths.csv}")
            return
        row = pending[0]
        complete = len(rows) - len(pending)
        typer.echo(
            f"Review row {row['review_order']} in {paths.html} "
            f"({complete}/{len(rows)} complete). Enter 'stop' to exit safely."
        )
        recoverability = typer.prompt("recoverability")
        if recoverability.strip().casefold() == "stop":
            typer.echo(f"stopped safely: {complete}/{len(rows)} labels persisted")
            return
        chosen_candidate = typer.prompt("chosen candidate", default="", show_default=False)
        manual_start = typer.prompt("manual raw start", default="", show_default=False)
        manual_end = typer.prompt("manual raw end", default="", show_default=False)
        candidate_error = typer.prompt("candidate error", default="none")
        rule_family = typer.prompt("rule family", default="", show_default=False)
        confidence = typer.prompt("confidence", default="medium")
        evidence_note = typer.prompt("short evidence note")
        try:
            label_audit_row(
                paths.csv,
                row["audit_uid"],
                recoverability=recoverability,
                chosen_candidate=chosen_candidate,
                manual_raw_start=manual_start,
                manual_raw_end=manual_end,
                candidate_error=candidate_error,
                rule_family=rule_family,
                confidence=confidence,
                evidence_note=evidence_note,
                html_path=paths.html,
                metadata={"seed": seed, "sample_size": len(rows)},
            )
        except ValueError as error:
            typer.echo(f"not saved: {error}", err=True)


@app.command("residual-ceiling-summarize")
def residual_ceiling_summarize(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    seed: Annotated[str, typer.Option()] = SEED,
) -> None:
    """Summarize a fully labeled residual-ceiling packet with survey weights."""

    _emit(summarize_residual_ceiling(_settings(config), seed=seed))


@app.command("llm-audit-bundle")
def llm_audit_bundle(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    seed: Annotated[str, typer.Option()] = SEED,
) -> None:
    """Build the self-contained LLM bundle from the frozen residual sample."""

    _emit(build_llm_audit_bundle(_settings(config), seed=seed))


@app.command("discussiontools-sample")
def discussiontools_sample(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    seed: Annotated[str, typer.Option()] = "20260831",
) -> None:
    """Write the hash-bound, deterministic 200-row rendered-structure pilot."""

    _emit(write_feasibility_sample(_settings(config), seed=seed))


@app.command("discussiontools-feasibility")
def discussiontools_feasibility(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    resume: Annotated[bool, typer.Option("--resume/--no-resume")] = True,
    checkpoint_every: Annotated[int, typer.Option(min=25, max=50)] = 25,
    allow_network: Annotated[bool, typer.Option("--allow-network")] = False,
    harness_image: Annotated[str, typer.Option()] = DEFAULT_IMAGE,
) -> None:
    """Render and parse the saved pilot; network access is explicit and resumable."""

    _emit(
        run_feasibility(
            _settings(config),
            resume=resume,
            checkpoint_every=checkpoint_every,
            allow_network=allow_network,
            harness_image=harness_image,
        )
    )
