#!/usr/bin/env python3
"""Filter and randomize Manifold markets.

Defaults:

    input:
        ../Markets/active_markets_until_end_june_2026_with_descriptions.csv

    output:
        ../Markets/MarketsRandomization.csv

    randomization check:
        ../Markets/MarketsRandomizationCheck.tex

Run directly:

    python market_randomization.py
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
from statistics import mean, stdev
from typing import Any

from scipy import stats


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INPUT_CSV = os.path.join(
    PROJECT_DIR,
    "Markets",
    "active_markets_until_end_june_2026_with_descriptions.csv",
)
DEFAULT_OUTPUT_CSV = os.path.join(
    PROJECT_DIR,
    "Markets",
    "MarketsRandomization.csv",
)
DEFAULT_CHECK_TEX = os.path.join(
    PROJECT_DIR,
    "Markets",
    "MarketsRandomizationCheck.tex",
)
REQUIRED_OUTCOME_TYPE = "BINARY"
DEFAULT_RANDOMIZATION_SEED = 2026
TREATMENT_GROUP = "Treatment"
CONTROL_GROUP = "Control"
RANDOMIZATION_COLUMNS = [
    "randomizationGroup",
    "randomizationOrder",
    "randomizationSeed",
]


def parse_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None

    try:
        return float(value)
    except ValueError:
        return None


def has_description(row: dict[str, str]) -> bool:
    return bool(row.get("textDescription", "").strip())


def is_unresolved(row: dict[str, str]) -> bool:
    value = row.get("isResolved", "").strip().lower()
    return value in {"", "false", "0", "no"}


def is_matching_market(row: dict[str, str]) -> bool:
    return (
        row.get("outcomeType", "").strip().upper() == REQUIRED_OUTCOME_TYPE
        and has_description(row)
        and is_unresolved(row)
    )


def filter_markets(input_csv: str) -> tuple[list[dict[str, str]], list[str], int]:
    with open(input_csv, newline="", encoding="utf-8") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None:
            raise ValueError(f"No CSV header found in {input_csv}")

        rows = list(reader)
        filtered_rows = [row for row in rows if is_matching_market(row)]

    return filtered_rows, reader.fieldnames, len(rows)


def randomize_markets(
    rows: list[dict[str, str]],
    seed: int,
) -> list[dict[str, str]]:
    randomized_rows = [dict(row) for row in rows]
    rng = random.Random(seed)

    treatment_count = len(randomized_rows) // 2
    group_assignments = [TREATMENT_GROUP] * treatment_count + [CONTROL_GROUP] * (
        len(randomized_rows) - treatment_count
    )
    rng.shuffle(group_assignments)

    for row, group in zip(randomized_rows, group_assignments):
        row["randomizationGroup"] = group
        row["randomizationSeed"] = str(seed)

    rng.shuffle(randomized_rows)
    for index, row in enumerate(randomized_rows, start=1):
        row["randomizationOrder"] = str(index)

    return randomized_rows


def write_randomized_csv(
    output_csv: str,
    rows: list[dict[str, str]],
    input_fieldnames: list[str],
) -> None:
    fieldnames = list(input_fieldnames)
    for column in RANDOMIZATION_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)

    rows.sort(key=lambda row: int(row["randomizationOrder"]))
    os.makedirs(os.path.dirname(output_csv), exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def sample_sd(values: list[float]) -> float:
    return stdev(values) if len(values) > 1 else 0.0


def welch_t_test(
    treatment_values: list[float],
    control_values: list[float],
) -> dict[str, float | None]:
    if len(treatment_values) < 2 or len(control_values) < 2:
        return {
            "tStatistic": None,
            "degreesOfFreedom": None,
            "pValue": None,
        }

    result = stats.ttest_ind(
        treatment_values,
        control_values,
        equal_var=False,
        nan_policy="omit",
    )

    return {
        "tStatistic": float(result.statistic),
        "degreesOfFreedom": float(result.df),
        "pValue": float(result.pvalue),
    }


def standardized_mean_difference(
    treatment_values: list[float],
    control_values: list[float],
) -> float | None:
    if not treatment_values or not control_values:
        return None

    treatment_sd = sample_sd(treatment_values)
    control_sd = sample_sd(control_values)
    pooled_sd = math.sqrt((treatment_sd**2 + control_sd**2) / 2)
    if pooled_sd == 0:
        return None

    return (mean(treatment_values) - mean(control_values)) / pooled_sd


def close_days_from_first_close(rows: list[dict[str, str]]) -> dict[str, float]:
    close_times = [
        value
        for value in (parse_float(row.get("closeTime")) for row in rows)
        if value is not None
    ]
    if not close_times:
        return {}

    first_close_time = min(close_times)
    milliseconds_per_day = 24 * 60 * 60 * 1000
    return {
        row["id"]: (close_time - first_close_time) / milliseconds_per_day
        for row in rows
        if (close_time := parse_float(row.get("closeTime"))) is not None
    }


def balance_variables(rows: list[dict[str, str]]) -> dict[str, dict[str, float]]:
    close_day_lookup = close_days_from_first_close(rows)
    variables: dict[str, dict[str, float]] = {
        "Probability": {},
        "Volume": {},
        "Volume in past 24 hours": {},
        "Unique bettor count": {},
        "Log description length": {},
        "Close timing, days from first close": {},
    }

    for row in rows:
        market_id = row["id"]
        probability = parse_float(row.get("probability"))
        volume = parse_float(row.get("volume"))
        volume_24_hours = parse_float(row.get("volume24Hours"))
        unique_bettors = parse_float(row.get("uniqueBettorCount"))

        if probability is not None:
            variables["Probability"][market_id] = probability
        if volume is not None:
            variables["Volume"][market_id] = volume
        if volume_24_hours is not None:
            variables["Volume in past 24 hours"][market_id] = volume_24_hours
        if unique_bettors is not None:
            variables["Unique bettor count"][market_id] = unique_bettors
        variables["Log description length"][market_id] = math.log1p(
            len(row.get("textDescription", "").strip())
        )
        if market_id in close_day_lookup:
            variables["Close timing, days from first close"][
                market_id
            ] = close_day_lookup[market_id]

    return variables


def group_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    return {
        CONTROL_GROUP: sum(
            1 for row in rows if row["randomizationGroup"] == CONTROL_GROUP
        ),
        TREATMENT_GROUP: sum(
            1 for row in rows if row["randomizationGroup"] == TREATMENT_GROUP
        ),
    }


def summarize_balance(rows: list[dict[str, str]]) -> list[dict[str, float | str | None]]:
    variables = balance_variables(rows)
    balance_rows: list[dict[str, float | str | None]] = []

    for variable, values_by_id in variables.items():
        treatment_values = [
            values_by_id[row["id"]]
            for row in rows
            if row["randomizationGroup"] == TREATMENT_GROUP
            and row["id"] in values_by_id
        ]
        control_values = [
            values_by_id[row["id"]]
            for row in rows
            if row["randomizationGroup"] == CONTROL_GROUP
            and row["id"] in values_by_id
        ]

        t_test = welch_t_test(treatment_values, control_values)

        balance_rows.append(
            {
                "variable": variable,
                "controlMean": mean(control_values) if control_values else None,
                "controlSd": sample_sd(control_values) if control_values else None,
                "treatmentMean": mean(treatment_values)
                if treatment_values
                else None,
                "treatmentSd": sample_sd(treatment_values)
                if treatment_values
                else None,
                "standardizedMeanDifference": standardized_mean_difference(
                    treatment_values,
                    control_values,
                ),
                "tStatistic": t_test["tStatistic"],
                "degreesOfFreedom": t_test["degreesOfFreedom"],
                "pValue": t_test["pValue"],
            }
        )

    return balance_rows


def format_tex_number(value: float | str | None, digits: int = 3) -> str:
    if value is None:
        return "--"
    if isinstance(value, str):
        return value
    return f"{value:.{digits}f}"


def write_randomization_check_tex(
    check_tex: str,
    rows: list[dict[str, str]],
    summary: dict[str, Any],
) -> list[dict[str, float | str | None]]:
    counts = group_counts(rows)
    balance_rows = summarize_balance(rows)

    lines = [
        r"\documentclass{article}",
        r"\usepackage[margin=1in]{geometry}",
        r"\usepackage{booktabs}",
        r"\begin{document}",
        r"\section*{Markets Randomization Check}",
        "",
        f"Input markets: {summary['totalRows']}. Filtered eligible markets: {summary['filteredRows']}.",
        "",
        f"Randomization seed: {summary['randomizationSeed']}.",
        "",
        r"\subsection*{Assignment Counts}",
        r"\begin{tabular}{lr}",
        r"\toprule",
        r"Group & Count \\",
        r"\midrule",
        f"{CONTROL_GROUP} & {counts[CONTROL_GROUP]} " + r"\\",
        f"{TREATMENT_GROUP} & {counts[TREATMENT_GROUP]} " + r"\\",
        r"\bottomrule",
        r"\end{tabular}",
        "",
        r"\subsection*{Balance Table}",
        r"\begin{tabular}{lrrrrrrrr}",
        r"\toprule",
        (
            r"Variable & Control mean & Control SD & Treatment mean & "
            r"Treatment SD & SMD & t & df & p-value \\"
        ),
        r"\midrule",
    ]

    for row in balance_rows:
        lines.append(
            " & ".join(
                [
                    str(row["variable"]),
                    format_tex_number(row["controlMean"]),
                    format_tex_number(row["controlSd"]),
                    format_tex_number(row["treatmentMean"]),
                    format_tex_number(row["treatmentSd"]),
                    format_tex_number(row["standardizedMeanDifference"]),
                    format_tex_number(row["tStatistic"]),
                    format_tex_number(row["degreesOfFreedom"], digits=1),
                    format_tex_number(row["pValue"]),
                ]
            )
            + r" \\"
        )

    lines.extend(
        [
            r"\bottomrule",
            r"\end{tabular}",
            "",
            (
                "Notes: SMD is the standardized mean difference, calculated as "
                "treatment minus control divided by the pooled standard deviation. "
                "The t-test columns report a two-sided Welch two-sample t-test "
                "comparing treatment and control means."
            ),
            "",
            r"\end{document}",
        ]
    )

    os.makedirs(os.path.dirname(check_tex), exist_ok=True)
    with open(check_tex, "w", newline="\n", encoding="utf-8") as output_file:
        output_file.write("\n".join(lines))

    return balance_rows


def filter_randomize_and_check(
    input_csv: str,
    output_csv: str,
    check_tex: str,
    seed: int,
) -> dict[str, Any]:
    filtered_rows, fieldnames, total_rows = filter_markets(input_csv)
    randomized_rows = randomize_markets(filtered_rows, seed)
    write_randomized_csv(output_csv, randomized_rows, fieldnames)

    summary: dict[str, Any] = {
        "inputCsv": input_csv,
        "outputCsv": output_csv,
        "randomizationCheckTex": check_tex,
        "totalRows": total_rows,
        "filteredRows": len(randomized_rows),
        "groupCounts": group_counts(randomized_rows),
        "randomizationSeed": seed,
        "criteria": {
            "requiresDescription": True,
            "outcomeType": REQUIRED_OUTCOME_TYPE,
            "unresolved": True,
        },
    }
    balance_rows = write_randomization_check_tex(check_tex, randomized_rows, summary)
    summary["balanceCheck"] = balance_rows

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Filter unresolved markets to BINARY outcomeType and a non-empty "
            "textDescription, then randomize eligible markets into treatment "
            "and control groups."
        )
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT_CSV,
        help=f"Input CSV path. Default: {DEFAULT_INPUT_CSV}",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_CSV,
        help=f"Output CSV path. Default: {DEFAULT_OUTPUT_CSV}",
    )
    parser.add_argument(
        "--check-output",
        default=DEFAULT_CHECK_TEX,
        help=f"Randomization check TeX path. Default: {DEFAULT_CHECK_TEX}",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_RANDOMIZATION_SEED,
        help=f"Randomization seed. Default: {DEFAULT_RANDOMIZATION_SEED}",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = filter_randomize_and_check(
        args.input,
        args.output,
        args.check_output,
        args.seed,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
