# Context: Problem Statement & Reference Documents
> This file explains what every document in `pre-context/` is, what it contains,
> and exactly how it feeds into the pipeline.

---

## 1. The Problem in One Paragraph

Pharma companies need to understand how restrictive each payer's PA policy is for their drugs. Today this is done manually by reading dense PDF documents. The task is to automate this: read the PDFs, extract 12 structured parameters per drug+indication pair, and compute an access quality score (0–100) that reflects how restrictive the policy is relative to the FDA label.

**Scope note.** Problem statement names TREMFYA and STELARA as primary focus, but the actual Submissions tab spans ~14 PsO brands. The extraction pipeline is brand-agnostic (Pass 1/2/3 substitute `{drug}` per row). The FDA-label baseline used by Access Score is currently only defined for TREMFYA and STELARA — expansion is a Phase 2 task owned by the access-score module.

---

## 2. Files in `pre-context/` — What Each One Is

### `H1_26_Hackathon_Problem_Statement.md`
**What it is:** Official problem statement from ZS Associates.  
**What it contains:**
- Full problem description and business context
- Definition of the 5 access score anchor points (0/25/50/75/100)
- Evaluation criteria (extraction accuracy + access score accuracy)
- Submission format requirements
- Technical constraints: open-source LLM only, Kaggle/Colab free tier, no paid APIs
- Timeline: submission deadline June 1, 2026, 9 AM IST

**Key quote for access score definition:**
> "0 = No access, 25 = restricted access against FDA guidelines, 50 = Parity with FDA label, 75 = preferred than FDA label, 100 = best possible access against all competitors / no restrictions applied"

**Critical constraint stated here:** Must use open-source LLMs only. Solutions tested for reproducibility on Kaggle free tier.

---

### `PA_Business_Rules.xlsx`
**What it is:** The single most important reference document. Contains all rules for extraction AND the only ground truth example.

**Tabs inside this file:**

#### Tab 1: `Business Rules`
Defines all 12 parameters with full extraction logic and edge cases.

| Param | Name | Key Rule |
|---|---|---|
| 1 | Age | Capture youngest if two groups. `FDA approved age` if no number. `NA` if absent (per [§A sentinel contract](./05_CONSTRAINTS.md)). |
| 2 | Step Therapy Requirements | Verbatim ALL step language — universal AND brand-specific. Phototherapy included in text if in step statements. Moderate-to-severe PsO only if policy distinguishes severity. |
| 3 | Number of Steps through Brands | AND universal+indication-specific. OR → least restrictive. Exclude phototherapy. NA if none. |
| 4 | Number of Steps through Generic | Same AND/OR logic. Unnamed steps default to generic. NA if none. |
| 5 | Step through Phototherapy | Yes/No/NA. Mandatory AND (not in OR) = Yes. |
| 6 | TB Test Required | Yes/No |
| 7 | Initial Auth Duration | Stated duration or "Unspecified" if PsO PA = Yes |
| 8 | Reauth Duration | Stated duration or "Unspecified" if Reauth Required = Yes |
| 9 | Reauth Required | Auto-Yes if either duration or reauth requirements are non-NA |
| 10 | Reauth Requirements | Continuation criteria — verbatim or summarized |
| 11 | Specialist Types | All listed specialties |
| 12 | Quantity Limits | ONLY explicit "quantity limit" label. NOT "dosage" or "dosing limit" |

#### Tab 2: `Reference`
**The only ground truth worked example.** A fully labeled policy (Yesintek/Stelara) with all 12 params filled AND a comments column explaining the step counting reasoning chain.

Ground truth values:
```
Reauth Duration: 12 Months
Reauth Required: Yes
Reauth Requirements: Documentation of positive clinical response — reduction in BSA OR improvement in symptoms from baseline
Specialist Types: Dermatologist
Initial Auth Duration: 6 Months
TB Test: Yes
Quantity Limits: Stelara 130mg/26mL: 4 vials (1 dose); 45mg/0.5mL: 1 vial/syringe per 84 days (exception: 2 per 28 days)
Age: >=6
Step Therapy Text: [Universal] Must try/fail Yesintek. [Indication-specific] Previously received biologic OR failed MTX/cyclosporine/acitretin
Steps through Brands: 1
Steps through Generic: 1
Step through Phototherapy: No
```

**Step counting comment in Reference tab (exact reasoning expected by judges):**
- Universal: try/fail Yesintek = 1 branded step (AND)
- Indication-specific: received biologic (1 branded) OR failed MTX/cyclosporine/acitretin (1 generic) → OR → least restrictive = 1 generic
- Final: 1 branded (universal) + 1 generic (indication-specific) = **1 branded, 1 generic**
- Phototherapy = No because it only appears under OR, not mandatory

#### Tab 3: `Submissions`
**The output template.** 79 pre-populated rows with filename+brand. Pipeline must fill all 13 columns for each row.

Column structure:
```
Filename | Brand | Age | Step Therapy Requirements | # Steps Brands | # Steps Generic | 
Step through Phototherapy | TB Test | Quantity Limits | Specialist Types | 
Initial Auth Duration | Reauth Duration | Reauth Required | Reauth Requirements | Access Score
```

Note: A single PDF filename can appear multiple times (once per drug covered in that document).

#### Tab 4: `Additional Extracted Data`
Contains supplementary extracted information. Review for any additional context that might be relevant to edge cases.

#### Tab 5: `PsO Brands - For Ground Truth`
List of 35 drugs in the PsO market basket. These are the drugs that appear as step therapy requirements in policies. Knowing this list helps classify whether a step is branded or generic.

Full list includes: Acitretin, Amjevita, Avsola, Bimzelx, Cimzia, Cosentyx, Cyclosporine, Cyltezo, Enbrel, Humira, Hyrimoz, Idacio, Ilumya, Inflectra, Hulio, Methotrexate, Otezla, Remicade, Renflexis, Siliq, Skyrizi, Sotyktu, Stelara, Taltz, Tremfya, Vtama, Yuflyma, Yusimry, Zoryve, Wezlana, Selarsdi, Yesintek, Psychiva/Quallent, Steqeyma, Otulfi

**Branded biologics in this list (relevant for step classification):** Amjevita, Avsola, Bimzelx, Cimzia, Cosentyx, Cyltezo, Enbrel, Humira, Hyrimoz, Idacio, Ilumya, Inflectra, Hulio, Otezla, Remicade, Renflexis, Siliq, Skyrizi, Sotyktu, Stelara, Taltz, Tremfya, Yuflyma, Yusimry, Wezlana, Selarsdi, Yesintek, Psychiva/Quallent, Steqeyma, Otulfi

**Generic/conventional drugs in this list:** Acitretin, Cyclosporine, Methotrexate, Vtama, Zoryve

---

### Dataset reality (audited 28 May 2026)

All 70 files in `Sample_PsO_ADS_Track/` are **real, born-digital PDFs**. Magic bytes `%PDF-1.4`/`1.6`/`1.7`. Every file yields extractable text via `pdftotext` / PyMuPDF — measured density ranges from **~600 to ~6000 chars per page**, mean ~2200. Zero ZIPs. Zero scanned-only files. Zero text-renamed-as-pdf files.

**Implication:** Single ingestion path. PyMuPDF (`fitz`) extracts text per-page. Vision is a defensive fallback only — used per-page if extraction returns suspiciously little text (< 100 chars on a page with non-trivial dimensions), which covers form-image overlays or table-only pages. The full document is otherwise text-only.

### `66156-4274314.pdf` — illustrative example (Oregon Medicaid)
441 pages of Prior Authorization Criteria, May 1 2024. Relevant section: "Targeted Immune Modulators for Autoimmune Conditions" starting page 374.

```
Page 374-376: Table 1 — Approved ages per drug per indication (TREMFYA: ≥18 PsO, ≥18 PsA; STELARA: ≥6 PsO, ≥18 CD)
Page 377-383: Universal approval criteria (Steps 1-25, applies to ALL drugs in section)
  Step 4:   TB test — universal requirement
  Step 10:  Plaque psoriasis gate → goes to Step 11
  Step 11:  Step therapy requirements for PsO (ALL AND conditions)
  Steps 1-7 (Renewal): Reauth criteria
Page 384+: Different therapeutic area (severe asthma / atopic dermatitis)
```

**Oregon Medicaid Step 11 — TREMFYA PsO step therapy.** Must fail ALL of:
1. Topical high-potency corticosteroid (betamethasone / clobetasol / fluocinonide / halcinonide / halobetasol / triamcinolone) — Generic
2. At least one other topical: calcipotriene, tazarotene, or anthralin — Generic
3. Phototherapy — Phototherapy param
4. At least one systemic: acitretin, cyclosporine, or methotrexate — Generic
5. One biologic: Humira® OR Enbrel® for at least 3 months — Branded

Result: Brands=1, Generic=3, Phototherapy=Yes

### Document-structure variability in the wider dataset

Two recurring structures the sectioning code must handle:

| Structure | Example | Drug section keyed by | Universal location |
|---|---|---|---|
| A — per-drug sections | Oregon Medicaid | Drug name as section header | Top of section |
| B — per-indication sections | Aetna-style | Drug listed inside indication block; class rules separate | Preferred / Non-Preferred tables outside the indication block |

Both must produce the same `[UNIVERSAL] + [DRUG-SPECIFIC] + [REAUTH]` context bundle. Class-level universal step rules (e.g. "Non-Preferred CAM antagonists require trial of ONE preferred anti-TNF") never name the target drug — section assembly must include preferred/non-preferred classification tables, not only criteria that mention the drug by name.

---

## 3. Submissions Tab — Exact Column Mapping

The `result.csv` output must match these exact column names:

```
Filename
Brand
Age
Step Therapy Requirements Documented in Policy
Number of Steps through Brands
Number of Steps through Generic
Step through-Phototherapy
TB Test required
Quantity Limits
Specialist Types
Initial Authorization Duration(in-months)
Reauthorization Duration(in-months)
Reauthorization Required
Reauthorization Requirements Documented in Policy
Access Score
```

Total: 15 columns. 79 rows. One row per (Filename, Brand) combination.

---

## 4. Evaluation Criteria

Two dimensions scored by judges:

| Dimension | What's Evaluated |
|---|---|
| Extraction Accuracy | Extracted values vs ground truth, per-parameter per-row |
| Access Score Accuracy | Your computed score vs gold standard score |

Both matter. A perfect extraction with wrong scores = partial credit.  
A wrong extraction that accidentally scores correctly = also partial credit.

---

## 5. Technical Constraints (From Problem Statement)

- Open-source LLM only — Groq hosts Llama (open-weight, Meta license) on its free tier and is reproducible on Kaggle/Colab. **Decision: we use Groq exclusively.**
- Kaggle or Google Colab free tier
- No paid APIs
- No local GPU
- Must be reproducible (judges will test it)
- Single result.csv + notebook/codebase in ZIP archive

**Locked LLM stack:**

| Use case | Model (Groq) | Why |
|---|---|---|
| Text extraction, step-counting CoT, JSON output | `llama-3.3-70b-versatile` | Production-grade 70B, strong instruction-following, generous free tier |
| Vision fallback (page-image OCR / table read) | `meta-llama/llama-4-scout-17b-16e-instruct` | Multimodal, fast (17B active params, 16-expert MoE), accepts base64 images via Groq Chat Completions API |

Both are accessed through the same `groq` Python SDK; `call_llm(vision=True)` selects the vision model. No second SDK, no second API key.
