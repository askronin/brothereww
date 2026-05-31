"""
Sanity re-run of Row 1 (330109 TREMFYA) after Codex fixes.
Compares vs locked expected values; warns on any regression.

  Usage: python3 run_sanity_row1.py
"""

import os, sys
from pathlib import Path

os.environ["PIPELINE_CHECKPOINT_PATH"] = "checkpoint_sanity.json"

import pandas as pd

from pipeline import (
    load_drug_classifications, load_submissions, load_all_fda_baselines,
    process_single_row, close_pdf_cache, format_row,
    summarize_llm_usage, DailyQuotaExceeded,
)
import pipeline


EXPECTED = {
    "Age":                                          ">=18",
    "Number of Steps through Brands":               "3",
    "Number of Steps through Generic":              "1",
    "Step through-Phototherapy":                    "No",
    "TB Test required":                             "Yes",
    "Specialist Types":                             "dermatologist",
    "Reauthorization Required":                     "Yes",
    "Reauthorization Duration(in-months)":          "12",
    "Initial Authorization Duration(in-months)":    "12",
    "Access Score":                                 "0",  # Bucket 0 Near-Impossible
}


def main() -> None:
    if not pipeline.GROQ_API_KEYS:
        print("ERROR: no Groq API keys configured"); sys.exit(1)

    print(">>> Sanity re-run: Row 1 (iloc[0]) after Codex fixes\n")

    if pipeline.CHECKPOINT_PATH.exists():
        pipeline.CHECKPOINT_PATH.unlink()

    pipeline.BRANDED_DRUGS, pipeline.GENERIC_DRUGS = load_drug_classifications(
        pipeline.XLSX_PATH
    )
    print(f"    Drug classifications: {len(pipeline.BRANDED_DRUGS)} branded, "
          f"{len(pipeline.GENERIC_DRUGS)} generic")

    submissions_df = load_submissions(pipeline.XLSX_PATH)
    pipeline.FDA_BASELINE = load_all_fda_baselines(["TREMFYA", "STELARA"])

    row = submissions_df.iloc[0]
    filename = str(row["Filename"])
    drug = str(row["Brand"])
    print(f"\n>>> iloc[0] FRESH: {filename} — {drug}")

    pdf_cache, outline_cache, section_cache = {}, {}, {}
    try:
        params = process_single_row(
            filename, drug, "Plaque Psoriasis",
            pdf_cache, outline_cache, section_cache,
        )
    except DailyQuotaExceeded as e:
        print(f"!!! Quota exceeded: {e}"); sys.exit(2)
    finally:
        close_pdf_cache(pdf_cache)

    params["Filename"] = filename
    params["Brand"] = drug
    csv_row = format_row(params)

    print("\n===== ROW 1 PREVIEW =====")
    passes, fails = 0, 0
    for col, expected in EXPECTED.items():
        got = str(csv_row.get(col, ""))
        # Allow integer-vs-string tolerance for counts (3 vs 3.0 etc.)
        ok = (got == expected) or (got.rstrip(".0") == expected.rstrip(".0"))
        mark = "✓" if ok else "✗"
        if ok: passes += 1
        else: fails += 1
        print(f"  {mark} {col:<48}: got {got!r:<20} expected {expected!r}")

    # Also show step therapy + reauth text (length only — must be non-NA)
    step_txt = str(csv_row.get("Step Therapy Requirements Documented in Policy", ""))
    reauth_txt = str(csv_row.get("Reauthorization Requirements Documented in Policy", ""))
    qty_txt = str(csv_row.get("Quantity Limits", ""))
    print(f"\n  Step Therapy ({len(step_txt)} chars): {step_txt[:120]}...")
    print(f"  Reauth Reqts ({len(reauth_txt)} chars): {reauth_txt[:120]}...")
    print(f"  Quantity Limits ({len(qty_txt)} chars): {qty_txt[:120]}...")

    # Check for marker leak (Codex fix verification)
    marker_leak = "[FROM STEP THERAPY ANCHOR" in step_txt or "[FROM PASS 2" in step_txt
    print(f"\n  Marker leak in step text? {marker_leak}  (must be False)")

    print(f"\n===== RESULT: {passes}/{passes+fails} fields match expected =====")

    summarize_llm_usage()

    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
