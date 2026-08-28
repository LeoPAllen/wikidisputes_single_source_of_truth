"""Immutable, versioned records used by the revision-diff recovery method.

Offsets in this module are Python string (Unicode code-point) offsets.  They
are deliberately not UTF-8 byte offsets or UTF-16 offsets.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Final

REVISION_DIFF_SCHEMA_VERSION: Final[int] = 1


class RevisionAvailability(StrEnum):
    """Whether revision text was available for a recovery decision."""

    AVAILABLE = "available"
    MISSING = "missing"
    SUPPRESSED = "suppressed"
    DELETED = "deleted"
    UNAVAILABLE = "unavailable"


class MethodBAction(StrEnum):
    """The explicit action inferred from a predecessor/target text diff."""

    INSERT = "insert"
    DELETE = "delete"
    REPLACE = "replace"
    NO_CHANGE = "no_change"
    UNRESOLVED = "unresolved"


class DiffOpKind(StrEnum):
    """A token-alignment operation."""

    EQUAL = "equal"
    INSERT = "insert"
    DELETE = "delete"
    REPLACE = "replace"


@dataclass(frozen=True, slots=True, order=True)
class TextSpan:
    """A half-open Unicode code-point range."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError("text spans must satisfy 0 <= start <= end")

    @property
    def length(self) -> int:
        return self.end - self.start

    def extract(self, raw_text: str) -> str:
        if self.end > len(raw_text):
            raise ValueError("text span exceeds raw text length")
        return raw_text[self.start : self.end]


@dataclass(frozen=True, slots=True, order=True)
class TokenRange:
    """A half-open token-index range."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError("token ranges must satisfy 0 <= start <= end")

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class RawMappedToken:
    """A token retaining its exact source-text range."""

    text: str
    char_span: TextSpan

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("tokens must not be empty")
        if len(self.text) != self.char_span.length:
            raise ValueError("token text length must equal its code-point span length")

    @property
    def start(self) -> int:
        return self.char_span.start

    @property
    def end(self) -> int:
        return self.char_span.end


@dataclass(frozen=True, slots=True)
class RevisionText:
    """An archival revision payload; raw text is never normalized or mutated."""

    revision_id: str
    availability: RevisionAvailability
    raw_text: str | None
    api_sha1: str | None = None
    local_content_sha256: str | None = None
    schema_version: int = REVISION_DIFF_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.revision_id:
            raise ValueError("revision_id must not be empty")
        if self.schema_version != REVISION_DIFF_SCHEMA_VERSION:
            raise ValueError("unsupported revision-diff schema version")
        if self.availability is RevisionAvailability.AVAILABLE and self.raw_text is None:
            raise ValueError("available revisions require raw_text")
        if self.availability is not RevisionAvailability.AVAILABLE and self.raw_text is not None:
            raise ValueError("unavailable revisions must not carry raw_text")
        if self.raw_text is not None:
            computed = local_content_sha256(self.raw_text)
            if self.local_content_sha256 is not None and self.local_content_sha256 != computed:
                raise ValueError("local_content_sha256 does not match raw_text")

    @classmethod
    def available(
        cls, revision_id: str, raw_text: str, api_sha1: str | None = None
    ) -> RevisionText:
        return cls(
            revision_id=revision_id,
            availability=RevisionAvailability.AVAILABLE,
            raw_text=raw_text,
            api_sha1=api_sha1,
            local_content_sha256=local_content_sha256(raw_text),
        )


@dataclass(frozen=True, slots=True)
class DiffOperation:
    """One coalesced alignment operation with both token and character ranges."""

    kind: DiffOpKind
    predecessor_tokens: TokenRange
    target_tokens: TokenRange
    predecessor_chars: TextSpan
    target_chars: TextSpan

    def __post_init__(self) -> None:
        predecessor_empty = self.predecessor_tokens.length == 0
        target_empty = self.target_tokens.length == 0
        if self.kind is DiffOpKind.EQUAL and (predecessor_empty or target_empty):
            raise ValueError("equal operations require tokens on both sides")
        if self.kind is DiffOpKind.INSERT and (not predecessor_empty or target_empty):
            raise ValueError("insert operations require empty predecessor tokens")
        if self.kind is DiffOpKind.DELETE and (predecessor_empty or not target_empty):
            raise ValueError("delete operations require empty target tokens")
        if self.kind is DiffOpKind.REPLACE and (predecessor_empty or target_empty):
            raise ValueError("replace operations require tokens on both sides")


@dataclass(frozen=True, slots=True)
class ChangedRanges:
    """All non-equal ranges, coalesced in alignment order."""

    predecessor_tokens: tuple[TokenRange, ...]
    target_tokens: tuple[TokenRange, ...]
    predecessor_chars: tuple[TextSpan, ...]
    target_chars: tuple[TextSpan, ...]

    def __post_init__(self) -> None:
        count = len(self.predecessor_tokens)
        if not all(
            len(ranges) == count
            for ranges in (
                self.target_tokens,
                self.predecessor_chars,
                self.target_chars,
            )
        ):
            raise ValueError("each changed-range projection must have the same length")


@dataclass(frozen=True, slots=True)
class RevisionDiff:
    """A complete deterministic token alignment between two archival revisions."""

    predecessor: RevisionText
    target: RevisionText
    predecessor_tokens: tuple[RawMappedToken, ...]
    target_tokens: tuple[RawMappedToken, ...]
    operations: tuple[DiffOperation, ...]
    changed: ChangedRanges
    schema_version: int = REVISION_DIFF_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class DiffCandidate:
    """A recoverable candidate produced from one changed alignment operation."""

    action: MethodBAction
    operation_index: int
    predecessor_text: str
    target_text: str
    predecessor_chars: TextSpan
    target_chars: TextSpan
    schema_version: int = REVISION_DIFF_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class DiffEvidence:
    """Provenance retained with an automated Method-B result."""

    predecessor_revision_id: str
    target_revision_id: str
    predecessor_api_sha1: str | None
    target_api_sha1: str | None
    predecessor_local_content_sha256: str | None
    target_local_content_sha256: str | None
    operation_count: int
    schema_version: int = REVISION_DIFF_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """An immutable downstream-facing Method-B decision and its evidence."""

    action: MethodBAction
    candidate: DiffCandidate | None
    evidence: DiffEvidence
    reason: str
    schema_version: int = REVISION_DIFF_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class MethodBEvidence:
    """Versioned, row-serializable evidence for one source occurrence.

    Full-page revision content remains in exact response blobs. Comment-sized
    candidates are retained inline so a safety decision can be audited without
    reconstructing boundaries from a rendered representation.
    """

    source_row_uid: str
    logical_utterance_uid: str
    action_uid: str
    action_type: str
    target_revision_id: str
    predecessor_revision_id: str | None
    page_id: str | None
    target_availability: str
    predecessor_availability: str
    method: str = "mediawiki_revision_diff"
    method_version: str = "1.0.0"
    safety_version: str = "method-b-safety-v2"
    schema_version: int = REVISION_DIFF_SCHEMA_VERSION
    target_api_sha1: str | None = None
    predecessor_api_sha1: str | None = None
    target_local_content_sha256: str | None = None
    predecessor_local_content_sha256: str | None = None
    target_response_hash: str | None = None
    predecessor_response_hash: str | None = None
    target_content_pointer: str | None = None
    predecessor_content_pointer: str | None = None
    diff_operations_json: str = "[]"
    predecessor_changed_ranges_json: str = "[]"
    target_changed_ranges_json: str = "[]"
    candidate_start: int | None = None
    candidate_end: int | None = None
    candidate_raw: str | None = None
    candidate_raw_sha256: str | None = None
    body_start: int | None = None
    body_end: int | None = None
    candidate_body: str | None = None
    candidate_body_sha256: str | None = None
    predecessor_candidate_start: int | None = None
    predecessor_candidate_end: int | None = None
    predecessor_candidate_raw: str | None = None
    predecessor_candidate_body: str | None = None
    boundary_method: str | None = None
    boundary_evidence_json: str = "[]"
    boundary_warnings_json: str = "[]"
    signature_status: str | None = None
    signature_raw: str | None = None
    signature_timestamp: str | None = None
    signature_author: str | None = None
    revision_actor: str | None = None
    wikiconv_speaker: str | None = None
    wikidisputes_speaker: str | None = None
    indentation: str | None = None
    thread_depth: int | None = None
    action_offset_hint: int | None = None
    action_offset_consistency: str | None = None
    lifecycle_consistency: str | None = None
    candidate_count: int = 0
    whole_page_candidate_count: int = 0
    localized_candidate_count: int = 0
    localization_evidence_json: str = "[]"
    action_target_changed_ranges_json: str = "[]"
    hunk_attribution_evidence_json: str = "[]"
    action_count: int = 0
    assignment_status: str | None = None
    assignment_evidence_json: str = "[]"
    assignment_reason_codes_json: str = "[]"
    assignment_margin: int | None = None
    assignment_conflicts_json: str = "[]"
    competing_candidates_json: str = "[]"
    competing_actions_json: str = "[]"
    ambiguity_flags_json: str = "[]"
    target_overlap: float | None = None
    target_coverage: float | None = None
    candidate_purity: float | None = None
    critical_token_contradictions_json: str = "[]"
    neighboring_comment_contamination: str = "unknown"
    structural_warnings_json: str = "[]"
    predecessor_target_continuity: str | None = None
    restoration_history_status: str | None = None
    status: str = "b_review"
    reason_codes_json: str = "[]"

    def __post_init__(self) -> None:
        if not self.source_row_uid or not self.action_uid or not self.target_revision_id:
            raise ValueError("Method-B evidence requires source, action, and target identities")
        if self.candidate_raw is not None:
            digest = local_content_sha256(self.candidate_raw)
            if self.candidate_raw_sha256 not in (None, digest):
                raise ValueError("candidate_raw_sha256 does not match candidate_raw")
        if self.candidate_body is not None:
            digest = local_content_sha256(self.candidate_body)
            if self.candidate_body_sha256 not in (None, digest):
                raise ValueError("candidate_body_sha256 does not match candidate_body")
        if self.neighboring_comment_contamination not in {"unknown", "clean", "detected"}:
            raise ValueError(
                "neighboring_comment_contamination must be unknown, clean, or detected"
            )
        for field in (
            "diff_operations_json",
            "predecessor_changed_ranges_json",
            "target_changed_ranges_json",
            "localization_evidence_json",
            "action_target_changed_ranges_json",
            "hunk_attribution_evidence_json",
            "boundary_evidence_json",
            "boundary_warnings_json",
            "assignment_evidence_json",
            "assignment_reason_codes_json",
            "assignment_conflicts_json",
            "competing_candidates_json",
            "competing_actions_json",
            "ambiguity_flags_json",
            "critical_token_contradictions_json",
            "structural_warnings_json",
            "reason_codes_json",
        ):
            parsed = json.loads(getattr(self, field))
            if not isinstance(parsed, list):
                raise ValueError(f"{field} must encode a JSON list")

    def to_row(self) -> dict[str, object]:
        return asdict(self)


def local_content_sha256(raw_text: str) -> str:
    """Return the SHA-256 of the exact UTF-8 encoding of archival text."""

    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
