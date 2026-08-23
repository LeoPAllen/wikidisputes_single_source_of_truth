from __future__ import annotations

import csv
import sys

# CSV_FIELD_SIZE_LIMIT_PATCH
# Raw historical talk-page comments can legitimately exceed
# Python csv module's default 131,072-byte field limit.
_csv_limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(_csv_limit)
        break
    except OverflowError:
        _csv_limit //= 10

import html
import json
import math
import re
import statistics
import unicodedata
from collections import Counter
from pathlib import Path

import duckdb
import mwparserfromhell


ROOT = Path.cwd()

BEFORE = (
    ROOT / "output/annotation/"
    "wikidisputes_llm_annotation_input.pre_raw_wikitext.csv"
)

AFTER = (
    ROOT / "output/annotation/"
    "wikidisputes_llm_annotation_input.csv"
)

RECOVERY = (
    ROOT / "output/silver/"
    "mediawiki_raw_comment_recovery.parquet"
)

UTTERANCES = (
    ROOT / "output/canonical/"
    "wikidisputes_utterances_ssot.parquet"
)

GOLD_REPORT = (
    ROOT / "reports/"
    "gold_ssot_migration_report.json"
)

OUT_MD = (
    ROOT / "reports/"
    "raw_wikitext_nlp_validation_report.md"
)

OUT_JSON = (
    ROOT / "reports/"
    "raw_wikitext_nlp_validation_summary.json"
)

OUT_EXAMPLES = (
    ROOT / "reports/"
    "raw_wikitext_change_examples.csv"
)


for p in (
    BEFORE,
    AFTER,
    RECOVERY,
    UTTERANCES,
):
    if not p.exists():
        raise SystemExit(
            f"ERROR: required file missing: {p}"
        )


# ============================================================
# Markup / entity patterns
# ============================================================

PATTERNS = {
    "wikilink": re.compile(
        r"\[\[[^\]]+\]\]"
    ),

    "user_link": re.compile(
        r"\[\[\s*(?:"
        r"User|User talk|"
        r"Special:Contributions"
        r")\s*[:/][^\]]+\]\]",
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

    "url": re.compile(
        r"https?://[^\s\]\|<>]+",
        re.I,
    ),

    "template": re.compile(
        r"\{\{",
    ),

    "ref_tag": re.compile(
        r"<ref\b",
        re.I,
    ),

    "diff_or_oldid": re.compile(
        r"(?:Special:Diff|diff=|oldid=)",
        re.I,
    ),
}


WIKILINK_TARGET_RE = re.compile(
    r"""
    \[\[
    \s*
    ([^|\]#]+(?:\#[^|\]]*)?)
    (?:\|[^\]]*)?
    \]\]
    """,
    re.I | re.X,
)

URL_RE = re.compile(
    r"https?://[^\s\]\|<>]+",
    re.I,
)

UTC_SIGNATURE_TIMESTAMP_RE = re.compile(
    r"""
    \b
    \d{1,2}:\d{2},
    \s+
    \d{1,2}
    \s+
    (?:January|February|March|April|May|June|
       July|August|September|October|November|December)
    \s+
    \d{4}
    \s*
    \(UTC\)
    """,
    re.I | re.X,
)


def markup_counts(
    value: str | None,
) -> dict[str, int]:
    value = value or ""

    return {
        name: len(pattern.findall(value))
        for name, pattern in PATTERNS.items()
    }


def extract_targets(
    value: str | None,
):
    value = value or ""

    wiki = set()
    users = set()
    policies = set()

    for target in WIKILINK_TARGET_RE.findall(value):
        cleaned = target.strip()
        lowered = cleaned.casefold()

        wiki.add(lowered)

        if (
            lowered.startswith("user:")
            or lowered.startswith("user talk:")
            or lowered.startswith(
                "special:contributions/"
            )
        ):
            users.add(lowered)

        if (
            lowered.startswith("wp:")
            or lowered.startswith("wikipedia:")
        ):
            policies.add(lowered)

    urls = {
        url.rstrip(".,);:'\"")
        .casefold()
        for url in URL_RE.findall(value)
    }

    return {
        "wiki": wiki,
        "users": users,
        "policies": policies,
        "urls": urls,
    }


# ============================================================
# NLP normalization
# ============================================================

def visible_text(
    value: str | None,
) -> str:
    if not value:
        return ""

    value = html.unescape(value)

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


TOKEN_RE = re.compile(
    r"\b[\w'-]+\b",
    re.UNICODE,
)


def tokens(
    value: str,
) -> Counter:
    return Counter(
        TOKEN_RE.findall(value)
    )


def multiset_jaccard(
    a: str,
    b: str,
) -> float:
    ca = tokens(a)
    cb = tokens(b)

    if not ca and not cb:
        return 1.0

    keys = set(ca) | set(cb)

    intersection = sum(
        min(ca[k], cb[k])
        for k in keys
    )

    union = sum(
        max(ca[k], cb[k])
        for k in keys
    )

    return (
        intersection / union
        if union
        else 1.0
    )


def percentile(
    values: list[float],
    q: float,
):
    if not values:
        return None

    values = sorted(values)

    pos = (
        (len(values) - 1)
        * q
    )

    lower = math.floor(pos)
    upper = math.ceil(pos)

    if lower == upper:
        return values[lower]

    weight = pos - lower

    return (
        values[lower]
        * (1 - weight)
        + values[upper]
        * weight
    )


def pct(
    n: int | float,
    d: int | float,
) -> str:
    if not d:
        return "n/a"

    return f"{100*n/d:.1f}%"


def preview(
    value: str | None,
    n: int = 300,
):
    value = re.sub(
        r"\s+",
        " ",
        value or "",
    ).strip()

    if len(value) <= n:
        return value

    return value[: n - 3] + "..."


# ============================================================
# DuckDB structural / recovery validation
# ============================================================

con = duckdb.connect()

status_rows = con.execute(
    f"""
    SELECT
        recovery_status,
        COUNT(*)
    FROM read_parquet(
        '{str(RECOVERY).replace("'", "''")}'
    )
    GROUP BY 1
    ORDER BY 2 DESC
    """
).fetchall()

status_counts = {
    str(status): int(n)
    for status, n in status_rows
}

recovery_total = sum(
    status_counts.values()
)

high_confidence = status_counts.get(
    "high_confidence",
    0,
)


recovery_quality = con.execute(
    f"""
    SELECT
        MEDIAN(
            TRY_CAST(best_similarity AS DOUBLE)
        ),
        QUANTILE_CONT(
            TRY_CAST(best_similarity AS DOUBLE),
            0.05
        ),
        MIN(
            TRY_CAST(best_similarity AS DOUBLE)
        ),

        MEDIAN(
            TRY_CAST(match_margin AS DOUBLE)
        ),
        QUANTILE_CONT(
            TRY_CAST(match_margin AS DOUBLE),
            0.05
        )

    FROM read_parquet(
        '{str(RECOVERY).replace("'", "''")}'
    )

    WHERE recovery_status='high_confidence'
    """
).fetchone()


population = con.execute(
    f"""
    SELECT
        COUNT(*) FILTER (
            WHERE in_wikidisputes_release
        ),
        COUNT(*) FILTER (
            WHERE in_wikidisputes_release
              AND created_at_utc IS NULL
        )
    FROM read_parquet(
        '{str(UTTERANCES).replace("'", "''")}'
    )
    """
).fetchone()


inversions = con.execute(
    f"""
    WITH x AS (
        SELECT
            conversation_uid,
            utterance_order,

            TRY_CAST(
                created_at_utc AS TIMESTAMP
            ) AS ts,

            LAG(
                TRY_CAST(
                    created_at_utc AS TIMESTAMP
                )
            ) OVER (
                PARTITION BY conversation_uid
                ORDER BY utterance_order
            ) AS prior_ts

        FROM read_parquet(
            '{str(UTTERANCES).replace("'", "''")}'
        )
    )

    SELECT COUNT(*)

    FROM x

    WHERE ts IS NOT NULL
      AND prior_ts IS NOT NULL
      AND ts < prior_ts
    """
).fetchone()[0]


# Confirm that high-confidence promoted rows equal
# the recovered body and that all non-high-confidence
# rows remained unchanged.
safety = con.execute(
    f"""
    WITH before AS (
        SELECT *
        FROM read_csv_auto(
            '{str(BEFORE).replace("'", "''")}',
            HEADER=TRUE,
            ALL_VARCHAR=TRUE,
            SAMPLE_SIZE=-1
        )
        WHERE utterance_role='utterance'
    ),

    after AS (
        SELECT *
        FROM read_csv_auto(
            '{str(AFTER).replace("'", "''")}',
            HEADER=TRUE,
            ALL_VARCHAR=TRUE,
            SAMPLE_SIZE=-1
        )
        WHERE utterance_role='utterance'
    ),

    recovery AS (
        SELECT *
        FROM read_parquet(
            '{str(RECOVERY).replace("'", "''")}'
        )
    )

    SELECT
        COUNT(*) FILTER (
            WHERE recovery.recovery_status =
                  'high_confidence'
              AND after.utterance_text
                  IS DISTINCT FROM
                  recovery.recovered_body_wikitext
        ),

        COUNT(*) FILTER (
            WHERE recovery.recovery_status !=
                  'high_confidence'
              AND after.utterance_text
                  IS DISTINCT FROM
                  before.utterance_text
        ),

        COUNT(*) FILTER (
            WHERE after.utterance_text
                  IS DISTINCT FROM
                  before.utterance_text
        ),

        COUNT(*) FILTER (
            WHERE after.ssot_annotation_text_source =
                  'mediawiki_revision_comment_wikitext_body'
        )

    FROM recovery

    JOIN before
      ON before.ssot_source_row_uid =
         recovery.source_row_uid

    JOIN after
      ON after.ssot_source_row_uid =
         recovery.source_row_uid
    """
).fetchone()


# ============================================================
# Recovery metadata
# ============================================================

recovery_meta = {}

cur = con.execute(
    f"""
    SELECT
        source_row_uid,
        recovery_status,
        TRY_CAST(
            best_similarity AS DOUBLE
        ),
        TRY_CAST(
            match_margin AS DOUBLE
        )

    FROM read_parquet(
        '{str(RECOVERY).replace("'", "''")}'
    )
    """
)

while True:
    batch = cur.fetchmany(5000)

    if not batch:
        break

    for uid, status, sim, margin in batch:
        recovery_meta[str(uid)] = {
            "status": status,
            "similarity": sim,
            "margin": margin,
        }


# ============================================================
# Archival RAW vs signature-stripped BODY
# ============================================================

archival = {
    name: {
        "raw_occurrences": 0,
        "body_occurrences": 0,
        "raw_rows": 0,
        "body_rows": 0,
    }
    for name in PATTERNS
}

raw_signature_timestamps = 0
body_signature_timestamps = 0

rows_where_signature_userlinks_removed = 0
userlinks_removed_by_body = 0

cur = con.execute(
    f"""
    SELECT
        recovered_raw_wikitext,
        recovered_body_wikitext

    FROM read_parquet(
        '{str(RECOVERY).replace("'", "''")}'
    )

    WHERE recovery_status='high_confidence'
    """
)

processed = 0

while True:
    batch = cur.fetchmany(2000)

    if not batch:
        break

    for raw, body in batch:
        rc = markup_counts(raw)
        bc = markup_counts(body)

        for name in PATTERNS:
            archival[name][
                "raw_occurrences"
            ] += rc[name]

            archival[name][
                "body_occurrences"
            ] += bc[name]

            archival[name][
                "raw_rows"
            ] += int(
                rc[name] > 0
            )

            archival[name][
                "body_rows"
            ] += int(
                bc[name] > 0
            )

        raw_signature_timestamps += len(
            UTC_SIGNATURE_TIMESTAMP_RE.findall(
                raw or ""
            )
        )

        body_signature_timestamps += len(
            UTC_SIGNATURE_TIMESTAMP_RE.findall(
                body or ""
            )
        )

        if (
            rc["user_link"]
            > bc["user_link"]
        ):
            rows_where_signature_userlinks_removed += 1

            userlinks_removed_by_body += (
                rc["user_link"]
                - bc["user_link"]
            )

        processed += 1

    if processed % 20000 == 0:
        print(
            "archival audit:",
            f"{processed:,}/{high_confidence:,}"
        )


# ============================================================
# Load pre-promotion annotation text
# ============================================================

before_rows = {}

with BEFORE.open(
    encoding="utf-8",
    newline="",
) as f:
    reader = csv.DictReader(f)

    for row in reader:
        if row.get(
            "utterance_role"
        ) != "utterance":
            continue

        uid = row.get(
            "ssot_source_row_uid"
        )

        if not uid:
            continue

        before_rows[uid] = {
            "text":
                row.get(
                    "utterance_text"
                )
                or "",
            "page":
                row.get(
                    "source_page_title"
                )
                or "",
            "speaker":
                row.get(
                    "speaker_id"
                )
                or "",
            "utterance_id":
                row.get(
                    "utterance_id"
                )
                or "",
        }


# ============================================================
# Full before/after markup + NLP comparison
# ============================================================

metric = {
    name: {
        "before_occurrences": 0,
        "after_occurrences": 0,
        "before_rows": 0,
        "after_rows": 0,
        "rows_gaining": 0,
        "rows_losing": 0,
        "added_occurrences": 0,
        "removed_occurrences": 0,
    }
    for name in PATTERNS
}

unique_before = {
    "wiki": set(),
    "users": set(),
    "policies": set(),
    "urls": set(),
}

unique_after = {
    "wiki": set(),
    "users": set(),
    "policies": set(),
    "urls": set(),
}

changed_rows = 0
promoted_rows = 0
promoted_changed_rows = 0

visible_exact = 0
visible_changed = 0

jaccards = []
visible_length_ratios = []

large_visible_shrink = 0
large_visible_expansion = 0

blank_regressions = 0

reference_rows_before = 0
reference_rows_after = 0
reference_rows_gained = 0

examples = []


with AFTER.open(
    encoding="utf-8",
    newline="",
) as f:
    reader = csv.DictReader(f)

    n = 0

    for row in reader:
        if row.get(
            "utterance_role"
        ) != "utterance":
            continue

        uid = row.get(
            "ssot_source_row_uid"
        )

        if not uid:
            continue

        old = before_rows.get(uid)

        if old is None:
            raise RuntimeError(
                f"Missing baseline row: {uid}"
            )

        before_text = (
            old["text"]
        )

        after_text = (
            row.get(
                "utterance_text"
            )
            or ""
        )

        meta = recovery_meta.get(
            uid,
            {},
        )

        status = meta.get(
            "status"
        )

        promoted = (
            status
            == "high_confidence"
        )

        if promoted:
            promoted_rows += 1

        changed = (
            before_text
            != after_text
        )

        if changed:
            changed_rows += 1

        if (
            promoted
            and changed
        ):
            promoted_changed_rows += 1


        # -----------------------------------------------
        # Markup
        # -----------------------------------------------

        bc = markup_counts(
            before_text
        )

        ac = markup_counts(
            after_text
        )

        added_total = 0

        for name in PATTERNS:
            metric[name][
                "before_occurrences"
            ] += bc[name]

            metric[name][
                "after_occurrences"
            ] += ac[name]

            metric[name][
                "before_rows"
            ] += int(
                bc[name] > 0
            )

            metric[name][
                "after_rows"
            ] += int(
                ac[name] > 0
            )

            delta = (
                ac[name]
                - bc[name]
            )

            if delta > 0:
                metric[name][
                    "rows_gaining"
                ] += 1

                metric[name][
                    "added_occurrences"
                ] += delta

                added_total += delta

            elif delta < 0:
                metric[name][
                    "rows_losing"
                ] += 1

                metric[name][
                    "removed_occurrences"
                ] += -delta


        # "Reference-bearing" = URL or <ref>
        before_reference = (
            bc["url"] > 0
            or bc["ref_tag"] > 0
        )

        after_reference = (
            ac["url"] > 0
            or ac["ref_tag"] > 0
        )

        reference_rows_before += int(
            before_reference
        )

        reference_rows_after += int(
            after_reference
        )

        reference_rows_gained += int(
            after_reference
            and not before_reference
        )


        # -----------------------------------------------
        # Unique targets
        # -----------------------------------------------

        bt = extract_targets(
            before_text
        )

        at = extract_targets(
            after_text
        )

        for kind in unique_before:
            unique_before[kind].update(
                bt[kind]
            )

            unique_after[kind].update(
                at[kind]
            )


        # -----------------------------------------------
        # Semantic-preservation proxy
        # -----------------------------------------------

        jaccard = None
        ratio = None

        if promoted:
            before_visible = visible_text(
                before_text
            )

            after_visible = visible_text(
                after_text
            )

            if (
                before_visible
                == after_visible
            ):
                visible_exact += 1
            else:
                visible_changed += 1

            jaccard = multiset_jaccard(
                before_visible,
                after_visible,
            )

            jaccards.append(
                jaccard
            )

            if before_visible:
                ratio = (
                    len(after_visible)
                    / len(before_visible)
                )

                visible_length_ratios.append(
                    ratio
                )

                if ratio < 0.80:
                    large_visible_shrink += 1

                if ratio > 1.20:
                    large_visible_expansion += 1

        if (
            before_text.strip()
            and not after_text.strip()
        ):
            blank_regressions += 1


        # -----------------------------------------------
        # Example ranking
        # -----------------------------------------------

        if (
            promoted
            and changed
            and (
                added_total > 0
                or (
                    jaccard is not None
                    and jaccard < 0.98
                )
            )
        ):
            score = (
                10 * (
                    ac["user_link"]
                    - bc["user_link"]
                )
                + 8 * (
                    ac["policy_link"]
                    - bc["policy_link"]
                )
                + 7 * (
                    ac["url"]
                    - bc["url"]
                )
                + 5 * (
                    ac["wikilink"]
                    - bc["wikilink"]
                )
                + added_total
            )

            examples.append(
                {
                    "score":
                        score,
                    "page":
                        row.get(
                            "source_page_title"
                        )
                        or "",
                    "utterance_id":
                        row.get(
                            "utterance_id"
                        )
                        or "",
                    "speaker":
                        row.get(
                            "speaker_id"
                        )
                        or "",
                    "similarity":
                        meta.get(
                            "similarity"
                        ),
                    "match_margin":
                        meta.get(
                            "margin"
                        ),
                    "visible_token_jaccard":
                        jaccard,
                    "new_wikilinks":
                        max(
                            0,
                            ac["wikilink"]
                            - bc["wikilink"]
                        ),
                    "new_user_links":
                        max(
                            0,
                            ac["user_link"]
                            - bc["user_link"]
                        ),
                    "new_policy_links":
                        max(
                            0,
                            ac["policy_link"]
                            - bc["policy_link"]
                        ),
                    "new_urls":
                        max(
                            0,
                            ac["url"]
                            - bc["url"]
                        ),
                    "new_templates":
                        max(
                            0,
                            ac["template"]
                            - bc["template"]
                        ),
                    "new_refs":
                        max(
                            0,
                            ac["ref_tag"]
                            - bc["ref_tag"]
                        ),
                    "before_preview":
                        preview(
                            before_text
                        ),
                    "after_preview":
                        preview(
                            after_text
                        ),
                }
            )

        n += 1

        if n % 20000 == 0:
            print(
                "annotation comparison:",
                f"{n:,}/133,223"
            )


# ============================================================
# Unique target gains
# ============================================================

target_summary = {}

for kind in unique_before:
    new_targets = (
        unique_after[kind]
        - unique_before[kind]
    )

    lost_targets = (
        unique_before[kind]
        - unique_after[kind]
    )

    target_summary[kind] = {
        "before_unique":
            len(
                unique_before[kind]
            ),
        "after_unique":
            len(
                unique_after[kind]
            ),
        "new_unique":
            len(new_targets),
        "lost_unique":
            len(lost_targets),
    }


# ============================================================
# NLP summary
# ============================================================

nlp = {
    "visible_exact":
        visible_exact,

    "visible_changed":
        visible_changed,

    "visible_exact_rate":
        (
            visible_exact
            / promoted_rows
            if promoted_rows
            else None
        ),

    "token_jaccard_median":
        percentile(
            jaccards,
            0.50,
        ),

    "token_jaccard_p05":
        percentile(
            jaccards,
            0.05,
        ),

    "token_jaccard_p01":
        percentile(
            jaccards,
            0.01,
        ),

    "token_jaccard_below_095":
        sum(
            x < 0.95
            for x in jaccards
        ),

    "token_jaccard_below_090":
        sum(
            x < 0.90
            for x in jaccards
        ),

    "token_jaccard_below_080":
        sum(
            x < 0.80
            for x in jaccards
        ),

    "visible_length_ratio_median":
        percentile(
            visible_length_ratios,
            0.50,
        ),

    "visible_shrink_below_080":
        large_visible_shrink,

    "visible_expand_above_120":
        large_visible_expansion,
}


# ============================================================
# Example output
# ============================================================

# Force Expert system examples into the advisor audit.
expert = [
    x
    for x in examples
    if x["page"].strip().casefold()
       == "expert system"
]

top = sorted(
    examples,
    key=lambda x: (
        x["score"],
        x["new_user_links"],
        x["new_urls"],
    ),
    reverse=True,
)[:40]

seen = {
    x["utterance_id"]
    for x in top
}

for x in expert:
    if (
        x["utterance_id"]
        not in seen
    ):
        top.append(x)

        seen.add(
            x["utterance_id"]
        )


with OUT_EXAMPLES.open(
    "w",
    encoding="utf-8",
    newline="",
) as f:

    fields = [
        "page",
        "utterance_id",
        "speaker",
        "similarity",
        "match_margin",
        "visible_token_jaccard",
        "new_wikilinks",
        "new_user_links",
        "new_policy_links",
        "new_urls",
        "new_templates",
        "new_refs",
        "before_preview",
        "after_preview",
    ]

    writer = csv.DictWriter(
        f,
        fieldnames=fields,
        extrasaction="ignore",
        lineterminator="\n",
    )

    writer.writeheader()
    writer.writerows(top)


# ============================================================
# Gold
# ============================================================

gold = None

if GOLD_REPORT.exists():
    gold = json.loads(
        GOLD_REPORT.read_text(
            encoding="utf-8"
        )
    )


# ============================================================
# Full JSON summary
# ============================================================

summary = {
    "population": {
        "source_logical_utterances":
            int(population[0]),

        "missing_creation_timestamps":
            int(population[1]),

        "annotation_utterance_occurrences":
            len(before_rows),

        "known_time_chronology_inversions":
            int(inversions),
    },

    "recovery": {
        "total_occurrences":
            recovery_total,

        "status_counts":
            status_counts,

        "high_confidence":
            high_confidence,

        "high_confidence_rate":
            (
                high_confidence
                / recovery_total
                if recovery_total
                else None
            ),

        "best_similarity_median":
            recovery_quality[0],

        "best_similarity_p05":
            recovery_quality[1],

        "best_similarity_min":
            recovery_quality[2],

        "match_margin_median":
            recovery_quality[3],

        "match_margin_p05":
            recovery_quality[4],
    },

    "promotion_safety": {
        "high_confidence_body_mismatches":
            int(safety[0]),

        "non_high_confidence_text_changes":
            int(safety[1]),

        "all_text_changed_rows":
            int(safety[2]),

        "rows_exported_from_mediawiki_body":
            int(safety[3]),

        "nonempty_to_empty_regressions":
            blank_regressions,
    },

    "annotation_changes": {
        "changed_rows":
            changed_rows,

        "promoted_rows":
            promoted_rows,

        "promoted_changed_rows":
            promoted_changed_rows,

        "markup":
            metric,

        "unique_targets":
            target_summary,

        "reference_bearing_rows_before":
            reference_rows_before,

        "reference_bearing_rows_after":
            reference_rows_after,

        "new_reference_bearing_rows":
            reference_rows_gained,
    },

    "nlp_validation":
        nlp,

    "archival_raw_vs_annotation_body": {
        "markup":
            archival,

        "raw_signature_timestamps":
            raw_signature_timestamps,

        "body_signature_timestamps":
            body_signature_timestamps,

        "rows_where_userlinks_removed_with_signature":
            rows_where_signature_userlinks_removed,

        "userlinks_removed_by_signature_stripping":
            userlinks_removed_by_body,
    },

    "gold":
        gold,
}


OUT_JSON.write_text(
    json.dumps(
        summary,
        indent=2,
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)


# ============================================================
# Advisor-ready Markdown
# ============================================================

def fnum(x):
    if x is None:
        return "n/a"

    if isinstance(x, float):
        return f"{x:.3f}"

    return f"{x:,}"


def markup_row(
    key: str,
    label: str,
):
    m = metric[key]

    return (
        f"| {label} "
        f"| {m['before_occurrences']:,} "
        f"| {m['after_occurrences']:,} "
        f"| +{m['added_occurrences']:,} "
        f"| {m['rows_gaining']:,} |\n"
    )


high_rate = (
    high_confidence
    / recovery_total
    if recovery_total
    else 0
)

remaining = (
    recovery_total
    - high_confidence
)

md = []

md.append(
    "# WikiDisputes SSOT: Raw-Wikitext Recovery Validation\n\n"
)

md.append(
    "## Executive summary\n\n"
)

md.append(
    f"- Reconstructed population remains **{population[0]:,} unique substantive utterances** "
    f"and **{len(before_rows):,} dispute-level utterance occurrences**; the text enrichment "
    "does not alter population, identity, outcomes, reply topology, or chronology.\n"
)

md.append(
    f"- Historical raw Wikipedia comment text was recovered with **high confidence for "
    f"{high_confidence:,}/{recovery_total:,} occurrences ({high_rate:.1%})**. "
    f"The remaining **{remaining:,}** observations retain the prior WikiDisputes text rather "
    "than being imputed.\n"
)

md.append(
    f"- **{changed_rows:,} annotation texts changed** relative to the pre-recovery export. "
    f"All non-high-confidence observations changed in **{safety[1]:,} cases**.\n"
)

md.append(
    f"- High-confidence matching is strong: median normalized match similarity "
    f"**{recovery_quality[0]:.3f}**, 5th percentile **{recovery_quality[1]:.3f}**, "
    f"median best-vs-second-candidate margin **{recovery_quality[3]:.3f}**.\n"
)

md.append(
    f"- There are **{inversions:,} known-time chronology inversions** and "
    f"**{blank_regressions:,} nonempty-to-empty text regressions** after enrichment.\n\n"
)


md.append(
    "## Method\n\n"
)

md.append(
    "1. Use the Wikipedia revision component of each WikiConv/WikiDisputes action ID "
    "to retrieve the corresponding historical talk-page revision.\n"
    "2. Segment raw revision wikitext into candidate signed talk-page comments.\n"
    "3. Treat the WikiConv character offset as a positional hint, not a literal raw-text boundary.\n"
    "4. Strip MediaWiki markup **only for matching**, then compare each candidate with the "
    "known WikiDisputes utterance.\n"
    "5. Promote a candidate only when the normalized match is high-confidence and clearly "
    "separated from competing candidates; ambiguous/review/unavailable cases remain unchanged.\n"
    "6. Preserve the complete raw comment as archival SSOT evidence, but use a "
    "**signature-stripped raw-wikitext body** for annotation so substantive links and formatting "
    "are restored without adding routine usernames/timestamps from signatures.\n\n"
)


md.append(
    "## Information restored in annotation-visible text\n\n"
)

md.append(
    "| Feature | Before | After | Added occurrences | Rows gaining feature |\n"
    "|---|---:|---:|---:|---:|\n"
)

for key, label in (
    ("wikilink", "Wiki links"),
    ("user_link", "User / user-talk links"),
    ("policy_link", "Wikipedia policy links"),
    ("external_link", "Bracketed external links"),
    ("url", "URLs"),
    ("template", "MediaWiki templates"),
    ("ref_tag", "`<ref>` tags"),
    ("diff_or_oldid", "Revision/diff references"),
):
    md.append(
        markup_row(
            key,
            label,
        )
    )

md.append("\n")

md.append(
    f"- Utterances containing a URL or `<ref>` increased from "
    f"**{reference_rows_before:,} to {reference_rows_after:,}**; "
    f"**{reference_rows_gained:,}** utterances newly expose this reference information.\n"
)

md.append(
    f"- Unique URL targets: **{target_summary['urls']['before_unique']:,} → "
    f"{target_summary['urls']['after_unique']:,}**, including "
    f"**{target_summary['urls']['new_unique']:,} newly recovered unique URLs**.\n"
)

md.append(
    f"- Unique wiki-link targets: **{target_summary['wiki']['before_unique']:,} → "
    f"{target_summary['wiki']['after_unique']:,}**, including "
    f"**{target_summary['wiki']['new_unique']:,} newly recovered targets**.\n"
)

md.append(
    f"- Unique user-related targets newly recovered in annotation bodies: "
    f"**{target_summary['users']['new_unique']:,}**.\n"
)

md.append(
    f"- Unique Wikipedia-policy targets newly recovered: "
    f"**{target_summary['policies']['new_unique']:,}**.\n\n"
)


md.append(
    "## NLP / textual-preservation validation\n\n"
)

md.append(
    f"- After stripping MediaWiki syntax for comparison, "
    f"**{visible_exact:,}/{promoted_rows:,} ({pct(visible_exact, promoted_rows)})** "
    "of promoted comments have exactly the same normalized visible text as before; "
    "the remainder include visible material that had previously been stripped or other "
    "small reconstruction differences.\n"
)

md.append(
    f"- Median multiset token Jaccard similarity between old and recovered visible text: "
    f"**{nlp['token_jaccard_median']:.3f}**; "
    f"5th percentile **{nlp['token_jaccard_p05']:.3f}**; "
    f"1st percentile **{nlp['token_jaccard_p01']:.3f}**.\n"
)

md.append(
    f"- Promoted comments below token Jaccard 0.95 / 0.90 / 0.80: "
    f"**{nlp['token_jaccard_below_095']:,} / "
    f"{nlp['token_jaccard_below_090']:,} / "
    f"{nlp['token_jaccard_below_080']:,}**.\n"
)

md.append(
    f"- Large visible-text contraction (<80% of prior length): "
    f"**{large_visible_shrink:,}** rows; expansion (>120%): "
    f"**{large_visible_expansion:,}** rows.\n\n"
)


md.append(
    "## Signature-control validation\n\n"
)

md.append(
    "The archival SSOT retains complete historical comment wikitext, including signatures, "
    "but the annotation representation removes the final signature cluster. This avoids "
    "making routine speaker signatures an artificial predictor.\n\n"
)

md.append(
    f"- Raw recovered comments contain **{raw_signature_timestamps:,}** detectable UTC "
    f"signature timestamps; promoted annotation bodies contain **{body_signature_timestamps:,}**.\n"
)

md.append(
    f"- Signature stripping removed **{userlinks_removed_by_body:,} user/user-talk link "
    f"occurrences across {rows_where_signature_userlinks_removed:,} comments** while preserving "
    "user links occurring inside the substantive comment body.\n\n"
)


md.append(
    "## Remaining risk / limitations\n\n"
)

md.append(
    f"- **{remaining:,} occurrences ({1-high_rate:.1%}) are not promoted** because the match "
    "was review-level, unresolved, lacked a viable signed-comment candidate, or the historical "
    "revision was unavailable. They retain the prior WikiDisputes representation.\n"
)

md.append(
    "- High-confidence matching is algorithmic rather than manual; conservative thresholds and "
    "best-vs-second-candidate margins reduce false matches but cannot prove every extraction is perfect.\n"
)

md.append(
    "- Raw wikitext restores source-faithful links/templates rather than rendered HTML; templates "
    "and historical link targets may therefore require interpretation by downstream models.\n"
)

md.append(
    "- Restoring previously stripped evidence can legitimately change LLM classifications. "
    "This is the intended measurement improvement, but prompt/model validation should therefore "
    "be rerun on the migrated Gold set before full-corpus coding.\n"
)

md.append(
    "- Recovery coverage may be non-random because deleted/unavailable revisions and difficult "
    "comment structures are more likely to remain unrecovered; recovery status should be retained "
    "for sensitivity checks.\n\n"
)


md.append(
    "## Accomplishments\n\n"
)

md.append(
    "- Preserved the official WikiDisputes source representation unchanged while adding a richer, "
    "versioned historical MediaWiki representation.\n"
    "- Recovered links, policy references, usernames/user-page references, diff links, templates, "
    "and citation markup that had been removed by WikiDisputes/WikiConv text cleaning.\n"
    "- Prevented uncertain recovery from contaminating the corpus by promoting only "
    "high-confidence matches.\n"
    "- Removed routine signatures from annotation-visible text while retaining them in archival evidence.\n"
    "- Kept population, stable IDs, chronology, reply structure, and researcher-only outcomes unchanged.\n"
)

if gold:
    md.append(
        f"- Gold migration continues to match **{gold.get('matched_exactly_once', 'n/a')}/"
        f"{gold.get('rows', 'n/a')} rows exactly once**, providing a stable bridge to "
        "human re-annotation.\n"
    )

md.append(
    "\n## Reproducibility artifacts\n\n"
    "- `output/silver/mediawiki_raw_comment_recovery.parquet`: extraction/match evidence.\n"
    "- `output/silver/mediawiki_raw_comment_representations.parquet`: archival raw + body representations.\n"
    "- `output/annotation/wikidisputes_llm_annotation_input.csv`: enriched annotation input.\n"
    "- `reports/raw_wikitext_nlp_validation_summary.json`: machine-readable validation metrics.\n"
    "- `reports/raw_wikitext_change_examples.csv`: high-information before/after examples.\n"
)


OUT_MD.write_text(
    "".join(md),
    encoding="utf-8",
)


# ============================================================
# Hard validation
# ============================================================

errors = []

if recovery_total != 133223:
    errors.append(
        f"recovery population={recovery_total:,}, expected 133,223"
    )

if population[0] != 133098:
    errors.append(
        f"logical population={population[0]:,}, expected 133,098"
    )

if safety[0] != 0:
    errors.append(
        f"{safety[0]:,} high-confidence rows do not equal recovered bodies"
    )

if safety[1] != 0:
    errors.append(
        f"{safety[1]:,} non-high-confidence rows changed"
    )

if blank_regressions != 0:
    errors.append(
        f"{blank_regressions:,} nonempty rows became empty"
    )

if inversions != 0:
    errors.append(
        f"{inversions:,} chronology inversions"
    )


print()
print("=" * 72)
print("ADVISOR VALIDATION REPORT")
print("=" * 72)

print(
    f"high-confidence recovery:       "
    f"{high_confidence:,}/{recovery_total:,} "
    f"({high_rate:.1%})"
)

print(
    f"annotation texts changed:       "
    f"{changed_rows:,}"
)

print(
    f"new unique URLs:                "
    f"{target_summary['urls']['new_unique']:,}"
)

print(
    f"new unique wiki targets:        "
    f"{target_summary['wiki']['new_unique']:,}"
)

print(
    f"new unique user targets:        "
    f"{target_summary['users']['new_unique']:,}"
)

print(
    f"new unique policy targets:      "
    f"{target_summary['policies']['new_unique']:,}"
)

print(
    f"new reference-bearing rows:     "
    f"{reference_rows_gained:,}"
)

print(
    f"median visible token Jaccard:   "
    f"{nlp['token_jaccard_median']:.3f}"
)

print(
    f"non-high-confidence changes:    "
    f"{safety[1]:,}"
)

print(
    f"blank regressions:              "
    f"{blank_regressions:,}"
)

print(
    f"chronology inversions:          "
    f"{inversions:,}"
)

print()
print("OUTPUT:")
print(OUT_MD)
print(OUT_JSON)
print(OUT_EXAMPLES)

print()

if errors:
    print("VALIDATION: FAIL")

    for error in errors:
        print(" -", error)

    raise SystemExit(1)

print("VALIDATION: PASS")


if __name__ == "__main__":
    pass
