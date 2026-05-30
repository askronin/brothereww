"""
Re-run rows 1 and 2 with the prompt fixes, merge with rows 3, 4, 5
already in result_first5.csv → updated result_first5.csv.

  Row 1 fixes the JSON-dict format regression on Quantity Limits +
  Reauth Requirements + the Age blanking.
  Row 2 fixes the step-therapy/coverage-criteria extraction regression
  (Pass 2 was returning NA on the large STELARA doc).

  Usage: python run_fix_1_2.py
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
BACKUP_PATH = Path("result_first5_v2.csv")  # current "v2" before this fix


def main() -> None:
    if not pipeline.GROQ_API_KEYS:
        print("ERROR: no Groq API keys configured")
        sys.exit(1)

    print(">>> Re-running rows 1, 2 with prompt fixes")
    if FINAL_PATH.exists() and not BACKUP_PATH.exists():
        shutil.copy(FINAL_PATH, BACKUP_PATH)
        print(f"    ✓ Backed up current result_first5.csv → {BACKUP_PATH}")

    if pipeline.CHECKPOINT_PATH.exists():
        pipeline.CHECKPOINT_PATH.unlink()

    pipeline.BRANDED_DRUGS, pipeline.GENERIC_DRUGS = load_drug_classifications(
        pipeline.XLSX_PATH
    )
    submissions_df = load_submissions(pipeline.XLSX_PATH)

    # Read current result_first5.csv (has rows 1, 2, 3, 4, 5 all extracted).
    # We'll replace rows 1 and 2 only; keep 3, 4, 5 as-is.
    current_df = pd.read_csv(FINAL_PATH).fillna("")
    current_by_key = {
        (str(r["Filename"]), str(r["Brand"])): r.to_dict()
        for _, r in current_df.iterrows()
    }
    print(f"    Current CSV has {len(current_by_key)} rows")

    process_fresh_ilocs = {0, 1}  # rows 1 + 2 only

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
                print(f"\n>>> iloc[{i}] FRESH (prompt fixes): {filename} — {drug}")
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
                print(f"    ≡ iloc[{i}] preserved from previous: {filename} — {drug}")
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

    # Per-row preview
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
