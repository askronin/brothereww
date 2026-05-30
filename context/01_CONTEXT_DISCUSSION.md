# Context: Full Discussion Summary
> Everything reasoned, decided, and discovered across our pre-build discussion.
> Use this file to understand WHY decisions were made, not just what they are.

---

## 1. What This Project Actually Is

A GenAI pipeline that:
- Reads payer Prior Authorization (PA) policy PDF documents
- Extracts 12 structured parameters per drug+indication combination
- Computes an Access Quality Score (0–100) per row
- Outputs a `result.csv` matching a predefined submission format

Target indication: **Plaque Psoriasis (PsO)** — all 79 rows.
Brands in the Submissions tab: TREMFYA and STELARA are the focal brands per the problem statement, but the actual 79 rows span ~14 brands including ENBREL, AMJEVITA, OTEZLA, YESINTEK, COSENTYX, REMICADE, SILIQ, CIMZIA, BIMZELX, SKYRIZI, OTULFI, ILUMYA, and the generic ACITRETIN.
Total rows to fill: **79 rows** in the Submissions tab.

**Phase scope:** the **extraction pipeline is brand-agnostic** — Pass 1/2/3 prompts substitute `{drug}` dynamically, classification comes from the XLSX-loaded `BRANDED_DRUGS` / `GENERIC_DRUGS` sets, no brand is hard-coded in logic. The **FDA-label baseline** used for Access Score (Phase 2) only covers TREMFYA and STELARA today; expansion to the other ~12 brands is a Phase 2 task and is owned by the access-score module, not the extraction phase.

---

## 2. Dataset Format — Audited

All 70 files in `Sample_PsO_ADS_Track/` are real, born-digital PDFs (magic bytes `%PDF-1.4`/`1.6`/`1.7`). Text-extractability audit using `pdftotext`: every file produced ≥600 chars/page (mean ~2200, max ~6000). **Zero scanned-only PDFs. Zero ZIPs. Zero renamed text files.**

**Implication:** Single ingestion path. PyMuPDF (`fitz`) does text extraction per page. Vision is a defensive fallback only — invoked per-page when extracted text is suspiciously sparse (< 100 chars on a page with non-trivial dimensions). On this dataset that fallback is expected to fire rarely if at all, but the pipeline must support it because the production dataset (judges' test set) may include scanned policies we haven't audited.

**Earlier assumption (now retired):** Three ingestion paths covering plain-text-renamed-as-pdf and ZIP archives. This was based on incorrect sample files from a different folder. The real `Sample_PsO_ADS_Track/` files are all standard PDFs.

---

## 3. Document Structure Reality

**Oregon Medicaid (66156-4274314.pdf)** — 441 pages covering every drug class.

Key structural insight discovered:
- **Universal criteria** (TB test, severity requirement, diagnosis funding check) apply to ALL drugs in the "Targeted Immune Modulators for Autoimmune Conditions" section — they appear ONCE, not repeated per drug
- **Drug-specific criteria** appear in numbered approval steps (e.g., Step 10 = plaque psoriasis gate, Step 11 = the actual step therapy requirements)
- **Renewal/reauth criteria** appear in a separate "Renewal Criteria" section at the end of the same block

This means: if you navigate to "TREMFYA section only," you WILL miss the universal criteria (TB test in Step 4, severity in Step 2). The business rules explicitly require combining universal AND drug-specific criteria. Navigation-only approach is wrong.

**Correct approach:** Split the document into labelled sections, then assemble [UNIVERSAL] + [DRUG-SPECIFIC] + [REAUTH] into one context block per extraction.

---

## 4. The 12 Parameters — Key Reasoning

> **Partner updates applied:** Age "No" vs "NA" conflict flagged (see below). Drug classification now loaded from XLSX. Validation reruns added for critical failures.


### Parameter Classification

**Hard extraction params (text lookup, low ambiguity):**
- Age (P1) — find the number, apply edge case rules
- TB Test (P6) — Yes/No
- Initial Auth Duration (P7) — number in months
- Reauth Duration (P8) — number in months
- Reauth Required (P9) — derived from P8 and P10
- Reauth Requirements (P10) — verbatim text
- Specialist Types (P11) — list of specialties
- Quantity Limits (P12) — verbatim, strict label check

**Medium complexity:**
- Step Therapy Text (P2) — verbatim extraction from TWO sources (universal + specific), must be combined

**Hard reasoning params (require CoT, not just extraction):**
- Number of Steps through Brands (P3)
- Number of Steps through Generic (P4)
- Step through Phototherapy (P5)

### Step Counting Logic — Full Detail

This is the hardest part of the entire pipeline. The logic:

```
1. Identify UNIVERSAL step criteria (apply to all brands in this policy)
2. Identify INDICATION-SPECIFIC step criteria (for moderate-to-severe PsO only)
3. Combine via AND (both layers must be satisfied — they are additive)
4. Within the combined set, resolve OR conditions → take LEAST RESTRICTIVE path (fewest steps)
5. From the resolved path:
   - Count branded/biologic steps → Param 3
   - Count generic/conventional steps → Param 4
   - Is phototherapy mandatory AND (not in OR)? → Param 5
6. Phototherapy is NEVER counted in Param 3 or Param 4 — it's separate
```

### Step Classification Rules

| Treatment Type | Counts As |
|---|---|
| Topical corticosteroids | Generic step |
| Other topicals (calcipotriene, tazarotene, anthralin) | Generic step |
| Conventional systemics (methotrexate, cyclosporine, acitretin) | Generic step |
| Branded biologics (Humira, Enbrel, Yesintek) | Branded step |
| Drug class reference (e.g., "CAM antagonists") | Branded if target drug belongs to that class |
| Phototherapy / PUVA | Phototherapy param only — not counted in brands or generics |
| "Previously received biologic or targeted synthetic drug" | Branded step |
| Unnamed steps with no biologic specification | Generic step (default) |

### Oregon Medicaid TREMFYA — Real Example

Step 11 in Oregon Medicaid is ALL AND conditions:
```
Topical corticosteroid (AND) Another topical (AND) Phototherapy (AND) Systemic MTX/CYC/acitretin (AND) Humira® or Enbrel® ≥3 months
```

Result:
- Branded steps: 1 (Humira/Enbrel)
- Generic steps: 3 (topical steroid + another topical + systemic)
- Phototherapy: Yes (mandatory AND — not in any OR)

No OR resolution needed because everything is AND. This was the easy case — real policies will have nested OR/AND combinations.

### Reference Tab Worked Example (Ground Truth)

Policy: Yesintek/Stelara  
Universal: must try/fail Yesintek (1 branded step) — AND  
Indication-specific: previously received biologic (1 branded) OR failed MTX/cyclosporine/acitretin (1 generic)  

Resolution: indication-specific has OR → take least restrictive path = 1 generic step  
Final: Universal branded (1) AND indication-specific generic (1) = **1 branded, 1 generic**  
Phototherapy: No — only appeared in an OR, not mandatory  

---

## 5. Access Score — Full Reasoning

### The 5 Anchor Buckets (from problem statement)

| Score | Meaning | Definition we derived |
|---|---|---|
| 0 | No access | Drug not covered, criteria impossible to meet |
| 25 | Restricted vs FDA | Any step therapy exists OR severe age restriction |
| 50 | Parity with FDA | PA required but zero step therapy, criteria match FDA label |
| 75 | Better than FDA | Criteria LESS restrictive than FDA label (rare) |
| 100 | Best possible | No PA required, open formulary |

### Bucket Classification Rules (Deterministic — Hard Triggers)

**Bucket 0** — if ANY:
- Drug not listed on formulary for this indication
- Drug explicitly excluded or non-covered
- Criteria require something impossible

**Bucket 25** — if ANY:
- Branded steps ≥ 1
- Generic steps ≥ 1
- Phototherapy = Yes (mandatory)

These are the ONLY hard triggers. Step therapy existence = automatic bucket 25. No override.

**Bucket 50** — if ALL:
- Branded steps = 0 (or NA)
- Generic steps = 0 (or NA)
- Phototherapy = No or NA
- Indication is covered

**Bucket 75** — if:
- Bucket 50 conditions met PLUS at least one param is explicitly LESS restrictive than FDA

**Bucket 100** — if:
- No PA required at all for this drug+indication

### What Does NOT Trigger Bucket 25 (Important — We Debated This)

Age restriction, TB test, auth duration, specialist restriction, reauth requirements are **NOT hard bucket triggers**. They are **within-bucket scoring modifiers**.

A policy with:
- Zero step therapy
- Age ≥25 vs FDA ≥18
- TB test required
- 6-month auth

→ This is **Bucket 50**, not Bucket 25. Age and TB lower the within-bucket score but don't change the bucket.

Exception: If age restriction is extreme (e.g., FDA ≥6, payer ≥18 — 12-year gap blocking a large FDA-approved population), this can be argued as Bucket 25. Apply judgment. Document the decision.

### Within-Bucket Scoring — Deduction Table (Approach A)

Start at bucket ceiling (e.g., 25 for restricted bucket), subtract deductions, floor at 1 within bucket.

| Factor | Deduction | Reasoning |
|---|---|---|
| Each generic step | −3 pts | Weeks-months delay per step |
| Each branded step | −4 pts | 3+ month trial, injection burden, monitoring |
| Mandatory phototherapy | −3 pts | 10–15 weeks, 2–3x/week clinic visits |
| Age restriction vs FDA | −2 pts | Excludes FDA-approved population |
| TB test required | −1 pt | Administrative delay only |
| Initial auth ≤6 months | −1 pt | Frequent resubmissions |
| Strict reauth criteria specified | −0.5 pt | Ongoing access risk |

**Applied to Oregon Medicaid TREMFYA:**
25 − (3×3) − (1×4) − 3 − 2 − 1 − 1 − 0.5 = 25 − 20.5 = **4.5 → floor to 5**

Note: These weights are designed by us — not sourced from external literature. No published rubric exists for this. They are defensible from clinical reasoning but should be documented as assumptions.

### Hybrid Approach E (Secondary)

Rule-based bucket classification (same as above) PLUS LLM reasons within the bucket range only.

Prompt structure: "This policy is in the restricted bucket (score 1–24). Given these 12 extracted parameters: [params]. Score between 1 and 24. Show reasoning step by step."

Use Approach A as primary output. Use Hybrid E as confidence check. If they agree within 5 points → high confidence. If they differ by 10+ points → flag for review.

### FDA Label Baselines (Used for Bucket Classification)

**TREMFYA (guselkumab):**
- Approved: moderate-to-severe plaque psoriasis
- Age: ≥18 (original approval), ≥6 added September 2025 (pediatric expansion)
- Step therapy required: None
- TB test required: Not required (monitoring recommended, not PA gate)

**STELARA (ustekinumab):**
- Approved: moderate-to-severe plaque psoriasis
- Age: ≥6 years
- Step therapy required: None
- TB test required: Not required as PA gate

**CRITICAL DATE ISSUE:** Oregon Medicaid policy is dated May 2024. TREMFYA pediatric expansion (≥6) was approved September 2025. Policy's ≥18 cutoff was CONSISTENT with FDA label at time of policy creation. Must decide: score against current FDA label or label-at-policy-date. This changes the score.

---

## 6. Validation Strategy

> **Important:** Additional Extracted Data tab (439 rows) discovered as additional ground truth beyond the single Reference tab example.
 — How to Know Extraction Is Good

Four layers, all necessary:

### Layer 1 — Reference Tab Smoke Test
Run pipeline on the exact policy in the Reference tab. Compare all 12 params to ground truth. Minimum expectation: 9/12 correct. If less — pipeline has fundamental issue.

### Layer 2 — Manual Spot Check (5 rows)
Before final submission run: manually read 5 PDFs and fill params by hand. Run pipeline on same 5. Compare. Target: 90%+ agreement. This is the only real validation.

### Layer 3 — Cross-Param Consistency Checks (Automated Python)
```
Step text non-empty but counts = NA → Pass 2 found steps, Pass 3 missed
Reauth required = No but duration specified → dependency rule failed
Branded step ≥ 1 but step text blank → extraction disagreement
Age is a sentence not a number/code → extraction format wrong
Quantity limit contains word "dosage" → validation rule failed
```

### Layer 4 — Verbatim Presence Check (Automated Python)
```python
def verbatim_check(extracted_value, source_text):
    return extracted_value.strip() in source_text
```
If extracted value not found verbatim in source → flag as potential hallucination or paraphrasing. Most powerful automated check.

---

## 7. LLM Strategy — What Uses What

**Groq is the sole provider.** Two models, one SDK, one API key.

| Task | Model | Reason |
|---|---|---|
| Simple param extraction (text) | `llama-3.3-70b-versatile` | Fast, strong instruction-following, generous free tier |
| Step therapy text extraction | `llama-3.3-70b-versatile` | Verbatim text task |
| Step counting CoT | `llama-3.3-70b-versatile` | Multi-step reasoning task |
| Sparse-page fallback (vision) | `meta-llama/llama-4-scout-17b-16e-instruct` | Multimodal, accepts base64 images via Groq Chat Completions API |
| Business rules validation | Pure Python | Deterministic, no LLM |
| Access score (Phase 2, later) | Pure Python | Deterministic rules |

**Groq free tier (current):** ~14,400 requests/day for `llama-3.3-70b-versatile`; vision model shares the daily request budget but has lower TPM. Empirical budget for this job: 79 rows × ~3 text calls + occasional vision = **~240–280 calls**, well within limits. Groq's Llama models are open-weight (Meta's license), fully reproducible on Kaggle/Colab via the `groq` SDK, and let us use one provider for both text and vision — single SDK, single API key.

---

## 8. Decisions Made — Locked

1. Single `pipeline.py` file — all code in one file
2. Three extraction passes — not one combined prompt
3. LLM abstracted behind single `call_llm()` function — swap provider in one place
4. PDF caching — ingest once per unique filename, not once per row
5. Checkpointing every 10 rows — resume on failure
6. Verbatim extraction — LLM returns exact policy text, never paraphrases
7. Validation is pure Python — no LLM in validation layer
8. Approach A (rule-based) as primary scorer, Hybrid E as sanity check
9. Access score added AFTER extraction is confirmed working

---

## 9. What We Explicitly Do NOT Know (Be Honest About These)

- Within-bucket score weights are hand-designed — not from any published source
- Whether FDA label comparison should use current label or label-at-policy-date
- Whether quantity limits appear in a separate document for Oregon Medicaid (not found in 441-page doc)
- Whether "Humira® biosimilar trial" counts the same as "Humira® trial" under Step 11 criteria
- Exact format variety in the full 79-row dataset beyond the 3 sample files
