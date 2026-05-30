# Important Notes — Do Not Miss These
> Read before writing any code and before final submission.

---

## 1. EXTRACTION — Critical Rules

### 1.1 Verbatim Is Non-Negotiable (Except Age and Durations)
Most params require EXACT text from the policy. Two exceptions:
- **Age** — standardised to `>=N` format (e.g. `>=18`, `>=6`)
- **Auth/Reauth durations** — plain number only (`"6"` not `"6 months"`)

All other params: copy verbatim. Every prompt must say "Copy exact text. Do not paraphrase."

### 1.2 Universal Criteria Are Easy to Miss
Oregon Medicaid's TB test (Step 4) is NOT in the TREMFYA-specific section. It's in universal criteria applying to ALL targeted immune modulators. Aetna's branded step requirement for TREMFYA is NOT stated per-drug — it's stated as a class rule ("Non-Preferred CAM Antagonists require...").

Always assemble context as: `[UNIVERSAL + TABLE] + [DRUG-SPECIFIC] + [REAUTH]`. Universal must include preferred/non-preferred classification tables, not just criteria mentioning the drug by name.

### 1.3 One PDF — Multiple Drugs
The pipeline is driven by the Submissions spreadsheet, not the PDF. The PDF does not tell us which drugs to extract — the spreadsheet does. `pdf_cache` ensures same PDF is ingested once even when it appears in multiple rows.

### 1.4 Step Therapy Text Must Preserve AND/OR
AND/OR connectors determine the entire step count. "Patient must have tried methotrexate AND phototherapy" vs "OR" = completely different counts. Prompts must say: "Preserve AND/OR connectors EXACTLY as written."

### 1.5 Param 2 Output Must Be Clean — No Internal Labels
`combined_step_text` written to result.csv must NOT contain our `[UNIVERSAL CRITERIA]` / `[INDICATION-SPECIFIC CRITERIA]` construction labels. Those are internal only (used for Pass 3 reasoning). Pass 2 now produces two versions:
- `combined_step_text_internal` → labelled, for Pass 3 input
- `combined_step_text` → clean policy text, for result.csv

### 1.6 Quantity Limits — Strict Label Rule
Only capture text explicitly labelled "quantity limit". Reject: "dosage", "dosing limit", "dosing information", "recommended dose", "administration", "quantity limits exist" (generic statement without specifics).

### 1.7 Step Counts — "NA" Not "0"
If no branded steps: output `"NA"`, not `"0"`. Same for generic steps. Business rules require "NA". "0" is wrong.

### 1.8 Age Format — Confirmed Against Ground Truth
Age output rules (verified against Additional Extracted Data tab, 439 rows):

| Situation | Output |
|---|---|
| Age threshold stated | `>=N` format e.g. `>=18`, `>=6`, `>=4` |
| Policy says "FDA approved age" (no number) | `FDA approved age` |
| Age not mentioned in policy | `NA` | Partner confirmed |

**Confirmed (partner):** Age not mentioned → `"NA"`. Partner instruction overrides ground truth tab which used `"No"` (337 rows). Pipeline uses `"NA"`.

If two age groups exist (e.g., ≥18 for most drugs, ≥4 for Enbrel) — capture the YOUNGEST (business rule). In the Aetna document this applies — TREMFYA is ≥18 but Enbrel is ≥4 in the same section.

---

## 2. STEP COUNTING — The Hardest Part

### 2.1 AND vs OR — The Only Logic That Matters
```
AND conditions: patient must satisfy ALL → count every step
OR conditions: patient satisfies ANY ONE → take least restrictive path (fewest steps)
Universal AND indication-specific: these two layers are ALWAYS joined by AND
```

### 2.2 OR Within a Step vs OR Between Steps
"Humira® OR Enbrel®" = ONE branded step (OR within step — patient chooses either)
"biologic OR MTX/cyclosporine" = OR between steps → take least restrictive = 1 generic step

### 2.3 When There Is No OR — No Resolution Needed
Oregon Medicaid Step 11 is ALL AND. Don't force OR-resolution on a purely AND chain.

### 2.4 Reference Tab + Additional Extracted Data = Ground Truth
Reference tab: 1 fully worked example with reasoning chain.
Additional Extracted Data tab: 439 rows with pre-extracted values for many params. Use BOTH for validation.

### 2.5 Phototherapy = Yes Only If Mandatory AND
- Mandatory AND condition → Yes
- Appears only in OR → No
- No criteria at all → NA

---

## 3. ACCESS SCORE — DEFERRED TO PHASE 2

**Not built in this phase.** Spec for the score lives in [05_CONSTRAINTS.md §Param 13](./05_CONSTRAINTS.md); implementation waits until extraction is green on the Reference tab and 5–10 spot-check rows. Until then `Access Score` is an empty column in `result.csv` (column exists, cell blank). The hard triggers (Bucket 25 = branded≥1 OR generic≥1 OR phototherapy=Yes), the FDA-label-date question, and the within-bucket deduction weights remain documented but unused.

---

## 4. TECHNICAL GOTCHAS

### 4.1 `parse_json_safe` Must Strip Trailing Commas
Standard `json.loads()` does NOT handle trailing commas. Use regex: `re.sub(r',\s*([}\]])', r'\1', text)` before parsing. Architecture file now includes this fix.

### 4.2 `_heuristic_section_split` Must Return List, Never None
`pass` in Python returns `None`. `len(None)` crashes. The function must always return a list (empty list `[]` to trigger LLM fallback, not `None`).

### 4.3 `_llm_section_split` Must Be Defined
Referenced in `split_sections()`. Must have an actual implementation even if minimal. Architecture file now includes a stub with correct interface.

### 4.4 `log_debug` Always Takes Dict, Never String
`retry()` previously passed a string. All `log_debug()` calls must pass a dict. Architecture file is corrected.

### 4.5 `reauth_required` Is Derived — Do Not Ask LLM For It
It is 100% derived by `rule_reauth_required()` from reauth_duration + reauth_requirements. Asking the LLM wastes tokens and its answer gets overwritten immediately. Removed from Pass 1 prompt.

### 4.6 Rate Limits (Groq only)
- Text model (`llama-3.3-70b-versatile`) free tier: ~14,400 requests/day, ~6,000 TPM
- Vision model (`meta-llama/llama-4-scout-17b-16e-instruct`): shares daily-request budget; per-minute payload limit is tighter — keep vision to **one page per call**, don't batch
- Per-row budget: 3 text calls (Pass 1, 2, 3) + 0–2 vision calls (rare). Expected total **~240–280 calls** across 79 rows
- On 429: `retry()` exponential backoff handles it. Add explicit `sleep(60)` if persistent

### 4.7 Temperature = 0.0 Always
Reproducibility requirement. Never use temperature > 0.

### 4.8 Context Window on Large Docs
Oregon Medicaid is 441 pages, ~688K chars. Full-doc context blows past any sensible LLM window. Sectioning + assembly is what keeps context in bounds — the assembled `[UNIVERSAL]+[DRUG-SPECIFIC]+[REAUTH]` bundle should stay under ~8K tokens. If a single section is still too large after assembly, trim the universal block to the criteria headers (TB, severity, diagnosis, preferred/non-preferred tables) and drop the unrelated indication tables.

### 4.9 Vision Fallback — Per-Page, Not Per-Document
`ingest_pdf()` extracts text per page. Only pages below `MIN_CHARS_PER_PAGE` (100) get re-OCR'd via the vision model. On the audited 70-PDF sample the fallback should not fire at all; it exists for held-out PDFs the judges may add. **Never** OCR whole documents — that's a token-budget disaster and unnecessary.

### 4.10 Vision Calls — One Page At A Time
Llama 4 Scout via Groq accepts the OpenAI-compatible `image_url` content type, one image per request. Send page-by-page with the PNG base64-encoded in a `data:image/png;base64,...` URL. Do not stack multiple page images per request — Groq's vision payload tolerance is tighter than the text model's token limit suggests.

### 4.11 Two Document Structures — Pipeline Handles Both
**Structure A (Oregon Medicaid):** Per-drug sections. Drug name = section header.  
**Structure B (Aetna):** Per-indication sections. Drug appears in a list within the section.

Critical for Structure B: class-level universal steps (e.g., "Non-Preferred CAM Antagonists require...") won't mention the drug by name. `find_universal_criteria()` must return the preferred/non-preferred tables to capture these.

### 4.12 Drug Classification Conflict — Apremilast/Otezla
`apremilast` (INN) was previously in GENERIC_CONVENTIONAL. `otezla` (brand) was in BRANDED_BIOLOGICS. Same drug, different classification depending on policy language. Fixed: classification now loaded from XLSX (brand names only), INN→Brand mapping handles matching. Any drug in the PsO market basket tab that is not a known generic → branded.

### 4.13 tofacitinib (Xeljanz) Classification
JAK inhibitors (tofacitinib, upadacitinib) = targeted synthetic DMARD. Per business rules: targeted synthetic = branded step. Was incorrectly in GENERIC_CONVENTIONAL previously. Now resolved by XLSX-driven classification (Xeljanz is not in the market basket tab, but if policy requires it as a step, the INN `tofacitinib` would be treated as generic by default — flag if this comes up).

---

## 5. INPUT FORMAT

### 5.1 Pipeline Driven by Spreadsheet, Not PDF
Pipeline never discovers drugs by reading the PDF. The spreadsheet tells it exactly which (Filename, Brand) to process.

### 5.2 XLSX or CSV Both Supported
`load_submissions()` handles both. Never call `pd.read_excel()` directly.

### 5.3 Column Names Normalised
`normalise_columns()` maps variants (brand, drug, filename, file) to standard names. Raises `ValueError` if unresolvable — never silently fails.

### 5.4 Drug Classification From XLSX
`load_drug_classifications()` reads "PsO Brands- For Ground Truth" tab at startup. Drugs in the tab minus `KNOWN_GENERICS_IN_MARKET_BASKET` = branded. No hardcoded drug lists in pipeline logic.

---

## 6. VALIDATION — Rerun vs Flag Only

### Which Failures Trigger a Rerun (max 2 reruns per row)
- Verbatim check fails on step_therapy_text, reauth_requirements_text, or quantity_limits_text
  → likely hallucination → rerun full extraction
- Step therapy text found (non-empty) but both brand AND generic counts = NA
  → Pass 3 likely failed → rerun full extraction

### Which Failures Are Flag-Only (no rerun)
- Quantity limits not found → valid output, not a failure
- Specialist types not specified → valid output
- Reauth required = No when duration is specified → rule fixes this, no rerun needed
- Age format issues → `rule_age_format()` fixes, no rerun needed

### Additional Extracted Data Tab as Validation Source
The Additional Extracted Data tab has 439 pre-extracted rows. This is additional ground truth beyond the single Reference tab example. Use it for spot-checking pipeline output on overlapping rows.

---

## 7. FILE PATHS AND SUBMISSION

### 7.1 Final Submission Structure
```
submission.zip
├── result.csv       ← 79 rows, 15 columns, exact format
├── pipeline.py      ← single file
├── requirements.txt ← groq, pymupdf, pandas, openpyxl
└── README.md        ← how to run (set GROQ_API_KEY, then `python pipeline.py`)
```

### 7.2 Exact Column Names (case-sensitive)
See COLUMN_MAP in Block 9. Key ones that are easy to get wrong:
- `"Step through-Phototherapy"` (hyphen not space)
- `"TB Test required"` (lowercase 'r')
- `"Initial Authorization Duration(in-months)"` (no spaces around parentheses)

### 7.3 API Keys — Never Hardcode
Use `os.environ.get()`. Kaggle: add as secrets. Local: `.env` file in `.gitignore`.

---

## 8. KNOWN UNKNOWNS

1. **Age output confirmed** — `"NA"` when not mentioned (partner confirmed). Ground truth tab used "No" but partner override accepted.
2. **Age "FDA labelled age" vs "FDA approved age"** — ground truth uses "FDA approved age". Architecture updated to match.
3. **Within-bucket score weights** — hand-designed, no external source.
4. **Oregon Medicaid quantity limits** — may be in separate formulary doc not in dataset.
5. **FDA label date** — current label vs label-at-policy-date for scoring. Undecided.
6. **Biosimilar equivalence** — does failing biosimilar count as failing reference product? Unclear.

---

## 9. PRE-SUBMISSION CHECKLIST

```
□ Drug classifications loaded from XLSX — not hardcoded
□ Reference tab test: Brands=1, Generic=1, Phototherapy=No, TB=Yes, Age=>=6
□ Additional Extracted Data: spot-check 10 rows against pipeline output
□ Verbatim check passing on test rows (no hallucinations detected)
□ Age format: all outputs in >=N format or "FDA approved age" or "NA"
□ Duration format: plain numbers only ("6" not "6 months")
□ Step therapy text in result.csv has NO [UNIVERSAL CRITERIA] labels
□ NA vs 0: zero-step policies output "NA"
□ Quantity limits: no "dosage" or generic "quantity limits exist" statements
□ Rerun logic: confirm max 2 reruns per row, not infinite
□ Checkpoint: resume-from-checkpoint works
□ Temperature = 0 on all LLM calls
□ Column names: exact match including hyphen in "Step through-Phototherapy"
□ Age = "NA" when not mentioned — confirmed by partner ✅
```

---

## 10. IF SOMETHING GOES WRONG

| Symptom | Likely Cause | Fix |
|---|---|---|
| Pipeline crashes on first PDF | `_heuristic_section_split` returning None | Add `return []` not `pass` |
| `NameError: _llm_section_split` | Function not implemented | Add stub implementation |
| Step counts all NA | Pass 2 empty OR universal criteria missing | Check section assembly, add rerun |
| TB test all No | Universal criteria not in context | Verify `find_universal_criteria` includes tables |
| Age = long sentence | LLM ignoring format instruction | Strengthen age format instruction in prompt |
| Param 2 has `[UNIVERSAL CRITERIA]` text | Wrong combined_step_text used | Use `combined_step_text` not `combined_step_text_internal` |
| JSON parse errors | Trailing commas from LLM | Confirm regex strip is in `parse_json_safe` |
| Drug classified wrong | INN vs brand mismatch | Check INN_TO_BRAND dict, add missing entry |
| API 429 | Rate limit | Increase backoff, add `sleep(60)` on 429 |
| Same PDF re-ingested | pdf_cache key mismatch | Print filename keys, check exact string match |
