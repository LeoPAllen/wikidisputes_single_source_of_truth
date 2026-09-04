"""CLI for deterministic before/after Method-A promotion reporting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from wikidisputes_ssot.io import atomic_write_json
from wikidisputes_ssot.method_a_comparison import (
    DENOMINATOR,
    HardRegressionError,
    compare_method_a,
    read_parquet_rows,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-recovery", type=Path, required=True)
    parser.add_argument("--baseline-audit", type=Path, required=True)
    parser.add_argument("--current-recovery", type=Path, required=True)
    parser.add_argument("--current-audit", type=Path, required=True)
    parser.add_argument("--method-b-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--denominator", type=int, default=DENOMINATOR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = compare_method_a(
        read_parquet_rows(str(args.baseline_recovery)),
        read_parquet_rows(str(args.baseline_audit)),
        read_parquet_rows(str(args.current_recovery)),
        read_parquet_rows(str(args.current_audit)),
        read_parquet_rows(str(args.method_b_evidence)),
        denominator=args.denominator,
        raise_on_regression=False,
    )
    atomic_write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    if report["regressions"]["hard_regression_count"]:
        raise HardRegressionError("hard Method-A regression(s) detected; report was written")


if __name__ == "__main__":
    main()
