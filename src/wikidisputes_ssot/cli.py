from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from .article_history import hydrate_article_histories
from .audit import audit_source
from .config import Settings, load_settings
from .constants import CURRENT, HISTORICAL, SAMPLED
from .core import materialize_source_core
from .events_dv import materialize_events_and_dvs
from .export import materialize_exports
from .full import materialize_full_rehydrated
from .historical import extract_historical_article_edits
from .hydration import hydrate_selected_parses, hydrate_selected_revisions
from .lineage import materialize_source_lineage
from .literature import materialize_literature_registry
from .mediawiki import MediaWikiClient
from .recover import recover_revision_representations
from .replication import materialize_replication_views
from .review import materialize_review_packet
from .source import (
    build_source_projection,
    download_pin,
    extract_archive,
    source_archive_path,
    verify_pin,
)
from .validate import validate_all
from .wikiconv import enumerate_year, merge_enumeration

app = typer.Typer(
    name="wikidisputes-ssot",
    help="Evidence-preserving WikiDisputes SSOT pipeline.",
    no_args_is_help=True,
)
source_app = typer.Typer(help="Download, verify, extract, project, and audit pinned releases.")
app.add_typer(source_app, name="source")
wikiconv_app = typer.Typer(help="Low-disk WikiConv annual enumeration and reconciliation.")
app.add_typer(wikiconv_app, name="wikiconv")
mediawiki_app = typer.Typer(help="Cached historical revision, parse, and compare hydration.")
app.add_typer(mediawiki_app, name="mediawiki")


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _settings(config: Path) -> Settings:
    return load_settings(config, _root())


def _emit(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


@source_app.command("verify")
def source_verify(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Verify all locally present pinned WikiDisputes archives; never accept drift."""
    settings = _settings(config)
    results = []
    for pin in (CURRENT, HISTORICAL, SAMPLED):
        results.append(verify_pin(source_archive_path(settings.roots.data, pin), pin))
    _emit(results)


@source_app.command("download")
def source_download(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Download byte-exact pinned sources, resuming only validated ranges."""
    settings = _settings(config)
    _emit(
        [
            download_pin(settings.roots.data, pin, settings.network.user_agent)
            for pin in (CURRENT, HISTORICAL, SAMPLED)
        ]
    )


@source_app.command("extract")
def source_extract(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    include_historical: Annotated[bool, typer.Option()] = True,
) -> None:
    """Safely extract exact source members and write byte/hash manifests."""
    settings = _settings(config)
    pins = [CURRENT, HISTORICAL] if include_historical else [CURRENT]
    _emit([extract_archive(settings.roots.data, pin) for pin in pins])


@source_app.command("project")
def source_project(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    pilot_cases: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Build one losslessly traceable row per current-release source row."""
    settings = _settings(config)
    _emit(
        build_source_projection(
            settings.roots.data,
            settings.roots.output,
            case_limit=pilot_cases,
        )
    )


@source_app.command("audit")
def source_audit(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    settings = _settings(config)
    projection = settings.roots.output / "canonical" / "wikidisputes_source_projection.parquet"
    _emit(audit_source(projection, settings.roots.output / "reports"))


@app.command("reconstruct")
def reconstruct(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Materialize source-evidence identities/lifecycles without false hydration claims."""
    settings = _settings(config)
    projection = settings.roots.output / "canonical" / "wikidisputes_source_projection.parquet"
    _emit(materialize_source_core(projection, settings.roots.output))


@app.command("lineage")
def lineage(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Materialize source manifest and extracted-file lineage tables."""
    settings = _settings(config)
    _emit(materialize_source_lineage(settings.roots.data, settings.roots.output))


@app.command("literature")
def literature(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Materialize the searched literature-cleaning registry."""
    settings = _settings(config)
    _emit(
        materialize_literature_registry(
            _root() / "literature" / "cleaning_registry.yaml", settings.roots.output
        )
    )


@app.command("literature-replicate")
def literature_replicate(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Materialize named, non-destructive publication replication flags/views."""
    settings = _settings(config)
    _emit(materialize_replication_views(settings.roots.output))


@app.command("events-dv")
def events_dv(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Materialize raw source events and candidate, non-consensus DV states."""
    settings = _settings(config)
    _emit(materialize_events_and_dvs(settings.roots.output))


@app.command("rehydrate")
def rehydrate(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Reconcile the completed WikiConv union into the canonical logical SSOT."""
    settings = _settings(config)
    _emit(materialize_full_rehydrated(settings.roots.output))


@app.command("export")
def export(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Write required canonical Parquets, timelines, manifests, and DuckDB views."""
    settings = _settings(config)
    _emit(materialize_exports(settings.roots.output, settings.canonical_dict(), _root()))


@app.command("review-packet")
def review_packet(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Create deterministic stratified evidence packets with blank adjudications."""
    settings = _settings(config)
    _emit(materialize_review_packet(settings.roots.output, settings.run.review_seed))


@app.command("validate")
def validate(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Execute every implemented acceptance check and report every matrix gate."""
    settings = _settings(config)
    _emit(validate_all(_root(), settings.roots.output, settings.roots.data))


@source_app.command("historical-edits")
def historical_edits(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Recover exact pre-removal article-edit and edit-summary evidence."""
    settings = _settings(config)
    _emit(extract_historical_article_edits(settings.roots.data, settings.roots.output))


@wikiconv_app.command("enumerate")
def wikiconv_enumerate(
    year: Annotated[int | None, typer.Option(min=2001, max=2018)] = None,
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    keep_archive: Annotated[bool, typer.Option(help="Retain the full annual ZIP.")] = False,
) -> None:
    """Download/filter one or all years; resume by annual success manifests."""
    settings = _settings(config)
    inventory = _root() / "config" / "wikiconv_archives.yaml"
    selected = settings.roots.output / "silver" / "selected_conversations.parquet"
    years = [year] if year is not None else settings.wikiconv.years
    results = []
    for selected_year in years:
        marker = (
            settings.roots.data
            / "bronze"
            / "wikiconv"
            / "selected"
            / str(selected_year)
            / "manifest.json"
        )
        if marker.exists():
            results.append(json.loads(marker.read_text(encoding="utf-8")))
            continue
        result = enumerate_year(
            settings,
            selected_year,
            inventory,
            selected,
            keep_archive=keep_archive,
        )
        results.append(result)
    _emit(
        {
            "years": [result["year"] for result in results],
            "selected_rows": sum(result["selected_utterance_rows"] for result in results),
            "manifests": [
                str(
                    settings.roots.data
                    / "bronze"
                    / "wikiconv"
                    / "selected"
                    / str(result["year"])
                    / "manifest.json"
                )
                for result in results
            ],
        }
    )


@wikiconv_app.command("merge")
def wikiconv_merge(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    settings = _settings(config)
    _emit(merge_enumeration(settings))


@mediawiki_app.command("revisions")
def mediawiki_revisions(
    revision_id: Annotated[list[int], typer.Option(min=1)],
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    include_content: Annotated[bool, typer.Option()] = True,
) -> None:
    """Hydrate exact historical revisions in conservative API batches."""
    client = MediaWikiClient(_settings(config))
    manifests = [
        manifest
        for _, manifest in client.revisions_by_ids(revision_id, include_content=include_content)
    ]
    _emit({"requested_revision_ids": len(revision_id), "manifests": manifests})


@mediawiki_app.command("parse")
def mediawiki_parse(
    revision_id: Annotated[int, typer.Option(min=1)],
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Parse one pinned historical revision as reconstructed, never archival, HTML."""
    _, manifest = MediaWikiClient(_settings(config)).parse_revision(revision_id)
    _emit(manifest)


@mediawiki_app.command("compare")
def mediawiki_compare(
    from_revision: Annotated[int, typer.Option(min=1)],
    to_revision: Annotated[int, typer.Option(min=1)],
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Retrieve and preserve an exact MediaWiki revision comparison response."""
    _, manifest = MediaWikiClient(_settings(config)).compare(from_revision, to_revision)
    _emit(manifest)


@mediawiki_app.command("hydrate-selected")
def mediawiki_hydrate_selected(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    include_content: Annotated[bool, typer.Option()] = True,
    max_revisions: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Hydrate all or a bounded pilot of selected WikiConv lifecycle revisions."""
    _emit(
        hydrate_selected_revisions(
            _settings(config),
            include_content=include_content,
            max_revisions=max_revisions,
        )
    )


@mediawiki_app.command("hydrate-article-histories")
def mediawiki_hydrate_article_histories(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    max_pages: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Hydrate metadata/SHA-1 article histories over episode outcome windows."""
    _emit(hydrate_article_histories(_settings(config), max_pages=max_pages))


@mediawiki_app.command("hydrate-selected-parses")
def mediawiki_hydrate_selected_parses(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
    max_revisions: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Hydrate reconstructed action=parse HTML for selected historical revisions."""
    _emit(hydrate_selected_parses(_settings(config), max_revisions=max_revisions))


@app.command("versions")
def versions(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Reconstruct logical lifecycle versions from the completed selected corpus."""
    settings = _settings(config)
    _emit(materialize_full_rehydrated(settings.roots.output))


@app.command("representations")
def representations(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Extract exact representations, explicit links, and signature evidence."""
    settings = _settings(config)
    _emit(recover_revision_representations(settings))


@app.command("reply-repair")
def reply_repair(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Resolve reply aliases while retaining unresolved raw targets and evidence."""
    settings = _settings(config)
    _emit(materialize_full_rehydrated(settings.roots.output))


@app.command("episodes")
def episodes(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Construct source-evidence episodes with explicit unresolved alignment."""
    settings = _settings(config)
    projection = settings.roots.output / "canonical" / "wikidisputes_source_projection.parquet"
    _emit(materialize_source_core(projection, settings.roots.output))


@app.command("events")
def events(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Extract raw source events independently of analytical cutoff rules."""
    settings = _settings(config)
    _emit(materialize_events_and_dvs(settings.roots.output))


@app.command("dv")
def dv(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Materialize separate candidate DV definitions, horizons, states, and evidence."""
    settings = _settings(config)
    _emit(materialize_events_and_dvs(settings.roots.output))


@app.command("pilot")
def pilot(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Run a deterministic source-projection pilot."""
    settings = _settings(config)
    extract_archive(settings.roots.data, CURRENT)
    result = build_source_projection(
        settings.roots.data,
        settings.roots.output / "pilot",
        case_limit=settings.run.pilot_case_limit,
    )
    _emit(result)


def _run_all(config: Path) -> dict[str, object]:
    settings = _settings(config)
    stages: dict[str, object] = {}
    stages["download"] = [
        download_pin(settings.roots.data, pin, settings.network.user_agent)
        for pin in (CURRENT, HISTORICAL, SAMPLED)
    ]
    stages["extract"] = [extract_archive(settings.roots.data, pin) for pin in (CURRENT, HISTORICAL)]
    stages["projection"] = build_source_projection(settings.roots.data, settings.roots.output)
    projection = settings.roots.output / "canonical" / "wikidisputes_source_projection.parquet"
    stages["audit"] = audit_source(projection, settings.roots.output / "reports")
    stages["lineage"] = materialize_source_lineage(settings.roots.data, settings.roots.output)
    stages["source_core"] = materialize_source_core(projection, settings.roots.output)
    stages["historical_edits"] = extract_historical_article_edits(
        settings.roots.data, settings.roots.output
    )
    stages["literature"] = materialize_literature_registry(
        _root() / "literature" / "cleaning_registry.yaml", settings.roots.output
    )
    stages["literature_replication"] = materialize_replication_views(settings.roots.output)
    inventory = _root() / "config" / "wikiconv_archives.yaml"
    selected = settings.roots.output / "silver" / "selected_conversations.parquet"
    annual = []
    for year in settings.wikiconv.years:
        marker = (
            settings.roots.data / "bronze" / "wikiconv" / "selected" / str(year) / "manifest.json"
        )
        if marker.exists():
            annual.append(json.loads(marker.read_text(encoding="utf-8")))
        else:
            annual.append(enumerate_year(settings, year, inventory, selected))
    stages["wikiconv_annual"] = {
        "years": [row["year"] for row in annual],
        "selected_rows": sum(row["selected_utterance_rows"] for row in annual),
    }
    stages["wikiconv_merge"] = merge_enumeration(settings)
    stages["events_dv"] = materialize_events_and_dvs(settings.roots.output)
    stages["full_rehydration"] = materialize_full_rehydrated(settings.roots.output)
    stages["article_histories"] = hydrate_article_histories(settings)
    stages["events_dv_hydrated"] = materialize_events_and_dvs(settings.roots.output)
    stages["full_rehydration_hydrated_dvs"] = materialize_full_rehydrated(settings.roots.output)
    stages["mediawiki_revisions"] = hydrate_selected_revisions(settings, include_content=True)
    stages["representation_recovery"] = recover_revision_representations(settings)
    stages["mediawiki_parses"] = hydrate_selected_parses(settings)
    stages["review_packet"] = materialize_review_packet(
        settings.roots.output, settings.run.review_seed
    )
    stages["export"] = materialize_exports(
        settings.roots.output, settings.canonical_dict(), _root()
    )
    stages["validate"] = validate_all(_root(), settings.roots.output, settings.roots.data)
    return stages


@app.command("full-run")
def full_run(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Execute the idempotent end-to-end production pipeline with bounded checkpoints."""
    _emit(_run_all(config))


@app.command("resume")
def resume(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)] = Path(
        "config/ssot.example.yaml"
    ),
) -> None:
    """Resume the same production DAG from verified stage/annual checkpoints."""
    _emit(_run_all(config))


if __name__ == "__main__":
    app()
