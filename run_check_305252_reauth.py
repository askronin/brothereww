"""
Re-run rows 1, 2, 3 (specifically 305252-5002815.pdf for reauth NA bug)
with the new TPM throttle + raised context size.

  - Row 1 + 2: regression check (must keep current values)
  - Row 3 (iloc=2, 305252): primary test — should now populate
    Reauthorization Requirements with the "For all adult members..." text
  - Rows 4 + 5 preserved from existing CSV

  Usage: python3 run_check_305252_reauth.py
"""

import os
import sys
import shutil
from pathlib import Path

os.environ["PIPELINE_CHECKPOINT_PATH"] = "checkpoint_fix.json"

import pandas as pd

from pipeline import (
    load_drug_classifications, load_submissions, load_all_fda_baselines,
    process_single_row, close_pdf_cache, format_row,
    summarize_llm_usage,
    COLUMN_MAP, DailyQuotaExceeded,
)
import pipeline


FINAL_PATH = Path("result_first5.csv")
BACKUP_PATH = Path("result_first5_v5.csv")  # backup before this run


def main() -> None:
    if not pipeline.GROQ_API_KEYS:
        print("ERROR: no Groq API keys configured")
        sys.exit(1)

    print(">>> Re-running rows 1, 2, 3 with TPM + context fixes")
    if FINAL_PATH.exists() and not BACKUP_PATH.exists():
        shutil.copy(FINAL_PATH, BACKUP_PATH)
        print(f"    ✓ Backed up current result_first5.csv → {BACKUP_PATH}")

    if pipeline.CHECKPOINT_PATH.exists():
        pipeline.CHECKPOINT_PATH.unlink()

    pipeline.BRANDED_DRUGS, pipeline.GENERIC_DRUGS = load_drug_classifications(
        pipeline.XLSX_PATH
    )
    submissions_df = load_submissions(pipeline.XLSX_PATH)

    # Load FDA baselines (cached from earlier fix)
    pipeline.FDA_BASELINE = load_all_fda_baselines(
        ["TREMFYA", "STELARA"]
    )
    print(f"    Loaded {len(pipeline.FDA_BASELINE)} FDA baselines")

    current_df = pd.read_csv(FINAL_PATH).fillna("")
    current_by_key = {
        (str(r["Filename"]), str(r["Brand"])): r.to_dict()
        for _, r in current_df.iterrows()
    }

    # iloc 0=Row1 (330109), 1=Row2 (148593), 2=Row3 (305252) — these fresh
    # iloc 3, 4 — preserved
    process_fresh_ilocs = {0, 1, 2}

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
                print(f"\n>>> iloc[{i}] FRESH: {filename} — {drug}")
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
                print(f"    ≡ iloc[{i}] preserved: {filename} — {drug}")
                if key in current_by_key:
                    row_d = current_by_key[key]
                    csv_row = {col: row_d.get(col, "") for col in COLUMN_MAP.values()}
                    final_csv_rows.append(csv_row)
    finally:
        close_pdf_cache(pdf_cache)

    df_out = pd.DataFrame(final_csv_rows)
    df_out = df_out[list(COLUMN_MAP.values())]
    df_out = df_out.fillna("")
    df_out.to_csv(FINAL_PATH, index=False)
    print(f"\n>>> Saved {len(final_csv_rows)} rows to {FINAL_PATH}")

    # Focused preview — Row 3's reauth column is the primary test
    print("\n===== ROW PREVIEW (focused on reauth fields) =====")
    for i, r in df_out.iterrows():
        print(f"\n  [{i}] {r['Filename']} - {r['Brand']}")
        for col in (
            "Age",
            "Number of Steps through Brands",
            "Number of Steps through Generic",
            "Reauthorization Required",
            "Reauthorization Duration(in-months)",
            "Reauthorization Requirements Documented in Policy",
        ):
            v = r[col]
            v_short = (str(v)[:150] + "…") if v and len(str(v)) > 150 else str(v)
            print(f"        {col:<54}: {v_short}")

    summarize_llm_usage()


if __name__ == "__main__":
    main()
