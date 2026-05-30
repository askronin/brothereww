"""
One-off runner for the first 5 rows of the Submissions tab.

Produces result_first5.csv for partner extraction review. Uses a separate
checkpoint + output path so it doesn't pollute the eventual full-batch run.

  Usage:  python run_first5.py
  Resume: python run_first5.py            # checkpoint_first5.json restarts where it left off

If Groq's daily TPD limit is hit mid-run, checkpoint is saved and the
process exits with code 2; just re-run tomorrow.
"""

import os
import sys
from pathlib import Path

# Override the output + checkpoint paths BEFORE importing pipeline.
# pipeline.py reads these from env at import time.
os.environ.setdefault("PIPELINE_CHECKPOINT_PATH", "checkpoint_first5.json")
os.environ.setdefault("PIPELINE_OUTPUT_PATH", "result_first5.csv")

from pipeline import (
    load_drug_classifications,
    load_submissions,
    preflight_check,
    process_all_rows,
    save_csv,
    print_end_of_batch_summary,
    DailyQuotaExceeded,
)
import pipeline


def main() -> None:
    print(">>> First-5-rows extraction (for partner review)")
    print(f"    Checkpoint: {pipeline.CHECKPOINT_PATH}")
    print(f"    Output:     {pipeline.OUTPUT_PATH}")

    if not pipeline.GROQ_API_KEY:
        print("ERROR: GROQ_API_KEY required (set env var or .env)")
        sys.exit(1)
    if not pipeline.XLSX_PATH.exists():
        print(f"ERROR: XLSX not found at {pipeline.XLSX_PATH}")
        sys.exit(1)
    if not pipeline.PDF_DIR.exists():
        print(f"ERROR: PDF dir not found at {pipeline.PDF_DIR}")
        sys.exit(1)

    print("\n=== Loading classifications + submissions ===")
    pipeline.BRANDED_DRUGS, pipeline.GENERIC_DRUGS = load_drug_classifications(
        pipeline.XLSX_PATH
    )
    print(f"  Branded={len(pipeline.BRANDED_DRUGS)}, "
          f"Generic={len(pipeline.GENERIC_DRUGS)}")

    submissions_df = load_submissions(pipeline.XLSX_PATH)
    print(f"  Total submissions: {len(submissions_df)}")

    first5 = submissions_df.head(5).copy()
    print("\n=== Slice (first 5 rows) ===")
    for i, row in first5.iterrows():
        print(f"  [{i}] {row['Filename']:<28} {row['Brand']}")

    print("\n=== Preflight (cheap, no LLM) ===")
    pdf_cache: dict = {}
    preflight_check(first5, pdf_cache)

    print("\n=== Running extraction (3 LLM calls per row + 1 per sectioned PDF) ===")
    try:
        results = process_all_rows(first5, pdf_cache=pdf_cache)
    except DailyQuotaExceeded:
        print("\n>>> DAILY QUOTA EXCEEDED mid-run. Checkpoint saved.")
        print(f">>> Resume tomorrow: python run_first5.py")
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n>>> Interrupted. Checkpoint saved. Resume any time.")
        sys.exit(130)

    save_csv(results)
    print_end_of_batch_summary(results)
    print(f"\n>>> Done. {len(results)} rows written to {pipeline.OUTPUT_PATH}")


if __name__ == "__main__":
    main()
