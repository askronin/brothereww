"""
Run rows 6-10 (iloc 5-9) of the Submissions file freshly.

  iloc[5] — 378692-5003182.pdf STELARA
  iloc[6] — 378792-5004240.pdf TREMFYA
  iloc[7] — 250819-3621812.pdf STELARA  (multi-brand policy)
  iloc[8] — 250819-3621812.pdf TREMFYA  (same PDF, different target)
  iloc[9] — 296961-4569911.pdf STELARA

Output: result_next5.csv (5 fresh rows)

  Usage: python3 run_next5.py
"""

import os
import sys
from pathlib import Path

os.environ["PIPELINE_CHECKPOINT_PATH"] = "checkpoint_next5.json"

import pandas as pd

from pipeline import (
    load_drug_classifications, load_submissions, load_all_fda_baselines,
    process_single_row, close_pdf_cache, format_row,
    summarize_llm_usage,
    COLUMN_MAP, DailyQuotaExceeded,
)
import pipeline


OUTPUT_PATH = Path("result_next5.csv")


def main() -> None:
    if not pipeline.GROQ_API_KEYS:
        print("ERROR: no Groq API keys configured")
        sys.exit(1)

    print(">>> Running rows 6-10 (iloc 5-9) freshly\n")

    if pipeline.CHECKPOINT_PATH.exists():
        pipeline.CHECKPOINT_PATH.unlink()

    pipeline.BRANDED_DRUGS, pipeline.GENERIC_DRUGS = load_drug_classifications(
        pipeline.XLSX_PATH
    )
    print(f"    Drug classifications: {len(pipeline.BRANDED_DRUGS)} branded, "
          f"{len(pipeline.GENERIC_DRUGS)} generic")

    submissions_df = load_submissions(pipeline.XLSX_PATH)

    # Load FDA baselines for the brands in our 5 targets (TREMFYA + STELARA)
    pipeline.FDA_BASELINE = load_all_fda_baselines(["TREMFYA", "STELARA"])
    print(f"    Loaded {len(pipeline.FDA_BASELINE)} FDA baselines\n")

    target_ilocs = [5, 6, 7, 8, 9]
    pdf_cache: dict = {}
    outline_cache: dict = {}
    section_cache: dict = {}
    final_csv_rows: list = []

    try:
        for i in target_ilocs:
            row = submissions_df.iloc[i]
            filename = str(row["Filename"])
            drug = str(row["Brand"])

            print(f">>> iloc[{i}] FRESH: {filename} — {drug}")
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
    finally:
        close_pdf_cache(pdf_cache)

    if not final_csv_rows:
        print("ERROR: no rows produced")
        sys.exit(1)

    df_out = pd.DataFrame(final_csv_rows)
    df_out = df_out[list(COLUMN_MAP.values())]
    df_out = df_out.fillna("")
    df_out.to_csv(OUTPUT_PATH, index=False)
    print(f"\n>>> Saved {len(final_csv_rows)} rows to {OUTPUT_PATH}")

    print("\n===== ROW PREVIEW =====")
    for i, r in df_out.iterrows():
        print(f"\n  [{i}] {r['Filename']} - {r['Brand']}")
        for col in (
            "Age",
            "Number of Steps through Brands",
            "Number of Steps through Generic",
            "Step through-Phototherapy",
            "TB Test required",
            "Specialist Types",
            "Initial Authorization Duration(in-months)",
            "Reauthorization Required",
            "Reauthorization Duration(in-months)",
            "Quantity Limits",
            "Step Therapy Requirements Documented in Policy",
            "Reauthorization Requirements Documented in Policy",
            "Access Score",
        ):
            v = r[col]
            v_short = (str(v)[:120] + "…") if v and len(str(v)) > 120 else str(v)
            print(f"        {col:<54}: {v_short}")

    summarize_llm_usage()


if __name__ == "__main__":
    main()
