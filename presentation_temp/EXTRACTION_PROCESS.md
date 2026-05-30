# PA Policy Extraction Pipeline — Complete Process Guide

> **Project:** H1'26 ZS Hackathon — Payer Prior Authorization (PA) Policy Intelligence for Plaque Psoriasis (PsO)  
> **Deliverable:** `result.csv` — 79 rows × 15 columns (12 extracted parameters + Access Score)  
> **Core file:** `pipeline.py` (single-file architecture)

---

## Table of Contents

1. [Executive Summary (Non-Technical)](#1-executive-summary-non-technical)
2. [The Business Problem](#2-the-business-problem)
3. [What We Extract — The 12 Parameters](#3-what-we-extract--the-12-parameters)
4. [End-to-End Pipeline Overview](#4-end-to-end-pipeline-overview)
5. [Phase-by-Phase Technical Deep Dive](#5-phase-by-phase-technical-deep-dive)
6. [The Three Extraction Passes (LLM)](#6-the-three-extraction-passes-llm)
7. [Step Counting Logic — The Hardest Part](#7-step-counting-logic--the-hardest-part)
8. [Validation & Quality Assurance](#8-validation--quality-assurance)
9. [Caching, Checkpointing & Resilience](#9-caching-checkpointing--resilience)
10. [Access Score (Phase 2 — Deferred)](#10-access-score-phase-2--deferred)
11. [Tech Stack & Constraints](#11-tech-stack--constraints)
12. [Known Edge Cases & Design Decisions](#12-known-edge-cases--design-decisions)
13. [Presentation Talking Points](#13-presentation-talking-points)

---

## 1. Executive Summary (Non-Technical)

### What are we building?

Pharma companies need to know **how hard it is for patients to get their drug covered** by each insurance payer. Today, teams manually read hundreds of dense PDF policy documents. We automate that:

1. **Read** payer Prior Authorization (PA) policy PDFs  
2. **Extract** 12 structured facts per drug (age limits, step therapy, TB tests, etc.)  
3. **Score** how restrictive each policy is vs. the FDA label (0–100 Access Score — Phase 2)  
4. **Output** a spreadsheet (`result.csv`) judges can evaluate against ground truth  

### Why is this hard?

- Policies are **441+ pages**, unstructured legal/clinical prose  
- One PDF covers **many drugs** — we must extract the right row for each brand  
- **Universal rules** (e.g., TB screening) apply to ALL drugs but appear far from drug-specific sections  
- **Step therapy** uses nested AND/OR logic — counting steps requires reasoning, not just copy-paste  
- Policies use **different document structures** (Oregon Medicaid vs. Aetna-style)  

### Our approach in one sentence

**Spreadsheet-driven batch processing:** ingest each PDF once, intelligently slice the relevant sections per drug, run three focused LLM extraction passes, validate with Python rules + a backup step counter, and write structured CSV output with checkpoint/resume support.

---

## 2. The Business Problem

| Stakeholder need | What policies contain | What we produce |
|------------------|-------------------------|-----------------|
| Market access teams | Age, step therapy, auth duration | Structured columns per (PDF, Brand) |
| Field force | How restrictive vs. FDA | Access Score 0–100 (Phase 2) |
| Strategy | Compare payers | 79-row submission matrix |

**Indication scope:** Plaque Psoriasis (moderate-to-severe).  
**Brand scope:** Pipeline is **brand-agnostic** — ~14 brands across 79 rows (TREMFYA, STELARA, ENBREL, COSENTYX, etc.), not just the two named in the problem statement.

**Dataset:** 70 real born-digital PDFs in `Sample_PsO_ADS_Track/`, driven by the **Submissions tab** in `pre-context/PA_Business_Rules.xlsx` (79 rows).

---

## 3. What We Extract — The 12 Parameters

| # | Parameter | Simple explanation | Output format |
|---|-----------|-------------------|---------------|
| 1 | **Age** | Minimum patient age for this drug + PsO | `>=18`, `>=6`, `FDA approved age`, or `NA` |
| 2 | **Step Therapy Requirements** | Verbatim text of prior treatments required | Exact policy wording |
| 3 | **# Steps through Brands** | Count of biologic/branded steps | `"1"`, `"2"`, or `NA` |
| 4 | **# Steps through Generic** | Count of conventional/generic steps | `"1"`, `"2"`, or `NA` |
| 5 | **Step through Phototherapy** | Is phototherapy mandatory? | `Yes`, `No`, or `NA` |
| 6 | **TB Test Required** | Tuberculosis screening gate? | `Yes` or `No` |
| 7 | **Initial Auth Duration** | How long first approval lasts (months) | `"6"`, `"12"`, or `Unspecified` |
| 8 | **Reauth Duration** | Renewal approval length (months) | Number, `Unspecified`, or `NA` |
| 9 | **Reauth Required** | Must patient re-apply? | `Yes` or `No` — **derived, not LLM** |
| 10 | **Reauth Requirements** | What patient must show to renew | Verbatim text or `NA` |
| 11 | **Specialist Types** | Who can prescribe | `"Dermatologist, Rheumatologist"` or `NA` |
| 12 | **Quantity Limits** | Dispensing caps | Verbatim — **only if labelled "quantity limit"** |
| 13 | **Access Score** | 0–100 restrictiveness vs FDA | **Phase 2 — column exists, cells empty in Phase 1** |

### Why verbatim extraction?

Judges compare our output to ground truth word-for-word. Paraphrasing causes false failures and hides hallucinations. **Exception:** Age and durations are normalized to standard formats.

---

## 4. End-to-End Pipeline Overview

```
???????????????????????????????????????????????????????????????????????????
?                         STARTUP & PRE-FLIGHT                            ?
?  Load .env ? Load drug classifications from XLSX ? Load Submissions    ?
?  ? Cross-check PDFs exist on disk ? Cheap text scan for drug presence  ?
?  ? Determinism test (3 identical LLM calls)                            ?
???????????????????????????????????????????????????????????????????????????
                                    ?
                                    ?
???????????????????????????????????????????????????????????????????????????
?                    FOR EACH ROW (Filename + Brand)                      ?
?  ????????????????   ????????????????   ????????????????                ?
?  ? PDF Ingest   ? ? ?  Sectioning  ? ? ? 3 LLM Passes ?                ?
?  ? (cached)     ?   ? (per drug)   ?   ? 1, 2, 3      ?                ?
?  ????????????????   ????????????????   ????????????????                ?
?         ?                   ?                   ?                       ?
?         ?                   ?                   ?                       ?
?  PyMuPDF text/page    Outline ? anchors    Pass 1: 7 simple params     ?
?  Vision OCR fallback  ? slice ? assemble   Pass 2: step therapy text   ?
?                       [UNIVERSAL]+[DRUG]   Pass 3: step counts (CoT)   ?
?                                            + rule-based counter         ?
?                                    ?                                    ?
?                                    ?                                    ?
?                         ????????????????????                            ?
?                         ?   Validation     ?                            ?
?                         ? Format rules     ?                            ?
?                         ? Verbatim check   ?                            ?
?                         ? Rerun if critical? (max 2)                   ?
?                         ????????????????????                            ?
???????????????????????????????????????????????????????????????????????????
                                    ?
                                    ?
                    result.csv (79 rows) + debug_log.jsonl + checkpoint.json
```

### Key architectural principle

**The spreadsheet drives the pipeline, never the PDF.**  
The PDF does not tell us which drugs to extract — the Submissions tab does. One PDF ? many rows (one per brand).

---

## 5. Phase-by-Phase Technical Deep Dive

### 5.1 Bootstrap & Configuration (Block 1)

**What:** Constants, paths, model names, drug classification sets.

**Why:**
- Single source of truth for Groq models, token limits, retry counts  
- Drug branded vs. generic classification loaded from XLSX — **not hardcoded** — so step counting stays aligned with business rules  
- `.env` loader uppercases keys (`groq_api_key` ? `GROQ_API_KEY`)

**Key constants:**
| Constant | Value | Purpose |
|----------|-------|---------|
| `GROQ_TEXT_MODEL` | `llama-3.3-70b-versatile` | Extraction + step counting |
| `GROQ_VISION_MODEL` | `meta-llama/llama-4-scout-17b-16e-instruct` | Sparse-page OCR fallback |
| `TEMPERATURE` | `0.0` | Reproducibility for judges |
| `MIN_CHARS_PER_PAGE` | 100 | Trigger vision OCR per page |
| `SMALL_DOC_CHAR_LIMIT` | 50,000 | Skip sectioning for small PDFs |
| `MAX_CONTEXT_CHARS` | 100,000 | Hard cap on assembled context |
| `TPM_LIMIT` | 5,500 | Stay under Groq 6K tokens/minute |

---

### 5.2 Input Loading (Block 9 — Phase 1)

**Functions:** `load_drug_classifications`, `load_submissions`, `normalise_columns`, `get_drug_aliases`, `get_other_brands_in_doc`

**What happens:**
1. Read **"PsO Brands- For Ground Truth"** tab ? branded set (market basket minus known generics) + generic set (acitretin, cyclosporine, methotrexate, vtama, zoryve)  
2. Add INN aliases (e.g., `ustekinumab` ? stelara) for policy text matching  
3. Add JAK inhibitors as branded steps even if not in basket tab  
4. Load **Submissions tab** ? 79 `(Filename, Brand)` pairs  
5. Normalize column names (`drug` ? `Brand`, etc.)  
6. Default indication to **"Plaque Psoriasis"** if absent  

**Why drug aliases?** Policies say "Humira®", "adalimumab", or biosimilar INNs — we must find all variants.

**Why `get_other_brands_in_doc`?** Multi-brand policies list different ages per drug in one table. Pass 1 must know which other brands appear so it extracts **only the target drug's value**.

---

### 5.3 PDF Ingestion (Block 2)

**Functions:** `ingest_pdf`, `_ingest_pdf_text_only`, `_ocr_page_with_vision`, `_ensure_vision_aware`, `close_pdf_cache`

**What happens:**
1. Open PDF with **PyMuPDF (`fitz`)**  
2. Extract text per page with `page.get_text("text")`  
3. If page has **< 100 characters** ? render page to PNG (200 DPI) ? send to **Llama 4 Scout vision model** ? use OCR text  
4. Concatenate all pages with `===== PAGE N =====` markers  
5. Keep PyMuPDF document handle **open** for outline extraction  

**Why PyMuPDF?** Fast, handles text + rendering in one library. Audited dataset: all 70 PDFs are born-digital with 600–6000 chars/page — vision fallback rarely fires but is required for judges' held-out scanned PDFs.

**Why page markers?** Debuggability — when extraction claims something is on "page 377", we can verify.

**Why per-page vision, not whole-document OCR?** Token budget — OCRing 441 pages would be prohibitively expensive.

**Caching:** `pdf_cache[filename]` — same PDF ingested **once** even when it appears in 10+ submission rows.

---

### 5.4 Document Sectioning — Outline-Driven (Block 3)

**The core problem:** Oregon Medicaid is 688K characters (~441 pages). We cannot send the full document to the LLM. But we also cannot naively search for "TREMFYA" — we'd miss **universal criteria** (TB test in Step 4) that apply to all drugs.

**Solution:** Assemble four labeled sections per drug:

```
[UNIVERSAL CRITERIA — applies to all drugs]
[CLASSIFICATION TABLES — preferred / non-preferred agents]
[DRUG-SPECIFIC CRITERIA — TREMFYA for Plaque Psoriasis]
[REAUTHORIZATION / RENEWAL CRITERIA]
```

#### Stage A — Local Outline Extraction (no API)

**Function:** `extract_outline`

1. Try PDF bookmarks (`doc.get_toc()`) — ~30% of PDFs have TOC  
2. Fallback: font/style scan — detect headings by font size, bold, ALL CAPS, keywords (`General`, `Renewal`, `Preferred`, `Step N`, etc.)  
3. Output: ordered list of `{page, level, text, char_offset}`  
4. Prune to ?400 entries for LLM mapping call  

**Why outline first?** LLM never sees full policy body during sectioning — only headings (~5–10K tokens vs. 688K).

#### Stage B — Outline ? Section Anchors (one LLM call per PDF+drug)

**Function:** `map_outline_to_sections`

- Input: pruned outline + drug name + indication  
- Output: JSON with `{heading, page}` for each of 4 section types  
- **Page number disambiguation:** "Step 1" repeats many times — `(heading, page)` tuple is the lookup key  

**Why one LLM call here?** Cheaper than sending policy text; anchors enable deterministic slicing.

#### Slicing — Deterministic (no LLM)

**Function:** `slice_by_anchors`

- Find anchor offset in full text  
- Slice until next outline entry ? minimum section size (e.g., 15K chars for drug-specific)  
- Prevents anchors on same page from bounding each other incorrectly  

#### Stage C — Recursive Zoom (only if section too large)

**Function:** `recursive_zoom`

- Trigger: section > 60,000 chars  
- Re-extract sub-headings within section  
- LLM picks which sub-headings are relevant (TB, severity, PsO steps, etc.)  
- Concatenate only those pieces  

#### Assembly

**Function:** `assemble_context`

- Label the four sections  
- Apply recursive zoom per oversized section  
- Hard cap at 100K chars — truncate largest section if still over budget  

#### Small-doc fast path

If `len(full_text) < 50,000` OR outline has < 5 headings ? use full text directly (truncated to cap). ~64 of 70 PDFs take this path.

**Caching:** `section_cache[(filename, drug)]` — different drugs on same PDF get different assembled contexts.

---

### 5.5 LLM Interface (Block 4)

**Functions:** `call_llm`, `TokenBudget`, `retry`, `parse_json_safe`, `determinism_test`

**What happens:**
- All text calls ? Groq Llama 3.3 70B  
- All vision calls ? Groq Llama 4 Scout (one image per request)  
- `TokenBudget` sliding 60-second window — sleep before sending if would exceed 5,500 TPM  
- `retry()` handles 429 (30s sleep), 5xx (exponential backoff), daily quota (halt batch)  
- `parse_json_safe()` strips markdown fences, fixes trailing commas, reprompts once on bad JSON  

**Why TokenBudget?** Without throttling, 79 rows × 3+ calls × large contexts would hit rate limits and take ~20 hours. With outline sectioning + throttle ? ~3 hours.

**Why temperature 0?** Hackathon requires reproducibility — judges re-run on Kaggle/Colab.

---

### 5.6 Pre-Flight Check (Block 8)

**Function:** `preflight_check`

**Before any LLM extraction:**
1. **Halt** if Submissions references PDFs not on disk  
2. Cheap text-only ingest of each unique PDF (no vision)  
3. Scan for drug alias presence — warn on zero matches (`CRITICAL_DRUG_NOT_FOUND` at row time)  
4. Populate `pdf_cache` so batch doesn't re-ingest  

**Why halt on missing files?** Can't produce valid output — fail loud early.

---

## 6. The Three Extraction Passes (LLM)

We use **three separate prompts**, not one mega-prompt.

**Why three passes?** Single-prompt extraction causes field conflation, dropped params, and hallucinations. Three focused calls × temp 0 × ~240 total calls fits Groq free tier.

### Pass 1 — Simple Parameters (7 fields)

**Function:** `extract_simple_params`

| Field | Extraction approach |
|-------|---------------------|
| Age | Standardize to `>=N`; youngest if multiple groups; `FDA approved age` if no number |
| TB Test | Yes/No — check universal + drug sections |
| Initial Auth Duration | Plain number months or `Unspecified` |
| Reauth Duration | Plain number, `Unspecified`, or `NA` |
| Reauth Requirements | Verbatim continuation criteria |
| Specialist Types | Comma-separated list |
| Quantity Limits | Verbatim **only** if labelled "quantity limit" |

**NOT extracted by LLM:** Reauth Required (Param 9) — derived in Python.

**Multi-brand directive:** Prompt lists other brands in document; model must return only target drug's value from shared tables.

---

### Pass 2 — Step Therapy Text (verbatim blob)

**Function:** `extract_step_therapy_text`

**Output:** Single `combined_step_text` — all step therapy language for target drug + PsO, verbatim.

**Rules enforced in prompt:**
- Preserve AND/OR connectors exactly  
- Include universal + drug-specific step language (one blob, not split)  
- Exclude TB test, age, diagnosis, reauth, quantity limits  
- Exclude other indications (PsA, Crohn's) and other drugs' steps  
- Return `NA` if no step therapy  

**Why single blob (not universal vs. indication split)?** Splitting was an error-prone judgment call. Pass 3 classifies using section markers from Block 3 assembly.

---

### Pass 3 — Step Counting (Chain-of-Thought)

**Function:** `extract_step_counts`

**Input:** Small Pass 2 text (~1–2K tokens) + section marker lines (not full context)

**Output:** `steps_brands`, `steps_generic`, `step_phototherapy` + full reasoning trace (`step_a` through `step_f`)

**Worked examples in prompt:**
1. Oregon Medicaid — all-AND chain ? 1 branded, 3 generic, phototherapy Yes  
2. Reference tab Yesintek/Stelara — OR resolution ? 1 branded, 1 generic, phototherapy No  

**Parallel backup:** `rule_based_step_count()` runs on every row — pattern + named drug matching. If LLM fails ? emit rule counts. If LLM vs rule disagree by ?2 ? flag for manual review.

---

## 7. Step Counting Logic — The Hardest Part

### Plain-language explanation

**Step therapy** means "try these cheaper treatments first before we'll pay for your biologic."

To count steps:
1. Find **universal** step requirements (apply to all drugs in the policy section)  
2. Find **indication-specific** steps (for plaque psoriasis)  
3. **Combine with AND** — patient must satisfy both layers  
4. Where there are **OR choices between steps**, take the **least restrictive path** (fewest steps)  
5. Classify each step: branded biologic, generic/conventional, or phototherapy  
6. Phototherapy is **never** counted in branded/generic — it's Param 5 only  

### Classification rules

| Treatment mentioned | Counts as |
|---------------------|-----------|
| Topical corticosteroids, calcipotriene, MTX, acitretin | Generic step |
| Named biologics (Humira, Enbrel, Stelara, etc.) | Branded step |
| "Previously received a biologic" | 1 branded step |
| "Humira OR Enbrel" (OR within one step) | 1 branded step |
| "Biologic OR methotrexate" (OR between steps) | Take generic path = 1 generic |
| Phototherapy (mandatory AND, not in OR) | Param 5 = Yes |
| Phototherapy (only in OR option) | Param 5 = No |
| Unnamed conventional step | Generic (default) |

### Oregon Medicaid TREMFYA example (Step 11)

All AND conditions:
1. Topical corticosteroid ? generic  
2. Another topical ? generic  
3. Phototherapy ? phototherapy Yes  
4. Systemic (MTX/CYC/acitretin) ? generic  
5. Humira OR Enbrel ?3 months ? 1 branded  

**Result:** Brands=1, Generic=3, Phototherapy=Yes

### Reference tab example (Yesintek/Stelara)

- Universal: must try/fail Yesintek ? 1 branded (AND)  
- Indication: biologic (1 branded) OR failed MTX/cyclosporine/acitretin (1 generic) ? OR ? least restrictive = 1 generic  
- **Final:** 1 branded + 1 generic, Phototherapy=No  

---

## 8. Validation & Quality Assurance

### Layer 1 — Format / Derivation Rules (always run, mutate in place)

| Rule | What it fixes |
|------|---------------|
| `rule_age_format` | `NA` when missing; `>=N` format; `FDA approved age` |
| `rule_auth_duration` | Initial auth never blank ? `Unspecified` |
| `rule_reauth_required` | Yes if duration OR requirements non-NA |
| `rule_quantity_limits_strict` | Reject "dosage", "dosing limit", generic statements |
| `rule_step_na_format` | `"0"` ? `"NA"` for step counts |

### Layer 2 — Critical Checks (trigger rerun, max 2)

| Check | Trigger |
|-------|---------|
| `critical_verbatim_check` | Token-recall < 70–80% for step text, reauth, quantity limits ? likely hallucination |
| `critical_step_extraction_check` | Step text exists but both brand AND generic counts = NA |

### Layer 3 — Advisory Checks (flag only, no rerun)

- `semantic_contradiction_checks` — TB yes but text says "no TB testing"; reauth No but duration specified; brand in text but count NA  
- `multi_brand_ambiguity_check` — extracted age/qty limit near another brand's name in source  
- Step count reconciliation flags — `STEP_COUNT_MAJOR_DISAGREE`, `STEP_COUNT_RULE_FALLBACK`  

### Validation gates (pre-submission)

| Gate | Target |
|------|--------|
| Reference tab smoke test | ?9/12 params match |
| Manual spot-check (5 rows) | ?90% agreement |
| Multi-source smoke gate | ?85% per-cell across Reference + manual labels |
| Verbatim spot-check (10 rows) | Extracted text findable in source PDF |

---

## 9. Caching, Checkpointing & Resilience

### Three-tier cache (per batch run)

| Cache | Key | What's stored |
|-------|-----|---------------|
| `pdf_cache` | filename | Full text, pages, open doc handle |
| `outline_cache` | filename | Document outline (headings) |
| `section_cache` | (filename, drug) | Assembled context for extraction |

### Checkpointing

- Save to `checkpoint.json` every **10 rows** (atomic write: temp file ? rename)  
- Keyed by `(Filename, Brand)` — survives spreadsheet reorder  
- Resume skips completed rows  
- On **daily quota exceeded** ? save checkpoint, exit code 2, resume tomorrow  

### Failure handling

| Failure | Behavior |
|---------|----------|
| Drug not in PDF | `CRITICAL_DRUG_NOT_FOUND` row, all NAs |
| PDF processing exception | NA row + warning, batch continues |
| Critical validation after 2 reruns | Emit row with warning tag |
| Missing PDF on disk | **Halt at preflight** |

### Debug logging

Append-only `debug_log.jsonl` — every retry, vision fallback, rerun, warning. Avoids O(N²) read-write.

---

## 10. Access Score (Phase 2 — Deferred)

**Current state:** Column exists in output schema; cells are **empty** until extraction is validated.

### Planned scoring framework

| Bucket | Score range | Hard trigger |
|--------|-------------|--------------|
| 0 | No access | Drug not covered |
| 25 | Restricted vs FDA | Any step therapy (branded ?1 OR generic ?1 OR phototherapy Yes) |
| 50 | Parity with FDA | Zero steps, PA required, matches FDA label |
| 75 | Better than FDA | Less restrictive than FDA (rare) |
| 100 | Best access | No PA required |

**Within-bucket deductions:** generic step ?3, branded step ?4, phototherapy ?3, age vs FDA ?2, TB test ?1, short auth ?1, strict reauth ?0.5

**FDA baselines today:** TREMFYA (?18/?6), STELARA (?6) — expansion to all 14 brands is Phase 2 work.

---

## 11. Tech Stack & Constraints

| Component | Choice | Why |
|-----------|--------|-----|
| LLM provider | Groq (sole) | Open-weight Llama models, free tier, reproducible |
| Text model | Llama 3.3 70B | Strong instruction-following |
| Vision model | Llama 4 Scout 17B | Multimodal page OCR |
| PDF library | PyMuPDF | Fast text + render |
| Data | pandas + openpyxl | XLSX Submissions tab |
| Validation | Pure Python | Deterministic, no LLM in validation |
| Deployment | Kaggle/Colab free tier | Hackathon requirement |

**Constraints:**
- No paid APIs  
- Temperature = 0 always  
- Single `pipeline.py` file  
- Open-source LLMs only  

**Expected API budget:** ~240–280 LLM calls for 79 rows (3 text passes + occasional section mapping + rare vision).

---

## 12. Known Edge Cases & Design Decisions

| Edge case | How we handle it |
|-----------|------------------|
| Universal TB test not in drug section | Section assembly includes `[UNIVERSAL CRITERIA]` block |
| Aetna class-level rules ("Non-Preferred CAM antagonists require...") | Include classification tables in context |
| Multi-brand age table in one section | Multi-brand directive in Pass 1 + ambiguity check |
| Drug not in policy (wrong PDF for brand) | `CRITICAL_DRUG_NOT_FOUND` — valid NA output |
| All-AND step chain (no OR) | Don't force OR resolution — just count |
| `"0"` vs `"NA"` for zero steps | Always `NA` per sentinel contract |
| Age not mentioned | `NA` (partner-confirmed, not `"No"`) |
| Quantity limit labelled "dosage" | Reject ? `NA` |
| Biosimilar vs reference product | Treat as separate named steps (conservative) |
| FDA label date vs policy date | Undecided for Access Score Phase 2 |

### Document structure types handled

| Type | Example | Key challenge |
|------|---------|---------------|
| A — Per-drug sections | Oregon Medicaid | Universal criteria at section top |
| B — Per-indication sections | Aetna-style | Class rules don't name target drug |

Both must produce the same four-part context bundle.

---

## 13. Presentation Talking Points

### Elevator pitch (30 seconds)

"We built an AI pipeline that reads insurance prior-authorization PDFs and extracts 12 structured access parameters per drug — things like age limits, step therapy requirements, and authorization durations. The hard part isn't reading the PDF; it's understanding that a 441-page policy has universal rules buried separately from drug-specific criteria, and that step therapy counting requires AND/OR legal logic. We solve that with outline-driven sectioning and three focused LLM passes, validated by Python business rules and a backup step counter."

### Demo flow suggestion

1. Show problem — dense PDF, manual process today  
2. Show Submissions spreadsheet — 79 rows, spreadsheet drives pipeline  
3. Walk through section assembly — universal + drug + reauth  
4. Show one Pass 1 output (age, TB) vs Pass 3 step counts  
5. Show Oregon Medicaid example — 1 branded, 3 generic, photo Yes  
6. Show validation — verbatim check, rerun logic, warnings summary  
7. Mention Phase 2 Access Score — deterministic bucket + deductions  

### Honest limitations to mention

- Access Score not yet implemented (Phase 1 = extraction only)  
- Within-bucket score weights are hand-designed, not from literature  
- Rule-based step counter is conservative — complex OR cases rely on LLM  
- Some rows may need manual review (`CRITICAL_DRUG_NOT_FOUND`, major step disagree)  

### Numbers to cite

- **79** submission rows across **~14** brands  
- **70** PDF policy documents  
- **3** LLM passes per row  
- **12** extracted parameters (+ Access Score Phase 2)  
- **~40%** of PDFs require outline-driven sectioning (>50K chars)  
- **441** pages in largest policy (Oregon Medicaid)  
- **688K** chars ? **~12K** token assembled context after sectioning  

---

## Appendix: Output Column Names (Exact)

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

Note sensitive formatting: `Step through-Phototherapy` (hyphen), `TB Test required` (lowercase r), `Initial Authorization Duration(in-months)` (no space before parenthesis).

---

*Generated for project presentation. Source: `pipeline.py`, `context/*.md`, `pre-context/PA_Business_Rules.xlsx` spec.*
