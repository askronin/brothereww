# Parameter Constraints & Rules
> Every rule and constraint applied per parameter.
> Written for partner confirmation — verify each rule before final submission.
> Source for each rule noted. Flagged items need explicit confirmation.

---

## §A. Sentinel contract — the single source of truth

Locked 29 May to eliminate the No / NA / "0" / Unspecified inconsistencies across earlier drafts. Validation rules enforce this — any param that drifts outside the contract is auto-corrected by `rule_*_format()` and flagged.

| Param | Format when present | Sentinel when missing | Notes |
|---|---|---|---|
| Age | `>=N` (e.g. `>=18`, `>=6`) or `FDA approved age` | `NA` | Partner-confirmed. Earlier `No` convention retired. |
| Step Therapy Requirements | verbatim text from policy | `NA` | Both universal + indication-specific, concatenated |
| Number of Steps through Brands | integer string `"1"`, `"2"` | `NA` | Not `"0"`. Not numeric. |
| Number of Steps through Generic | integer string | `NA` | Not `"0"`. Not numeric. |
| Step through-Phototherapy | `Yes` / `No` | `NA` | `NA` only when step therapy itself is `NA` |
| TB Test required | `Yes` / `No` | `No` | Binary — absence means not required |
| Initial Authorization Duration (in-months) | integer string (`"6"`, `"12"`) | `Unspecified` | PA is always required in this dataset, so never blank |
| Reauthorization Duration (in-months) | integer string | `NA` if no reauth; `Unspecified` if reauth required but duration unstated | |
| Reauthorization Required | `Yes` / `No` (derived) | derived — never blank | `Yes` iff `reauth_duration` or `reauth_requirements` non-NA |
| Reauthorization Requirements | verbatim text | `NA` | |
| Specialist Types | comma + space separated (`"Dermatologist, Rheumatologist"`) | `NA` | Set-equality after lowercase for validation |
| Quantity Limits | verbatim text labelled "quantity limit" | `NA` | Strict label rule — reject `dosage`, `dosing limit`, etc. |
| Access Score | *(Phase 2 — deferred)* | empty cell | Column exists, cells blank in Phase 1 |

**Why this matters.** Earlier drafts of these docs had Age → `No` in one place, `NA` in another. Quantity Limits, Phototherapy, durations had similar drift. Codex review (29 May) flagged this as dangerous because hidden labels in the ground-truth tab used `No` in 337 rows. Partner override locked `NA` for Age; consistency for everything else is per the table above. No exceptions.

---

## How to Use This File

Each parameter has:
- **Output format** — exact format expected in result.csv
- **Extraction rule** — how to get the value from the policy
- **Edge cases** — known special situations and how to handle them
- **Validation** — what checks run after extraction
- **Flags** — anything that needs partner confirmation

---

## Param 1 — Age

**Output format:** `>=N` (e.g. `>=18`, `>=6`, `>=4`) or `"FDA approved age"` or `"NA"`

**Extraction rule:**
- Read the minimum age threshold stated for the target drug and the target indication (Plaque Psoriasis)
- Convert to `>=N` format regardless of how the policy phrases it
  - "18 years of age or older" → `>=18`
  - "Member is 6 years or older" → `>=6`
  - "at least 4 years of age" → `>=4`

**Edge cases:**
| Situation | Output | Source |
|---|---|---|
| Policy says "FDA approved age" / "per FDA labeling" without a number | `FDA approved age` | Additional Extracted Data ground truth |
| Age not mentioned in policy | `NA` | Partner confirmed (§A sentinel contract) |
| Two different age groups for different drugs in same section | Capture YOUNGEST | Business Rules tab |
| Age stated only for other indication, not PsO | `NA` — only capture PsO-specific age | Business Rules tab + §A sentinel contract |

**Validation:** If extracted value is > 30 characters → flag as likely wrong format

**Confirmed:** Age not mentioned → `"NA"` per partner instruction.

---

## Param 2 — Step Therapy Requirements Documented in Policy

**Output format:** Verbatim text from policy (combined universal + indication-specific)

**Extraction rule:**
- Extract ALL step therapy language relevant to target drug for PsO
- Must capture from TWO sources and combine:
  1. Universal criteria (applies to all drugs in the section)
  2. Indication-specific criteria (specific to this drug or plaque psoriasis)
- Copy EXACT text — no paraphrasing, no summarising
- Preserve AND/OR connectors exactly as written

**Output when not found:** `"NA"`

**What counts as step therapy text:**
- Any statement requiring trial/failure of prior treatments
- Phototherapy requirements (include here AND in Param 5)
- Class-level requirements (e.g. "Non-Preferred CAM Antagonists require...") — include if target drug belongs to that class

**What does NOT go here:**
- General eligibility criteria (TB test, age, diagnosis confirmation)
- Quantity limits
- Reauthorisation criteria

**Output in result.csv:** Clean policy text only — NO construction labels like `[UNIVERSAL CRITERIA]`

**Validation:** Verbatim presence check — extracted text must exist in source document

---

## Param 3 — Number of Steps through Brands

**Output format:** Integer as string (`"1"`, `"2"`) or `"NA"` if none

**Counting rules:**
1. Merge universal criteria AND indication-specific criteria (AND relationship — both required)
2. Within merged set, resolve OR conditions → take LEAST RESTRICTIVE path (fewest branded steps)
3. Count branded/biologic/targeted synthetic steps on resolved path
4. Phototherapy is NEVER counted here (it goes to Param 5 only)

**What counts as a branded step:**
- Any drug in the PsO market basket EXCEPT known generics (Acitretin, Cyclosporine, Methotrexate, Vtama, Zoryve)
- Any drug described as "biologic", "targeted synthetic", "biologic immunomodulator"
- "Previously received a biologic or targeted synthetic" = 1 branded step
- Drug class reference that the target drug belongs to (e.g. "CAM antagonist" = branded step)
- "Humira OR Enbrel" = 1 branded step (OR within the step, not between steps)

**Output:**
- 0 branded steps → `"NA"` (not `"0"`)
- No step therapy at all → `"NA"`

**Validation:** If step therapy text is non-empty but this = NA → warning → rerun

---

## Param 4 — Number of Steps through Generic

**Output format:** Integer as string (`"1"`, `"2"`, `"3"`) or `"NA"` if none

**Counting rules:** Same AND/OR merge logic as Param 3 (same combined step set)

**What counts as a generic step:**
- Acitretin, Cyclosporine, Methotrexate, Vtama, Zoryve
- Any topical corticosteroid (betamethasone, clobetasol, fluocinonide, triamcinolone, etc.)
- Any non-biologic systemic (sulfasalazine, leflunomide, hydroxychloroquine)
- Other topicals: calcipotriene, tazarotene, anthralin
- Any step with no biologic/targeted specification → defaults to generic
- NSAIDs when required as a step

**Output:**
- 0 generic steps → `"NA"` (not `"0"`)
- No step therapy at all → `"NA"`

**Validation:** Cross-check with Param 2 — if step text mentions only biologics, generic count should be NA or 0 (→ NA)

---

## Param 5 — Step through Phototherapy

**Output format:** `"Yes"` | `"No"` | `"NA"`

**Rules:**
| Situation | Output |
|---|---|
| Phototherapy is a mandatory AND condition (not in any OR) | `Yes` |
| Phototherapy appears only as an OR option | `No` |
| No step therapy criteria at all | `NA` |
| Phototherapy mentioned in text but only as general eligibility | `No` |

**What counts as phototherapy:** phototherapy, PUVA, UVB, narrowband UVB, UVA, psoralen ultraviolet, light therapy, photochemotherapy

**NOT counted in Param 3 or Param 4** — phototherapy is its own separate param.

---

## Param 6 — TB Test Required

**Output format:** `"Yes"` | `"No"`

**Extraction rule:**
- Look in BOTH universal criteria AND drug-specific criteria
- Universal TB requirement (e.g. Oregon Medicaid Step 4) applies even if not repeated in drug section
- "Member has been screened for tuberculosis" = Yes
- "TB screening required" = Yes
- If no TB requirement mentioned anywhere = No

**Note:** TB test is NOT a bucket trigger for access score. It is a within-bucket deduction modifier (-1 point).

---

## Param 7 — Initial Authorization Duration (in months)

**Output format:** Plain number string (`"6"`, `"12"`) or `"Unspecified"`

**Extraction rule:**
- Find how long the initial approval lasts
- Convert to number of months: "6 months" → `"6"`, "one year" → `"12"`, "up to 6 months" → `"6"`
- If global duration stated (applies to all drugs, e.g. Aetna "Initial Approval: 6 months") → use it
- If PA is required for PsO but duration not stated → `"Unspecified"`
- Never leave blank if PA is required

**Validation rule:** If duration is blank or `"NA"` input → force to `"Unspecified"` (business rule)

---

## Param 8 — Reauthorization Duration (in months)

**Output format:** Plain number string (`"6"`, `"12"`) or `"Unspecified"` if required, or `"NA"` if not mentioned

**Extraction rule:**
- Output `"NA"` if not mentioned at all
- Find how long renewal approvals last
- Same number conversion as Param 7
- If reauth is required but duration not stated → `"Unspecified"`
- If no reauth mentioned → `"NA"`

**Dependency:** If this is non-`"NA"` → automatically sets Param 9 (Reauth Required) = Yes

---

## Param 9 — Reauthorization Required

**Output format:** `"Yes"` | `"No"`

**⚠️ FULLY DERIVED — DO NOT EXTRACT FROM LLM**

This is 100% computed from other params. Never ask the LLM for this.

**Rule:**
```
IF reauth_duration (Param 8) is non-"NA"
  OR reauth_requirements (Param 10) is non-"NA"
THEN reauth_required = "Yes"
ELSE reauth_required = "No"
```

---

## Param 10 — Reauthorization Requirements Documented in Policy

**Output format:** Verbatim text from policy (or `"NA"` if not mentioned)

**Extraction rule:**
- Find continuation criteria — what the patient must demonstrate to get renewal
- Copy EXACT text — verbatim
- Common examples:
  - "Documentation indicating member has shown improvement in signs and symptoms of disease"
  - "Patient has had clinical improvement (slowing of disease progression...)"
  - "Continued clinical benefit documented"

**Dependency:** If non-`"NA"` → automatically sets Param 9 = Yes

**Validation:** Verbatim presence check — text must appear in source document

---

## Param 11 — Specialist Types

**Output format:** Comma-separated list of specialties (e.g. `"Dermatologist, Rheumatologist"`) or `"NA"`

**Extraction rule:**
- List ALL specialties that the policy allows to prescribe or manage this drug for PsO
- Only capture if the policy explicitly names specialties
- If policy says "appropriate specialist" without naming one → `"NA"`
- If no specialist requirement mentioned → `"NA"`

**Note:** Specialist restriction is a within-bucket deduction modifier for access score, not a bucket trigger.

---

## Param 12 — Quantity Limits

**Output format:** Verbatim text of the quantity limit (or `"NA"`)

**⚠️ STRICT LABEL RULE:**
- ONLY capture if the policy uses the exact phrase "quantity limit" or "quantity limits" as a label or header
- REJECT if labelled: "dosage", "dosing limit", "dosing information", "recommended dose", "administration", "dosing and administration"
- REJECT the generic statement "Quantity limits exist" with no specifics — must have drug-specific detail

**What valid quantity limit text looks like:**
- "Stelara 45mg/0.5mL: 1 vial per 84 days; 90mg/mL: 1 syringe per 56 days"
- Drug name + strength + quantity + days supply

**Validation rule:** `rule_quantity_limits_strict()` checks for reject terms and removes false positives

---

## Param 13 — Access Score

**⚠️ DEFERRED TO PHASE 2.** Not built in this extraction phase. During Phase 1 the column exists in `result.csv` with empty values. The spec below is the design we will implement once extraction is validated.

**Output format:** Integer 0–100

**Bucket classification (deterministic):**
| Score | Condition |
|---|---|
| 0 | Drug not covered / criteria impossible to meet |
| 25 | ANY step therapy: branded ≥1 OR generic ≥1 OR phototherapy = Yes |
| 50 | Zero step therapy, PA required, criteria match FDA label |
| 75 | Criteria less restrictive than FDA label (rare) |
| 100 | No PA required for this drug+indication |

**Within-bucket deductions (from bucket ceiling):**

| Factor | Deduction | Basis |
|---|---|---|
| Each generic step | −3 pts | Clinical: weeks-months delay per step |
| Each branded step | −4 pts | Clinical: 3+ month trial, injection burden |
| Mandatory phototherapy | −3 pts | Clinical: 10–15 weeks, 2–3x/week visits |
| Age restriction vs FDA | −2 pts | Excludes FDA-approved population |
| TB test required | −1 pt | Administrative delay |
| Initial auth ≤6 months | −1 pt | More frequent resubmissions |
| Strict reauth criteria | −0.5 pt | Ongoing access risk |

**FDA baseline (for bucket classification):**
- TREMFYA: ≥18 (original), ≥6 (from Sept 2025). Zero step therapy required.
- STELARA: ≥6. Zero step therapy required.

**⚠️ Phase 2 obligations — resolve in the access-score module:**
- **FDA-baseline coverage.** Currently only TREMFYA and STELARA have defined FDA labels above. The Submissions tab spans ~14 brands (ENBREL, AMJEVITA, OTEZLA, YESINTEK, COSENTYX, REMICADE, SILIQ, CIMZIA, BIMZELX, SKYRIZI, OTULFI, ILUMYA, ACITRETIN, etc.). Expand baseline data for all in-scope brands before scoring.
- **Bucket date semantics.** Should bucket be assigned relative to current FDA label or label-at-policy-date? TREMFYA's ≥6 pediatric extension (Sept 2025) postdates most policies in this dataset. Pick one and document.
- **Bucket 0 for drug-not-covered rows.** Rows tagged `CRITICAL_DRUG_NOT_FOUND` by extraction (e.g. `287728-4459856.pdf` is a hematologic policy with no STELARA/PsO content) should map to Bucket 0 in scoring, not be left blank.
- **Within-bucket weights.** Hand-designed; no published source. Document as assumptions in submission README.

---

## Summary — Output Value Quick Reference

| Param | When found | When not found | Format |
|---|---|---|---|
| Age | `>=N` or `FDA approved age` | `NA` | Standardised (§A) |
| Step therapy text | verbatim text | `NA` | Verbatim |
| Steps brands | `"1"`, `"2"`, etc. | `"NA"` | Number string |
| Steps generic | `"1"`, `"2"`, etc. | `"NA"` | Number string |
| Phototherapy | `Yes` | `No` or `NA` | Enum |
| TB test | `Yes` or `No` | `No` | Enum |
| Initial auth | `"6"`, `"12"` etc. | `"Unspecified"` | Number string |
| Reauth duration | `"6"`, `"12"` etc. | `"Unspecified"` if required, `"NA"` if not mentioned | Number string |
| Reauth required | `Yes` or `No` | Derived — never blank | Enum (derived) |
| Reauth requirements | verbatim text | `"NA"` | Verbatim |
| Specialist types | comma-separated | `"NA"` | List or NA |
| Quantity limits | verbatim text | `"NA"` | Verbatim (strict) |
| Access score | *(deferred — empty in Phase 1)* | *(deferred — empty in Phase 1)* | Integer (Phase 2) |
