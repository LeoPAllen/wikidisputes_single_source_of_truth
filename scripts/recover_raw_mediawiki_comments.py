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

def candidate_comments(
    raw: str,
) -> list[dict]:

    timestamps = list(
        TIMESTAMP_RE.finditer(raw)
    )

    candidates = []
    previous_end = 0

    for i, stamp in enumerate(
        timestamps
    ):
        start = previous_end
        end = stamp.end()

        fragment = raw[start:end]

        # Remove blank lines and section headings from
        # candidate prefix only.
        consumed = 0

        for line in fragment.splitlines(
            keepends=True
        ):
            stripped = line.strip()

            if not stripped:
                consumed += len(line)
                continue

            if HEADING_RE.match(line):
                consumed += len(line)
                continue

            break

        fragment = fragment[consumed:]
        absolute_start = (
            start + consumed
        )

        if fragment.strip():
            candidates.append(
                {
                    "candidate_index": i,
                    "start": absolute_start,
                    "end": end,
                    "raw": fragment,
                }
            )

        previous_end = end

    return candidates


def body_without_signature(
    raw: str,
) -> str:

    timestamps = list(
        TIMESTAMP_RE.finditer(raw)
    )

    if not timestamps:
        return raw

    stamp = timestamps[-1]

    before = raw[
        :stamp.start()
    ]

    # Signatures normally occur very near the timestamp.
    search_start = max(
        0,
        len(before) - 600,
    )

    tail = before[
        search_start:
    ]

    matches = list(
        SIGNATURE_LINK_RE.finditer(
            tail
        )
    )

    if matches:
        signature_start = (
            search_start
            + matches[0].start()
        )

        return raw[
            :signature_start
        ]

    return before


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

    if target in candidate:
        return 0.999

    if candidate in target:
        return 0.995

    return difflib.SequenceMatcher(
        None,
        target,
        candidate,
        autojunk=False,
    ).ratio()


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


def rank_candidates(
    target_text: str,
    candidates: list[dict],
    action_offset: int,
) -> list[dict]:

    target = normalize(
        target_text
    )

    ranked = []

    for candidate in candidate_pool(
        candidates,
        action_offset,
    ):
        body = body_without_signature(
            candidate["raw"]
        )

        cleaned = normalize(
            body
        )

        ratio = similarity(
            target,
            cleaned,
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

        ranked.append(
            {
                **candidate,
                "body_without_signature":
                    body,
                "normalized_body":
                    cleaned,
                "similarity":
                    ratio,
                "offset_distance":
                    distance,
                "combined_score":
                    ratio
                    + proximity_bonus,
            }
        )

    ranked.sort(
        key=lambda x: (
            x["combined_score"],
            x["similarity"],
            -x["offset_distance"],
        ),
        reverse=True,
    )

    return ranked


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
                    revision["content"]
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
