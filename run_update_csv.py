"""
Update result_first5.csv to reflect the new code for all 5 rows.

  Rows 1, 2, 5 — re-extracted fresh with new code (smart routing +
                 updated prompts + multi-key fallback).
  Rows 3, 4   — preserved from the A/B run (result_rows_3_4_NEW.csv),
                already extracted with the new code.

Final file: result_first5.csv (overwrites the v1).
A v1 backup (result_first5_v1.csv) is created first.

  Usage: python run_update_csv.py
"""

import os
import sys
import shutil
from pathlib import Path

# Separate checkpoint so we don't disturb anything else.
os.environ["PIPELINE_CHECKPOINT_PATH"] = "checkpoint_update.json"

import pandas as pd

from pipeline import (
    load_drug_classifications, load_submissions,
    process_single_row, close_pdf_cache, format_row,
    print_end_of_batch_summary, summarize_llm_usage,
    COLUMN_MAP, DailyQuotaExceeded,
)
import pipeline


BACKUP_PATH = Path("result_first5_v1.csv")
FINAL_PATH = Path("result_first5.csv")
AB_PATH = Path("result_rows_3_4_NEW.csv")


def main() -> None:
    if not pipeline.GROQ_API_KEYS:
        print("ERROR: no Groq API keys configured")
        sys.exit(1)
    if not AB_PATH.exists():
        print(f"ERROR: A/B output not found at {AB_PATH}")
        sys.exit(1)

    print(">>> Updating result_first5.csv")
    print(f"    Backup of v1: {BACKUP_PATH}")
    print(f"    Keys configured: {len(pipeline.GROQ_API_KEYS)}")

    # Back up v1
    if FINAL_PATH.exists() and not BACKUP_PATH.exists():
        shutil.copy(FINAL_PATH, BACKUP_PATH)
        print(f"    ✓ v1 backed up")

    # Clean stale update-checkpoint
    if pipeline.CHECKPOINT_PATH.exists():
        pipeline.CHECKPOINT_PATH.unlink()

    pipeline.BRANDED_DRUGS, pipeline.GENERIC_DRUGS = load_drug_classifications(
        pipeline.XLSX_PATH
    )
    submissions_df = load_submissions(pipeline.XLSX_PATH)
    print(f"    Submissions: {len(submissions_df)} rows")

    # Read A/B output for rows 3, 4 (keep these as-is)
    ab_df = pd.read_csv(AB_PATH)
    ab_by_key = {
        (str(r["Filename"]), str(r["Brand"])): r.to_dict()
        for _, r in ab_df.iterrows()
    }
    print(f"    A/B preserved: {list(ab_by_key.keys())}")

    # iloc indices to re-extract fresh — rows 1, 2, 5 (1-indexed)
    process_fresh_ilocs = {0, 1, 4}

    pdf_cache: dict = {}
    outline_cache: dict = {}
    section_cache: dict = {}
    final_csv_rows: list = []
    aborted = False
    last_completed: tuple = ()

    try:
        for i in range(5):
            row = submissions_df.iloc[i]
            filename = str(row["Filename"])
            drug = str(row["Brand"])
            key = (filename, drug)

            if i in process_fresh_ilocs:
                print(f"\n>>> iloc[{i}] FRESH (new code): {filename} — {drug}")
                try:
                    params = process_single_row(
                        filename, drug, "Plaque Psoriasis",
                        pdf_cache, outline_cache, section_cache,
                    )
                except DailyQuotaExceeded as e:
                    print(f"!!! Quota exceeded mid-row: {e}")
                    aborted = True
                    break
                params["Filename"] = filename
                params["Brand"] = drug
                csv_row = format_row(params)
                final_csv_rows.append(csv_row)
                last_completed = key
            else:
                print(f"    ≡ iloc[{i}] from A/B: {filename} — {drug}")
                if key in ab_by_key:
                    # A/B row is already in CSV-column form
                    row_d = ab_by_key[key]
                    csv_row = {col: row_d.get(col, "") for col in COLUMN_MAP.values()}
                    final_csv_rows.append(csv_row)
                    last_completed = key
                else:
                    print(f"      WARNING: no A/B entry — processing fresh")
                    try:
                        params = process_single_row(
                            filename, drug, "Plaque Psoriasis",
                            pdf_cache, outline_cache, section_cache,
                        )
                    except DailyQuotaExceeded as e:
                        print(f"!!! Quota exceeded: {e}")
                        aborted = True
                        break
                    params["Filename"] = filename
                    params["Brand"] = drug
                    final_csv_rows.append(format_row(params))
                    last_completed = key
    finally:
        close_pdf_cache(pdf_cache)

    if not final_csv_rows:
        print("No rows produced. Aborting (v1 preserved).")
        return

    # Write to result_first5.csv
    df_out = pd.DataFrame(final_csv_rows)
    # Restore canonical column order
    df_out = df_out[list(COLUMN_MAP.values())]
    # Replace NaN with empty string for cleanliness in CSV
    df_out = df_out.fillna("")
    df_out.to_csv(FINAL_PATH, index=False)
    print(f"\n>>> Saved {len(final_csv_rows)} rows to {FINAL_PATH}")
    if aborted:
        print(f"    (Partial — quota exhausted after {last_completed})")

    # Summary + per-row preview
    print("\n===== ROW PREVIEW =====")
    for i, r in df_out.iterrows():
        print(f"  [{i}] {r['Filename']} - {r['Brand']}")
        for col in (
            "Age",
            "Step Therapy Requirements Documented in Policy",
            "Number of Steps through Brands",
            "Number of Steps through Generic",
            "Step through-Phototherapy",
            "Quantity Limits",
        ):
            v = r[col]
            v_short = (str(v)[:80] + "…") if v and len(str(v)) > 80 else str(v)
            print(f"        {col:<48}: {v_short}")

    summarize_llm_usage()


if __name__ == "__main__":
    main()
