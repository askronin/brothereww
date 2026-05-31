"""
Re-run rows 1, 2, 3, 5 with two prompt fixes addressing partner feedback:
  - Pass 1 specialist_types: indication-filtering rule
  - Pass 3: Worked Example 6 + zero-step OR carve-out recognition

Row 3 (305252) is the primary target. Rows 1, 2, 5 for regression check.
Row 4 preserved.

  Usage: python3 run_fix_partner_feedback.py
"""

import os, sys, shutil
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
BACKUP_PATH = Path("result_first5_v6.csv")


def main() -> None:
    if not pipeline.GROQ_API_KEYS:
        print("ERROR: no Groq API keys configured")
        sys.exit(1)

    print(">>> Re-running rows 1, 2, 3, 5 with partner-feedback fixes")
    if FINAL_PATH.exists() and not BACKUP_PATH.exists():
        shutil.copy(FINAL_PATH, BACKUP_PATH)
        print(f"    ✓ Backed up current → {BACKUP_PATH}")

    if pipeline.CHECKPOINT_PATH.exists():
        pipeline.CHECKPOINT_PATH.unlink()

    pipeline.BRANDED_DRUGS, pipeline.GENERIC_DRUGS = load_drug_classifications(
        pipeline.XLSX_PATH
    )
    submissions_df = load_submissions(pipeline.XLSX_PATH)
    pipeline.FDA_BASELINE = load_all_fda_baselines(["TREMFYA", "STELARA"])
    print(f"    Loaded {len(pipeline.FDA_BASELINE)} FDA baselines")

    current_df = pd.read_csv(FINAL_PATH).fillna("")
    current_by_key = {
        (str(r["Filename"]), str(r["Brand"])): r.to_dict()
        for _, r in current_df.iterrows()
    }

    process_fresh_ilocs = {0, 1, 2, 4}  # row 4 (iloc 3) preserved

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

    print("\n===== ROW PREVIEW (focused on partner feedback fields) =====")
    for i, r in df_out.iterrows():
        print(f"\n  [{i}] {r['Filename']} - {r['Brand']}")
        for col in (
            "Number of Steps through Brands",
            "Number of Steps through Generic",
            "Step through-Phototherapy",
            "Specialist Types",
            "Reauthorization Required",
            "Reauthorization Duration(in-months)",
        ):
            v = r[col]
            v_short = (str(v)[:120] + "…") if v and len(str(v)) > 120 else str(v)
            print(f"        {col:<54}: {v_short}")

    summarize_llm_usage()


if __name__ == "__main__":
    main()
