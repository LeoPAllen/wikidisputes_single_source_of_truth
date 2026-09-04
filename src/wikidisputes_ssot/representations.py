from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

import mwparserfromhell

from .hashing import canonical_json_hash

LinkKind = Literal[
    "article",
    "external",
    "user",
    "user_talk",
    "special_contributions",
    "diff_revision",
    "section_anchor",
    "interwiki",
    "template_generated",
    "other_internal",
]


@dataclass(frozen=True)
class ExtractedLink:
    link_uid: str
    raw_link_wikitext: str
    raw_url: str | None
    raw_target: str | None
    normalized_target: str | None
    displayed_anchor_text: str | None
    link_kind: LinkKind
    character_start: int | None
    character_end: int | None
    target_status: str
    extraction_method: str
    extraction_version: str = "1.0.0"


def _kind(target: str) -> LinkKind:
    lowered = target.casefold().replace("_", " ")
    if lowered.startswith("user talk:"):
        return "user_talk"
    if lowered.startswith("user:"):
        return "user"
    if lowered.startswith("special:contributions"):
        return "special_contributions"
    if "special:diff" in lowered or "oldid=" in lowered or "diff=" in lowered:
        return "diff_revision"
    if target.startswith("#"):
        return "section_anchor"
    if ":" in target and not lowered.startswith(("category:", "file:", "template:")):
        prefix = target.split(":", 1)[0]
        if 1 < len(prefix) < 12 and prefix.isalpha():
            return "interwiki"
    return "article"


def normalize_internal_target(target: str) -> str:
    return " ".join(target.replace("_", " ").strip().split())


def extract_links(
    wikitext: str,
    *,
    logical_utterance_uid: str,
    version_uid: str,
) -> list[ExtractedLink]:
    """Extract only explicit targets; never infer a target from anchor text."""
    code = mwparserfromhell.parse(wikitext)
    output: list[ExtractedLink] = []
    search_position = 0
    occurrence = 0
    for node in code.filter_wikilinks(recursive=True):
        raw = str(node)
        target = str(node.title)
        start = wikitext.find(raw, search_position)
        end = start + len(raw) if start >= 0 else None
        if start >= 0:
            search_position = end or search_position
        normalized = normalize_internal_target(target)
        output.append(
            ExtractedLink(
                link_uid="wdlink:v1:"
                + canonical_json_hash([logical_utterance_uid, version_uid, occurrence, raw, start]),
                raw_link_wikitext=raw,
                raw_url=None,
                raw_target=target,
                normalized_target=normalized,
                displayed_anchor_text=str(node.text) if node.text is not None else target,
                link_kind=_kind(target),
                character_start=start if start >= 0 else None,
                character_end=end,
                target_status="explicit",
                extraction_method="mwparserfromhell_wikilink",
            )
        )
        occurrence += 1
    external_pattern = re.compile(r"https?://[^\s\]\[<>\"']+")
    for match in external_pattern.finditer(wikitext):
        raw_url = match.group(0).rstrip(".,;:)")
        parsed = urlparse(raw_url)
        if not parsed.scheme or not parsed.netloc:
            continue
        kind: LinkKind = (
            "diff_revision"
            if parsed.netloc.endswith("wikipedia.org")
            and ("oldid=" in parsed.query or "diff=" in parsed.query)
            else "external"
        )
        output.append(
            ExtractedLink(
                link_uid="wdlink:v1:"
                + canonical_json_hash(
                    [logical_utterance_uid, version_uid, occurrence, raw_url, match.start()]
                ),
                raw_link_wikitext=match.group(0),
                raw_url=raw_url,
                raw_target=raw_url,
                normalized_target=raw_url,
                displayed_anchor_text=None,
                link_kind=kind,
                character_start=match.start(),
                character_end=match.start() + len(raw_url),
                target_status="explicit",
                extraction_method="explicit_url_regex",
            )
        )
        occurrence += 1
    return output


SIGNATURE_TIMESTAMP = re.compile(
    r"(?P<time>\d{1,2}:\d{2},\s+\d{1,2}\s+[A-Z][a-z]+\s+\d{4}\s+\(UTC\))"
)
USER_LINK = re.compile(r"\[\[(?P<namespace>User(?: talk)?):(?P<target>[^\]|#]+)")
CONTRIB_LINK = re.compile(r"\[\[Special:Contributions/(?P<target>[^\]|#]+)", re.IGNORECASE)


def extract_signature_evidence(wikitext_fragment: str) -> dict[str, str | None]:
    """Return explicit signature evidence; absence remains `not_observed`, not no authorship."""
    timestamp = SIGNATURE_TIMESTAMP.search(wikitext_fragment)
    user_targets = list(USER_LINK.finditer(wikitext_fragment))
    contribution = CONTRIB_LINK.search(wikitext_fragment)
    if not timestamp and not user_targets and not contribution:
        return {
            "signature_status": "not_observed_in_fragment",
            "raw_signature_wikitext": None,
            "displayed_signature_name": None,
            "user_target": None,
            "user_talk_target": None,
            "contributions_target": None,
            "raw_signature_timestamp_text": None,
        }
    start_candidates = [match.start() for match in user_targets]
    if contribution:
        start_candidates.append(contribution.start())
    if timestamp:
        start_candidates.append(timestamp.start())
    start = min(start_candidates)
    user_target = next(
        (match.group("target") for match in user_targets if match.group("namespace") == "User"),
        None,
    )
    talk_target = next(
        (
            match.group("target")
            for match in user_targets
            if match.group("namespace").casefold() == "user talk"
        ),
        None,
    )
    return {
        "signature_status": "explicit_evidence_observed",
        "raw_signature_wikitext": wikitext_fragment[start:],
        "displayed_signature_name": None,
        "user_target": user_target,
        "user_talk_target": talk_target,
        "contributions_target": contribution.group("target") if contribution else None,
        "raw_signature_timestamp_text": timestamp.group("time") if timestamp else None,
    }
