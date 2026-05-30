# Access Score — Complete Technical Specification
> Full end-to-end process for computing the access score.
> Uses partner's classification framework (bucket only).
> Layer 2 (within-bucket score) is intentionally excluded — not in current scope.

---

## 1. What the Access Score Is

A bucket value from the set {0, 15, 25, 50, 75, 100} that answers:
**how restrictive is this payer's PA policy compared to what the FDA actually requires?**

| Bucket | Label |
|---|---|
| 0 | Near Impossible Access |
| 15 | Very Restrictive Access |
| 25 | Restrictive Access |
| 50 | FDA Parity |
| 75 | Better Than FDA |
| 100 | Best Access |

**Note on within-bucket scoring:**
The problem statement asks for a continuous 0–100 score. A within-bucket deduction layer exists in our design but is out of scope for now. Current output will be one of the six bucket values above. This will be revisited after extraction pipeline is confirmed working.

---

## 2. Architecture — Single Layer

```
INPUT: 12 extracted params (from extraction pipeline)
    ↓
FDA COMPARISON
    Compare each of 6 params against FDA baseline
    Each comparison → MORE_RESTRICTIVE / EQUIVALENT / LESS_RESTRICTIVE / UNKNOWN
    ↓
SEVERITY AGGREGATION
    Count Severe / Major / Moderate / Minor / Improvement
    ↓
BUCKET ASSIGNMENT
    Deterministic rules → one of {0, 15, 25, 50, 75, 100}
    ↓
OUTPUT: bucket, restriction_summary, reasoning
```

Zero LLM calls. Pure Python. Fully deterministic.

---

## 3. FDA Baseline — Where It Comes From

### 3.1 The openFDA API

FDA baseline values are fetched from the public openFDA drug label API at pipeline startup.
Free. No authentication required.

**⚠️ IMPORTANT FOR DEVELOPER — DO THIS BEFORE WRITING ANY CODE:**

Make a raw API request manually and read the actual response before writing any parsing logic.

```bash
# Run this first — read the full JSON output carefully
curl "https://api.fda.gov/drug/label.json?search=openfda.brand_name:\"TREMFYA\"&limit=1"

# Also for STELARA
curl "https://api.fda.gov/drug/label.json?search=openfda.brand_name:\"STELARA\"&limit=1"
```

The response is large (~200KB). Before writing the parser, specifically check:

1. All clinical content fields return as **arrays of strings**, not plain strings — always access `[0]`
2. `effective_time` is formatted as `YYYYMMDD` not `YYYY-MM-DD`
3. `indications_and_usage` contains everything in one large text block — you will need regex to extract specific values
4. `recent_major_changes` tells you which sections changed and when — check this for TREMFYA's pediatric age update
5. Many sections are duplicated as `*_table` fields containing raw HTML — ignore those

Do not assume the structure. Read the actual response first. Then design the parser around what you see.

### 3.2 Which API Fields We Use

| API Field | What We Extract | Used For |
|---|---|---|
| `indications_and_usage` | Min age, weight condition, indication severity | FDA baseline P1 |
| `dosage_and_administration` §2.1 | TB evaluation recommendation | FDA baseline P6 nuance |
| `warnings_and_cautions` §5.3 | Confirms TB is clinical monitoring not PA gate | FDA baseline P6 |
| `pediatric_use` | Age and weight confirmation | FDA baseline P1 cross-check |
| `effective_time` | Label effective date | Traceability |
| `recent_major_changes` | Date of pediatric expansion | TREMFYA age date issue |
| `openfda.brand_name` | Drug name validation | Confirm correct drug queried |
| `openfda.generic_name` | INN name | INN_TO_BRAND mapping |
| `openfda.pharm_class_epc` | Drug class | Step classification context |

### 3.3 Fields to Ignore Completely

Do NOT use these — not relevant to any param:
- `adverse_reactions` / `adverse_reactions_table`
- `clinical_studies` / `clinical_studies_table` (largest section — ~40% of response)
- `pharmacokinetics` / `pharmacodynamics`
- `dosage_forms_and_strengths`
- `instructions_for_use`
- `spl_medguide`
- `package_label_principal_display_panel`
- All `*_table` fields (raw HTML duplicates)

### 3.4 FDA Baseline Dict Structure

After parsing the API response, populate this:

```python
FDA_BASELINE = {
    "TREMFYA": {
        "min_age": 6,
        "min_weight_kg": 40,
        "indication_severity": "moderate-to-severe",
        "brand_steps": 0,
        "generic_steps": 0,
        "phototherapy_required": False,
        "tb_test_required_as_pa_gate": False,
        "tb_evaluation_recommended": True,
        "specialist_restriction": None,
        "label_effective_date": "2025-09-29",
        "inn": "guselkumab",
        "pharm_class": "Interleukin-23 Antagonist",
    },
    "STELARA": {
        "min_age": 6,
        "min_weight_kg": None,        # verify from API — STELARA may have no weight condition
        "indication_severity": "moderate-to-severe",
        "brand_steps": 0,
        "generic_steps": 0,
        "phototherapy_required": False,
        "tb_test_required_as_pa_gate": False,
        "tb_evaluation_recommended": True,
        "specialist_restriction": None,
        "label_effective_date": None,  # populate from API
        "inn": "ustekinumab",
        "pharm_class": "Interleukin-12/23 Antagonist",
    }
}
```

**⚠️ TREMFYA DATE ISSUE — CONFIRMED DECISION:**
TREMFYA's pediatric approval (≥6, ≥40kg) was granted September 2025.
Original 2017 approval was adults ≥18 only.
Policies written before September 2025 legitimately used ≥18.

**We compare against the CURRENT FDA label (≥6) regardless of policy creation date.**
Document this decision explicitly in the submission.

### 3.5 For Other Brands

The Submissions tab has ~14 brands beyond TREMFYA and STELARA.
Query the openFDA API for each at startup using the brand name.

If FDA baseline cannot be fetched:
- Log warning in debug file
- Set all baseline values to None → all comparisons return UNKNOWN
- UNKNOWN inputs → bucket defaults to 25
- Flag row for manual review

---

## 4. The Six Parameter Comparisons

Only these 6 params participate in access scoring.
P2, P7, P8, P9, P10, P12 do not contribute to bucket assignment.

| Code | Parameter |
|---|---|
| P1 | Age |
| P3 | Brand Steps |
| P4 | Generic Steps |
| P5 | Phototherapy |
| P6 | TB Test |
| P11 | Specialist Types |

Every comparison returns exactly one state plus severity (when MORE_RESTRICTIVE):

```
MORE_RESTRICTIVE  → with severity: MINOR / MODERATE / MAJOR / SEVERE
EQUIVALENT
LESS_RESTRICTIVE
UNKNOWN
```

UNKNOWN contributes to nothing. Ignored during bucket assignment.

---

### 4.1 Age Comparison

```python
def compare_age(fda_age, policy_age):
    if fda_age is None or policy_age is None:
        return "UNKNOWN", None

    fda_num = parse_age(fda_age)      # ">=6"  → 6
    policy_num = parse_age(policy_age) # ">=18" → 18

    if fda_num is None or policy_num is None:
        return "UNKNOWN", None

    delta = policy_num - fda_num

    if delta > 0:
        if delta <= 4:   severity = "MINOR"
        elif delta <= 9: severity = "MODERATE"
        else:            severity = "MAJOR"    # delta >= 10
        return "MORE_RESTRICTIVE", severity
    elif delta == 0:
        return "EQUIVALENT", None
    else:
        return "LESS_RESTRICTIVE", None
```

---

### 4.2 Brand Steps Comparison

```python
def compare_brand_steps(fda_steps, policy_steps):
    fda_n = 0 if str(fda_steps).upper() in ("NA", "NONE", "NULL", "") else int(fda_steps)
    policy_n = 0 if str(policy_steps).upper() in ("NA", "NONE", "NULL", "") else int(policy_steps)

    delta = policy_n - fda_n

    if delta > 0:
        if delta == 1:   severity = "MODERATE"
        elif delta == 2: severity = "MAJOR"
        else:            severity = "SEVERE"   # >= 3
        return "MORE_RESTRICTIVE", severity
    elif delta == 0:
        return "EQUIVALENT", None
    else:
        return "LESS_RESTRICTIVE", None
```

---

### 4.3 Generic Steps Comparison

```python
def compare_generic_steps(fda_steps, policy_steps):
    # Same logic as brand steps, different severity thresholds
    delta = policy_n - fda_n

    if delta > 0:
        if delta == 1:   severity = "MINOR"
        elif delta == 2: severity = "MODERATE"
        else:            severity = "MAJOR"    # >= 3
        return "MORE_RESTRICTIVE", severity
    elif delta == 0:
        return "EQUIVALENT", None
    else:
        return "LESS_RESTRICTIVE", None
```

---

### 4.4 Phototherapy Comparison

```python
def compare_phototherapy(fda_val, policy_val):
    if fda_val is None or policy_val is None:
        return "UNKNOWN", None

    fda_yes    = str(fda_val).lower() == "yes"
    policy_yes = str(policy_val).lower() == "yes"

    if not fda_yes and policy_yes:
        return "MORE_RESTRICTIVE", "MODERATE"
    elif fda_yes and not policy_yes:
        return "LESS_RESTRICTIVE", None
    else:
        return "EQUIVALENT", None
```

---

### 4.5 TB Test Comparison

```python
def compare_tb_test(fda_val, policy_val):
    fda_yes    = str(fda_val).lower() == "yes"
    policy_yes = str(policy_val).lower() == "yes"

    if not fda_yes and policy_yes:
        return "MORE_RESTRICTIVE", "MINOR"
    elif fda_yes and not policy_yes:
        return "LESS_RESTRICTIVE", None
    else:
        return "EQUIVALENT", None
```

---

### 4.6 Specialist Comparison

```python
def compare_specialist(fda_val, policy_val):
    # Only presence vs absence matters
    # Specific type (Dermatologist vs Rheumatologist) treated equally
    NULL_VALS = ("NA", "NONE", "NULL", "")
    fda_has    = fda_val is not None and str(fda_val).upper() not in NULL_VALS
    policy_has = policy_val is not None and str(policy_val).upper() not in NULL_VALS

    if not fda_has and policy_has:
        return "MORE_RESTRICTIVE", "MINOR"
    elif fda_has and not policy_has:
        return "LESS_RESTRICTIVE", None
    else:
        return "EQUIVALENT", None
```

---

## 5. Severity Aggregation

```python
def aggregate_counts(comparisons: dict) -> dict:
    counts = {
        "severe": 0, "major": 0, "moderate": 0,
        "minor": 0, "improvement": 0, "unknown": 0
    }

    for param, (state, severity) in comparisons.items():
        if state == "MORE_RESTRICTIVE":
            counts[severity.lower()] += 1
        elif state == "LESS_RESTRICTIVE":
            counts["improvement"] += 1
        elif state == "UNKNOWN":
            counts["unknown"] += 1

    return counts
```

---

## 6. Bucket Assignment

Check rules in this exact order. First match wins. Stop immediately.

```python
def assign_bucket(counts: dict) -> int:
    severe      = counts["severe"]
    major       = counts["major"]
    moderate    = counts["moderate"]
    minor       = counts["minor"]
    improvement = counts["improvement"]

    # BUCKET 0 — Near impossible access
    if (severe >= 1 and major >= 1) or major >= 3 or severe >= 2:
        return 0

    # BUCKET 15 — Very restrictive
    if major >= 2 or severe >= 1:
        return 15

    # BUCKET 25 — Restrictive
    if major == 1 or moderate >= 2 or minor >= 3:
        return 25

    # BUCKET 100 — Best access (check before 75)
    if (improvement >= 2 and severe == 0 and major == 0
            and moderate == 0 and minor == 0):
        return 100

    # BUCKET 75 — Better than FDA
    if improvement >= 1 and severe == 0 and major == 0:
        return 75

    # BUCKET 50 — FDA parity
    if severe == 0 and major == 0 and moderate == 0 and minor <= 1:
        return 50

    # Fallback — should not reach here if logic is complete
    return 25
```

---

## 7. Oregon Medicaid Validation Case

Must pass before running any rows.

```
Extracted params:
  Age:          >=18  (FDA >=6)
  Brand steps:  1     (FDA 0)
  Generic steps:3     (FDA 0)
  Phototherapy: Yes   (FDA No)
  TB test:      Yes   (FDA No)
  Specialist:   NA    (FDA None)

Comparisons:
  Age:          delta=12 → MORE_RESTRICTIVE, MAJOR
  Brand steps:  delta=+1 → MORE_RESTRICTIVE, MODERATE
  Generic steps:delta=+3 → MORE_RESTRICTIVE, MAJOR
  Phototherapy:          → MORE_RESTRICTIVE, MODERATE
  TB test:               → MORE_RESTRICTIVE, MINOR
  Specialist:            → EQUIVALENT

Counts:
  Severe   = 0
  Major    = 2  (age + generic steps)
  Moderate = 2  (brand steps + phototherapy)
  Minor    = 1  (TB test)
  Improvement = 0

Bucket check:
  Bucket 0?   severe>=1 AND major>=1 → NO
              major>=3               → 2>=3? NO
              severe>=2              → NO
  Bucket 15?  major>=2               → 2>=2? YES ✅

RESULT: Bucket 15 ✓
```

If this returns anything other than 15 — the implementation has a bug.

---

## 8. Master Function

```python
def compute_access_score(params: dict, drug: str) -> dict:
    """
    Master access score function.
    Returns bucket and full reasoning. No LLM calls. Pure Python.
    """
    fda = FDA_BASELINE.get(drug.upper())

    if not fda:
        return {
            "bucket": 25,
            "score": 25,
            "reasoning": [f"FDA baseline not found for {drug} — defaulting to Bucket 25"],
            "warning": "FDA_BASELINE_MISSING"
        }

    comparisons = {
        "age":           compare_age(fda["min_age"], params.get("age")),
        "brand_steps":   compare_brand_steps(fda["brand_steps"], params.get("steps_brands")),
        "generic_steps": compare_generic_steps(fda["generic_steps"], params.get("steps_generic")),
        "phototherapy":  compare_phototherapy(fda["phototherapy_required"], params.get("step_phototherapy")),
        "tb_test":       compare_tb_test(fda["tb_test_required_as_pa_gate"], params.get("tb_test_required")),
        "specialist":    compare_specialist(fda["specialist_restriction"], params.get("specialist_types")),
    }

    counts = aggregate_counts(comparisons)
    bucket = assign_bucket(counts)

    return {
        "bucket": bucket,
        "score": bucket,   # score = bucket until Layer 2 is added
        "restriction_summary": counts,
        "comparisons": {
            k: {"state": v[0], "severity": v[1]}
            for k, v in comparisons.items()
        },
        "reasoning": build_reasoning(comparisons, counts, bucket)
    }


def build_reasoning(comparisons, counts, bucket) -> list:
    lines = []
    for param, (state, severity) in comparisons.items():
        if state == "MORE_RESTRICTIVE":
            lines.append(f"{param}: MORE_RESTRICTIVE ({severity})")
        elif state == "LESS_RESTRICTIVE":
            lines.append(f"{param}: LESS_RESTRICTIVE (improvement)")
        elif state == "UNKNOWN":
            lines.append(f"{param}: UNKNOWN (null — ignored)")
    lines.append(
        f"Bucket: {bucket} | "
        f"Severe={counts['severe']}, Major={counts['major']}, "
        f"Moderate={counts['moderate']}, Minor={counts['minor']}, "
        f"Improvements={counts['improvement']}"
    )
    return lines
```

---

## 9. FDA API Integration

```python
import requests

FDA_API_BASE = "https://api.fda.gov/drug/label.json"


def fetch_fda_baseline(brand_name: str) -> dict | None:
    """
    Fetch FDA label for a brand from openFDA API.
    Parse and return structured baseline dict.

    ⚠️ DEVELOPER — DO THIS FIRST:
    Run the curl command manually for TREMFYA and STELARA.
    Read the raw response before implementing this function.
    Specifically check:
      - Field names and nesting (fields are arrays, always use [0])
      - effective_time format (YYYYMMDD)
      - Where age, weight, severity appear in indications_and_usage text
      - What recent_major_changes looks like for TREMFYA
    Only then implement parse_fda_label() below.
    """
    try:
        resp = requests.get(
            FDA_API_BASE,
            params={"search": f'openfda.brand_name:"{brand_name}"', "limit": 1},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        if data["meta"]["results"]["total"] == 0:
            log_debug({"event": "fda_api_no_result", "drug": brand_name})
            return None

        return parse_fda_label(data["results"][0], brand_name)

    except Exception as e:
        log_debug({"event": "fda_api_error", "drug": brand_name, "error": str(e)})
        return None


def parse_fda_label(label: dict, brand_name: str) -> dict:
    """
    Extract baseline fields from raw FDA label.

    ⚠️ DEVELOPER NOTE:
    Implement this AFTER reading the raw API response manually.
    The field names, nesting, and text format must be verified from
    the actual response — do not implement blindly from assumptions.

    Key fields to parse (verified from TREMFYA response):
      label["indications_and_usage"][0]      → text block
      label["effective_time"]                → "20250929"
      label["openfda"]["generic_name"][0]    → "GUSELKUMAB"
      label["openfda"]["pharm_class_epc"][0] → "Interleukin-23 Antagonist [EPC]"
    """
    import re

    indications = label.get("indications_and_usage", [""])[0]
    eff_time    = label.get("effective_time", "")

    # Parse effective date: YYYYMMDD → YYYY-MM-DD
    eff_date = None
    if len(eff_time) == 8:
        eff_date = f"{eff_time[:4]}-{eff_time[4:6]}-{eff_time[6:]}"

    # Parse min age
    age_match = re.search(r'(\d+)\s+years?\s+of\s+age\s+and\s+older', indications)
    min_age = int(age_match.group(1)) if age_match else None

    # Parse weight condition
    weight_match = re.search(r'weigh\s+at\s+least\s+(\d+)\s*kg', indications)
    min_weight = int(weight_match.group(1)) if weight_match else None

    # Parse severity
    severity = "moderate-to-severe" if "moderate-to-severe" in indications.lower() else None

    return {
        "min_age":                     min_age,
        "min_weight_kg":               min_weight,
        "indication_severity":         severity,
        "brand_steps":                 0,
        "generic_steps":               0,
        "phototherapy_required":       False,
        "tb_test_required_as_pa_gate": False,
        "tb_evaluation_recommended":   True,
        "specialist_restriction":      None,
        "label_effective_date":        eff_date,
        "inn":  label.get("openfda", {}).get("generic_name",    [None])[0],
        "pharm_class": label.get("openfda", {}).get("pharm_class_epc", [None])[0],
    }


def load_all_fda_baselines(brands: list[str]) -> dict:
    """
    Load FDA baselines for all unique brands in Submissions tab.
    Caches to disk — skip API calls on second run.
    """
    cache_path = Path("fda_baselines_cache.json")

    if cache_path.exists():
        with open(cache_path) as f:
            return json.load(f)

    baselines = {}
    for brand in set(brands):
        baseline = fetch_fda_baseline(brand)
        if baseline:
            baselines[brand.upper()] = baseline
        else:
            log_debug({"event": "fda_baseline_missing", "drug": brand})

    with open(cache_path, "w") as f:
        json.dump(baselines, f, indent=2)

    return baselines
```

---

## 10. Helper Functions

```python
def parse_age(age_str) -> int | None:
    """Extract numeric age from '>=18' format."""
    import re
    if not age_str or str(age_str).upper() in ("NA", "NO", "NONE", "NULL", "FDA APPROVED AGE"):
        return None
    match = re.search(r'\d+', str(age_str))
    return int(match.group()) if match else None
```

---

## 11. Where Access Score Fits in pipeline.py

```
STARTUP
    load_drug_classifications()     Block 1
    load_submissions()              Block 1
    load_all_fda_baselines()        ← NEW — call at startup, cache to disk

FOR EACH ROW
    ingest_pdf()                    Block 2
    split_sections()                Block 3
    extract_simple_params()         Block 5 Pass 1
    extract_step_therapy_text()     Block 5 Pass 2
    extract_step_counts()           Block 5 Pass 3
    validate_all()                  Block 6
    compute_access_score()          Block 7 ← runs here, pure Python, 0 LLM calls
    format_row()                    Block 9

SAVE result.csv
```

`compute_access_score()` receives the validated params dict and drug name.
Returns `{"bucket": N, "score": N, "reasoning": [...]}`.
`score` equals `bucket` until Layer 2 within-bucket scoring is added later.

---

## 12. Known Limitations — Document in Submission

1. **Score = Bucket value.** Current output is one of {0, 15, 25, 50, 75, 100}. 

3. **Bucket 75 and 100 are near-empty.** For TREMFYA and STELARA, FDA already requires nothing (zero steps, no phototherapy, no TB test, no specialist). There is almost no room for a payer to be LESS_RESTRICTIVE. Expect ≤2 rows in these buckets.

4. **P7, P8, P9, P10, P12 excluded from scoring.** Auth duration, reauth requirements, and quantity limits do not influence bucket assignment in the current framework. Deliberate design decision.

---

## 13. Validation Checklist Before Final Run

```
□ Manual curl test done — TREMFYA and STELARA API responses read carefully
□ parse_fda_label() tested against real API response (not assumed structure)
□ FDA_BASELINE populated for all brands in Submissions tab
□ Oregon Medicaid TREMFYA → Bucket 15 (validation case passes)
□ All six comparison functions tested with known inputs
□ Bucket assignment logic tested for all six buckets with synthetic data
□ compute_access_score() returns dict with bucket, score, reasoning
□ result.csv Access Score column populated — not blank
□ No LLM calls in scoring pipeline — confirm zero API calls during scoring
```
