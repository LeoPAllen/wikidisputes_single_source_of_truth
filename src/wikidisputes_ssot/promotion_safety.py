"""Conservative safety gate for recovered MediaWiki comment bodies.

Recovery confidence answers "did the matcher find a plausible historical
comment?"  This module answers the separate question "may that candidate replace
the trusted annotation text?"  It deliberately prefers a false negative to a
destructive replacement.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Any

import mwparserfromhell

TOKEN_RE = re.compile(
    r"https?://[^\s\]\[<>\"']+|[\w]+(?:['\u2019][\w]+)*|!=|==|<=|>=|[%+*/^=<>-]",
    re.IGNORECASE | re.UNICODE,
)
HEADING_RE = re.compile(r"(?m)^\s*={2,6}\s*[^=\n].*?={2,6}\s*$")
UNSIGNED_RE = re.compile(
    r"Template:Unsigned|Autosigned\s+by\s+SineBot|\{\{\s*unsigned(?:IP)?\b|"
    r"preceding\s+unsigned\s+comment",
    re.IGNORECASE,
)
PAGE_TEMPLATE_RE = re.compile(
    r"\{\{\s*(?:WikiProject|Talk\s*header|Article\s*history|GA|FAQ|Archive|"
    r"Controversial|Not\s*a\s*forum|Connected\s*contributor)\b",
    re.IGNORECASE,
)
TERMINAL_SIGNATURE_RE = re.compile(
    r"(?:\[\[\s*(?:User(?:\s+talk)?|Special:Contributions)\s*[:/]\s*[^\]]+\]\]"
    r"|\b(?:Autosigned\s+by\s+SineBot|preceding\s+unsigned\s+comment)\b)"
    r"[\s\S]{0,240}(?:\d{1,2}:\d{2},\s+\d{1,2}\s+[A-Za-z]+\s+\d{4}\s*\(UTC\))?\s*$",
    re.IGNORECASE,
)
MONTHS = {
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}
NEGATIONS = {
    "not",
    "n't",
    "never",
    "no",
    "none",
    "neither",
    "nor",
    "nothing",
    "nobody",
    "nowhere",
    "cannot",
    "can't",
    "without",
    "hardly",
    "scarcely",
}


@dataclass(frozen=True)
class SafetyDecision:
    decision: str
    reasons: tuple[str, ...]
    anchor_token_count: int
    candidate_token_count: int
    matched_anchor_tokens: int
    matched_candidate_tokens: int
    ordered_token_retention: float
    candidate_token_purity: float
    sequence_ratio: float
    deleted_token_spans: tuple[str, ...]
    added_token_spans: tuple[str, ...]
    missing_critical_tokens: tuple[str, ...]
    added_critical_tokens: tuple[str, ...]
    structural_flags: tuple[str, ...]
    adjacent_contamination: bool
    trusted_comparison_adjustments: tuple[str, ...]
    v33_target_coverage: float | None
    v33_candidate_purity: float | None
    v33_match_margin: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).strip().lower() in {"1", "true", "yes"}


def visible_text(value: str) -> str:
    """Return comparison text while treating wiki markup structurally.

    This is not an exact-equality normalizer.  Wiki/external links retain their
    visible labels, comments and formatting tags disappear, and substantive
    tokens remain available to the ordered alignment and critical-token checks.
    """

    value = html.unescape(_text(value))
    if not value:
        return ""
    with suppress(Exception):
        value = mwparserfromhell.parse(value).strip_code(normalize=True, collapse=False)
    value = re.sub(r"<!--[\s\S]*?-->", " ", value)
    value = re.sub(r"</?[A-Za-z][^>]*>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def comparison_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(visible_text(value)):
        token = match.group(0).casefold().replace("\u2019", "'")
        # Split contractions so loss of n't is independently visible.
        if token.endswith("n't") and len(token) > 3:
            stem = token[:-3]
            if stem == "ca":
                stem = "can"
            elif stem == "wo":
                stem = "will"
            tokens.extend((stem, "n't"))
        else:
            tokens.append(token)
    return tokens


def _critical(token: str) -> bool:
    return (
        token in NEGATIONS
        or token in MONTHS
        or bool(re.search(r"\d", token))
        or token.startswith(("http://", "https://"))
        or token in {"%", "+", "*", "/", "^", "=", "==", "!=", "<", ">", "<=", ">="}
    )


def _unmatched_spans(
    anchor: Sequence[str], candidate: Sequence[str]
) -> tuple[list[list[str]], list[list[str]], int]:
    deleted: list[list[str]] = []
    added: list[list[str]] = []
    matched = 0
    matcher = SequenceMatcher(None, anchor, candidate, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            matched += i2 - i1
        if tag in {"delete", "replace"} and i2 > i1:
            deleted.append(list(anchor[i1:i2]))
        if tag in {"insert", "replace"} and j2 > j1:
            added.append(list(candidate[j1:j2]))
    return deleted, added, matched


def structural_flags(candidate: str) -> tuple[str, ...]:
    raw = _text(candidate)
    stripped = raw.strip()
    flags: set[str] = set()
    if not stripped:
        flags.add("empty_candidate")
    if HEADING_RE.search(raw):
        flags.add("section_heading")
    if UNSIGNED_RE.search(raw):
        flags.add("unsigned_signature_residue")
    if PAGE_TEMPLATE_RE.search(raw[:1200]):
        flags.add("page_template")
    if TERMINAL_SIGNATURE_RE.search(raw):
        flags.add("terminal_signature")
    if re.search(r"(?is)^\s*</(?:span|div|small|sup|sub|blockquote|font)\b", raw):
        flags.add("leading_closing_html")
    if re.match(r"^~+(?:\s|$)", stripped) or re.search(r"(?:^|\s)~+$", stripped):
        flags.add("dangling_tilde")
    if re.search(r"\[\s*\|\s*\]", raw):
        flags.add("malformed_empty_link")
    return tuple(sorted(flags))


def _contains_neighbor(candidate: Sequence[str], anchor: Sequence[str], neighbor: str) -> bool:
    neighbor_tokens = comparison_tokens(neighbor)
    if len(neighbor_tokens) < 8:
        return False
    candidate_match = (
        SequenceMatcher(None, candidate, neighbor_tokens, autojunk=False)
        .find_longest_match(0, len(candidate), 0, len(neighbor_tokens))
        .size
    )
    anchor_match = (
        SequenceMatcher(None, anchor, neighbor_tokens, autojunk=False)
        .find_longest_match(0, len(anchor), 0, len(neighbor_tokens))
        .size
    )
    threshold = min(25, max(8, len(neighbor_tokens) // 2))
    return candidate_match >= threshold and candidate_match >= anchor_match + 5


def _trusted_comparison_text(
    trusted_text: str, evidence: Mapping[str, Any]
) -> tuple[str, tuple[str, ...]]:
    """Remove only V3.3-diagnosed terminal source artifacts for comparison.

    The trusted fallback itself is never changed.  This prevents a clean body
    candidate from being rejected merely because the pre-recovery projection
    retained an unsigned-attribution or conventional valediction artifact.
    Candidate-side occurrences remain structural contamination and are rejected.
    """

    if not _bool(evidence.get("source_signature_artifact_stripped")):
        return trusted_text, ()
    reasons = {
        item.strip()
        for item in _text(evidence.get("source_signature_artifact_reason")).split("|")
        if item.strip()
    }
    updated = trusted_text
    adjustments: list[str] = []
    if "terminal_unsigned_attribution" in reasons:
        cleaned = re.sub(
            r"(?is)\s*(?:(?:--+|[–—])\s*)?(?:preceding\s+)?"
            r"unsigned\s+comment\s+added\s+by.*$",
            "",
            updated,
        ).rstrip()
        if cleaned != updated:
            updated = cleaned
            adjustments.append("ignored_source_artifact:terminal_unsigned_attribution")
    if "terminal_unsigned_attribution_v32" in reasons:
        cleaned = re.sub(
            r"""(?isx)\s*(?:(?:--+|[–—])\s*)?(?:preceding\s+)?
            unsigned\s+comment\s+added\s+by.*$""",
            "",
            updated,
        ).rstrip()
        if cleaned != updated:
            updated = cleaned
            adjustments.append("ignored_source_artifact:terminal_unsigned_attribution_v32")
    if "terminal_bare_ipv4_after_sentence" in reasons:
        match = re.search(
            r"""(?xs)(?P<body>.*?[.!?])[ \t]+(?P<ip>
            (?:\d{1,3}\.){3}\d{1,3})[ \t]*$""",
            updated,
        )
        if match is not None and all(int(part) <= 255 for part in match.group("ip").split(".")):
            cleaned = match.group("body").rstrip()
            if cleaned != updated:
                updated = cleaned
                adjustments.append("ignored_source_artifact:terminal_bare_ipv4_after_sentence")
    if "terminal_explicit_ip_signature" in reasons:
        cleaned = re.sub(
            r"""(?ix)\s*(?:--+|[–—])[ \t]*
            (?:(?:\d{1,3}\.){3}\d{1,3}|[0-9a-f:]{4,})[ \t'"]*$""",
            "",
            updated,
        ).rstrip()
        if cleaned != updated:
            updated = cleaned
            adjustments.append("ignored_source_artifact:terminal_explicit_ip_signature")
    if "terminal_wikidisputes_signature_glyphs" in reasons:
        cleaned = re.sub(
            r"""(?x)\s+[–—][ \t]*[·•][ \t]*$""",
            "",
            updated,
        ).rstrip()
        if cleaned != updated:
            updated = cleaned
            adjustments.append("ignored_source_artifact:terminal_wikidisputes_signature_glyphs")
    if "terminal_valediction_artifact" in reasons:
        cleaned = re.sub(
            r"(?i)(?<=[.!?])\s+(?:best|regards|cheers|best\s+wishes)"
            r"\s*[,:;.!]*\s*(?:(?:--+|[–—])\s*)?[']*\s*$",
            "",
            updated,
        ).rstrip()
        if cleaned != updated:
            updated = cleaned
            adjustments.append("ignored_source_artifact:terminal_valediction_artifact")
    return updated, tuple(adjustments)


def assess_promotion(
    trusted_text: str,
    candidate_text: str,
    evidence: Mapping[str, Any],
    adjacent_trusted_texts: Iterable[str] = (),
) -> SafetyDecision:
    """Assess a V3.3 candidate against trusted same-occurrence evidence."""

    comparison_trusted, comparison_adjustments = _trusted_comparison_text(trusted_text, evidence)
    anchor = comparison_tokens(comparison_trusted)
    candidate = comparison_tokens(candidate_text)
    deleted, added, matched = _unmatched_spans(anchor, candidate)
    anchor_count = len(anchor)
    candidate_count = len(candidate)
    retention = matched / anchor_count if anchor_count else (1.0 if not candidate else 0.0)
    purity = matched / candidate_count if candidate_count else (1.0 if not anchor else 0.0)
    ratio = (
        SequenceMatcher(None, anchor, candidate, autojunk=False).ratio()
        if (anchor or candidate)
        else 1.0
    )

    deleted_flat = [token for span in deleted for token in span]
    added_flat = [token for span in added for token in span]
    missing_critical = tuple(token for token in deleted_flat if _critical(token))
    added_critical = tuple(token for token in added_flat if _critical(token))
    flags = structural_flags(candidate_text)
    adjacent = any(
        _contains_neighbor(candidate, anchor, neighbor)
        for neighbor in adjacent_trusted_texts
        if neighbor
    )

    status = _text(evidence.get("recovery_status"))
    target_coverage = _float(evidence.get("target_coverage"))
    v33_purity = _float(evidence.get("candidate_purity"))
    margin = _float(evidence.get("match_margin"))
    reasons: list[str] = []

    if _text(evidence.get("recovery_tier")) == "legacy_candidate_current_safety":
        reasons.append("legacy_candidate_raw_boundary_unvalidated")
    if status != "high_confidence":
        reasons.append("v33_not_high_confidence")
    if not candidate_text.strip():
        reasons.append("empty_candidate")
    if target_coverage is not None and target_coverage < 0.995:
        reasons.append("v33_target_coverage")
    if v33_purity is not None and v33_purity < 0.97:
        reasons.append("v33_candidate_purity")
    if _bool(evidence.get("signature_residue_detected")):
        reasons.append("v33_signature_residue")
    if retention < 0.995:
        reasons.append("substantive_token_loss")
    if purity < 0.98:
        reasons.append("candidate_extra_content")
    if ratio < 0.99:
        reasons.append("ordered_content_mismatch")
    if missing_critical:
        reasons.append("critical_token_loss")
    if added_critical:
        reasons.append("critical_token_addition")
    if any(len(span) >= 3 for span in added):
        reasons.append("unmatched_candidate_prose")
    if flags:
        reasons.extend(f"structure:{flag}" for flag in flags)
    if adjacent:
        reasons.append("adjacent_utterance_contamination")

    reasons = list(dict.fromkeys(reasons))
    decision = (
        "promote" if not reasons else ("fallback" if status != "high_confidence" else "review")
    )
    return SafetyDecision(
        decision=decision,
        reasons=tuple(reasons),
        anchor_token_count=anchor_count,
        candidate_token_count=candidate_count,
        matched_anchor_tokens=matched,
        matched_candidate_tokens=matched,
        ordered_token_retention=retention,
        candidate_token_purity=purity,
        sequence_ratio=ratio,
        deleted_token_spans=tuple(" ".join(span) for span in deleted),
        added_token_spans=tuple(" ".join(span) for span in added),
        missing_critical_tokens=missing_critical,
        added_critical_tokens=added_critical,
        structural_flags=flags,
        adjacent_contamination=adjacent,
        trusted_comparison_adjustments=comparison_adjustments,
        v33_target_coverage=target_coverage,
        v33_candidate_purity=v33_purity,
        v33_match_margin=margin,
    )
