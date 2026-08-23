from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import html
import http.client
import json
import re
import sqlite3
import statistics
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import duckdb
import mwparserfromhell


ROOT = Path.cwd()

ANNOTATION = (
    ROOT
    / "output"
    / "annotation"
    / "wikidisputes_llm_annotation_input.csv"
)

CACHE_DB = (
    ROOT
    / "data"
    / "cache"
    / "mediawiki_revision_content.sqlite"
)

OUTPUT_CSV = (
    ROOT
    / "output"
    / "silver"
    / "mediawiki_raw_comment_recovery.csv"
)

OUTPUT_PARQUET = (
    ROOT
    / "output"
    / "silver"
    / "mediawiki_raw_comment_recovery.parquet"
)

SUMMARY_JSON = (
    ROOT
    / "reports"
    / "mediawiki_raw_comment_recovery_summary.json"
)

REVIEW_CSV = (
    ROOT
    / "reports"
    / "mediawiki_raw_comment_recovery_review.csv"
)


API = "https://en.wikipedia.org/w/api.php"

USER_AGENT = (
    "WikiDisputes-SSOT-raw-wikitext-recovery/1.0 "
    "(research corpus reconstruction)"
)

MONTH = (
    r"(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)"
)

TIMESTAMP_RE = re.compile(
    rf"""
    \b
    \d{{1,2}}:\d{{2}},
    \s+
    \d{{1,2}}
    \s+
    {MONTH}
    \s+
    \d{{4}}
    \s*
    \(UTC\)
    """,
    re.I | re.X,
)

HEADING_RE = re.compile(
    r"^\s*=+\s*.*?\s*=+\s*$"
)

SIGNATURE_LINK_RE = re.compile(
    r"""
    \[\[
    (?:
        User(?:\s+talk)?
        |
        Special:Contributions
    )
    [:/]
    [^\]]+
    \]\]
    """,
    re.I | re.X,
)

MARKUP_PATTERNS = {
    "url": re.compile(
        r"https?://[^\s\]\|<>]+",
        re.I,
    ),
    "wikilink": re.compile(
        r"\[\[[^\]]+\]\]"
    ),
    "user_link": re.compile(
        r"\[\[\s*(?:User|User talk)\s*:[^\]]+\]\]",
        re.I,
    ),
    "special_contributions": re.compile(
        r"\[\[\s*Special:Contributions/",
        re.I,
    ),
    "policy_link": re.compile(
        r"\[\[\s*(?:WP|Wikipedia)\s*:[^\]]+\]\]",
        re.I,
    ),
    "external_link": re.compile(
        r"\[(?:https?://|//)[^\]]+\]",
        re.I,
    ),
    "template": re.compile(
        r"\{\{",
    ),
    "ref_tag": re.compile(
        r"</?ref\b",
        re.I,
    ),
}


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional utterance limit for testing.",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
    )

    parser.add_argument(
        "--cache-only",
        action="store_true",
        help=(
            "Do not contact MediaWiki. Abort if a "
            "required revision is absent from the "
            "local SQLite cache."
        ),
    )

    return parser.parse_args()


def iso_now() -> str:
    return dt.datetime.now(
        dt.timezone.utc
    ).isoformat()


# ============================================================
# Cache
# ============================================================

def open_cache() -> sqlite3.Connection:
    CACHE_DB.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    db = sqlite3.connect(
        CACHE_DB,
        timeout=60,
    )

    # Allow the recovery writer and side-terminal watcher
    # to access the cache concurrently.
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA busy_timeout=60000")

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS revision_cache (
            revision_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            page_id INTEGER,
            title TEXT,
            parent_id INTEGER,
            revision_timestamp TEXT,
            revision_user TEXT,
            sha1 TEXT,
            content TEXT,
            fetched_at TEXT NOT NULL
        )
        """
    )

    db.commit()

    return db


def cached_revision_ids(
    db: sqlite3.Connection,
) -> set[int]:

    return {
        int(row[0])
        for row in db.execute(
            """
            SELECT revision_id
            FROM revision_cache
            """
        )
    }


def cache_revision(
    db: sqlite3.Connection,
    *,
    revision_id: int,
    status: str,
    page_id=None,
    title=None,
    parent_id=None,
    revision_timestamp=None,
    revision_user=None,
    sha1=None,
    content=None,
):
    db.execute(
        """
        INSERT OR REPLACE INTO revision_cache (
            revision_id,
            status,
            page_id,
            title,
            parent_id,
            revision_timestamp,
            revision_user,
            sha1,
            content,
            fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision_id,
            status,
            page_id,
            title,
            parent_id,
            revision_timestamp,
            revision_user,
            sha1,
            content,
            iso_now(),
        ),
    )


def get_revision(
    db: sqlite3.Connection,
    revision_id: int,
) -> dict | None:

    row = db.execute(
        """
        SELECT
            revision_id,
            status,
            page_id,
            title,
            parent_id,
            revision_timestamp,
            revision_user,
            sha1,
            content
        FROM revision_cache
        WHERE revision_id = ?
        """,
        (revision_id,),
    ).fetchone()

    if row is None:
        return None

    fields = [
        "revision_id",
        "status",
        "page_id",
        "title",
        "parent_id",
        "revision_timestamp",
        "revision_user",
        "sha1",
        "content",
    ]

    return dict(zip(fields, row))


# ============================================================
# MediaWiki
# ============================================================

def api_request(
    revision_ids: list[int],
) -> dict:

    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "prop": "revisions",
        "revids": "|".join(
            str(x)
            for x in revision_ids
        ),
        "rvprop":
            "ids|timestamp|user|sha1|content",
        "rvslots": "main",
        "maxlag": "5",
    }

    url = (
        API
        + "?"
        + urllib.parse.urlencode(
            params,
            doseq=True,
        )
    )

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
        },
    )

    last_error = None

    for attempt in range(8):
        try:
            with urllib.request.urlopen(
                req,
                timeout=90,
            ) as response:

                return json.loads(
                    response
                    .read()
                    .decode("utf-8")
                )

        except urllib.error.HTTPError as exc:
            last_error = exc

            if exc.code == 429:
                retry_after = exc.headers.get(
                    "Retry-After"
                )

                delay = (
                    float(retry_after)
                    if retry_after
                    else min(
                        30,
                        2 ** attempt,
                    )
                )

            elif 500 <= exc.code < 600:
                delay = min(
                    30,
                    2 ** attempt,
                )

            else:
                raise

            print(
                f"HTTP {exc.code}; retrying batch"
            )

            time.sleep(delay)

        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
            ConnectionResetError,
        ) as exc:

            last_error = exc

            time.sleep(
                min(
                    30,
                    2 ** attempt,
                )
            )

    raise RuntimeError(
        "MediaWiki request failed after retries: "
        f"{last_error}"
    )


def fetch_missing_revisions(
    db: sqlite3.Connection,
    revision_ids: list[int],
    batch_size: int,
):
    existing = cached_revision_ids(
        db
    )

    missing = [
        rid
        for rid in revision_ids
        if rid not in existing
    ]

    print()
    print(
        f"unique revisions required: "
        f"{len(revision_ids):,}"
    )
    print(
        f"already cached:            "
        f"{len(revision_ids) - len(missing):,}"
    )
    print(
        f"to fetch:                  "
        f"{len(missing):,}"
    )

    for start in range(
        0,
        len(missing),
        batch_size,
    ):
        batch = missing[
            start:start + batch_size
        ]

        payload = api_request(
            batch
        )

        returned: set[int] = set()

        for page in (
            payload
            .get("query", {})
            .get("pages", [])
        ):
            page_id = page.get("pageid")
            title = page.get("title")

            for rev in page.get(
                "revisions",
                [],
            ):
                rid = int(
                    rev["revid"]
                )

                returned.add(rid)

                slot = (
                    rev
                    .get("slots", {})
                    .get("main", {})
                )

                content = (
                    slot.get("content")
                    if isinstance(
                        slot,
                        dict,
                    )
                    else None
                )

                # Compatibility fallback.
                if content is None:
                    content = rev.get("*")

                cache_revision(
                    db,
                    revision_id=rid,
                    status=(
                        "found"
                        if content is not None
                        else "content_unavailable"
                    ),
                    page_id=page_id,
                    title=title,
                    parent_id=rev.get(
                        "parentid"
                    ),
                    revision_timestamp=rev.get(
                        "timestamp"
                    ),
                    revision_user=rev.get(
                        "user"
                    ),
                    sha1=rev.get(
                        "sha1"
                    ),
                    content=content,
                )

        for rid in batch:
            if rid not in returned:
                cache_revision(
                    db,
                    revision_id=rid,
                    status="revision_unavailable",
                )

        db.commit()

        done = min(
            start + len(batch),
            len(missing),
        )

        if (
            done % 100 == 0
            or done == len(missing)
        ):
            print(
                f"fetched/cache-resolved: "
                f"{done:,}/{len(missing):,}"
            )

        time.sleep(1.25)


# ============================================================
# Raw comment segmentation
# ============================================================

# BOUNDARY_EXTRACTION_V2
# ============================================================
# Raw comment segmentation
# ============================================================

_SIGNATURE_TARGET_RE = re.compile(
    r"""
    \[\[
    \s*
    (?:
        User(?:\s+talk)?\s*:
        |
        Special:Contributions\s*[:/]
    )
    \s*
    ([^|\]#]+)
    """,
    re.I | re.X,
)


def _line_start(
    raw: str,
    pos: int,
) -> int:
    return (
        raw.rfind(
            "\n",
            0,
            max(0, pos),
        )
        + 1
    )


def _line_end(
    raw: str,
    pos: int,
) -> int:
    found = raw.find(
        "\n",
        max(0, pos),
    )

    if found < 0:
        return len(raw)

    return found + 1


def _trim_leading_blank_lines(
    raw: str,
    start: int,
    end: int,
) -> int:
    pos = start

    while pos < end:
        line_end = min(
            _line_end(
                raw,
                pos,
            ),
            end,
        )

        line = raw[
            pos:line_end
        ]

        if line.strip():
            break

        if line_end <= pos:
            break

        pos = line_end

    return pos


def _last_heading_end(
    raw: str,
    start: int,
    end: int,
) -> int | None:
    pos = start
    last = None

    while pos < end:
        line_end = min(
            _line_end(
                raw,
                pos,
            ),
            end,
        )

        line = raw[
            pos:line_end
        ]

        if HEADING_RE.match(
            line.rstrip("\r\n")
        ):
            last = line_end

        if line_end <= pos:
            break

        pos = line_end

    return last


def _normalize_username(
    value: str | None,
) -> str | None:
    if not value:
        return None

    value = urllib.parse.unquote(
        str(value)
    )

    value = (
        value
        .replace("_", " ")
        .strip()
        .casefold()
    )

    return value or None


def _signature_link_target(
    match: re.Match,
) -> str | None:
    found = _SIGNATURE_TARGET_RE.search(
        match.group(0)
    )

    if not found:
        return None

    return _normalize_username(
        found.group(1)
    )


def _signature_gap_is_structural(
    value: str,
) -> bool:
    # Remove simple HTML presentation wrappers often
    # found inside signatures.
    value = re.sub(
        r"</?(?:small|sup|sub|span|font)\b[^>]*>",
        "",
        value,
        flags=re.I,
    )

    value = re.sub(
        r"(?:&nbsp;|&#160;)",
        "",
        value,
        flags=re.I,
    )

    # Remove Wiki bold/italic delimiters without placing
    # literal triple single-quotes in this embedded source.
    value = value.replace(
        "'" * 3,
        "",
    )

    value = value.replace(
        "'" * 2,
        "",
    )

    # If substantive words occur after a supposed user
    # signature link and before the timestamp, that user
    # link is probably an inline mention/ping.
    return not bool(
        re.search(
            r"[A-Za-z0-9]{3,}",
            value,
        )
    )


def _terminal_signature_span(
    raw: str,
    stamp: re.Match,
    expected_user: str | None = None,
) -> tuple[int, int] | None:

    before = raw[
        :stamp.start()
    ]

    search_start = max(
        0,
        stamp.start() - 700,
    )

    tail = before[
        search_start:
    ]

    matches = list(
        SIGNATURE_LINK_RE.finditer(
            tail
        )
    )

    if not matches:
        return None

    expected = _normalize_username(
        expected_user
    )

    # Work from the rightmost signature-like links.
    ordered = list(
        reversed(matches)
    )

    # Revision editor is a useful preference, never a
    # requirement.
    if expected is not None:
        ordered.sort(
            key=lambda match: (
                _signature_link_target(
                    match
                )
                != expected,
                -match.end(),
            )
        )

    chosen = None

    for match in ordered:
        absolute_end = (
            search_start
            + match.end()
        )

        # Terminal signature should occur reasonably close
        # to its timestamp.
        if (
            stamp.start()
            - absolute_end
            > 320
        ):
            continue

        suffix = before[
            absolute_end:
            stamp.start()
        ]

        if not _signature_gap_is_structural(
            suffix
        ):
            continue

        chosen = match
        break

    if chosen is None:
        return None

    chosen_target = (
        _signature_link_target(
            chosen
        )
    )

    cluster_start = (
        search_start
        + chosen.start()
    )

    # Expand backward through an adjacent conventional
    # signature cluster such as:
    #
    # [[User:X]] ([[User talk:X|talk]])
    #
    # but do not cross substantive prose or another user.
    prior_matches = [
        match
        for match in matches
        if match.end()
        <= chosen.start()
    ]

    for prior in reversed(
        prior_matches
    ):
        prior_target = (
            _signature_link_target(
                prior
            )
        )

        if (
            chosen_target is not None
            and prior_target is not None
            and prior_target
            != chosen_target
        ):
            break

        absolute_prior_end = (
            search_start
            + prior.end()
        )

        gap = before[
            absolute_prior_end:
            cluster_start
        ]

        if len(gap) > 120:
            break

        if not _signature_gap_is_structural(
            gap
        ):
            break

        cluster_start = (
            search_start
            + prior.start()
        )

    # Include a directly attached conventional signature
    # separator, but never arbitrary preceding prose.
    prefix_start = max(
        search_start,
        cluster_start - 20,
    )

    prefix = before[
        prefix_start:
        cluster_start
    ]

    delimiter = re.search(
        r"(?:[ \t]*(?:--+|[–—]|~~~~?)[ \t]*)$",
        prefix,
    )

    if delimiter:
        cluster_start = (
            prefix_start
            + delimiter.start()
        )

    return (
        cluster_start,
        stamp.end(),
    )


def body_without_signature(
    raw: str,
    expected_user: str | None = None,
) -> str:

    timestamps = list(
        TIMESTAMP_RE.finditer(
            raw
        )
    )

    if not timestamps:
        return raw

    stamp = timestamps[-1]

    signature = (
        _terminal_signature_span(
            raw,
            stamp,
            expected_user=expected_user,
        )
    )

    prefix_end = (
        signature[0]
        if signature is not None
        else stamp.start()
    )

    # Preserve legitimate text after the timestamp.
    # This permits P.S./Edit/final-sentence tails.
    return (
        raw[:prefix_end]
        + raw[stamp.end():]
    )


def _indent_depth(
    line: str,
) -> int:
    found = re.match(
        r"^[ \t]*([:*#;]+)",
        line,
    )

    if not found:
        return 0

    return len(
        found.group(1)
    )


def _candidate_start_variants(
    raw: str,
    region_start: int,
    stamp: re.Match,
) -> list[tuple[int, str]]:

    # A section heading between the previous timestamp and
    # this one is a hard boundary.
    heading_end = _last_heading_end(
        raw,
        region_start,
        stamp.start(),
    )

    hard_start = (
        heading_end
        if heading_end is not None
        else region_start
    )

    hard_start = (
        _trim_leading_blank_lines(
            raw,
            hard_start,
            stamp.start(),
        )
    )

    starts: dict[int, str] = {
        hard_start: (
            "after_heading"
            if heading_end is not None
            else "timestamp_region"
        )
    }

    before = raw[
        hard_start:
        stamp.start()
    ]

    # Nearest paragraph boundary: useful when unsigned
    # neighboring material precedes the signed comment.
    paragraph_breaks = list(
        re.finditer(
            r"\n[ \t]*\n",
            before,
        )
    )

    if paragraph_breaks:
        candidate_start = (
            hard_start
            + paragraph_breaks[-1].end()
        )

        candidate_start = (
            _trim_leading_blank_lines(
                raw,
                candidate_start,
                stamp.start(),
            )
        )

        if candidate_start < stamp.start():
            starts.setdefault(
                candidate_start,
                "paragraph_block",
            )

    signature = (
        _terminal_signature_span(
            raw,
            stamp,
        )
    )

    signature_start = (
        signature[0]
        if signature is not None
        else stamp.start()
    )

    signature_line_start = max(
        hard_start,
        _line_start(
            raw,
            signature_start,
        ),
    )

    # Conservative final-line candidate.
    if (
        signature_line_start
        > hard_start
        and raw[
            signature_line_start:
            signature_start
        ].strip()
    ):
        starts.setdefault(
            signature_line_start,
            "signature_line",
        )

    # Talk-page indentation/list structure.
    signature_line = raw[
        signature_line_start:
        min(
            _line_end(
                raw,
                signature_line_start,
            ),
            stamp.end(),
        )
    ]

    depth = _indent_depth(
        signature_line
    )

    if depth > 0:
        pos = signature_line_start
        block_start = (
            signature_line_start
        )

        while pos > hard_start:
            prior_start = max(
                hard_start,
                _line_start(
                    raw,
                    pos - 1,
                ),
            )

            prior_line = raw[
                prior_start:
                pos
            ]

            if not prior_line.strip():
                break

            if HEADING_RE.match(
                prior_line.rstrip(
                    "\r\n"
                )
            ):
                break

            if (
                _indent_depth(
                    prior_line
                )
                < depth
            ):
                break

            block_start = (
                prior_start
            )

            if prior_start >= pos:
                break

            pos = prior_start

        if (
            block_start > hard_start
            and block_start
            < stamp.start()
        ):
            starts.setdefault(
                block_start,
                "indent_block",
            )

    return sorted(
        starts.items()
    )


def _candidate_end_variants(
    raw: str,
    stamp: re.Match,
    next_stamp_start: int,
) -> list[tuple[int, str]]:

    ends: dict[int, str] = {
        stamp.end():
            "through_timestamp"
    }

    # Preserve same-line post-timestamp material.
    line_end = min(
        _line_end(
            raw,
            stamp.end(),
        ),
        next_stamp_start,
    )

    if raw[
        stamp.end():
        line_end
    ].strip():
        ends.setdefault(
            line_end,
            "same_line_tail",
        )

    structural_limit = (
        next_stamp_start
    )

    # Never extend a candidate across a section heading.
    pos = stamp.end()

    while pos < structural_limit:
        current_end = min(
            _line_end(
                raw,
                pos,
            ),
            structural_limit,
        )

        line = raw[
            pos:current_end
        ]

        if HEADING_RE.match(
            line.rstrip("\r\n")
        ):
            structural_limit = pos
            break

        if current_end <= pos:
            break

        pos = current_end

    # One bounded post-signature paragraph candidate allows
    # legitimate P.S./Edit/final-tail content to survive.
    if structural_limit > stamp.end():
        tail = raw[
            stamp.end():
            structural_limit
        ]

        first_nonspace = re.search(
            r"\S",
            tail,
        )

        if first_nonspace:
            content_start = (
                stamp.end()
                + first_nonspace.start()
            )

            cap = min(
                structural_limit,
                stamp.end() + 1600,
            )

            after = raw[
                content_start:
                cap
            ]

            paragraph_break = re.search(
                r"\n[ \t]*\n",
                after,
            )

            if paragraph_break:
                paragraph_end = (
                    content_start
                    + paragraph_break.start()
                )
            else:
                paragraph_end = cap

            if (
                paragraph_end
                > stamp.end()
                and raw[
                    stamp.end():
                    paragraph_end
                ].strip()
            ):
                ends.setdefault(
                    paragraph_end,
                    "post_signature_paragraph",
                )

    return sorted(
        ends.items()
    )


def candidate_comments(
    raw: str,
    expected_user: str | None = None,
) -> list[dict]:

    timestamps = list(
        TIMESTAMP_RE.finditer(
            raw
        )
    )

    candidates = []
    previous_end = 0
    seen = set()

    for (
        anchor_index,
        stamp,
    ) in enumerate(
        timestamps
    ):

        next_stamp_start = (
            timestamps[
                anchor_index + 1
            ].start()
            if (
                anchor_index + 1
                < len(timestamps)
            )
            else len(raw)
        )

        starts = (
            _candidate_start_variants(
                raw,
                previous_end,
                stamp,
            )
        )

        ends = (
            _candidate_end_variants(
                raw,
                stamp,
                next_stamp_start,
            )
        )

        for (
            start,
            start_method,
        ) in starts:

            for (
                end,
                end_method,
            ) in ends:

                if end <= start:
                    continue

                key = (
                    anchor_index,
                    start,
                    end,
                )

                if key in seen:
                    continue

                seen.add(key)

                fragment = raw[
                    start:end
                ]

                if not fragment.strip():
                    continue

                body = (
                    body_without_signature(
                        fragment,
                        expected_user=expected_user,
                    )
                )

                candidates.append(
                    {
                        "candidate_index":
                            len(candidates),

                        # Boundary variants for the same
                        # timestamp share one anchor.
                        "anchor_index":
                            anchor_index,

                        "start":
                            start,

                        "end":
                            end,

                        "raw":
                            fragment,

                        "body_without_signature":
                            body,

                        "expected_user":
                            expected_user,

                        "boundary_method":
                            (
                                start_method
                                + "+"
                                + end_method
                            ),
                    }
                )

        previous_end = (
            stamp.end()
        )

    return candidates


# ============================================================
# Normalization/matching
# ============================================================

def normalize(
    value: str | None,
) -> str:

    if not value:
        return ""

    value = html.unescape(
        value
    )

    try:
        value = str(
            mwparserfromhell
            .parse(
                value,
                skip_style_tags=True,
            )
            .strip_code(
                normalize=True,
                collapse=True,
                keep_template_params=False,
            )
        )

    except Exception:
        pass

    value = unicodedata.normalize(
        "NFKC",
        value,
    )

    value = re.sub(
        r"(?m)^\s*[:#*;]+\s*",
        "",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    value = (
        value
        .replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
        .replace("—", "-")
        .replace("–", "-")
    )

    return value.strip().casefold()


def similarity(
    target: str,
    candidate: str,
) -> float:

    if not target or not candidate:
        return 0.0

    if target == candidate:
        return 1.0

    # Deliberately do NOT award .999/.995 merely because
    # one string contains the other. Containment can be a
    # symptom of boundary contamination/truncation.
    return difflib.SequenceMatcher(
        None,
        target,
        candidate,
        autojunk=False,
    ).ratio()


def _alignment_metrics(
    target: str,
    candidate: str,
) -> tuple[
    float,
    float,
    float,
    int,
]:

    if not target or not candidate:
        return (
            0.0,
            0.0,
            0.0,
            len(candidate)
            - len(target),
        )

    matcher = difflib.SequenceMatcher(
        None,
        target,
        candidate,
        autojunk=False,
    )

    ratio = matcher.ratio()

    matching = sum(
        block.size
        for block
        in matcher.get_matching_blocks()
    )

    target_coverage = (
        matching / len(target)
        if target
        else 0.0
    )

    candidate_purity = (
        matching / len(candidate)
        if candidate
        else 0.0
    )

    return (
        ratio,
        target_coverage,
        candidate_purity,
        len(candidate)
        - len(target),
    )


def offset_distance(
    offset: int,
    start: int,
    end: int,
) -> int:

    if start <= offset <= end:
        return 0

    return min(
        abs(offset - start),
        abs(offset - end),
    )


def markup_counts(
    value: str | None,
) -> dict[str, int]:

    value = value or ""

    return {
        label: len(
            pattern.findall(value)
        )
        for label, pattern
        in MARKUP_PATTERNS.items()
    }


# ============================================================
# Candidate prefilter
# ============================================================

def candidate_pool(
    candidates: list[dict],
    action_offset: int,
) -> list[dict]:

    # WikiConv offsets are not literal raw offsets, but the
    # Expert system calibration showed displacement of roughly
    # 1k characters. Use a broad ±15k position window first.
    nearby = [
        c
        for c in candidates
        if offset_distance(
            action_offset,
            c["start"],
            c["end"],
        ) <= 15000
    ]

    if nearby:
        return nearby

    return candidates


# BOUNDARY_V3_SIGNATURE_SAFETY
# ============================================================
# V3 terminal-signature overrides
#
# These deliberately override the earlier V2 helpers at runtime.
# Once validated, they can be folded into the primary definitions.
# ============================================================

SIGNATURE_LINK_RE = re.compile(
    r"""
    \[\[
    \s*
    (?:
        User(?:[\s_]+talk)?
        |
        Special\s*:\s*Contributions
    )
    \s*
    [:/]
    [^\]]+
    \]\]
    """,
    re.I | re.X,
)

_SIGNATURE_TARGET_RE = re.compile(
    r"""
    \[\[
    \s*
    (?:
        User(?:[\s_]+talk)?\s*:
        |
        Special\s*:\s*Contributions\s*[:/]
    )
    \s*
    ([^|\]#]+)
    """,
    re.I | re.X,
)


def _is_ip_user_v3(
    value: str | None,
) -> bool:
    if not value:
        return False

    value = str(value).strip()

    if re.fullmatch(
        r"(?:\d{1,3}\.){3}\d{1,3}",
        value,
    ):
        parts = value.split(".")

        return all(
            0 <= int(part) <= 255
            for part in parts
        )

    return bool(
        ":" in value
        and re.fullmatch(
            r"[0-9A-Fa-f:]+",
            value,
        )
    )


def _same_line_signature_end_v3(
    raw: str,
    stamp_end: int,
) -> int:
    """
    Consume only closing presentation markup on the same line
    as the signature timestamp. Never consume a newline.
    """
    line_end = raw.find(
        "\n",
        stamp_end,
    )

    if line_end < 0:
        line_end = len(raw)

    suffix = raw[
        stamp_end:line_end
    ]

    match = re.match(
        r"""
        ^[ \t]*
        (?:
            '+
            |
            </?(?:small|sup|sub|span|font)\b[^>]*>
            |
            [\]\)}]
            |
            &nbsp;
            |
            &#160;
            |
            [ \t]
        )*
        """,
        suffix,
        flags=re.I | re.X,
    )

    if not match:
        return stamp_end

    return (
        stamp_end
        + match.end()
    )


def _unsigned_signature_span_v3(
    raw: str,
    stamp: re.Match,
) -> tuple[int, int] | None:
    window_start = max(
        0,
        stamp.start() - 700,
    )

    before_window = raw[
        window_start:stamp.start()
    ]

    template_matches = list(
        re.finditer(
            r"""
            \{\{
            \s*
            (?:
                subst\s*:\s*
            )?
            (?:
                unsigned
                (?:\s*ip|\s*2)?
                |
                unsignedip
                |
                nosign
            )
            \b
            """,
            before_window,
            flags=re.I | re.X,
        )
    )

    if template_matches:
        start = (
            window_start
            + template_matches[-1].start()
        )

        close = raw.find(
            "}}",
            stamp.end(),
            min(
                len(raw),
                stamp.end() + 400,
            ),
        )

        if close >= 0:
            return (
                start,
                close + 2,
            )

    phrase_matches = list(
        re.finditer(
            r"""
            (?:
                <!--[^>]*-->\s*
            )?
            (?:
                --+
                |
                [–—]
            )?
            [ \t]*
            (?:
                preceding[ \t]+
            )?
            unsigned[ \t]+comment[ \t]+
            added[ \t]+by
            \b
            """,
            before_window,
            flags=re.I | re.X,
        )
    )

    if phrase_matches:
        start = (
            window_start
            + phrase_matches[-1].start()
        )

        return (
            start,
            _same_line_signature_end_v3(
                raw,
                stamp.end(),
            ),
        )

    return None


def _bare_ip_signature_span_v3(
    raw: str,
    stamp: re.Match,
    expected_user: str | None,
) -> tuple[int, int] | None:
    before = raw[
        :stamp.start()
    ]

    # Explicit --IP / —IP is signature-shaped even if the
    # revision editor differs from the historical commenter.
    explicit = re.search(
        r"""
        (?P<signature>
            (?:
                --+
                |
                [–—]
            )
            [ \t]*
            (?:
                (?:\d{1,3}\.){3}\d{1,3}
                |
                [0-9A-Fa-f:]{4,}
            )
            [ \t]*
        )
        $
        """,
        before,
        flags=re.I | re.X,
    )

    if explicit:
        return (
            explicit.start(
                "signature"
            ),
            _same_line_signature_end_v3(
                raw,
                stamp.end(),
            ),
        )

    if not _is_ip_user_v3(
        expected_user
    ):
        return None

    user = str(
        expected_user
    ).strip()

    terminal = re.search(
        rf"""
        (?P<signature>
            {re.escape(user)}
            [ \t]*
        )
        $
        """,
        before,
        flags=re.I | re.X,
    )

    if not terminal:
        return None

    return (
        terminal.start(
            "signature"
        ),
        _same_line_signature_end_v3(
            raw,
            stamp.end(),
        ),
    )


def _terminal_signature_span(
    raw: str,
    stamp: re.Match,
    expected_user: str | None = None,
) -> tuple[int, int] | None:
    """
    V3 terminal-signature parser.

    A terminal User/User-talk link is treated as signature
    evidence only when supported by at least one independent
    structural signal:

    - revision-editor identity;
    - explicit signature punctuation; or
    - a same-user link cluster.

    This protects legitimate final inline user references.
    """

    unsigned_span = (
        _unsigned_signature_span_v3(
            raw,
            stamp,
        )
    )

    if unsigned_span is not None:
        return unsigned_span

    ip_span = (
        _bare_ip_signature_span_v3(
            raw,
            stamp,
            expected_user,
        )
    )

    if ip_span is not None:
        return ip_span

    before = raw[
        :stamp.start()
    ]

    search_start = max(
        0,
        stamp.start() - 700,
    )

    tail = before[
        search_start:
    ]

    matches = list(
        SIGNATURE_LINK_RE.finditer(
            tail
        )
    )

    if not matches:
        return None

    expected = _normalize_username(
        expected_user
    )

    ordered = list(
        reversed(matches)
    )

    if expected is not None:
        ordered.sort(
            key=lambda match: (
                _signature_link_target(
                    match
                )
                != expected,
                -match.end(),
            )
        )

    chosen = None

    for match in ordered:
        absolute_start = (
            search_start
            + match.start()
        )

        absolute_end = (
            search_start
            + match.end()
        )

        if (
            stamp.start()
            - absolute_end
            > 320
        ):
            continue

        suffix = before[
            absolute_end:
            stamp.start()
        ]

        if not _signature_gap_is_structural(
            suffix
        ):
            continue

        target = (
            _signature_link_target(
                match
            )
        )

        matches_expected = (
            expected is not None
            and target == expected
        )

        prefix = before[
            max(
                0,
                absolute_start - 30,
            ):
            absolute_start
        ]

        has_separator = bool(
            re.search(
                r"(?:--+|[–—]|~~~~?)\s*['\"]*\s*$",
                prefix,
            )
        )

        has_same_target_neighbor = False

        for prior in reversed(
            [
                candidate
                for candidate in matches
                if candidate.end()
                <= match.start()
            ]
        ):
            prior_target = (
                _signature_link_target(
                    prior
                )
            )

            if (
                target is None
                or prior_target != target
            ):
                continue

            prior_absolute_end = (
                search_start
                + prior.end()
            )

            gap = before[
                prior_absolute_end:
                absolute_start
            ]

            if (
                len(gap) <= 120
                and _signature_gap_is_structural(
                    gap
                )
            ):
                has_same_target_neighbor = True

            break

        if not (
            matches_expected
            or has_separator
            or has_same_target_neighbor
        ):
            continue

        chosen = match
        break

    if chosen is None:
        return None

    chosen_target = (
        _signature_link_target(
            chosen
        )
    )

    cluster_start = (
        search_start
        + chosen.start()
    )

    prior_matches = [
        match
        for match in matches
        if match.end()
        <= chosen.start()
    ]

    for prior in reversed(
        prior_matches
    ):
        prior_target = (
            _signature_link_target(
                prior
            )
        )

        if (
            chosen_target is not None
            and prior_target is not None
            and prior_target
            != chosen_target
        ):
            break

        absolute_prior_end = (
            search_start
            + prior.end()
        )

        gap = before[
            absolute_prior_end:
            cluster_start
        ]

        if len(gap) > 120:
            break

        if not _signature_gap_is_structural(
            gap
        ):
            break

        cluster_start = (
            search_start
            + prior.start()
        )

    prefix_start = max(
        search_start,
        cluster_start - 40,
    )

    prefix = before[
        prefix_start:
        cluster_start
    ]

    delimiter = re.search(
        r"""
        (?:
            [ \t]*
            (?:
                --+
                |
                [–—]
                |
                ~~~~?
            )
            [ \t'"]*
        )
        $
        """,
        prefix,
        flags=re.X,
    )

    if delimiter:
        cluster_start = (
            prefix_start
            + delimiter.start()
        )

    # A short valediction directly attached to an identified
    # signature is signature material. Deliberately exclude
    # "thanks", preserving e.g. "Thanks for the title."
    valediction_window_start = max(
        _line_start(
            raw,
            cluster_start,
        ),
        cluster_start - 100,
    )

    valediction_window = raw[
        valediction_window_start:
        cluster_start
    ]

    valediction = re.search(
        r"""
        (?:
            ^
            |
            [.!?][ \t]+
        )
        (?P<valediction>
            (?:
                best
                |
                regards
                |
                cheers
                |
                best[ \t]+wishes
            )
            [ \t]*
            [,:;.!'"]*
            [ \t]*
            (?:
                --+
                |
                [–—]
            )?
            [ \t'"]*
        )
        $
        """,
        valediction_window,
        flags=re.I | re.X,
    )

    if valediction:
        cluster_start = (
            valediction_window_start
            + valediction.start(
                "valediction"
            )
        )

    return (
        cluster_start,
        _same_line_signature_end_v3(
            raw,
            stamp.end(),
        ),
    )


def _strip_terminal_signature_residue_v3(
    value: str,
    expected_user: str | None = None,
) -> str:
    if not value:
        return value

    result = value.rstrip()

    # Leftover {{unsigned...}} machinery.
    result = re.sub(
        r"""
        \s*
        \{\{
        \s*
        (?:
            subst\s*:\s*
        )?
        (?:
            unsigned
            (?:\s*ip|\s*2)?
            |
            unsignedip
            |
            nosign
        )
        \b
        .*?
        \}\}
        \s*
        $
        """,
        "",
        result,
        flags=re.I | re.X | re.S,
    )

    # SineBot / "Preceding unsigned comment added by..." residue.
    result = re.sub(
        r"""
        \s*
        (?:
            <!--[^>]*-->\s*
        )?
        (?:
            --+
            |
            [–—]
        )?
        [ \t]*
        (?:
            preceding[ \t]+
        )?
        unsigned[ \t]+comment[ \t]+
        added[ \t]+by
        .*?
        $
        """,
        "",
        result,
        flags=re.I | re.X | re.S,
    )

    # Bare terminal IP matching the revision editor.
    if _is_ip_user_v3(
        expected_user
    ):
        user = str(
            expected_user
        ).strip()

        result = re.sub(
            rf"""
            \s*
            (?:
                --+
                |
                [–—]
            )?
            [ \t]*
            {re.escape(user)}
            [ \t'"]*
            $
            """,
            "",
            result,
            flags=re.I | re.X,
        )

    # Short valediction residue after link-cluster removal.
    result = re.sub(
        r"""
        (?:
            (?<=\n)
            |
            (?<=[.!?])
            [ \t]+
            |
            ^
        )
        (?:
            best
            |
            regards
            |
            cheers
            |
            best[ \t]+wishes
        )
        [ \t]*
        [,:;.!'"]*
        [ \t]*
        (?:
            --+
            |
            [–—]
        )?
        [ \t'"]*
        $
        """,
        "",
        result,
        flags=re.I | re.X,
    )

    return result.rstrip()


def body_without_signature(
    raw: str,
    expected_user: str | None = None,
) -> str:
    timestamps = list(
        TIMESTAMP_RE.finditer(
            raw
        )
    )

    if not timestamps:
        return _strip_terminal_signature_residue_v3(
            raw,
            expected_user=expected_user,
        )

    stamp = timestamps[-1]

    signature = (
        _terminal_signature_span(
            raw,
            stamp,
            expected_user=expected_user,
        )
    )

    if signature is not None:
        body = (
            raw[
                :signature[0]
            ]
            + raw[
                signature[1]:
            ]
        )
    else:
        # If no reliable terminal signature can be identified,
        # preserve V2's conservative timestamp removal.
        body = (
            raw[
                :stamp.start()
            ]
            + raw[
                stamp.end():
            ]
        )

    return _strip_terminal_signature_residue_v3(
        body,
        expected_user=expected_user,
    )


def signature_residue_detected(
    body: str | None,
    expected_user: str | None = None,
) -> bool:
    """
    Conservative HC safety detector.

    A false positive here creates a review/false-negative case,
    which is preferable to promoting contaminated text.
    """
    if not body:
        return False

    tail = body[-600:]

    if re.search(
        r"""
        \{\{
        \s*
        (?:
            subst\s*:\s*
        )?
        (?:
            unsigned
            (?:\s*ip|\s*2)?
            |
            unsignedip
            |
            nosign
        )
        \b
        """,
        tail,
        flags=re.I | re.X,
    ):
        return True

    if re.search(
        r"""
        (?:
            preceding[ \t]+
        )?
        unsigned[ \t]+comment[ \t]+
        added[ \t]+by
        \b
        """,
        tail,
        flags=re.I | re.X,
    ):
        return True

    if re.search(
        r"""
        (?:
            (?<=\n)
            |
            (?<=[.!?])
            [ \t]+
            |
            ^
        )
        (?:
            best
            |
            regards
            |
            cheers
            |
            best[ \t]+wishes
        )
        [ \t]*
        [,:;.!'"]*
        [ \t]*
        (?:
            --+
            |
            [–—]
        )?
        [ \t'"]*
        $
        """,
        tail,
        flags=re.I | re.X,
    ):
        return True

    if re.search(
        r"""
        (?:
            --+
            |
            [–—]
        )
        [ \t]*
        (?:
            (?:\d{1,3}\.){3}\d{1,3}
            |
            [0-9A-Fa-f:]{4,}
        )
        [ \t'"]*
        $
        """,
        tail,
        flags=re.I | re.X,
    ):
        return True

    if _is_ip_user_v3(
        expected_user
    ):
        user = str(
            expected_user
        ).strip()

        if re.search(
            rf"""
            {re.escape(user)}
            [ \t'"]*
            $
            """,
            tail,
            flags=re.I | re.X,
        ):
            return True

    links = list(
        SIGNATURE_LINK_RE.finditer(
            tail
        )
    )

    if not links:
        return False

    last = links[-1]

    after = tail[
        last.end():
    ]

    if not _signature_gap_is_structural(
        after
    ):
        return False

    target = (
        _signature_link_target(
            last
        )
    )

    expected = _normalize_username(
        expected_user
    )

    if (
        expected is not None
        and target == expected
    ):
        return True

    if len(links) >= 2:
        previous_target = (
            _signature_link_target(
                links[-2]
            )
        )

        if (
            target is not None
            and previous_target == target
        ):
            return True

    before_last = tail[
        max(
            0,
            last.start() - 30,
        ):
        last.start()
    ]

    if re.search(
        r"(?:--+|[–—]|~~~~?)\s*['\"]*\s*$",
        before_last,
    ):
        return True

    return False



# BOUNDARY_V2_PERFORMANCE_OPTIMIZATION

def _candidate_normalized_body(
    candidate: dict,
) -> str:
    """
    Normalize each segmented boundary candidate at most once.

    segment_cache retains candidate dictionaries by revision,
    so this cache also survives multiple WikiDisputes actions
    referring to the same historical revision.

    This does not change normalization semantics.
    """

    cache_key = "_normalized_body_cache_v2"

    cached = candidate.get(
        cache_key
    )

    if cached is not None:
        return cached

    body = candidate.get(
        "body_without_signature"
    )

    if body is None:
        body = body_without_signature(
            candidate["raw"]
        )

        candidate[
            "body_without_signature"
        ] = body

    cleaned = normalize(
        body
    )

    candidate[
        cache_key
    ] = cleaned

    return cleaned


def _length_ratio_upper_bound(
    left: str,
    right: str,
) -> float:
    """
    Mathematical upper bound on SequenceMatcher.ratio().

    The number of matched characters cannot exceed the
    shorter string length.

        ratio <= 2 * min(len(a), len(b)) / (len(a) + len(b))

    Therefore this can safely reject a boundary variant only
    when it cannot possibly beat the current exact winner.
    """

    if not left or not right:
        return 0.0

    if left == right:
        return 1.0

    left_len = len(left)
    right_len = len(right)

    return (
        2.0
        * min(
            left_len,
            right_len,
        )
        / (
            left_len
            + right_len
        )
    )


def _quick_ratio_upper_bound(
    target: str,
    candidate: str,
) -> float:
    """
    Another documented SequenceMatcher upper bound.

    quick_ratio() is cheaper than ratio() and cannot be
    smaller than the eventual exact ratio.

    Combined with the length bound, it allows exact
    SequenceMatcher work to be skipped safely.
    """

    if not target or not candidate:
        return 0.0

    if target == candidate:
        return 1.0

    length_bound = (
        _length_ratio_upper_bound(
            target,
            candidate,
        )
    )

    matcher = difflib.SequenceMatcher(
        None,
        target,
        candidate,
        autojunk=False,
    )

    quick_bound = matcher.quick_ratio()

    return min(
        length_bound,
        quick_bound,
    )


def _exact_alignment_metrics(
    target: str,
    candidate: str,
) -> tuple[
    float,
    float,
    float,
    int,
]:
    """
    Exact metrics, identical in meaning to the previous
    _alignment_metrics() calculation.
    """

    if not target or not candidate:
        return (
            0.0,
            0.0,
            0.0,
            len(candidate)
            - len(target),
        )

    if target == candidate:
        return (
            1.0,
            1.0,
            1.0,
            0,
        )

    matcher = difflib.SequenceMatcher(
        None,
        target,
        candidate,
        autojunk=False,
    )

    ratio = matcher.ratio()

    matching = sum(
        block.size
        for block
        in matcher.get_matching_blocks()
    )

    target_coverage = (
        matching / len(target)
        if target
        else 0.0
    )

    candidate_purity = (
        matching / len(candidate)
        if candidate
        else 0.0
    )

    return (
        ratio,
        target_coverage,
        candidate_purity,
        len(candidate)
        - len(target),
    )


def rank_candidates(
    target_text: str,
    candidates: list[dict],
    action_offset: int,
) -> list[dict]:
    """
    Exact-semantics optimized V2 ranking.

    Two optimizations:

    1. normalized candidate bodies are cached on the segmented
       revision candidate itself;

    2. sibling boundary variants belonging to one timestamp
       are evaluated with mathematically safe upper bounds.
       An expensive exact SequenceMatcher call is skipped only
       when that variant provably cannot exceed the current
       exact similarity winner for that timestamp.

    The selected winner for every evaluated anchor therefore
    has the same ranking semantics as the unoptimized V2.
    """

    target = normalize(
        target_text
    )

    pool = candidate_pool(
        candidates,
        action_offset,
    )

    if not pool:
        return []

    # --------------------------------------------------------
    # Prepare candidates once.
    # --------------------------------------------------------

    prepared = []

    for candidate in pool:

        body = candidate.get(
            "body_without_signature"
        )

        if body is None:
            body = body_without_signature(
                candidate["raw"]
            )

            candidate[
                "body_without_signature"
            ] = body

        cleaned = (
            _candidate_normalized_body(
                candidate
            )
        )

        distance = offset_distance(
            action_offset,
            candidate["start"],
            candidate["end"],
        )

        proximity_bonus = max(
            0.0,
            0.025
            * (
                1
                - min(
                    distance,
                    15000,
                )
                / 15000
            ),
        )

        upper = (
            _quick_ratio_upper_bound(
                target,
                cleaned,
            )
        )

        prepared.append(
            {
                "candidate":
                    candidate,

                "body":
                    body,

                "cleaned":
                    cleaned,

                "distance":
                    distance,

                "proximity_bonus":
                    proximity_bonus,

                # similarity upper bound
                "upper":
                    upper,

                # Safe upper bound on eventual combined score.
                "combined_upper":
                    upper
                    + proximity_bonus,
            }
        )

    # --------------------------------------------------------
    # Group sibling boundary hypotheses by timestamp anchor.
    # --------------------------------------------------------

    by_anchor = {}

    for item in prepared:

        candidate = item[
            "candidate"
        ]

        anchor = candidate.get(
            "anchor_index",
            candidate.get(
                "candidate_index"
            ),
        )

        by_anchor.setdefault(
            anchor,
            []
        ).append(
            item
        )

    # --------------------------------------------------------
    # An anchor's winner can never have combined score above
    # the maximum bound of one of its variants.
    #
    # Process the most promising anchors first. Once two exact
    # anchor winners exist, anchors whose maximum possible
    # combined score is STRICTLY below the current second-best
    # exact combined score cannot affect best/second.
    # --------------------------------------------------------

    anchor_items = []

    for anchor, items in (
        by_anchor.items()
    ):
        anchor_upper = max(
            item[
                "combined_upper"
            ]
            for item
            in items
        )

        anchor_items.append(
            (
                anchor_upper,
                anchor,
                items,
            )
        )

    anchor_items.sort(
        key=lambda value: (
            value[0],
            -value[1]
            if isinstance(
                value[1],
                int,
            )
            else 0,
        ),
        reverse=True,
    )

    winners = []

    current_second_combined = None

    for (
        anchor_upper,
        anchor,
        items,
    ) in anchor_items:

        if (
            current_second_combined
            is not None
            and anchor_upper
            < current_second_combined
        ):
            # Strict inequality is required for tie safety.
            continue

        # The V2 within-anchor winner is chosen primarily by
        # exact similarity, so test variants in descending
        # similarity upper-bound order.
        items.sort(
            key=lambda item: (
                item["upper"],
                -item["distance"],
            ),
            reverse=True,
        )

        best = None
        best_key = None
        best_similarity = -1.0

        for item in items:

            # If even the mathematical upper bound is strictly
            # below the current exact best similarity, this
            # sibling cannot win the anchor.
            if (
                best is not None
                and item["upper"]
                < best_similarity
            ):
                continue

            (
                ratio,
                target_coverage,
                candidate_purity,
                normalized_length_delta,
            ) = _exact_alignment_metrics(
                target,
                item["cleaned"],
            )

            candidate = item[
                "candidate"
            ]

            result = {
                **candidate,

                "body_without_signature":
                    item["body"],

                "normalized_body":
                    item["cleaned"],

                "similarity":
                    ratio,

                "target_coverage":
                    target_coverage,

                "candidate_purity":
                    candidate_purity,

                "normalized_length_delta":
                    normalized_length_delta,

                "offset_distance":
                    item["distance"],

                "combined_score":
                    ratio
                    + item[
                        "proximity_bonus"
                    ],
            }

            result_key = (
                result[
                    "similarity"
                ],
                result[
                    "candidate_purity"
                ],
                result[
                    "target_coverage"
                ],
                -abs(
                    result[
                        "normalized_length_delta"
                    ]
                ),
                -result[
                    "offset_distance"
                ],
            )

            if (
                best is None
                or result_key
                > best_key
            ):
                best = result
                best_key = result_key
                best_similarity = ratio

        if best is None:
            continue

        winners.append(
            best
        )

        # We only need the exact top two comment anchors because
        # main() consumes ranked[0] and ranked[1].
        winners.sort(
            key=lambda x: (
                x[
                    "combined_score"
                ],
                x[
                    "similarity"
                ],
                x[
                    "candidate_purity"
                ],
                x[
                    "target_coverage"
                ],
                -abs(
                    x[
                        "normalized_length_delta"
                    ]
                ),
                -x[
                    "offset_distance"
                ],
            ),
            reverse=True,
        )

        if len(winners) >= 2:
            current_second_combined = (
                winners[1][
                    "combined_score"
                ]
            )

    winners.sort(
        key=lambda x: (
            x[
                "combined_score"
            ],
            x[
                "similarity"
            ],
            x[
                "candidate_purity"
            ],
            x[
                "target_coverage"
            ],
            -abs(
                x[
                    "normalized_length_delta"
                ]
            ),
            -x[
                "offset_distance"
            ],
        ),
        reverse=True,
    )

    # Only best and second are consumed downstream.
    return winners[:2]


def classify(
    best: dict | None,
    second: dict | None,
) -> tuple[str, float | None]:

    if best is None:
        return (
            "unresolved_no_candidate",
            None,
        )

    second_score = (
        second["similarity"]
        if second is not None
        else 0.0
    )

    margin = (
        best["similarity"]
        - second_score
    )

    # Same conservative thresholds validated on
    # Expert system.
    if (
        best["similarity"] >= 0.985
        and (
            margin >= 0.03
            or best["similarity"]
               >= 0.999
        )
    ):
        return (
            "high_confidence",
            margin,
        )

    if (
        best["similarity"] >= 0.94
        and margin >= 0.02
    ):
        return (
            "review",
            margin,
        )

    return (
        "unresolved",
        margin,
    )


# ============================================================
# V3 conservative high-confidence safety gate
# ============================================================

def _high_confidence_safety_v3(
    best: dict,
) -> tuple[bool, list[str]]:
    reasons = []

    target_coverage = best.get(
        "target_coverage"
    )

    if (
        target_coverage is None
        or target_coverage < 0.995
    ):
        reasons.append(
            "target_coverage_below_0.995"
        )

    residue = signature_residue_detected(
        best.get(
            "body_without_signature"
        ),
        expected_user=best.get(
            "expected_user"
        ),
    )

    best[
        "signature_residue_detected"
    ] = residue

    if residue:
        reasons.append(
            "signature_residue_detected"
        )

    best[
        "hc_safety_reason"
    ] = "|".join(
        reasons
    )

    return (
        not reasons,
        reasons,
    )


def classify(
    best: dict | None,
    second: dict | None,
) -> tuple[str, float | None]:
    if best is None:
        return (
            "unresolved_no_candidate",
            None,
        )

    second_score = (
        second["similarity"]
        if second is not None
        else 0.0
    )

    margin = (
        best["similarity"]
        - second_score
    )

    safety_ok, _ = (
        _high_confidence_safety_v3(
            best
        )
    )

    # Preserve the existing .985/.03 matching rule.
    # V3 adds only conservative boundary-safety gates.
    if (
        safety_ok
        and best["similarity"] >= 0.985
        and (
            margin >= 0.03
            or best["similarity"]
               >= 0.999
        )
    ):
        return (
            "high_confidence",
            margin,
        )

    if (
        best["similarity"] >= 0.94
        and margin >= 0.02
    ):
        return (
            "review",
            margin,
        )

    return (
        "unresolved",
        margin,
    )



# BOUNDARY_V31_SOURCE_SIGNATURE_MATCHING
# ============================================================
# V3.1 source-side signature normalization FOR MATCHING ONLY.
#
# ssot_source_text_exact / source_text is never modified.
#
# The matcher gets a conservative second representation from
# which positively-recognized terminal signature artifacts may
# be removed. The original-target alignment is also retained
# as an audit diagnostic.
# ============================================================


def _source_terminal_link_signature_v31(
    value: str,
) -> tuple[str, str | None]:
    """
    Remove a terminal linked signature only when there is
    structural evidence independent of mere terminal position:

      * an explicit signature separator; OR
      * a same-user User/User-talk cluster.

    A lone terminal link with neither signal survives.
    """
    if not value:
        return (
            value,
            None,
        )

    search_start = max(
        0,
        len(value) - 800,
    )

    tail = value[
        search_start:
    ]

    matches = list(
        SIGNATURE_LINK_RE.finditer(
            tail
        )
    )

    if not matches:
        return (
            value,
            None,
        )

    last = matches[-1]

    absolute_start = (
        search_start
        + last.start()
    )

    absolute_end = (
        search_start
        + last.end()
    )

    suffix = value[
        absolute_end:
    ]

    if not _signature_gap_is_structural(
        suffix
    ):
        return (
            value,
            None,
        )

    last_target = (
        _signature_link_target(
            last
        )
    )

    cluster_start = (
        absolute_start
    )

    same_target_neighbor = False

    prior_matches = [
        match
        for match in matches
        if match.end()
        <= last.start()
    ]

    for prior in reversed(
        prior_matches
    ):
        prior_target = (
            _signature_link_target(
                prior
            )
        )

        prior_end = (
            search_start
            + prior.end()
        )

        gap = value[
            prior_end:
            cluster_start
        ]

        if len(gap) > 140:
            break

        if not _signature_gap_is_structural(
            gap
        ):
            break

        if (
            last_target is None
            or prior_target is None
            or prior_target
            != last_target
        ):
            break

        same_target_neighbor = True

        cluster_start = (
            search_start
            + prior.start()
        )

    prefix_start = max(
        0,
        cluster_start - 60,
    )

    prefix = value[
        prefix_start:
        cluster_start
    ]

    separator = re.search(
        r"""
        (?:
            [ \t]*
            (?:
                --+
                |
                [–—]
                |
                ~~~~?
            )
            [ \t'"]*
        )
        $
        """,
        prefix,
        flags=re.X,
    )

    has_separator = (
        separator is not None
    )

    if not (
        same_target_neighbor
        or has_separator
    ):
        return (
            value,
            None,
        )

    if separator is not None:
        cluster_start = (
            prefix_start
            + separator.start()
        )

    # If a short conventional valediction is directly attached
    # to an already-confirmed linked signature, strip it too.
    # "thanks" is deliberately NOT included.
    val_start = max(
        0,
        cluster_start - 100,
    )

    val_text = value[
        val_start:
        cluster_start
    ]

    valediction = re.search(
        r"""
        (?:
            ^
            |
            [.!?][ \t]+
        )
        (?P<value>
            (?:
                best
                |
                regards
                |
                cheers
                |
                best[ \t]+wishes
            )
            [ \t]*
            [,:;.!'"]*
            [ \t]*
            (?:
                --+
                |
                [–—]
            )?
            [ \t'"]*
        )
        $
        """,
        val_text,
        flags=re.I | re.X,
    )

    if valediction:
        cluster_start = (
            val_start
            + valediction.start(
                "value"
            )
        )

    return (
        value[
            :cluster_start
        ].rstrip(),
        "terminal_link_signature",
    )


def _source_match_text_v31(
    value: str | None,
    expected_user: str | None = None,
) -> tuple[
    str,
    bool,
    str,
]:
    """
    Return a matching-only source representation.

    The original input string is never mutated elsewhere.

    Removal rules are intentionally narrow. Ambiguous material
    remains in the target rather than being silently discarded.
    """
    original = (
        value
        if value is not None
        else ""
    )

    result = original.rstrip()

    reasons = []

    # --------------------------------------------------------
    # 1. Explicit {{unsigned...}} terminal template.
    # --------------------------------------------------------

    updated = re.sub(
        r"""
        \s*
        \{\{
        \s*
        (?:
            subst\s*:\s*
        )?
        (?:
            unsigned
            (?:\s*ip|\s*2)?
            |
            unsignedip
            |
            nosign
        )
        \b
        .*?
        \}\}
        [ \t'"]*
        $
        """,
        "",
        result,
        flags=re.I | re.X | re.S,
    ).rstrip()

    if updated != result:
        result = updated

        reasons.append(
            "terminal_unsigned_template"
        )

    # --------------------------------------------------------
    # 2. SineBot / unsigned attribution phrase.
    # --------------------------------------------------------

    updated = re.sub(
        r"""
        \s*
        (?:
            <!--[^>]*-->\s*
        )?
        (?:
            --+
            |
            [–—]
        )?
        [ \t]*
        (?:
            preceding[ \t]+
        )?
        unsigned[ \t]+comment[ \t]+
        added[ \t]+by
        .*?
        $
        """,
        "",
        result,
        flags=re.I | re.X | re.S,
    ).rstrip()

    if updated != result:
        result = updated

        reasons.append(
            "terminal_unsigned_attribution"
        )

    # --------------------------------------------------------
    # 3. Terminal linked signature cluster.
    # --------------------------------------------------------

    updated, reason = (
        _source_terminal_link_signature_v31(
            result
        )
    )

    if reason is not None:
        result = updated

        reasons.append(
            reason
        )

    # --------------------------------------------------------
    # 4. Bare IP signature.
    #
    # Without a link/template, remove only:
    #   a) explicit --IP / —IP; or
    #   b) terminal IP exactly matching revision editor.
    # --------------------------------------------------------

    explicit_ip = re.sub(
        r"""
        \s*
        (?:
            --+
            |
            [–—]
        )
        [ \t]*
        (?:
            (?:\d{1,3}\.){3}\d{1,3}
            |
            [0-9A-Fa-f:]{4,}
        )
        [ \t'"]*
        $
        """,
        "",
        result,
        flags=re.I | re.X,
    ).rstrip()

    if explicit_ip != result:
        result = explicit_ip

        reasons.append(
            "terminal_explicit_ip_signature"
        )

    elif _is_ip_user_v3(
        expected_user
    ):
        user = str(
            expected_user
        ).strip()

        matching_ip = re.sub(
            rf"""
            \s*
            {re.escape(user)}
            [ \t'"]*
            $
            """,
            "",
            result,
            flags=re.I | re.X,
        ).rstrip()

        if matching_ip != result:
            result = matching_ip

            reasons.append(
                "terminal_revision_user_ip"
            )

    stripped = (
        result != original.rstrip()
    )

    return (
        result,
        stripped,
        "|".join(
            reasons
        ),
    )


# Preserve the exact V3 ranking implementation as an oracle.
_rank_candidates_v3_exact = rank_candidates


def rank_candidates(
    target_text: str,
    candidates: list[dict],
    action_offset: int,
) -> list[dict]:
    """
    V3.1 ranking wrapper.

    Matching is performed against a conservatively
    signature-cleaned source target only when a recognizable
    terminal source-side signature artifact is found.

    The V3 optimized candidate ranking itself is unchanged.
    """

    expected_user = None

    if candidates:
        expected_user = candidates[
            0
        ].get(
            "expected_user"
        )

    (
        source_match_text,
        artifact_stripped,
        artifact_reason,
    ) = _source_match_text_v31(
        target_text,
        expected_user=expected_user,
    )

    ranked = _rank_candidates_v3_exact(
        source_match_text,
        candidates,
        action_offset,
    )

    if not ranked:
        return ranked

    original_target = normalize(
        target_text
    )

    # Only best/second survive the V3 optimized ranker,
    # so retaining original-target audit scores adds at most
    # two exact alignments per utterance.
    for result in ranked:
        candidate = result.get(
            "normalized_body",
            "",
        )

        (
            original_ratio,
            original_coverage,
            original_purity,
            original_length_delta,
        ) = _exact_alignment_metrics(
            original_target,
            candidate,
        )

        result[
            "source_signature_artifact_stripped"
        ] = artifact_stripped

        result[
            "source_signature_artifact_reason"
        ] = artifact_reason

        result[
            "source_original_similarity"
        ] = original_ratio

        result[
            "source_original_target_coverage"
        ] = original_coverage

        result[
            "source_original_candidate_purity"
        ] = original_purity

        result[
            "source_original_length_delta"
        ] = original_length_delta

    return ranked



# BOUNDARY_V32_TERMINAL_SIGNATURE_CLEANUP
# ============================================================
# V3.2
#
# Fix demonstrated remaining signature problem:
# a recovered WikiDisputes logical utterance may contain a
# terminal conventional signature WITHOUT a timestamp because
# the source action spans multiple same-user comment fragments.
#
# V3 removed signature+timestamp clusters. V3.2 additionally
# removes positively-identified terminal signature-only
# clusters from the candidate body.
#
# Original source text remains immutable.
# ============================================================


_strip_terminal_signature_residue_v31 = (
    _strip_terminal_signature_residue_v3
)

_source_match_text_v31_previous = (
    _source_match_text_v31
)


def _terminal_link_signature_span_v32(
    value: str,
) -> tuple[int, int] | None:
    """
    Identify a signature-only cluster at the END of a body.

    Require structural evidence:
      - explicit -- / — / ~~~~ separator; OR
      - same-user User + User-talk link cluster.

    A lone terminal user link without either signal is
    preserved as potentially substantive.
    """

    if not value:
        return None

    search_start = max(
        0,
        len(value) - 1000,
    )

    tail = value[
        search_start:
    ]

    matches = list(
        SIGNATURE_LINK_RE.finditer(
            tail
        )
    )

    if not matches:
        return None

    last = matches[-1]

    absolute_start = (
        search_start
        + last.start()
    )

    absolute_end = (
        search_start
        + last.end()
    )

    suffix = value[
        absolute_end:
    ]

    if not _signature_gap_is_structural(
        suffix
    ):
        return None

    target = (
        _signature_link_target(
            last
        )
    )

    cluster_start = absolute_start

    same_target_neighbor = False

    prior_matches = [
        match
        for match in matches
        if match.end()
        <= last.start()
    ]

    for prior in reversed(
        prior_matches
    ):
        prior_target = (
            _signature_link_target(
                prior
            )
        )

        prior_absolute_end = (
            search_start
            + prior.end()
        )

        gap = value[
            prior_absolute_end:
            cluster_start
        ]

        if len(gap) > 160:
            break

        if not _signature_gap_is_structural(
            gap
        ):
            break

        if (
            target is None
            or prior_target is None
            or prior_target != target
        ):
            break

        same_target_neighbor = True

        cluster_start = (
            search_start
            + prior.start()
        )

    prefix_start = max(
        0,
        cluster_start - 60,
    )

    prefix = value[
        prefix_start:
        cluster_start
    ]

    separator = re.search(
        r"""
        (?:
            [ \t]*
            (?:
                --+
                |
                [–—]
                |
                ~~~~?
            )
            [ \t'"]*
        )
        $
        """,
        prefix,
        flags=re.X,
    )

    has_separator = (
        separator is not None
    )

    if not (
        same_target_neighbor
        or has_separator
    ):
        return None

    if separator is not None:
        cluster_start = (
            prefix_start
            + separator.start()
        )

    return (
        cluster_start,
        len(value),
    )


def _strip_terminal_signature_residue_v3(
    value: str,
    expected_user: str | None = None,
) -> str:
    """
    V3.2 candidate-body cleanup.

    Start with V3/V3.1 behavior, then remove additional
    terminal signature-only structures demonstrated in the
    real-data audit.
    """

    result = (
        _strip_terminal_signature_residue_v31(
            value,
            expected_user=expected_user,
        )
    )

    if not result:
        return result

    result = result.rstrip()

    # --------------------------------------------------------
    # 1. Markup-bearing unsigned/SineBot attribution.
    #
    # Handles:
    # [[Wikipedia:Signatures|unsigned]] comment added by
    # --------------------------------------------------------

    result = re.sub(
        r"""
        \s*
        (?:
            <[^>]+>\s*
        )*
        (?:
            --+
            |
            [–—]
        )?
        (?:&nbsp;|\s)*
        (?:
            preceding
            (?:&nbsp;|\s)+
        )?
        (?:
            \[\[
            \s*
            Wikipedia\s*:\s*Signatures
            \s*
            \|
            \s*
        )?
        unsigned
        (?:
            \s*
            \]\]
        )?
        (?:&nbsp;|\s)+
        comment
        (?:&nbsp;|\s)+
        added
        (?:&nbsp;|\s)+
        by
        .*?
        $
        """,
        "",
        result,
        flags=re.I | re.X | re.S,
    ).rstrip()

    # --------------------------------------------------------
    # 2. Terminal User/User-talk signature with no timestamp.
    # --------------------------------------------------------

    span = (
        _terminal_link_signature_span_v32(
            result
        )
    )

    if span is not None:
        result = (
            result[
                :span[0]
            ]
            .rstrip()
        )

    # --------------------------------------------------------
    # 3. Explicit terminal IP signature.
    # --------------------------------------------------------

    result = re.sub(
        r"""
        \s*
        (?:
            --+
            |
            [–—]
        )
        [ \t]*
        (?:
            (?:\d{1,3}\.){3}\d{1,3}
            |
            [0-9A-Fa-f:]{4,}
        )
        [ \t'"]*
        $
        """,
        "",
        result,
        flags=re.I | re.X,
    ).rstrip()

    # Revision-user IP may appear without --.
    if _is_ip_user_v3(
        expected_user
    ):
        user = str(
            expected_user
        ).strip()

        result = re.sub(
            rf"""
            \s+
            {re.escape(user)}
            [ \t'"]*
            $
            """,
            "",
            result,
            flags=re.I | re.X,
        ).rstrip()

    # --------------------------------------------------------
    # 4. Conventional terminal valediction residue.
    #
    # Require preceding sentence punctuation. "Thanks" is
    # deliberately excluded.
    # --------------------------------------------------------

    result = re.sub(
        r"""
        (?<=[.!?])
        [ \t]+
        (?:
            best
            |
            regards
            |
            cheers
            |
            best[ \t]+wishes
        )
        [ \t]*
        [,:;.!]*
        [ \t]*
        (?:
            --+
            |
            [–—]
        )?
        [ \t']*
        $
        """,
        "",
        result,
        flags=re.I | re.X,
    ).rstrip()

    return result


def _source_match_text_v31(
    value: str | None,
    expected_user: str | None = None,
) -> tuple[
    str,
    bool,
    str,
]:
    """
    V3.2 extension of V3.1 matching-only target cleanup.

    Adds only two demonstrated source artifacts:
      * terminal bare IPv4 after a completed sentence;
      * terminal conventional "Best — ..." valediction.

    This does not alter source_text itself.
    """

    (
        result,
        stripped,
        reason,
    ) = _source_match_text_v31_previous(
        value,
        expected_user=expected_user,
    )

    reasons = [
        item
        for item
        in str(
            reason or ""
        ).split("|")
        if item
    ]

    before = result

    # --------------------------------------------------------
    # Terminal bare IPv4.
    #
    # Require a completed sentence before the standalone IP.
    # Thus:
    #
    #   "... credibility. 66.31.39.76" -> signature artifact
    #
    # but:
    #
    #   "server address is 66.31.39.76" -> preserved
    # --------------------------------------------------------

    ip_match = re.search(
        r"""
        (?P<body>
            .*?
            [.!?]
        )
        [ \t]+
        (?P<ip>
            (?:\d{1,3}\.){3}\d{1,3}
        )
        [ \t]*
        $
        """,
        result,
        flags=re.X | re.S,
    )

    if (
        ip_match is not None
        and _is_ip_user_v3(
            ip_match.group(
                "ip"
            )
        )
    ):
        result = (
            ip_match.group(
                "body"
            )
            .rstrip()
        )

        reasons.append(
            "terminal_bare_ipv4_after_sentence"
        )

    # --------------------------------------------------------
    # Conventional source valediction.
    # Example from audit:
    #
    # Example: terminal conventional Best valediction artifact.
    # --------------------------------------------------------

    updated = re.sub(
        r"""
        (?<=[.!?])
        [ \t]+
        (?:
            best
            |
            regards
            |
            cheers
            |
            best[ \t]+wishes
        )
        [ \t]*
        [,:;.!]*
        [ \t]*
        (?:
            --+
            |
            [–—]
        )?
        [ \t']*
        $
        """,
        "",
        result,
        flags=re.I | re.X,
    ).rstrip()

    if updated != result:
        result = updated

        reasons.append(
            "terminal_valediction_artifact"
        )

    # --------------------------------------------------------
    # Markup/plain unsigned attribution suffix.
    # --------------------------------------------------------

    updated = re.sub(
        r"""
        \s*
        (?:
            --+
            |
            [–—]
        )?
        [ \t]*
        (?:
            preceding[ \t]+
        )?
        unsigned[ \t]+comment[ \t]+
        added[ \t]+by
        .*?
        $
        """,
        "",
        result,
        flags=re.I | re.X | re.S,
    ).rstrip()

    if updated != result:
        result = updated

        reasons.append(
            "terminal_unsigned_attribution_v32"
        )

    stripped_now = (
        result
        != (
            value
            if value is not None
            else ""
        ).rstrip()
    )

    return (
        result,
        stripped_now,
        "|".join(
            dict.fromkeys(
                reasons
            )
        ),
    )



# BOUNDARY_V33_SIGNATURE_GLYPH_AND_PERF
# ============================================================
# V3.3
#
# 1. Recognize one repeated WikiDisputes source-side signature
#    remnant observed in the 500-row audit:
#
#           –  ·
#
#    This affects matching only. source_text remains immutable.
#
# 2. Avoid redundant exact SequenceMatcher calls used only
#    for source-original diagnostics when the source matching
#    target was not modified.
# ============================================================


_source_match_text_v32_previous = (
    _source_match_text_v31
)


def _source_match_text_v31(
    value: str | None,
    expected_user: str | None = None,
) -> tuple[
    str,
    bool,
    str,
]:
    (
        result,
        stripped,
        reason,
    ) = _source_match_text_v32_previous(
        value,
        expected_user=expected_user,
    )

    reasons = [
        item
        for item
        in str(
            reason or ""
        ).split("|")
        if item
    ]

    # Exact artifact repeatedly observed after WikiDisputes
    # source processing removed the username/signature links.
    #
    # Require BOTH:
    #   - a terminal en/em dash; and
    #   - a decorative middle-dot/bullet.
    #
    # A normal substantive sentence ending in a dash is not
    # touched.
    updated = re.sub(
        r"""
        \s+
        [–—]
        [ \t]*
        [·•]
        [ \t]*
        $
        """,
        "",
        result,
        flags=re.X,
    ).rstrip()

    if updated != result:
        result = updated

        reasons.append(
            "terminal_wikidisputes_signature_glyphs"
        )

    original = (
        value
        if value is not None
        else ""
    )

    stripped_now = (
        result
        != original.rstrip()
    )

    return (
        result,
        stripped_now,
        "|".join(
            dict.fromkeys(
                reasons
            )
        ),
    )


def rank_candidates(
    target_text: str,
    candidates: list[dict],
    action_offset: int,
) -> list[dict]:
    """
    V3.3 version of the V3.1 matching wrapper.

    Ranking semantics are unchanged.

    Performance fix:
    when source_match_text is identical to target_text, the
    already-computed candidate alignment metrics ARE the
    original-target metrics. Do not run another expensive
    SequenceMatcher merely to duplicate them.
    """

    expected_user = None

    if candidates:
        expected_user = candidates[
            0
        ].get(
            "expected_user"
        )

    (
        source_match_text,
        artifact_stripped,
        artifact_reason,
    ) = _source_match_text_v31(
        target_text,
        expected_user=expected_user,
    )

    ranked = _rank_candidates_v3_exact(
        source_match_text,
        candidates,
        action_offset,
    )

    if not ranked:
        return ranked

    if artifact_stripped:
        original_target = normalize(
            target_text
        )

    for result in ranked:

        result[
            "source_signature_artifact_stripped"
        ] = artifact_stripped

        result[
            "source_signature_artifact_reason"
        ] = artifact_reason

        if artifact_stripped:

            (
                original_ratio,
                original_coverage,
                original_purity,
                original_length_delta,
            ) = _exact_alignment_metrics(
                original_target,
                result.get(
                    "normalized_body",
                    "",
                ),
            )

            result[
                "source_original_similarity"
            ] = original_ratio

            result[
                "source_original_target_coverage"
            ] = original_coverage

            result[
                "source_original_candidate_purity"
            ] = original_purity

            result[
                "source_original_length_delta"
            ] = original_length_delta

        else:
            # Same target => these values are mathematically
            # identical to a second exact alignment.
            result[
                "source_original_similarity"
            ] = result.get(
                "similarity"
            )

            result[
                "source_original_target_coverage"
            ] = result.get(
                "target_coverage"
            )

            result[
                "source_original_candidate_purity"
            ] = result.get(
                "candidate_purity"
            )

            result[
                "source_original_length_delta"
            ] = result.get(
                "normalized_length_delta"
            )

    return ranked



# ============================================================
# Main
# ============================================================

def main():
    args = parse_args()

    if not ANNOTATION.exists():
        raise SystemExit(
            f"Missing {ANNOTATION}"
        )

    con = duckdb.connect()

    limit_sql = (
        f"LIMIT {args.limit}"
        if args.limit is not None
        else ""
    )

    rows = con.execute(
        f"""
        SELECT
            ssot_source_row_uid,
            ssot_episode_uid,
            ssot_conversation_uid,
            ssot_logical_utterance_uid,
            utterance_id,
            speaker_id,
            utterance_type,
            timestamp,
            source_page_title,
            ssot_source_text_exact,
            utterance_text,
            ssot_annotation_text_source

        FROM read_csv_auto(
            '{str(ANNOTATION).replace("'", "''")}',
            HEADER=TRUE,
            ALL_VARCHAR=TRUE,
            SAMPLE_SIZE=-1
        )

        WHERE utterance_role = 'utterance'
          AND utterance_id IS NOT NULL

        ORDER BY
            ssot_episode_uid,
            TRY_CAST(
                utterance_order
                AS BIGINT
            ),
            ssot_source_row_uid

        {limit_sql}
        """
    ).fetchall()

    columns = [
        "source_row_uid",
        "episode_uid",
        "conversation_uid",
        "logical_utterance_uid",
        "utterance_id",
        "speaker_id",
        "utterance_type",
        "timestamp",
        "page_title",
        "source_text",
        "current_annotation_text",
        "current_annotation_text_source",
    ]

    entities = [
        dict(zip(columns, row))
        for row in rows
    ]

    print(
        f"utterance occurrences: "
        f"{len(entities):,}"
    )

    valid_entities = []

    for row in entities:
        parts = str(
            row["utterance_id"]
        ).split(".")

        try:
            revision_id = int(
                parts[0]
            )
            action_offset = int(
                parts[1]
            )

        except (
            IndexError,
            TypeError,
            ValueError,
        ):
            row["id_parse_status"] = (
                "invalid"
            )
            continue

        row["revision_id"] = (
            revision_id
        )
        row["action_offset"] = (
            action_offset
        )
        row["id_parse_status"] = (
            "valid"
        )

        valid_entities.append(
            row
        )

    revision_ids = sorted(
        {
            row["revision_id"]
            for row in valid_entities
        }
    )

    db = open_cache()

    if args.cache_only:
        existing = cached_revision_ids(
            db
        )

        missing = [
            revision_id
            for revision_id
            in revision_ids
            if revision_id
            not in existing
        ]

        print()
        print("CACHE-ONLY MODE")
        print(
            f"required revisions: "
            f"{len(revision_ids):,}"
        )
        print(
            f"cached revisions:   "
            f"{len(revision_ids) - len(missing):,}"
        )
        print(
            f"missing revisions:  "
            f"{len(missing):,}"
        )

        if missing:
            preview = ", ".join(
                str(value)
                for value
                in missing[:20]
            )

            raise SystemExit(
                "CACHE-ONLY ABORT: "
                f"{len(missing):,} required revisions "
                "are absent from SQLite. "
                "No network request was made. "
                f"First missing IDs: {preview}"
            )

    else:
        fetch_missing_revisions(
            db,
            revision_ids,
            args.batch_size,
        )

    # Segment each revision only once.
    segment_cache: dict[
        int,
        list[dict]
    ] = {}

    results = []

    status_counts = Counter()
    markup_gain_counts = Counter()

    similarities = []
    margins = []

    for index, row in enumerate(
        valid_entities,
        start=1,
    ):
        rid = row["revision_id"]

        revision = get_revision(
            db,
            rid,
        )

        base = {
            **row,
            "revision_status":
                None,
            "revision_title":
                None,
            "revision_timestamp":
                None,
            "revision_sha1":
                None,
            "recovery_status":
                None,
            "best_similarity":
                None,
            "second_similarity":
                None,
            "match_margin":
                None,
            "offset_distance":
                None,
            "raw_start":
                None,
            "raw_end":
                None,
            "boundary_method":
                None,
            "target_coverage":
                None,
            "candidate_purity":
                None,
            "normalized_length_delta":
                None,
            "signature_residue_detected":
                None,
            "hc_safety_reason":
                "",
            "source_signature_artifact_stripped":
                None,
            "source_signature_artifact_reason":
                "",
            "source_original_similarity":
                None,
            "source_original_target_coverage":
                None,
            "source_original_candidate_purity":
                None,
            "source_original_length_delta":
                None,
            "recovered_body_wikitext":
                None,
            "recovered_raw_wikitext":
                None,
            "markup_gained":
                False,
            "markup_gain_types":
                "",
        }

        if (
            revision is None
            or revision["status"]
               != "found"
            or not revision["content"]
        ):
            base[
                "revision_status"
            ] = (
                revision["status"]
                if revision
                else "not_cached"
            )

            base[
                "recovery_status"
            ] = (
                "revision_unavailable"
            )

            results.append(base)

            status_counts[
                "revision_unavailable"
            ] += 1

            continue

        base["revision_status"] = (
            revision["status"]
        )
        base["revision_title"] = (
            revision["title"]
        )
        base[
            "revision_timestamp"
        ] = (
            revision[
                "revision_timestamp"
            ]
        )
        base["revision_sha1"] = (
            revision["sha1"]
        )

        if rid not in segment_cache:
            segment_cache[rid] = (
                candidate_comments(
                    revision["content"],
                    expected_user=revision.get(
                        "revision_user"
                    ),
                )
            )

        ranked = rank_candidates(
            row["source_text"] or "",
            segment_cache[rid],
            row["action_offset"],
        )

        best = (
            ranked[0]
            if ranked
            else None
        )

        second = (
            ranked[1]
            if len(ranked) > 1
            else None
        )

        status, margin = classify(
            best,
            second,
        )

        base[
            "recovery_status"
        ] = status

        status_counts[status] += 1

        if best is not None:
            best_similarity = (
                best["similarity"]
            )

            second_similarity = (
                second["similarity"]
                if second
                else None
            )

            base[
                "best_similarity"
            ] = best_similarity
            base[
                "second_similarity"
            ] = second_similarity
            base[
                "match_margin"
            ] = margin
            base[
                "offset_distance"
            ] = best[
                "offset_distance"
            ]
            base[
                "raw_start"
            ] = best["start"]
            base[
                "raw_end"
            ] = best["end"]

            base[
                "boundary_method"
            ] = best.get(
                "boundary_method"
            )

            base[
                "target_coverage"
            ] = best.get(
                "target_coverage"
            )

            base[
                "candidate_purity"
            ] = best.get(
                "candidate_purity"
            )

            base[
                "normalized_length_delta"
            ] = best.get(
                "normalized_length_delta"
            )

            base[
                "signature_residue_detected"
            ] = best.get(
                "signature_residue_detected"
            )

            base[
                "hc_safety_reason"
            ] = best.get(
                "hc_safety_reason",
                "",
            )

            base[
                "source_signature_artifact_stripped"
            ] = best.get(
                "source_signature_artifact_stripped"
            )

            base[
                "source_signature_artifact_reason"
            ] = best.get(
                "source_signature_artifact_reason",
                "",
            )

            base[
                "source_original_similarity"
            ] = best.get(
                "source_original_similarity"
            )

            base[
                "source_original_target_coverage"
            ] = best.get(
                "source_original_target_coverage"
            )

            base[
                "source_original_candidate_purity"
            ] = best.get(
                "source_original_candidate_purity"
            )

            base[
                "source_original_length_delta"
            ] = best.get(
                "source_original_length_delta"
            )

            if (
                status
                == "high_confidence"
            ):
                raw = best["raw"]
                body = best[
                    "body_without_signature"
                ]

                base[
                    "recovered_raw_wikitext"
                ] = raw
                base[
                    "recovered_body_wikitext"
                ] = body

                source_counts = (
                    markup_counts(
                        row["source_text"]
                    )
                )

                raw_counts = (
                    markup_counts(raw)
                )

                gained = [
                    kind
                    for kind
                    in MARKUP_PATTERNS
                    if (
                        raw_counts[kind]
                        > source_counts[kind]
                    )
                ]

                base[
                    "markup_gained"
                ] = bool(gained)

                base[
                    "markup_gain_types"
                ] = "|".join(gained)

                if gained:
                    markup_gain_counts[
                        "rows"
                    ] += 1

                    for kind in gained:
                        markup_gain_counts[
                            kind
                        ] += 1

            similarities.append(
                best_similarity
            )

            if margin is not None:
                margins.append(
                    margin
                )

        results.append(base)

        if (
            index % 1000 == 0
            or index
               == len(valid_entities)
        ):
            print(
                f"matched: "
                f"{index:,}/"
                f"{len(valid_entities):,}"
            )

    db.close()

    # --------------------------------------------------------
    # CSV
    # --------------------------------------------------------

    OUTPUT_CSV.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fieldnames = [
        "source_row_uid",
        "episode_uid",
        "conversation_uid",
        "logical_utterance_uid",
        "utterance_id",
        "speaker_id",
        "utterance_type",
        "timestamp",
        "page_title",
        "revision_id",
        "action_offset",
        "revision_status",
        "revision_title",
        "revision_timestamp",
        "revision_sha1",
        "recovery_status",
        "best_similarity",
        "second_similarity",
        "match_margin",
        "offset_distance",
        "raw_start",
        "raw_end",
        "boundary_method",
        "target_coverage",
        "candidate_purity",
        "normalized_length_delta",
        "signature_residue_detected",
        "hc_safety_reason",
        "source_signature_artifact_stripped",
        "source_signature_artifact_reason",
        "source_original_similarity",
        "source_original_target_coverage",
        "source_original_candidate_purity",
        "source_original_length_delta",
        "markup_gained",
        "markup_gain_types",
        "source_text",
        "current_annotation_text",
        "current_annotation_text_source",
        "recovered_body_wikitext",
        "recovered_raw_wikitext",
    ]

    with OUTPUT_CSV.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )

        writer.writeheader()
        writer.writerows(results)

    # --------------------------------------------------------
    # Parquet
    # --------------------------------------------------------

    out_con = duckdb.connect()

    out_con.execute(
        f"""
        COPY (
            SELECT *
            FROM read_csv_auto(
                '{str(OUTPUT_CSV).replace("'", "''")}',
                HEADER=TRUE,
                ALL_VARCHAR=TRUE,
                SAMPLE_SIZE=-1
            )
        )
        TO '{str(OUTPUT_PARQUET).replace("'", "''")}'
        (
            FORMAT PARQUET,
            COMPRESSION ZSTD
        )
        """
    )

    # --------------------------------------------------------
    # Review CSV
    # --------------------------------------------------------

    with REVIEW_CSV.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )

        writer.writeheader()

        for row in results:
            if row[
                "recovery_status"
            ] != "high_confidence":
                writer.writerow(row)

    # --------------------------------------------------------
    # Expert system regression
    # --------------------------------------------------------

    expert = [
        row
        for row in results
        if (
            str(
                row["page_title"]
            )
            .strip()
            .casefold()
            == "expert system"
        )
    ]

    expert_high = sum(
        row["recovery_status"]
        == "high_confidence"
        for row in expert
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    summary = {
        "status": "complete",
        "utterance_occurrences":
            len(entities),
        "valid_action_ids":
            len(valid_entities),
        "unique_revision_ids":
            len(revision_ids),
        "recovery_status_counts":
            dict(status_counts),
        "high_confidence_rate":
            (
                status_counts[
                    "high_confidence"
                ]
                / len(valid_entities)
                if valid_entities
                else None
            ),
        "rows_gaining_markup":
            markup_gain_counts[
                "rows"
            ],
        "markup_gain_counts":
            {
                key: value
                for key, value
                in markup_gain_counts.items()
                if key != "rows"
            },
        "best_similarity_median":
            (
                statistics.median(
                    similarities
                )
                if similarities
                else None
            ),
        "best_similarity_min":
            (
                min(similarities)
                if similarities
                else None
            ),
        "match_margin_median":
            (
                statistics.median(
                    margins
                )
                if margins
                else None
            ),
        "expert_system_rows":
            len(expert),
        "expert_system_high_confidence":
            expert_high,
        "output_parquet":
            str(OUTPUT_PARQUET),
        "review_csv":
            str(REVIEW_CSV),
        "cache_db":
            str(CACHE_DB),
    }

    SUMMARY_JSON.write_text(
        json.dumps(
            summary,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    print()
    print(
        "=" * 72
    )
    print(
        "FULL RAW-WIKITEXT RECOVERY SUMMARY"
    )
    print(
        "=" * 72
    )

    for key, value in (
        summary[
            "recovery_status_counts"
        ].items()
    ):
        print(
            f"{key:32s} "
            f"{value:,}"
        )

    print()
    print(
        "high-confidence rate: "
        f"{summary['high_confidence_rate']:.3%}"
    )

    print(
        "rows gaining markup:  "
        f"{summary['rows_gaining_markup']:,}"
    )

    print()
    print("Markup gains:")

    for key, value in sorted(
        summary[
            "markup_gain_counts"
        ].items()
    ):
        print(
            f"  {key:24s} "
            f"{value:,}"
        )

    print()
    print(
        "median best similarity: "
        f"{summary['best_similarity_median']:.6f}"
    )

    print(
        "median match margin:    "
        f"{summary['match_margin_median']:.6f}"
    )

    print()
    print(
        "Expert system: "
        f"{expert_high}/{len(expert)} "
        "high confidence"
    )

    print()
    print("OUTPUT:")
    print(OUTPUT_PARQUET)
    print(SUMMARY_JSON)
    print(REVIEW_CSV)
    print(CACHE_DB)


if __name__ == "__main__":
    main()
