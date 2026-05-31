"""
Re-run iloc 7 (250819 STELARA) and iloc 8 (250819 TREMFYA) freshly to verify
the specialist-hallucination fix (Pass 1 prompt + rule_specialist_not_in_source).

Expected: specialist_types = NA for both (source only has 'appropriate
specialist based on indication' — no specific specialty).

  Usage: python3 run_fix_250819.py
"""

import os, sys, shutil
from pathlib import Path

os.environ["PIPELINE_CHECKPOINT_PATH"] = "checkpoint_250819.json"

import pandas as pd

from pipeline import (
    load_drug_classifications, load_submissions, load_all_fda_baselines,
    process_single_row, close_pdf_cache, format_row,
    summarize_llm_usage, COLUMN_MAP, DailyQuotaExceeded,
)
import pipeline


FINAL_PATH = Path("result_next5.csv")
BACKUP_PATH = Path("result_next5_v1.csv")


def main() -> None:
    if not pipeline.GROQ_API_KEYS:
        print("ERROR: no Groq API keys configured"); sys.exit(1)

    print(">>> Re-running iloc 7 + 8 (250819 multi-brand policy) after specialist fix\n")
    if FINAL_PATH.exists() and not BACKUP_PATH.exists():
        shutil.copy(FINAL_PATH, BACKUP_PATH)
        print(f"    ✓ Backup: {BACKUP_PATH}")

    if pipeline.CHECKPOINT_PATH.exists():
        pipeline.CHECKPOINT_PATH.unlink()

    pipeline.BRANDED_DRUGS, pipeline.GENERIC_DRUGS = load_drug_classifications(
        pipeline.XLSX_PATH
    )
    submissions_df = load_submissions(pipeline.XLSX_PATH)
    pipeline.FDA_BASELINE = load_all_fda_baselines(["TREMFYA", "STELARA"])
    print(f"    Loaded {len(pipeline.FDA_BASELINE)} FDA baselines\n")

    current = pd.read_csv(FINAL_PATH, na_filter=False)
    current_by_key = {
        (str(r["Filename"]), str(r["Brand"])): r.to_dict()
        for _, r in current.iterrows()
    }

    # result_next5.csv currently has rows for iloc 5, 6, 7, 8, 9 (rows 1-5 in CSV)
    # We want to re-run iloc 7 (idx 2 in CSV) and iloc 8 (idx 3 in CSV)
    # Other rows preserved.
    fresh_ilocs = {7, 8}
    pdf_cache, outline_cache, section_cache = {}, {}, {}
    final_rows = []

    try:
        for iloc in [5, 6, 7, 8, 9]:
            row = submissions_df.iloc[iloc]
            filename = str(row["Filename"])
            drug = str(row["Brand"])
            if iloc in fresh_ilocs:
                print(f">>> iloc[{iloc}] FRESH: {filename} — {drug}")
                try:
                    params = process_single_row(
                        filename, drug, "Plaque Psoriasis",
                        pdf_cache, outline_cache, section_cache,
                    )
                except DailyQuotaExceeded as e:
                    print(f"!!! Quota exceeded: {e}"); break
                params["Filename"] = filename
                params["Brand"] = drug
                final_rows.append(format_row(params))
            else:
                print(f"    ≡ iloc[{iloc}] preserved: {filename} — {drug}")
                key = (filename, drug)
                if key in current_by_key:
                    csv_row = {c: current_by_key[key].get(c, "") for c in COLUMN_MAP.values()}
                    final_rows.append(csv_row)
    finally:
        close_pdf_cache(pdf_cache)

    df_out = pd.DataFrame(final_rows)
    df_out = df_out[list(COLUMN_MAP.values())]
    df_out = df_out.fillna("")
    df_out.to_csv(FINAL_PATH, index=False)
    print(f"\n>>> Saved {len(final_rows)} rows to {FINAL_PATH}\n")

    print("===== SPECIALIST CHECK =====")
    for i, r in df_out.iterrows():
        spec = r["Specialist Types"]
        marker = ""
        if "250819" in str(r["Filename"]):
            marker = "  ← target (expected NA)"
        print(f"  [{i}] {r['Filename']} — {r['Brand']:<12} specialist: {spec!r}{marker}")

    summarize_llm_usage()


if __name__ == "__main__":
    main()
