"""
A/B-compare runner for rows 3 and 4 (1-indexed) of the Submissions tab.

Processes rows with the NEW code (smart routing + updated prompts) and
writes a separate CSV. Does NOT touch the existing result_first5.csv or
checkpoint_first5.json — those remain the "v1" baseline for partner review.

After processing, prints a per-row side-by-side diff so the partner can see
exactly which fields changed and why.

  Usage:  python run_a_b_compare.py
"""

import os
import sys
import json
from pathlib import Path

# Force separate output paths so we don't disturb the v1 artefacts.
os.environ["PIPELINE_CHECKPOINT_PATH"] = "checkpoint_ab.json"
os.environ["PIPELINE_OUTPUT_PATH"] = "result_rows_3_4_NEW.csv"

import pandas as pd

from pipeline import (
    load_drug_classifications, load_submissions,
    process_single_row, save_csv, close_pdf_cache,
    print_end_of_batch_summary, format_row, COLUMN_MAP,
    DailyQuotaExceeded,
)
import pipeline


# Columns to highlight in the diff (the ones flagged in feedback).
DIFF_FIELDS_FOR_FEEDBACK = [
    "Step Therapy Requirements Documented in Policy",
    "Number of Steps through Brands",
    "Number of Steps through Generic",
    "Step through-Phototherapy",
    "Quantity Limits",
    "Reauthorization Requirements Documented in Policy",
]


def _shorten(s: object, n: int = 120) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return "<NaN>"
    s = str(s)
    return s if len(s) <= n else s[:n] + "…"


def print_side_by_side(old_row: dict, new_row: dict, filename: str, brand: str) -> None:
    print(f"\n{'='*100}")
    print(f"  {filename}  —  {brand}")
    print(f"{'='*100}")
    print(f"  {'field':<48} | {'OLD (v1, llama)':<32} | NEW (Scout+routing)")
    print(f"  {'-'*48} + {'-'*32} + {'-'*40}")
    for col, key in COLUMN_MAP.items():
        if key in ("Filename", "Brand", "Access Score"):
            continue
        old_v = old_row.get(key)
        new_v = new_row.get(key)
        # Mark changed fields with → arrow
        is_diff = str(old_v).strip() != str(new_v).strip()
        marker = "→" if is_diff else " "
        is_feedback = key in DIFF_FIELDS_FOR_FEEDBACK
        flag = " ⭐" if is_diff and is_feedback else ("    " if is_diff else "    ")
        print(f"  {marker} {key:<46} | {_shorten(old_v, 30):<32} | {_shorten(new_v, 38)}{flag}")


def main() -> None:
    if not pipeline.GROQ_API_KEYS:
        print("ERROR: no Groq API keys configured")
        sys.exit(1)

    print(f">>> A/B comparison run")
    print(f"    Keys configured: {len(pipeline.GROQ_API_KEYS)}")
    print(f"    Checkpoint (separate): {pipeline.CHECKPOINT_PATH}")
    print(f"    Output (new): {pipeline.OUTPUT_PATH}")
    print(f"    Baseline (untouched): result_first5.csv")

    # Wipe any stale A/B checkpoint
    if pipeline.CHECKPOINT_PATH.exists():
        pipeline.CHECKPOINT_PATH.unlink()

    # Load classifications + submissions
    pipeline.BRANDED_DRUGS, pipeline.GENERIC_DRUGS = load_drug_classifications(
        pipeline.XLSX_PATH
    )
    submissions_df = load_submissions(pipeline.XLSX_PATH)
    print(f"    Submissions total: {len(submissions_df)}")

    # Take rows 3 and 4 (1-indexed) = iloc[2:4]
    target = submissions_df.iloc[2:4].copy()
    print(f"\n=== Target rows ===")
    for i, row in target.iterrows():
        print(f"  iloc[{i}] {row['Filename']} — {row['Brand']}")

    # Load v1 baseline (existing CSV)
    baseline_df = pd.read_csv("result_first5.csv")
    baseline_by_key = {
        (str(r["Filename"]), str(r["Brand"])): r.to_dict()
        for _, r in baseline_df.iterrows()
    }

    # Run
    pdf_cache: dict = {}
    outline_cache: dict = {}
    section_cache: dict = {}
    new_results = []
    try:
        for _, row in target.iterrows():
            filename = str(row["Filename"])
            drug = str(row["Brand"])
            print(f"\n>>> Processing {filename} — {drug}")
            try:
                params = process_single_row(
                    filename, drug, "Plaque Psoriasis",
                    pdf_cache, outline_cache, section_cache,
                )
            except DailyQuotaExceeded as e:
                print(f"!!! Quota exceeded before completing this row: {e}")
                break
            params["Filename"] = filename
            params["Brand"] = drug
            new_results.append(params)
    finally:
        close_pdf_cache(pdf_cache)

    if not new_results:
        print("No rows extracted. Aborting comparison.")
        return

    save_csv(new_results, path=pipeline.OUTPUT_PATH)
    print_end_of_batch_summary(new_results)

    # Read back as DataFrame for direct comparison
    new_df = pd.read_csv(pipeline.OUTPUT_PATH)
    new_by_key = {
        (str(r["Filename"]), str(r["Brand"])): r.to_dict()
        for _, r in new_df.iterrows()
    }

    # Side-by-side diff
    print("\n\n" + "#"*100)
    print("# A/B COMPARISON  —  OLD (v1, llama, original prompts) vs NEW (Scout + routing + updated prompts)")
    print("#"*100)
    print("# Legend: → marks changed fields. ⭐ marks fields the partner flagged in feedback.")
    for (fn, brand), new_row in new_by_key.items():
        old_row = baseline_by_key.get((fn, brand), {})
        if not old_row:
            print(f"\n  {fn} — {brand}: no baseline row found")
            continue
        print_side_by_side(old_row, new_row, fn, brand)


if __name__ == "__main__":
    main()
