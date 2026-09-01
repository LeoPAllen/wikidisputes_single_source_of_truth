"""Design-based residual ceiling sampling and analysis.

This is intentionally downstream-only: it reads the frozen selection and
recovery evidence, and never changes Method-B recovery or selection decisions.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SEED = "20260831"
SAMPLE_SIZE = 600
B_UNAVAILABLE = "b_unavailable"
RECOVERABILITY_CLASSES = (
    "existing_evidence_exact",
    "deterministic_rule_possible",
    "human_only",
    "ambiguous",
    "source_action_mismatch",
    "no_identifiable_comment",
)
POSITIVE_CEILING_CLASSES = {
    "current_evidence": {"existing_evidence_exact"},
    "deterministic_engineering": {
        "existing_evidence_exact",
        "deterministic_rule_possible",
    },
    "human_assisted": {
        "existing_evidence_exact",
        "deterministic_rule_possible",
        "human_only",
    },
}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _uid(row: Mapping[str, Any]) -> str:
    value = _text(row.get("source_row_uid"))
    if not value:
        raise ValueError("row has no source_row_uid")
    return value


def _status(row: Mapping[str, Any]) -> str:
    return _text(row.get("method_b_status") or row.get("status")) or B_UNAVAILABLE


def _lifecycle(row: Mapping[str, Any]) -> str:
    return _text(row.get("action_type") or row.get("lifecycle")) or "unobserved"


def _json_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return sorted({_text(item) for item in value if _text(item)})
    if value in (None, ""):
        return []
    try:
        decoded = json.loads(_text(value))
    except json.JSONDecodeError:
        return [part.strip() for part in _text(value).split("|") if part.strip()]
    return (
        sorted({_text(item) for item in decoded if _text(item)})
        if isinstance(decoded, list)
        else []
    )


def _rank(uid: str, seed: str) -> str:
    return hashlib.sha256(f"{seed}:{uid}".encode()).hexdigest()


def _stratum(row: Mapping[str, Any]) -> str:
    return f"{_status(row)}|{_lifecycle(row)}"


def _truthy(value: Any) -> bool:
    return value is True or _text(value).strip().casefold() in {"1", "true", "yes"}


def _diagnostic_domain(row: Mapping[str, Any]) -> str:
    """Mutually exclusive second-stage diagnostic domains.

    Callers may precompute these booleans from frozen evidence.  Token
    persistence takes precedence so its rare cases are always visible; a row
    cannot appear twice and its design-cell probability remains explicit.
    """

    if _truthy(row.get("token_persistence")):
        return "token_persistence"
    if _truthy(row.get("discussiontools_evidence")):
        return "discussiontools_evidence"
    return "ordinary"


@dataclass(frozen=True)
class SamplePlan:
    """A self-weighting-within-stratum audit sample and its full frame."""

    frame: tuple[dict[str, Any], ...]
    sampled: tuple[dict[str, Any], ...]
    primary_strata: tuple[dict[str, Any], ...]
    design_cells: tuple[dict[str, Any], ...]
    unavailable_count: int
    seed: str = SEED
    requested_size: int = SAMPLE_SIZE


def derive_residual_rows(
    source_rows: Iterable[Mapping[str, Any]],
    recovery_rows: Iterable[Mapping[str, Any]],
    selection_rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Return the dynamically selected residual plus the full population size.

    The frozen combined-selection table determines the residual: a row is in it
    only when selection retained the Method-A fallback.  Recovery evidence is
    joined solely to provide audit covariates.  A missing recovery row is an
    upstream invariant violation, not a fabricated unavailable classification.
    """

    sources = {_uid(row): dict(row) for row in source_rows}
    recovery = {_uid(row): dict(row) for row in recovery_rows}
    selections = {_uid(row): dict(row) for row in selection_rows}
    if set(sources) != set(selections):
        raise ValueError("source and frozen selection source_row_uid sets differ")

    residual: list[dict[str, Any]] = []
    for uid in sorted(sources):
        selection = selections[uid]
        if _text(selection.get("selected_method")) != "method_a_fallback":
            continue
        evidence = recovery.get(uid)
        if evidence is None:
            raise ValueError(f"residual selection row has no recovery evidence: {uid}")
        row = {**sources[uid], **evidence}
        row["source_row_uid"] = uid
        row["selected_method"] = selection.get("selected_method")
        if not _text(evidence.get("status")):
            raise ValueError(f"residual recovery row has no status: {uid}")
        row["method_b_status"] = _status(evidence)
        row["lifecycle"] = _lifecycle(row)
        row["failure_reasons"] = _json_list(
            evidence.get("reason_codes_json", evidence.get("reason_codes"))
        )
        row["primary_stratum"] = _stratum(row)
        residual.append(row)
    return residual, len(sources)


def _allocate_strata(
    populations: Mapping[str, int], requested_size: int, min_per_stratum: int
) -> dict[str, int]:
    """Allocate a fixed sample with a rare-stratum floor then proportional rest."""

    total = sum(populations.values())
    if requested_size < 0:
        raise ValueError("requested_size must be non-negative")
    if requested_size >= total:
        return dict(populations)
    keys = sorted(populations)
    floor = min(min_per_stratum, requested_size // len(keys))
    allocation = {key: min(populations[key], floor) for key in keys}
    remaining = requested_size - sum(allocation.values())
    capacity = {key: populations[key] - allocation[key] for key in keys}
    while remaining:
        capacity_total = sum(capacity.values())
        if not capacity_total:
            break
        quotas = {
            key: remaining * capacity[key] / capacity_total if capacity[key] else 0.0
            for key in keys
        }
        additions = {key: min(capacity[key], math.floor(quotas[key])) for key in keys}
        assigned = sum(additions.values())
        for key in keys:
            allocation[key] += additions[key]
            capacity[key] -= additions[key]
        remaining -= assigned
        if not remaining:
            break
        for key in sorted(keys, key=lambda item: (-(quotas[item] % 1), item)):
            if remaining == 0:
                break
            if capacity[key]:
                allocation[key] += 1
                capacity[key] -= 1
                remaining -= 1
    return allocation


def deterministic_stratified_sample(
    residual_rows: Iterable[Mapping[str, Any]],
    *,
    requested_size: int = SAMPLE_SIZE,
    seed: str = SEED,
    min_per_stratum: int = 20,
) -> SamplePlan:
    """Sample non-unavailable residual rows with transparent two-stage cells.

    Primary allocation is B-status x lifecycle.  Within each primary stratum,
    disjoint diagnostic domains ensure token-persistence cases are censused and
    DiscussionTools-evidence cases are represented.  The deterministic hash
    ordering is a reproducible realization of SRSWOR in every design cell; all
    reported estimates use the cell's actual inclusion probability.
    """

    frame = [dict(row) for row in residual_rows]
    unavailable = [row for row in frame if _status(row) == B_UNAVAILABLE]
    eligible = [row for row in frame if _status(row) != B_UNAVAILABLE]
    by_stratum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        row["method_b_status"] = _status(row)
        row["lifecycle"] = _lifecycle(row)
        row["primary_stratum"] = _stratum(row)
        by_stratum[row["primary_stratum"]].append(row)
    allocation = _allocate_strata(
        {key: len(rows) for key, rows in by_stratum.items()},
        min(requested_size, len(eligible)),
        min_per_stratum,
    )
    sampled: list[dict[str, Any]] = []
    primary_strata: list[dict[str, Any]] = []
    design_cells: list[dict[str, Any]] = []
    for key in sorted(by_stratum):
        members = by_stratum[key]
        population = len(members)
        size = allocation[key]
        status, lifecycle = key.split("|", 1)
        primary_strata.append(
            {
                "primary_stratum": key,
                "b_status": status,
                "lifecycle": lifecycle,
                "population_n": population,
                "sample_n": size,
            }
        )
        by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in members:
            by_domain[_diagnostic_domain(row)].append(row)
        mandatory = {
            "token_persistence": len(by_domain["token_persistence"]),
            "discussiontools_evidence": min(2, len(by_domain["discussiontools_evidence"])),
            "ordinary": 0,
        }
        if sum(mandatory.values()) > size:
            raise ValueError(f"primary allocation cannot cover diagnostic cells: {key}")
        cell_allocation = dict(mandatory)
        remaining = size - sum(cell_allocation.values())
        capacities = {
            domain: len(rows) - cell_allocation[domain] for domain, rows in by_domain.items()
        }
        while remaining:
            capacity_total = sum(capacities.values())
            if not capacity_total:
                break
            quotas = {
                domain: remaining * capacities[domain] / capacity_total
                for domain in sorted(capacities)
            }
            additions = {
                domain: min(capacities[domain], math.floor(quotas[domain])) for domain in capacities
            }
            assigned = sum(additions.values())
            for domain, amount in additions.items():
                cell_allocation[domain] += amount
                capacities[domain] -= amount
            remaining -= assigned
            if not remaining:
                break
            for domain in sorted(capacities, key=lambda item: (-(quotas[item] % 1), item)):
                if remaining == 0:
                    break
                if capacities[domain]:
                    cell_allocation[domain] += 1
                    capacities[domain] -= 1
                    remaining -= 1
        for domain in sorted(by_domain):
            cell_members = sorted(
                by_domain[domain], key=lambda row: (_rank(_uid(row), seed), _uid(row))
            )
            cell_population = len(cell_members)
            cell_size = cell_allocation[domain]
            if not cell_size:
                continue
            probability = cell_size / cell_population
            weight = cell_population / cell_size
            cell_key = f"{key}|{domain}"
            design_cells.append(
                {
                    "primary_stratum": key,
                    "design_cell": cell_key,
                    "diagnostic_domain": domain,
                    "b_status": status,
                    "lifecycle": lifecycle,
                    "population_n": cell_population,
                    "sample_n": cell_size,
                    "inclusion_probability": probability,
                    "survey_weight": weight,
                }
            )
            for row in cell_members[:cell_size]:
                sampled.append(
                    {
                        **row,
                        "design_cell": cell_key,
                        "diagnostic_domain": domain,
                        "population_n": cell_population,
                        "sample_n": cell_size,
                        "inclusion_probability": probability,
                        "survey_weight": weight,
                    }
                )
    sampled.sort(key=lambda row: (_rank(_uid(row), seed), _uid(row)))
    return SamplePlan(
        frame=tuple(frame),
        sampled=tuple(sampled),
        primary_strata=tuple(primary_strata),
        design_cells=tuple(design_cells),
        unavailable_count=len(unavailable),
        seed=seed,
        requested_size=requested_size,
    )


def _estimate_binary(rows: Sequence[Mapping[str, Any]], predicate: Any) -> dict[str, float]:
    """HT total and SRSWOR stratified normal interval for an indicator."""

    by_stratum: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_stratum[_text(row["design_cell"])].append(row)
    estimate = variance = 0.0
    for members in by_stratum.values():
        n = len(members)
        population = int(members[0]["population_n"])
        values = [1.0 if predicate(row) else 0.0 for row in members]
        estimate += population * sum(values) / n
        if n > 1:
            mean = sum(values) / n
            sample_variance = sum((value - mean) ** 2 for value in values) / (n - 1)
            variance += population**2 * (1 - n / population) * sample_variance / n
    standard_error = math.sqrt(variance)
    return {
        "estimated_count": estimate,
        "standard_error": standard_error,
        "ci95_low": estimate - 1.96 * standard_error,
        "ci95_high": estimate + 1.96 * standard_error,
    }


def _bounded(estimate: Mapping[str, float], denominator: int) -> dict[str, float]:
    return {
        **estimate,
        "estimated_count": max(0.0, min(float(denominator), estimate["estimated_count"])),
        "ci95_low": max(0.0, min(float(denominator), estimate["ci95_low"])),
        "ci95_high": max(0.0, min(float(denominator), estimate["ci95_high"])),
    }


def _as_percent(estimate: Mapping[str, float], denominator: int) -> dict[str, float]:
    bounded = _bounded(estimate, denominator)
    return {
        **bounded,
        "estimated_percent": 100 * bounded["estimated_count"] / denominator,
        "ci95_percent_low": 100 * bounded["ci95_low"] / denominator,
        "ci95_percent_high": 100 * bounded["ci95_high"] / denominator,
    }


def _completed_rows(
    plan: SamplePlan, labels: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    expected = {_uid(row) for row in plan.sampled}
    missing = sorted(
        uid for uid in expected if not _text(labels.get(uid, {}).get("recoverability"))
    )
    if missing:
        raise ValueError(
            f"summary requires completed labels for all sampled rows; missing={len(missing)}"
        )
    unknown = sorted(
        uid
        for uid in expected
        if _text(labels[uid].get("recoverability")) not in RECOVERABILITY_CLASSES
    )
    if unknown:
        raise ValueError(f"unknown recoverability labels: {unknown[:5]}")
    return [{**row, **labels[_uid(row)]} for row in plan.sampled]


def _category_matches(row: Mapping[str, Any], field: str, value: str) -> bool:
    if field == "failure_reason":
        return value in _json_list(row.get("failure_reasons"))
    return (_text(row.get(field)) or "unrecorded") == value


def _breakdown(
    rows: Sequence[Mapping[str, Any]],
    frame: Sequence[Mapping[str, Any]],
    field: str,
    eligible_population: int,
) -> dict[str, dict[str, Any]]:
    """Estimate every recoverability class within each requested category."""

    values: set[str] = set()
    for row in frame:
        if field == "failure_reason":
            values.update(_json_list(row.get("failure_reasons")))
        elif field != "rule_family":
            values.add(_text(row.get(field)) or "unrecorded")
    if field == "rule_family":
        values.update(_text(row.get(field)).strip() or "unrecorded" for row in rows)
    output: dict[str, dict[str, Any]] = {}
    for value in sorted(values):

        def predicate(row: Mapping[str, Any], value: str = value) -> bool:
            return _category_matches(row, field, value)

        exact_population = (
            sum(1 for row in frame if predicate(row)) if field != "rule_family" else None
        )
        output[value] = {
            "population_exact_count": exact_population,
            "population_estimate": _as_percent(
                _estimate_binary(rows, predicate), eligible_population
            ),
            "recoverability_classes": {
                label: _as_percent(
                    _estimate_binary(
                        rows,
                        lambda row, label=label, predicate=predicate: (
                            predicate(row) and _text(row.get("recoverability")) == label
                        ),
                    ),
                    eligible_population,
                )
                for label in RECOVERABILITY_CLASSES
            },
        }
    return output


def _rule_family_gate(
    rows: Sequence[Mapping[str, Any]], eligible_population: int
) -> dict[str, Any]:
    families: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if _text(row.get("recoverability")) == "deterministic_rule_possible":
            family = _text(row.get("rule_family")).strip()
            if family:
                families[family].append(row)
    reports: list[dict[str, Any]] = []
    for family, members in sorted(families.items()):
        total = _estimate_binary(
            rows,
            lambda row, family=family: (
                _text(row.get("rule_family")).strip() == family
                and _text(row.get("recoverability")) == "deterministic_rule_possible"
            ),
        )
        risks = [
            row
            for row in members
            if _text(row.get("candidate_error")) in {"wrong", "overwide", "underwide"}
        ]
        # Rule labels themselves assert mechanical availability.  The observed
        # boundary/wrong rate is retained rather than silently treating blanks as safe.
        risk_rate = len(risks) / len(members)
        zero_risk_upper = 3 / len(members) if not risks else risk_rate
        meaningful_yield = max(100, math.ceil(0.005 * eligible_population))
        high_confidence = sum(_text(row.get("confidence")) == "high" for row in members)
        mechanically_available = high_confidence == len(members)
        reports.append(
            {
                "rule_family": family,
                "sampled_deterministic_count": len(members),
                "projected_yield_count": total["estimated_count"],
                "projected_yield_ci95_low": max(0.0, total["ci95_low"]),
                "wrong_or_boundary_count": len(risks),
                "wrong_or_boundary_rate": risk_rate,
                "zero_event_95_upper_rate": zero_risk_upper,
                "high_confidence_count": high_confidence,
                "mechanically_available_evidence": mechanically_available,
                "passes": (
                    len(members) >= 3
                    and total["estimated_count"] >= meaningful_yield
                    and mechanically_available
                    and not risks
                    and zero_risk_upper <= 0.05
                ),
            }
        )
    passing = [report for report in reports if report["passes"]]
    return {
        "rule_families": reports,
        "recommend_more_engineering": bool(passing),
        "recommendation": (
            "engineering_justified_for_named_rule_family"
            if passing
            else "further_heuristic_recovery_not_justified"
        ),
        "criteria": {
            "minimum_sampled_deterministic_cases": 3,
            "minimum_projected_yield": max(100, math.ceil(0.005 * eligible_population)),
            "maximum_zero_event_95_upper_risk": 0.05,
        },
    }


def summarize_completed_labels(
    plan: SamplePlan,
    labels: Mapping[str, Mapping[str, Any]],
    *,
    total_population: int,
) -> dict[str, Any]:
    """Return design-based residual estimates and ceiling accounting.

    ``b_unavailable`` is outside the sample frame and reported as an exact,
    irrecoverable residual count.  Selection-derived validated coverage is the
    complement of the whole residual, not an assumed constant.
    """

    rows = _completed_rows(plan, labels)
    residual_total = len(plan.frame)
    eligible_population = residual_total - plan.unavailable_count
    eligible_frame = [row for row in plan.frame if _status(row) != B_UNAVAILABLE]
    if total_population < residual_total:
        raise ValueError("total_population cannot be smaller than the residual")
    validated = total_population - residual_total
    classes = {
        label: _as_percent(
            _estimate_binary(
                rows, lambda row, label=label: _text(row.get("recoverability")) == label
            ),
            eligible_population,
        )
        for label in RECOVERABILITY_CLASSES
    }
    ceilings: dict[str, dict[str, Any]] = {}
    for name, positive in POSITIVE_CEILING_CLASSES.items():
        residual_estimate = _bounded(
            _estimate_binary(
                rows, lambda row, positive=positive: _text(row.get("recoverability")) in positive
            ),
            eligible_population,
        )
        total_estimate = {
            "estimated_count": validated + residual_estimate["estimated_count"],
            "ci95_low": validated + residual_estimate["ci95_low"],
            "ci95_high": validated + residual_estimate["ci95_high"],
        }
        ceilings[name] = {
            "additional_recoverable_residual": _as_percent(residual_estimate, residual_total),
            "implied_total_ssot_coverage": _as_percent(total_estimate, total_population),
        }
    return {
        "design": {
            "seed": plan.seed,
            "requested_sample_size": plan.requested_size,
            "completed_sample_size": len(rows),
            "eligible_residual_population": eligible_population,
            "b_unavailable_exact_count": plan.unavailable_count,
            "residual_total": residual_total,
            "total_population": total_population,
            "validated_a_plus_b_exact_count": validated,
            "validated_a_plus_b_exact_percent": 100 * validated / total_population,
            "primary_strata": list(plan.primary_strata),
            "design_cells": list(plan.design_cells),
        },
        "recoverability_classes": classes,
        "breakdowns": {
            "b_status": _breakdown(rows, eligible_frame, "method_b_status", eligible_population),
            "lifecycle": _breakdown(rows, eligible_frame, "lifecycle", eligible_population),
            "failure_reason": _breakdown(
                rows, eligible_frame, "failure_reason", eligible_population
            ),
            "rule_family": _breakdown(rows, eligible_frame, "rule_family", eligible_population),
        },
        "ceilings": ceilings,
        "decision_gate": _rule_family_gate(rows, eligible_population),
    }
