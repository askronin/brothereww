"""
Re-run rows 2-5 fresh with the new architecture (step-therapy anchor scanner,
Pass 3 ANCHOR RULE + OR-tie-breaker + Worked Example 5).

Keeps the already-locked Row 1 untouched.

  Usage: python3 run_fix_rows_2_to_5.py
"""

import os
import sys
import shutil
from pathlib import Path

os.environ["PIPELINE_CHECKPOINT_PATH"] = "checkpoint_fix.json"

import pandas as pd

from pipeline import (
    load_drug_classifications, load_submissions,
    process_single_row, close_pdf_cache, format_row,
    summarize_llm_usage,
    COLUMN_MAP, DailyQuotaExceeded,
)
import pipeline


FINAL_PATH = Path("result_first5.csv")
BACKUP_PATH = Path("result_first5_v4.csv")  # current "v4" before this fix


def main() -> None:
    if not pipeline.GROQ_API_KEYS:
        print("ERROR: no Groq API keys configured")
        sys.exit(1)

    print(">>> Re-running rows 2..5 with the new architecture "
          "(anchor scanner + improved Pass 3)")
    if FINAL_PATH.exists() and not BACKUP_PATH.exists():
        shutil.copy(FINAL_PATH, BACKUP_PATH)
        print(f"    ✓ Backed up current result_first5.csv → {BACKUP_PATH}")

    if pipeline.CHECKPOINT_PATH.exists():
        pipeline.CHECKPOINT_PATH.unlink()

    pipeline.BRANDED_DRUGS, pipeline.GENERIC_DRUGS = load_drug_classifications(
        pipeline.XLSX_PATH
    )
    submissions_df = load_submissions(pipeline.XLSX_PATH)

    current_df = pd.read_csv(FINAL_PATH).fillna("")
    current_by_key = {
        (str(r["Filename"]), str(r["Brand"])): r.to_dict()
        for _, r in current_df.iterrows()
    }
    print(f"    Current CSV has {len(current_by_key)} rows")

    process_fresh_ilocs = {1, 2, 3, 4}  # rows 2..5 only — Row 1 stays locked

    pdf_cache: dict = {}
    outline_cache: dict = {}
    section_cache: dict = {}
    final_csv_rows: list = []

    try:
        for i in range(5):
            row = submissions_df.iloc[i]
            filename = str(row["Filename"])
            drug = str(row["Brand"])
            key = (filename, drug)

            if i in process_fresh_ilocs:
                print(f"\n>>> iloc[{i}] FRESH (new architecture): {filename} — {drug}")
                try:
                    params = process_single_row(
                        filename, drug, "Plaque Psoriasis",
                        pdf_cache, outline_cache, section_cache,
                    )
                except DailyQuotaExceeded as e:
                    print(f"!!! Quota exceeded mid-row: {e}")
                    break
                params["Filename"] = filename
                params["Brand"] = drug
                final_csv_rows.append(format_row(params))
            else:
                print(f"    ≡ iloc[{i}] preserved (Row 1 locked): {filename} — {drug}")
                if key in current_by_key:
                    row_d = current_by_key[key]
                    csv_row = {col: row_d.get(col, "") for col in COLUMN_MAP.values()}
                    final_csv_rows.append(csv_row)
    finally:
        close_pdf_cache(pdf_cache)

    if len(final_csv_rows) != 5:
        print(f"WARNING: only {len(final_csv_rows)} rows produced (expected 5)")

    df_out = pd.DataFrame(final_csv_rows)
    df_out = df_out[list(COLUMN_MAP.values())]
    df_out = df_out.fillna("")
    df_out.to_csv(FINAL_PATH, index=False)
    print(f"\n>>> Saved {len(final_csv_rows)} rows to {FINAL_PATH}")

    print("\n===== ROW PREVIEW =====")
    for i, r in df_out.iterrows():
        print(f"\n  [{i}] {r['Filename']} - {r['Brand']}")
        for col in (
            "Age",
            "Step Therapy Requirements Documented in Policy",
            "Number of Steps through Brands",
            "Number of Steps through Generic",
            "Step through-Phototherapy",
            "Quantity Limits",
            "Reauthorization Requirements Documented in Policy",
        ):
            v = r[col]
            v_short = (str(v)[:120] + "…") if v and len(str(v)) > 120 else str(v)
            print(f"        {col:<48}: {v_short}")

    summarize_llm_usage()


if __name__ == "__main__":
    main()
