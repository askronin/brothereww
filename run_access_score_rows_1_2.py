"""
Run JUST the access-score part (Block 7) for rows 1 and 2 of result_first5.csv.

  - No extraction calls. No LLM. No PDFs read.
  - Reads already-extracted params from result_first5.csv.
  - Loads FDA_BASELINE from openFDA (or cached fda_baselines_cache.json).
  - Calls compute_access_score(params, drug) — fully traced output.
  - Writes the resulting bucket back into the 'Access Score' column.

  Usage: python3 run_access_score_rows_1_2.py
"""

from pathlib import Path
import pandas as pd

import pipeline as pl


CSV_PATH = Path("result_first5.csv")

BUCKET_LABEL = {
    0: "Near-Impossible Access",
    15: "Very Restrictive Access",
    25: "Restrictive Access",
    50: "FDA Parity",
    75: "Better Than FDA",
    100: "Best Access",
}


def csv_row_to_params(row: pd.Series) -> dict:
    """Map result_first5.csv columns → compute_access_score() params keys."""
    return {
        "age":                          str(row["Age"]) or "NA",
        "tb_test_required":             str(row["TB Test required"]) or "NA",
        "steps_brands":                 str(row["Number of Steps through Brands"]),
        "steps_generic":                str(row["Number of Steps through Generic"]),
        "step_phototherapy":            str(row["Step through-Phototherapy"]),
        "specialist_types":             str(row["Specialist Types"]) or "NA",
        "initial_auth_duration_months": str(row["Initial Authorization Duration(in-months)"]) or "NA",
        "reauth_duration_months":       str(row["Reauthorization Duration(in-months)"]) or "NA",
        "reauth_required":              str(row["Reauthorization Required"]) or "NA",
    }


def print_fda_baseline(drug: str) -> None:
    """Surface what FDA values we're comparing against for this drug."""
    bl = pl.FDA_BASELINE.get(drug.upper())
    if not bl:
        print(f"    ⚠ NO FDA BASELINE for {drug} — access score will default to bucket 25")
        return
    print(f"    FDA baseline ({drug}):")
    print(f"      min_age           = {bl['min_age']}")
    print(f"      min_weight_kg     = {bl['min_weight_kg']}")
    print(f"      brand_steps       = {bl['brand_steps']}")
    print(f"      generic_steps     = {bl['generic_steps']}")
    print(f"      phototherapy_req  = {bl['phototherapy_required']}")
    print(f"      tb_test_as_PA     = {bl['tb_test_required_as_pa_gate']}")
    print(f"      specialist_rest   = {bl['specialist_restriction']}")
    print(f"      label_eff_date    = {bl['label_effective_date']}")
    print(f"      inn / pharm_class = {bl['inn']} / {bl['pharm_class']}")


def main() -> None:
    print(">>> Running access score on rows 1 + 2 of result_first5.csv only\n")

    # Load FDA baselines (uses fda_baselines_cache.json if present, else live curl).
    print("Loading FDA baselines (cached after first run)...")
    pl.FDA_BASELINE = pl.load_all_fda_baselines(["TREMFYA", "STELARA"])
    print(f"  Loaded {len(pl.FDA_BASELINE)} baselines\n")

    df = pd.read_csv(CSV_PATH).fillna("")

    for iloc_idx in (0, 1):
        row = df.iloc[iloc_idx]
        drug = str(row["Brand"])
        params = csv_row_to_params(row)

        print("=" * 72)
        print(f"  ROW {iloc_idx + 1}: {row['Filename']}  —  {drug}")
        print("=" * 72)
        print("    Input params (from result_first5.csv):")
        for k, v in params.items():
            print(f"      {k:<32} = {v!r}")

        print()
        print_fda_baseline(drug)

        print()
        result = pl.compute_access_score(params, drug)

        bucket = result["bucket"]
        print(f"\n    >>> Bucket = {bucket}  ({BUCKET_LABEL.get(bucket, '?')})")
        print(f"    >>> Reason = {result.get('reason')}")
        print(f"    >>> Coverage count = {result.get('coverage_count')}")

        counts = result.get("restriction_summary", {})
        if counts:
            print(f"\n    Severity counts:")
            print(f"      Severe       = {counts.get('severe', 0)}")
            print(f"      Major        = {counts.get('major', 0)}")
            print(f"      Moderate     = {counts.get('moderate', 0)}")
            print(f"      Minor        = {counts.get('minor', 0)}")
            print(f"      Improvement  = {counts.get('improvement', 0)}")
            print(f"      Unknown      = {counts.get('unknown', 0)}")

        comparisons = result.get("comparisons", {})
        if comparisons:
            print(f"\n    Per-parameter FDA comparison (Layer B):")
            for param, cmp in comparisons.items():
                state = cmp["state"]
                sev = cmp["severity"] or ""
                sev_str = f"  ({sev})" if sev else ""
                print(f"      {param:<16} → {state}{sev_str}")

        modifiers = result.get("modifiers", {})
        if modifiers:
            print(f"\n    Access modifier features (Layer C):")
            print(f"      Feature A (initial auth dur)   → {modifiers['feature_a']}")
            print(f"      Feature B (reauth required)    → {modifiers['feature_b']}")
            print(f"      Feature C (reauth dur)         → {modifiers['feature_c']}")
            print(f"      Consistency penalty            → {modifiers['consistency_penalty']}")
            print(f"      → +{modifiers['improvements']} improvements, "
                  f"+{modifiers['minor_restrictions']} minor restrictions")

        inf = result.get("reauth_inference")
        if inf:
            print(f"\n    Reauth inference applied: {inf}")

        # Update the CSV value
        df.at[iloc_idx, "Access Score"] = bucket
        print()

    df.to_csv(CSV_PATH, index=False)
    print("=" * 72)
    print(f"✓ Updated 'Access Score' column for rows 1 + 2 in {CSV_PATH}")
    print("=" * 72)


if __name__ == "__main__":
    main()
