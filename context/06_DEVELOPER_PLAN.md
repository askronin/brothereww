# Developer Plan — `pipeline.py` (Extraction Phase)
> Single-file build plan for Param 1–12 extraction across 79 (Filename, Brand) rows.
> Access score (Param 13) is Phase 2 — out of scope here.
> Deadline: 1 Jun 2026, 9 AM IST. Today is 28 May 2026 — **~4 days**.

---

## 0. Engineering principles I'm building under

1. **Boring is good.** Single file, single provider (Groq), single ingestion path (PyMuPDF), no abstraction we don't need today. Every "in case we want to swap" hook is a bug surface; we have one provider and one dataset shape.
2. **The spreadsheet drives the pipeline, never the PDF.** Submissions tab tells us exactly which `(Filename, Brand)` pairs to fill. The PDF is opaque input.
3. **Cache aggressively, idempotent everywhere.** Same PDF appears in many rows — ingest once. Same row re-run with same inputs — produce same output. Resume from checkpoint by content key, not by index.
4. **Verbatim first, paraphrase never.** Every text param is the policy's words. The model is doing extraction, not summarisation. Prompts and validators both enforce this.
5. **Show your work in JSON.** Every LLM call returns a structured object with intermediate reasoning fields (e.g. `step_a_universal`, `step_d_or_resolution`). Cheap to add, invaluable when something is wrong.
6. **Fail loud per-row, never crash the batch.** One bad PDF should produce a row of NAs with `_warnings`, not stop processing.

---

## 1. Build order (4 days, deferring access score)

| Day | Build | Done = |
|---|---|---|
| **D1 (today, 28 May)** | Block 1 + Block 4 + ingestion sanity | `call_llm()` returns text & JSON; `ingest_pdf(Oregon-Medicaid)` returns text containing the Step 11 string |
| **D2 (29 May)** | Block 3 sectioning + Block 5 Pass 1 & Pass 2 + Block 6 verbatim check | Pass 1 + Pass 2 reproduce the Reference-tab row to ≥9/12 params |
| **D3 (30 May)** | Pass 3 (step-counting CoT) + critical validations + rerun loop + orchestrator + checkpoint | Full 79-row dry run produces a complete `result.csv`; spot-check 5 rows manually |
| **D4 (31 May AM)** | Fix highest-impact errors, finalise column names, README, requirements.txt, ZIP up | Submission package matches exact column spec; reproducible from fresh clone with `GROQ_API_KEY` env var |
| **D4 (31 May PM)** | **Phase 2 if extraction is green by noon** — implement `classify_bucket()` + `compute_within_bucket_score()` + smoke-test against Reference tab | Otherwise: submit with empty `Access Score` column and document rationale in README |

---

## 2. Block-by-block plan, with the decisions I'm making

### Block 1 — Config & constants
**Already updated.** Two model constants (`GROQ_TEXT_MODEL`, `GROQ_VISION_MODEL`), one API key, one PDF directory pointing at `Sample_PsO_ADS_Track/`. `TEMPERATURE = 0.0` is mandatory and not parameterised. `MIN_CHARS_PER_PAGE = 100` triggers the vision fallback.

**Decision I'm making here:** No environment-toggleable model swap. If we later need a different model, change the constant.

### Block 2 — File ingestion
**Already updated.** Single function `ingest_pdf()`:
1. Open with PyMuPDF.
2. Per-page `page.get_text("text")`.
3. If page text < 100 chars, render at 200 DPI → PNG → base64 → vision model call → use that text.
4. Concatenate with `===== PAGE N =====` markers.

**Decisions:**
- **PyMuPDF over pdfplumber.** PyMuPDF is faster, preserves layout reliably enough for our needs, and bundles page-rendering for the vision fallback — one library does both jobs.
- **Page markers preserved into context.** Negligible token cost, enormous value for debug ("model claims it found X on page 8 — what's on page 8?").
- **Tables.** PyMuPDF's default text extraction loses some table structure. Acceptable for this pipeline because policies overwhelmingly express criteria in numbered prose, not pure tables. If a table-heavy page comes through and breaks extraction, the vision fallback covers it. Don't pre-emptively add `page.find_tables()` complexity.
- **No batching of vision calls.** One image per request — Groq's vision endpoint accepts one `image_url` per message cleanly and stays inside payload limits.

### Block 3 — Document sectioning (**the most under-spec'd block; needs design**)

This is where the existing architecture has the largest gap (`_heuristic_section_split` is essentially empty). My approach:

**Rule: only section when context is too large.**

```
if len(full_text) < 50_000:           # ~12K tokens of text
    use full_text as the context
else:
    apply windowed section assembly (below)
```

For the audited 70-PDF set, **~30 files exceed 50K chars** and will exercise the sectioning path; ~14 exceed 200K, ~8 exceed 300K, with Oregon Medicaid the outlier at 688K. Sectioning is **core path, not long tail** — it must work reliably on ~40% of the dataset. Build the small-doc fast path first to get a working pipeline end-to-end, then validate sectioning on the medium-sized docs (50K–200K) before touching Oregon Medicaid.

**Windowed section assembly** for the large docs:

1. **Locate target drug** — find all matches for `drug_aliases` (brand + INN + biosimilar suffixes) in `full_text`. Use a case-insensitive `re.finditer` with word boundaries to avoid matching `tremfyaXYZ`.
2. **Window each match** — take ±8000 chars around each match. Coalesce overlapping windows. Cap total drug-section length at ~20K chars.
3. **Find universal block** — search the full doc for the FIRST occurrence of any of `["General Authorization Guidelines", "General Criteria", "Universal Criteria", "General Approval", "Approval Criteria:"]`. Take the section from that anchor to the next major header. If none of those headers appear, prepend the first 5K chars of the document — most policies put universal criteria up front.
4. **Find preferred/non-preferred classification tables** — search for `["Preferred Agent", "Non-Preferred", "Preferred Status"]`. Take ±2000 chars around each match. These tables are critical for Aetna-style class-level rules (e.g. "Non-Preferred CAM Antagonists require…").
5. **Find reauth block** — search for `["Renewal Criteria", "Renewal Approval", "Reauthorization", "Continued Approval", "Continuation Criteria"]`. Take ±3000 chars around the first match.
6. **Assemble** with explicit labels:
   ```
   [UNIVERSAL CRITERIA]
   …universal block…
   
   [CLASSIFICATION TABLES]
   …preferred/non-preferred…
   
   [DRUG-SPECIFIC: TREMFYA for Plaque Psoriasis]
   …windowed text around drug name…
   
   [REAUTHORIZATION]
   …reauth block…
   ```
7. **Labels are internal.** Pass 1 and Pass 3 see them. Pass 2 produces TWO outputs: a labelled version for Pass 3 input, a clean version for `result.csv`. (This is the `combined_step_text_internal` vs `combined_step_text` split already documented.)

**LLM-based section finder as fallback** (per partner direction). Three tiers:

| Tier | Trigger | Action |
|---|---|---|
| **1** | `len(full_text) < 50K` chars | Pass full doc, no sectioning |
| **2** | Heuristic finds drug-window + at least one of universal/classification/reauth | Use the heuristic-assembled context |
| **3** | Heuristic returns nothing useful (no drug match, or no universal/reauth anchor found) | Call `_llm_section_split()` — sample the doc (first 4K + middle 2K + last 2K) to the text model, ask it to return page-number anchors for `universal`, `drug_specific`, `reauth` sections, then slice and assemble |

The LLM-fallback prompt asks for **page anchors** (e.g. `"universal": [377, 383]`) rather than asking the model to return section *text* — anchors are tiny tokens, the slicing is deterministic, and we don't risk the LLM paraphrasing the policy in transit. If the LLM fallback also fails to find sections (returns empty / unparseable), final fallback is truncated full doc (60K chars).

**Drug-not-found case.** If `full_text` contains zero matches for any drug alias: the policy genuinely doesn't cover this drug. Return assembled context = "[DRUG NOT FOUND IN POLICY]" and let Pass 1 return NAs across the board. This is a valid output, not an error.

### Block 4 — LLM interface
**Already updated.** Two private callers (`_call_groq_text`, `_call_groq_vision`), one public router `call_llm(vision=False, image_b64=None)`. `retry()` wraps them with exponential backoff and dict logging.

**Decisions:**
- **`parse_json_safe()` adds a second-chance retry.** If JSON parsing fails after fence-stripping and trailing-comma cleanup, re-prompt once with a system message of `"Your previous response was not valid JSON. Return ONLY the JSON object."` That catches ~80% of Llama's JSON failures without burning the row.
- **`retry()` distinguishes 429 from other errors.** On 429: sleep 30s before retry (rate limit needs real wait, not exponential backoff from 2s). On 5xx / connection error: exponential backoff. On 4xx other: do not retry (probably a malformed payload).

### Block 5 — Extraction passes

**Pass 1 — simple params** (Age, TB Test, Initial Auth, Reauth Duration, Reauth Requirements, Specialist Types, Quantity Limits). 7 fields.

**Pass 2 — step therapy text** (verbatim). Output: `universal_step_text`, `indication_specific_step_text`, `has_step_therapy`, plus the two-variant combined strings.

**Pass 3 — step-counting CoT** consuming the labelled `combined_step_text_internal` from Pass 2. Output: `steps_brands`, `steps_generic`, `step_phototherapy`, plus the reasoning trace.

**Three passes not one.** Tempting to do everything in one prompt to save calls — don't. Single-prompt extraction is where this kind of pipeline goes to die: the model conflates fields, drops some, hallucinates others. Three small prompts × Temperature 0 × ~240 calls is well within budget.

**Pass-3 prompt reinforcements I'm adding to the existing draft:**
- An explicit `IGNORE: criteria stated only for other indications (PsA, Crohn's, UC, etc.)` directive — many policies bundle indications and the model picks up the wrong one.
- Two named worked examples in the prompt (Reference-tab Yesintek case + Oregon Medicaid TREMFYA case), so the model has 1 OR-resolved case and 1 all-AND case to anchor on.
- An explicit `"Humira OR Enbrel" = 1 branded step` micro-example to head off the most common counting error.

### Block 6 — Validation

Splits cleanly into three groups:

| Group | Rules | Effect of failure |
|---|---|---|
| **Format/derivation** (always run) | `rule_age_format`, `rule_auth_duration`, `rule_step_na_format`, `rule_quantity_limits_strict`, `rule_reauth_required` | Mutate params in place; no rerun |
| **Critical** (per-row) | `critical_verbatim_check`, `critical_step_extraction_check` | Trigger rerun (max 2) |
| **Advisory** (per-row) | `advisory_consistency_checks` | Log warning only |

**Verbatim check upgrade I'm making.** Current implementation uses normalised-whitespace substring match. That's too brittle (PDF extraction inserts soft hyphens, weird Unicode dashes, ligatures). Replace with a **token-recall check**: tokenise both extracted-value and source, lowercase, strip punctuation, require ≥80% of extracted tokens appear in source. Catches paraphrasing while tolerating PDF noise.

**Step-count cross-check.** After Pass 3, count how many BRANDED_DRUGS names appear in `combined_step_text`. If LLM's `steps_brands` differs from this naive count by ≥2, flag in `_warnings`. Don't auto-rerun — the LLM is often *right* and we're just rough-counting, but the disagreement is worth a human glance during spot-checks.

### Block 7 — Access score
**Deferred. Stubs only.** See [05_CONSTRAINTS.md §Param 13](./05_CONSTRAINTS.md) for the Phase-2 spec.

### Block 8 — Orchestrator

Per-row pipeline:
```
ingest_pdf (cached per filename)
  → split_sections + assemble_context (per drug — not cached)
  → Pass 1 → Pass 2 → Pass 3
  → validate_all
  → if critical_failures and rerun_count < 2: recurse
  → else: emit row (with _warnings if applicable)
```

**Checkpoint design.** Persist `results: dict[(filename, brand), params]` — keyed by tuple, not by index. On startup, load checkpoint and skip any `(filename, brand)` already present. Write checkpoint after every 10 rows. This makes the pipeline interruptible *and* tolerant to spreadsheet edits (adding/reordering rows doesn't invalidate prior work).

**Failure budget.** If a row fails all 2 reruns, emit a row of NAs with `_warnings = ["EXTRACTION_FAILED_AFTER_RERUNS"]` and move on. Track failures per filename — if one filename fails on multiple drugs, log it loudly so we can manually inspect that PDF.

### Block 9 — Output

**Column map locked.** The 15 column names are case- and punctuation-sensitive — see [04_IMPORTANT_NOTES.md §7.2](./04_IMPORTANT_NOTES.md#72-exact-column-names-case-sensitive). Three names to triple-check before submission:
- `Step through-Phototherapy` (hyphen, no space)
- `TB Test required` (lowercase r in "required")
- `Initial Authorization Duration(in-months)` (no space before `(`)

**Access Score column writes empty string in Phase 1.** Column must exist; cell must be empty. This is the deliberate Phase-2 deferral signal.

---

## 3. Decisions I'm making to close previously-open questions

| Question (where it was open) | My call | Reasoning |
|---|---|---|
| Section the document with LLM if heuristic fails? | **Yes** (partner direction). LLM returns page-number anchors only, never section text. Final fallback if LLM fails: truncated full doc (60K chars). | Three-tier: full-doc → heuristic → LLM-anchor → truncated-full-doc. |
| Vision OCR — whole doc or per page? | **Per page**, only for pages below `MIN_CHARS_PER_PAGE`. | Token budget; the audited dataset has zero such pages, so the fallback usually doesn't fire. |
| What if PDF for a row isn't on disk? | **Log warning, emit NA row, continue.** Do not crash. | Per-row failure budget already in design. |
| `Indication` column in Submissions? | **Hardcode `"Plaque Psoriasis"`.** Document the assumption. | Dataset is PsO-only per problem statement. If the input spreadsheet has an Indication column, we read it and pass it through; if not, we use the hardcode. |
| Age when not mentioned — `"NA"` vs `"No"`? | **`"NA"`** (partner-confirmed override, already in `rule_age_format`). | Partner instruction takes precedence over the Additional-Extracted-Data tab's `"No"` convention. |
| Run sectioning + LLM on all PDFs, even tiny ones? | **No.** Bypass sectioning entirely when full text < 50K chars. | 64 of 70 PDFs are small enough; skip the section-assembly complexity for them. |
| JSON parse fails after retry? | **Emit row of NAs for that pass**, log raw response. | Don't infinite-loop. The row goes through with partial NAs and a `_warnings` entry. |
| Vision fallback model on Groq? | **`meta-llama/llama-4-scout-17b-16e-instruct`** (not Maverick). | Scout's 17B-active 16-expert MoE is the right speed/quality point for single-page OCR; Maverick's 128 experts buy capability we don't need for page-image-to-text. |
| What about `apremilast` vs `otezla` brand/generic classification? | **Use the XLSX-loaded `BRANDED_DRUGS` set** (already the design). Otezla is branded. INN→Brand mapping handles policy text that says "apremilast". | No hardcoded INN classifications. |
| Reference-tab smoke test as a gate? | **Yes — `--smoke-test` flag on `pipeline.py` runs only the Reference row and exits.** | Cheap CI gate before burning ~240 LLM calls on 79 rows. |
| Tofacitinib / Xeljanz classification (JAK inhibitor as step)? | **Branded** (added to `TARGETED_SYNTHETICS_NOT_IN_BASKET`). | Per business rules: targeted synthetic DMARDs count as branded. |
| What if a policy distinguishes mild vs moderate-to-severe PsO? | **Extract moderate-to-severe only.** Prompt explicitly scopes to "moderate to severe plaque psoriasis". | Business rules tab specifies this. |
| Biosimilar substitution — does failing a biosimilar count as failing the reference product? | **Treat each as separate.** Don't conflate. If a policy says "must fail Yesintek" (Stelara biosimilar), count Yesintek as the branded step, not Stelara. | Conservative; matches verbatim-extraction philosophy. Flag in `_warnings` if a row's branded drug appears nowhere in step text. |

---

## 4. Edge cases by parameter

| Param | Edge case | Handling |
|---|---|---|
| Age | Two ages stated (e.g. ≥18 for most, ≥4 for Enbrel in same section) | Capture youngest. Prompt directive. |
| Age | "Per FDA approved age" with no number | Output `"FDA approved age"`. |
| Age | Age stated for other indication only | Output `"NA"` (PsO-specific only). |
| Step text | OR within step ("Humira OR Enbrel") vs OR between steps ("biologic OR MTX") | Pass 3 prompt has explicit examples for both. |
| Step text | Phototherapy mentioned but only as eligibility, not step | `step_phototherapy = "No"`. |
| Step text | Step therapy stated only for severe PsO and policy distinguishes severity | Extract only moderate-to-severe path. |
| Step counts | All-AND chain with no OR | Don't force OR-resolution; just count. |
| Step counts | Unnamed step ("must try one conventional systemic") | Defaults to generic per business rules. |
| Step counts | Class reference ("CAM antagonist") and target drug is in that class | Counts as 1 branded step. |
| TB test | Mentioned in universal but not drug section | Yes (universal applies). |
| TB test | Mentioned as recommendation, not requirement | No (not a PA gate). |
| Initial auth | Stated globally for all drugs in policy | Use the global value. |
| Initial auth | PsO is covered but duration not stated | `"Unspecified"`. |
| Reauth duration | Not mentioned at all | `"NA"` (not `"Unspecified"` — different rules from Initial). |
| Reauth required | Derived from duration OR requirements being non-NA | Never asked to LLM. |
| Reauth requirements | Several criteria listed | Verbatim, include all. |
| Specialist | "Appropriate specialist" without naming | `"NA"`. |
| Specialist | Multiple types ("dermatologist or rheumatologist") | Comma-separated. |
| Quantity limits | Labelled "dosing" instead of "quantity limit" | Reject (`rule_quantity_limits_strict`). |
| Quantity limits | Generic phrase "quantity limits exist" with no specifics | Reject. |
| Quantity limits | Drug+strength+quantity+days supply | Accept verbatim. |

---

## 5. Validation strategy — gates, not hopes

**Gate 1 (D2):** Reference-tab smoke test. Run pipeline on the Reference row's `(Filename, Brand)`. Must produce ≥9/12 params matching ground truth. If not, **do not proceed** — extraction has a fundamental bug.

**Gate 2 (D3):** Manual spot-check on 5 rows. Pick 5 rows spanning small/large PDFs and TREMFYA/STELARA. Manually fill all 12 params by reading the PDF. Compare to pipeline. Target: ≥90% per-cell agreement. Disagreements get categorised: model error (fix prompt), source ambiguity (document interpretation), bug (fix code).

**Gate 3 (D3-D4):** Cross-param consistency. Automated. Runs after batch completes:
- `step_text` non-empty AND both counts NA → flag for rerun
- `reauth_required = "No"` but `reauth_duration` is a number → impossible, fix derivation
- `quantity_limits` contains "dosage" → strict rule should have caught it
- `age` longer than 30 chars or contains spaces beyond `>=N` → format failure

**Gate 4 (D4):** Verbatim spot-check. For 10 random rows, copy the extracted step-therapy text and search for it in the source PDF text. Must be findable (modulo whitespace and ligatures). Target: 10/10.

**Additional Extracted Data tab as background validator** — for any of the 439 rows that overlap our 79 inputs, compare our outputs cell by cell. Disagreements are signal, not blocking — that tab may not be fully canonical.

---

## 6. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Llama 3.3 70B JSON output instability | Medium | High | `parse_json_safe` + retry-with-reprompt; per-row `_warnings` |
| Sectioning misses the universal block on a Structure-B (Aetna) doc | Medium | High | Fallback: send truncated full doc (60K chars) instead |
| Step-counting OR-resolution error | Medium | High | Two worked examples in prompt, cross-check via naive brand-name count |
| Verbatim check too strict, rejects correct extractions | Medium | Medium | Token-recall (80%) instead of substring; advisory-only when ambiguous |
| Groq rate limit hit during batch run | Low (well under quota) | Medium | Exponential backoff + 30s sleep on 429 |
| PDF not found on disk for a Submissions row | Low | Low | Per-row NA + warning; batch continues |
| Vision fallback fires on a page where it shouldn't and returns garbage | Low (audit shows 0 sparse pages) | Medium | Log every fallback invocation; if quality is suspect, lower threshold from 100 to 30 |
| Column name typo in `result.csv` | Low | High | Hardcoded `COLUMN_MAP`, snapshot-test against the Submissions tab header row before write |
| Spreadsheet has an `Indication` column we ignore | Low | Low | Read it if present, hardcode otherwise; log which path taken |
| Checkpoint corruption mid-write | Very low | Medium | Write to `checkpoint.json.tmp` then `os.replace` to `checkpoint.json` (atomic) |

---

## 7. New gaps I'm closing that weren't in the existing docs

1. **No atomic checkpoint write** in the original orchestrator. Added: write-temp-then-rename pattern.
2. **No 429-specific handling** in the original retry — added: detect Groq's rate-limit error code and sleep 30s, not 2s.
3. **No reprompt-on-bad-JSON** in `parse_json_safe`. Added: one retry with a stricter system message before failing the pass.
4. **No drug-not-found case** in `find_drug_section`. Added: return a `[DRUG NOT FOUND]` marker; let Pass 1 produce NAs.
5. **No indication-scope filter** in Pass 3 prompt. Added: explicit "ignore criteria that apply only to other indications".
6. **No naive-count cross-check** on step-counts. Added: count brand-name token occurrences as a sanity check against the LLM's count.
7. **No smoke-test mode** (`--smoke-test` flag) on `pipeline.py`. Added: runs Reference-tab row only, prints diff vs ground truth, exits.
8. **No column-header snapshot test** before writing `result.csv`. Added: compare `COLUMN_MAP.values()` against the Submissions tab header row at startup.
9. **Atomic per-row idempotency.** Checkpoint keyed by `(filename, brand)` not row index — survives spreadsheet edits / row reordering.

---

## 8. Final deliverable shape

```
submission.zip
├── pipeline.py           # single file, ~900 lines target
├── result.csv            # 79 rows × 15 columns, exact header match
├── requirements.txt      # groq, pymupdf, pandas, openpyxl
├── README.md             # 1-page: env var, run cmd, smoke-test, assumptions
└── debug_log.json        # JSONL: every retry, warning, vision fallback, rerun
```

**README content (~1 page):**
1. **Setup:** `pip install -r requirements.txt`; `export GROQ_API_KEY=...`
2. **Run:** `python pipeline.py` (full batch) or `python pipeline.py --smoke-test` (Reference row only)
3. **Output:** `result.csv` in working directory
4. **LLM choice rationale:** open-source Groq Llama 3.3 70B for text, Llama 4 Scout for vision OCR fallback. Reproducible: temperature 0, deterministic seeds, no provider variability across runs
5. **Assumptions explicit:** Age=NA when not mentioned, OR→least-restrictive path, Yesintek treated as distinct from Stelara for step counting, indication hardcoded to Plaque Psoriasis, Access Score deferred to Phase 2 (column empty)
6. **Known limitations:** within-bucket access score weights would have been hand-designed; FDA-label-date vs current-FDA-label scoring decision pending

---

## 9. Resolved questions (28 May)

| Question | Decision |
|---|---|
| Sectioning fallback if heuristic fails | **LLM-based section finder** added as Tier 3 (returns page anchors only, not section text). Tier 4 final fallback: truncated full doc 60K chars. **SUPERSEDED on 29 May — see §11.1 outline-driven design.** |
| Phase-2 access score timing | **Defer entirely.** Column exists, cells empty in Phase 1. |
| Smoke-test gate behaviour | **Halt** if multi-source validation set produces < 85% per-cell agreement. See §11.4. |
| Indication source | **Read from spreadsheet if column present**, else hardcode `"Plaque Psoriasis"`. Log which path was taken. |

---

## 10. Critique resolution — context

On 28 May, a third-party senior-engineer review of the plan surfaced 12 issues split into 5 critical, 7 serious. §11 below contains the resolved design for each. All 12 are now locked into both this plan and [03_PIPELINE_ARCHITECTURE.md](./03_PIPELINE_ARCHITECTURE.md).

---

## 11. Resolved findings — designs in detail

### 11.1 (Critical #1) Outline-driven sectioning — replaces all prior sectioning tiers

**The problem.** The previous 4-tier ladder (full-doc → heuristic-window → LLM-anchor → truncated-fallback) collapses on the 441-page Oregon Medicaid doc: the LLM-anchor sampler sees < 2% of the document and cannot anchor pages it never sees. Heuristic windowing offers no guarantee that the universal block is captured. The truncated-full-doc fallback throws away ~92% of a large doc.

**The design — three stages, one path.**

**Stage A — Local outline extraction** (no API call):
1. `doc.get_toc()` — use PDF bookmarks if present (~30% of audited PDFs).
2. If no TOC, scan each page with `page.get_text("dict")` (gives font size, weight, bbox per span). A line is a heading when:
   - `font_size > 1.15 × median_font_size_on_page`, OR
   - span is bold AND `len(text) < 100`, OR
   - line is ALL CAPS AND `len(text) < 100`, OR
   - line matches `^(Step|Section|Part)\s+\d+/i`, OR
   - line matches `^(General|Universal|Initial|Renewal|Reauthorization|Continuation|Quantity|Diagnosis|Approval|Coverage)\b`, OR
   - line matches `^\d+\.\s` at indent 0.
3. Output: ordered list of `(page_num, level, heading_text, char_offset)` tuples — typically 150–300 entries for large docs, 15–30 for small docs.
4. Cached per PDF. Re-used by every row pointing at that PDF.

**Stage B — Outline-to-section mapping** (one LLM call per (PDF, drug)):
- Input: outline only (~5–10K tokens), drug name, indication.
- Prompt asks for exact heading text that starts each of `universal_criteria`, `classification_tables`, `drug_specific_criteria`, `reauth_criteria`. Null if absent.
- Output: 4-key JSON anchors → look up `char_offset` from Stage A → deterministic slice of `full_text` between anchor offsets.
- Cached per `(filename, drug)`. Different drug on same PDF = re-run Stage B only, not Stage A or ingestion.

**Stage C — Recursive zoom** (only if any section > 30K tokens):
- Re-run Stage A+B on the outline of just that section.
- Find sub-headings relevant to TB / severity / classification / diagnosis.
- Concatenate only those sub-sections.

**Net result.** Oregon Medicaid TREMFYA: 688K-char full doc → ~10K-token outline → 1 LLM call → ~12K-token assembled context. Two orders of magnitude reduction. The LLM never sees policy body text during sectioning.

**Where it lands.** [03_PIPELINE_ARCHITECTURE.md §Block 3](./03_PIPELINE_ARCHITECTURE.md) — full rewrite.

### 11.2 (Critical #2) Drug-not-found — loud raise across three layers

**The problem.** Treating a `find_drug_section()` empty result as a "valid NA row" hides sectioning bugs. Every Submissions row is curated; the drug IS in that policy.

**The design.**

**Layer 1 — Pre-flight check** at startup, before any row processes:
```
For each (Filename, Brand) in Submissions:
  If Filename not in PDF_DIR/: → MISSING_FILES list
  Else: extract outline, scan for drug aliases
    If zero matches in outline AND zero matches in full_text → ZERO_MATCH_FILES

If MISSING_FILES: HALT loudly with the list (we can't proceed).
If ZERO_MATCH_FILES: PRINT warning + dump first 2 pages of each to debug log
                     for manual inspection. Allow batch to proceed.
```

**Layer 2 — Per-row guard** inside orchestrator:
- If Stage A+B return empty drug_specific section AND the brand's known aliases don't appear anywhere in full_text: emit row tagged `_warnings = ["CRITICAL_DRUG_NOT_FOUND"]` with all NAs.
- The row exists in the CSV (we never skip), but it's loudly broken.

**Layer 3 — End-of-batch summary**:
- Print `X rows failed with CRITICAL_DRUG_NOT_FOUND` followed by the list.
- These get a forced manual-rerun pass before submission.

**Where it lands.** [03_PIPELINE_ARCHITECTURE.md §Block 8 — `preflight_check()` + `process_single_row()` guard + `print_summary()`](./03_PIPELINE_ARCHITECTURE.md).

### 11.3 (Critical #3) TPM rate limit — pre-flight measurement + adaptive throttle

**The problem.** Groq's free-tier ceiling on `llama-3.3-70b-versatile` is ~6,000 TPM, not RPD. Pre-fix average context (~30K tokens) × 3 passes × 79 rows = ~7M tokens = ~20 hours of TPM-throttled wall-clock. Plan ignored this.

**The design — two layers.**

**Layer 1 — Pre-flight token budget** (D1 task):
- Dry-run Stages A+B for all 70 PDFs (no Pass 1/2/3 calls — just outline + section map).
- Measure assembled-context token count per row.
- Sum: predicted total tokens.
- If predicted > daily-token-cap × 0.7: force Stage C (recursive zoom) on all sections > 15K tokens.
- Print the prediction. We commit knowing the wall-clock.

**Layer 2 — `TokenBudget` class** wrapping every `call_llm()`:
```python
class TokenBudget:
    def __init__(self, tpm=5500):           # conservative under 6K cap
        self.window = deque()                # (timestamp, tokens)
    def consume(self, est_tokens):
        now = time.time()
        while self.window and now - self.window[0][0] > 60:
            self.window.popleft()
        used = sum(t for _, t in self.window)
        if used + est_tokens > self.tpm:
            sleep_for = 60 - (now - self.window[0][0]) + 0.5
            time.sleep(sleep_for)
        self.window.append((now, est_tokens))
```
- `est_tokens` = `len(prompt) // 4 + max_tokens`.
- No 429 retries needed — we never send a call that would breach budget.
- Adaptive: if Groq's serving region changes (lower TPM), update one constant.

**Combined with outline-driven sectioning** (§11.1), expected wall-clock for 79 rows drops from ~20 hours to **~3 hours**.

**Where it lands.** [03_PIPELINE_ARCHITECTURE.md §Block 4 — `TokenBudget` class + `call_llm()` integration](./03_PIPELINE_ARCHITECTURE.md). Pre-flight in Block 8.

### 11.4 (Critical #4) Multi-source validation set replaces single-row smoke test

**The problem.** Reference tab has 1 row. Passing 9/12 on one row is sanity, not validation. The Reference row is also the row our prompts were designed against — near-trivially overfittable.

**The design — three-source validation set built before D3:**

| Source | Size | Confidence | Used as |
|---|---|---|---|
| Reference tab | 1 row | Gold (canonical worked example) | Per-param sanity |
| Manual labels | 5 rows | Gold (labelled by us during D1–D2) | Per-row + per-param accuracy |
| Additional Extracted Data tab overlap | ?–? rows (TBD §11.11) | Silver (use only if tab content verified) | Aggregate per-param accuracy |

**Gate threshold:** ≥85% per-cell agreement across the combined set. Per-param tolerance:

| Param type | Match rule |
|---|---|
| Age, durations, counts, Yes/No flags | Exact equality (after `rule_*_format`) |
| Step-therapy text, reauth-requirements text, quantity-limits text | Token-recall ≥ 0.70 vs ground truth |
| Specialist types | Set-equality after lowercase + strip |

If gate fails: HALT batch run. Print the failing rows + failing params. We fix or revise the rule before re-attempting.

**Where it lands.** [03_PIPELINE_ARCHITECTURE.md §Block 9 — `load_validation_set()` + `run_smoke_test()`](./03_PIPELINE_ARCHITECTURE.md). CLI flag `python pipeline.py --smoke-test`.

### 11.5 (Critical #5) Rule-based step counter in parallel with Pass 3

**The problem.** If Pass 3 fails (bad CoT, malformed JSON, prompt regression mid-batch), we have no fallback. On a 4-day timeline this is a project-ending risk.

**Update 29 May (Codex finding #7):** The earlier draft of this counter only matched named drugs against `GENERIC_DRUGS`. Business rules count topical corticosteroids, other topicals (calcipotriene/tazarotene/anthralin), conventional systemics, NSAIDs, and unnamed conventional steps as generic too — none of which appear as named drugs. The counter now combines named-drug hits with pattern hits (`GENERIC_STEP_PATTERNS` in Block 6), clustered by 80-char proximity to avoid double-counting overlapping matches on the same step description.

**The design.** A deterministic counter runs alongside Pass 3 on every row:

```python
def rule_based_step_count(combined_step_text: str) -> dict:
    text = combined_step_text.lower()
    
    branded_hits = {b for b in BRANDED_DRUGS if re.search(rf"\b{re.escape(b)}\b", text)}
    generic_hits = {g for g in GENERIC_DRUGS if re.search(rf"\b{re.escape(g)}\b", text)}
    
    photo_mentioned = any(t in text for t in PHOTOTHERAPY_TERMS)
    # phototherapy "in OR" pattern: an OR within 80 chars before a phototherapy term
    photo_in_or = bool(re.search(r"\bor\b[^.]{0,80}(phototherapy|puva|uvb|light therapy)", text))
    
    # Cluster brand mentions that appear within ~50 chars of each other 
    # joined by "or" — those are ONE step (Humira OR Enbrel = 1 step)
    brand_clusters = _cluster_by_or_proximity(branded_hits, text, window=50)
    
    return {
        "brands_rule":   str(len(brand_clusters)) if brand_clusters else "NA",
        "generic_rule":  str(len(generic_hits))   if generic_hits   else "NA",
        "photo_rule":    "Yes" if (photo_mentioned and not photo_in_or) 
                         else "No"  if photo_mentioned 
                         else "NA"
    }
```

**Reconciliation:**

| Condition | Action |
|---|---|
| LLM count == rule count | High confidence, emit LLM, no flag |
| `\|LLM − rule\| ≤ 1` | Medium confidence, emit LLM, flag `STEP_COUNT_MINOR_DISAGREE` |
| `\|LLM − rule\| ≥ 2` | Low confidence, emit LLM, flag `STEP_COUNT_MAJOR_DISAGREE` for manual review |
| LLM call failed (parse/exception, after retry) | **Emit rule counts**, flag `STEP_COUNT_RULE_FALLBACK` |

The rule counter is conservative — it underestimates complex OR-resolution cases. That's fine: in those cases it disagrees and the LLM wins, but we never have zero answer.

**Where it lands.** [03_PIPELINE_ARCHITECTURE.md §Block 6 — `rule_based_step_count()` + `reconcile_step_counts()`](./03_PIPELINE_ARCHITECTURE.md).

### 11.6 (Serious #6) ±8000-char windowing — deleted

Obsoleted by §11.1. The windowing concept disappears from the architecture entirely.

### 11.7 (Serious #7) Pass 2 — single blob, not split

**The problem.** Forcing Pass 2 to classify each step as "universal" vs "indication-specific" adds an error source that propagates to Pass 3.

**The design.** Pass 2 returns one field: `combined_step_text` (verbatim, AND/OR preserved). The internal-vs-clean variant distinction (`combined_step_text_internal`, `combined_step_text`) collapses too — there's only one variant now.

Pass 3 prompt absorbs the classification responsibility:
```
"In your step_a_universal list, include only criteria that appear in 
 [UNIVERSAL CRITERIA] section markers from the assembled context, or 
 that the policy explicitly labels 'General' / 'Universal' / 'Approval 
 Criteria'. In step_b_indication_specific, include only criteria that 
 mention {drug} or {indication} by name."
```

The labels Pass 3 sees come from the section assembly (Stage B output), not from Pass 2's judgment. One less error surface.

**Where it lands.** [03_PIPELINE_ARCHITECTURE.md §Block 5 Pass 2 + Pass 3](./03_PIPELINE_ARCHITECTURE.md).

### 11.8 (Serious #8) Token-recall threshold — calibrate from data

**The problem.** 0.80 was plucked from the air.

**The design.** D2 calibration task:
1. After Pass 2 runs on Reference row + 5 manually-labelled rows, compute `token_recall(extracted_text, source_full_text)` for each row's `combined_step_text`, `reauth_requirements_text`, `quantity_limits_text`.
2. Per field, take the 5th percentile of the distribution as the threshold.
3. Log per-field thresholds; checkpoint them.

If we only have 1 calibration point (Reference) by D2: start permissive at 0.70 across the board, tighten on D3 after running the full 79.

**Where it lands.** [03_PIPELINE_ARCHITECTURE.md §Block 6 — `calibrate_verbatim_thresholds()` + per-field threshold dict](./03_PIPELINE_ARCHITECTURE.md).

### 11.9 (Serious #9) Semantic contradiction rules — advisory layer

**The problem.** Verbatim presence detects hallucination, not correctness. No semantic checks.

**The design — advisory rules, no rerun:**

| Rule | Trigger | Flag |
|---|---|---|
| TB contradiction | `tb_test_required="Yes"` AND step/source text contains "no TB testing" | `TB_CONTRADICTION` |
| Brand missed | Step text mentions a known `BRANDED_DRUGS` member AND `steps_brands == "NA"` | `BRAND_MISSED` |
| Reauth contradiction | `reauth_required="No"` AND `reauth_duration` is numeric | `REAUTH_CONTRADICTION` |
| Age out of range | Age numeric < 1 or > 99 | `AGE_OUT_OF_RANGE` |
| Specialist vague | `specialist_types` contains "appropriate", "qualified", "licensed" | `SPECIALIST_VAGUE` |
| Auth duration outlier | Initial auth > 24 months or < 1 month | `AUTH_DURATION_OUTLIER` |

These tag rows for manual review. They don't rerun. They surface in the end-of-batch summary.

**Where it lands.** [03_PIPELINE_ARCHITECTURE.md §Block 6 — `semantic_contradiction_checks()`](./03_PIPELINE_ARCHITECTURE.md).

### 11.10 (Serious #10) Submissions ↔ disk cross-reference at startup

**The design.**
```python
submissions_files = set(submissions_df["Filename"])
disk_files = {f.name for f in PDF_DIR.glob("*.pdf")}
missing = submissions_files - disk_files
unused  = disk_files - submissions_files

if missing:
    raise RuntimeError(f"Submissions references {len(missing)} missing PDFs: {sorted(missing)}")
if unused:
    print(f"INFO: {len(unused)} PDFs on disk not in Submissions: {sorted(unused)}")
```
Halts on missing. Tolerant of extra. Loud on both.

**Where it lands.** [03_PIPELINE_ARCHITECTURE.md §Block 8 — `preflight_check()`](./03_PIPELINE_ARCHITECTURE.md).

### 11.11 (Serious #11) Verify Additional Extracted Data tab content

**The design.** D1 task — load it, print structure:
```python
df = pd.read_excel(XLSX_PATH, sheet_name="Additional Extracted Data")
print(f"Rows: {len(df)}, Columns: {list(df.columns)}")
print(f"Unique filenames: {df['Filename'].nunique()}")
print(f"Overlap with Submissions: {len(set(df['Filename']) & set(submissions_df['Filename']))}")
```

Outcome table:

| Overlap | Action |
|---|---|
| 0 rows | Drop this tab from the validation plan entirely. Stop referencing it. |
| 1–10 rows | Use as supplementary silver-standard for spot checks |
| >10 rows | Use as aggregate per-param accuracy gate alongside Reference + manual |

**Where it lands.** [03_PIPELINE_ARCHITECTURE.md §Block 9 — `inspect_additional_data_tab()`](./03_PIPELINE_ARCHITECTURE.md).

### 11.12 (Serious #12) Determinism — measure, don't assume

**The design.** D1 task — 5-minute test:
```python
prompt = "Extract the minimum age from: 'Member must be 18 years or older'. Return JSON: {\"age\": \">=N\"}"
hashes = {hashlib.md5(call_llm(prompt).encode()).hexdigest() for _ in range(3)}
determinism = "STABLE" if len(hashes) == 1 else "UNSTABLE"
log_debug({"event": "determinism_test", "result": determinism, "n_unique_outputs": len(hashes)})
```

| Outcome | Action |
|---|---|
| Stable (1 unique output) | No action. Document in README. |
| Minor drift (2 outputs differing in whitespace) | No action. Normalise output strings on read. |
| Significant drift (3 unique outputs) | For high-confidence rows, run Pass 1+2+3 three times, take majority per field. README discloses. |

**Where it lands.** [03_PIPELINE_ARCHITECTURE.md §Block 9 — `determinism_test()`, called by `main()` on startup](./03_PIPELINE_ARCHITECTURE.md).

---

### 11.13 (Goal-level gap discovered 29 May) Multi-brand disambiguation within a single section

**The problem.** Aetna-style PA documents put many drugs into one PsO indication section. A typical age table reads:

```
Age requirement (Plaque Psoriasis):
  ≥18 years — TREMFYA, HUMIRA, SILIQ
  ≥4 years  — ENBREL
  ≥6 years  — COSENTYX, STELARA, TALTZ
```

Our sectioning correctly returns "the PsO section". But that section contains data for **every drug in it**. When Pass 1 extracts Age for TREMFYA, the LLM has to pick the correct row from the table. With no targeted disambiguation, the model can return any of the four values.

The same risk applies to Quantity Limits (always per-drug — strength/dose differs per brand) and Specialist Types (sometimes per-drug). Lower risk for TB Test, auth durations, and reauth requirements which are usually policy-wide. Step therapy is already handled because Pass 2 + Pass 3 take a per-drug prompt and the rule-based counter cross-checks.

**Risk matrix:**

| Param | Risk | Solution |
|---|---|---|
| Age | 🔴 High | Prompt-level disambiguation + validation check |
| Quantity Limits | 🔴 High | Prompt-level disambiguation + validation check |
| Specialist Types | 🟡 Medium | Prompt-level disambiguation |
| Step Therapy text | 🟡 Medium | Already drug-scoped in Pass 2 prompt + rule counter cross-checks |
| TB Test | 🟢 Low | Mention in prompt but no extra check |
| Initial Auth Duration | 🟢 Low | Mention in prompt but no extra check |
| Reauth Duration / Requirements | 🟢 Low | Mention in prompt but no extra check |

**The design — two layers.**

**Layer 1 — Prompt-level disambiguation.** Pass 1 and Pass 2 prompts add an explicit `OTHER_BRANDS_IN_DOC` instruction:

```
TARGET DRUG: {drug}
OTHER DRUGS THAT MAY APPEAR IN THIS POLICY: {other_brands_list}

CRITICAL INSTRUCTION ON MULTI-BRAND TABLES:
If the policy lists separate values for different drugs in the same field
(e.g. "Age: ≥18 (TREMFYA, HUMIRA), ≥4 (ENBREL), ≥6 (COSENTYX, STELARA)"),
return ONLY the value associated with {drug}. Do NOT return values stated 
for OTHER_DRUGS.

If the policy gives ONE value that applies to ALL drugs in the class or 
section (e.g. "All biologics require TB screening"), use that value.
```

`other_brands_list` is computed dynamically: scan the assembled context for any brand name from `BRANDED_DRUGS ∪ GENERIC_DRUGS`. Pass only the ones that actually appear in this document, not all 35. Keeps prompt size bounded.

**Layer 2 — Validation rule** `multi_brand_ambiguity_check()`. After extraction, for each at-risk field (Age, Quantity Limits, Specialist Types):
1. Locate the extracted value in `source_text`.
2. Look at a window (300 chars for Age, 500 for Quantity Limits) around the match.
3. If another brand name (not the target) appears in that window AND the field value contains "age" / "year" / "mg" / "specialist" type keywords nearby: flag `MULTI_BRAND_AMBIGUOUS_<field>`.

This doesn't auto-rerun — manual review during the end-of-batch summary catches the rows that need a second look. Catches the failure mode where the LLM picked the wrong row but the value is internally plausible.

**Where it lands.**
- Block 3: new `get_other_brands_in_doc(source_text)` helper.
- Block 5: Pass 1 and Pass 2 prompt additions.
- Block 6: new `multi_brand_ambiguity_check()` wired into `validate_all()` as advisory.

### 11.14 manual_labels.csv — schema, procedure, ownership

**The schema** (locked):

```csv
Filename,Brand,Indication,Age,Step Therapy Requirements Documented in Policy,Number of Steps through Brands,Number of Steps through Generic,Step through-Phototherapy,TB Test required,Quantity Limits,Specialist Types,Initial Authorization Duration(in-months),Reauthorization Duration(in-months),Reauthorization Required,Reauthorization Requirements Documented in Policy
```

Exactly the COLUMN_MAP values minus `Access Score` (Phase 2), plus `Indication`. CSV at project root `manual_labels.csv`, committed.

**Which 5 rows** (stratified):

| Row | Filename | Brand | Why |
|---|---|---|---|
| 1 | Small TREMFYA PDF (~10K chars) | TREMFYA | Baseline: simple doc, dominant brand |
| 2 | Small STELARA PDF (~10K chars) | STELARA | Baseline: dominant brand, other policy structure |
| 3 | Medium multi-drug PDF (~50K chars) | TREMFYA or STELARA | Tests Aetna-style multi-brand sections |
| 4 | Large policy (Oregon Medicaid `66156-4274314.pdf`) | TREMFYA or CIMZIA | Tests outline-driven sectioning on 441-page doc |
| 5 | Wildcard — pick from a flagged `MULTI_BRAND_AMBIGUOUS` row after first pipeline run | any | Targeted test of the multi-brand fix |

Pick rows 1–4 on **D1 evening** (today) by reading PDFs by hand and filling all 12 columns. Row 5 is reserved — fill it after D2 pipeline output reveals which `(Filename, Brand)` pairs are most ambiguous.

**Procedure for labelling each row:**
1. Open the PDF, find the section relevant to that drug.
2. Copy verbatim the step therapy text, reauth requirements, quantity limits.
3. Apply our format rules ( `>=N` for age, plain number for durations, "NA" not "0", etc.)
4. Cross-check against the Business Rules tab definitions.
5. If a value is ambiguous in the policy itself, record what you decided and why (in a `_notes` column appended at the end).

**Ownership:** I (Claude) cannot create authoritative labels — these need to be made by you reading the PDFs. If you want, I can pre-fill a draft CSV with extracted values + my interpretation, you correct what's wrong. That cuts your effort to ~15 minutes of review per row.

### 11.15 Kaggle / Colab quick-start

The judges will test reproducibility on Kaggle free tier. The submission `pipeline.py` already supports environment-variable config (per Block 1 §M5/M6 fix below). Reproduction instructions for the README:

```bash
# 1. Install dependencies
pip install groq pymupdf pandas openpyxl

# 2. Set API key as Kaggle secret (or env var locally)
export GROQ_API_KEY=<your_groq_api_key>

# 3. Run
python pipeline.py \
  --pdf-dir /kaggle/input/pso-pa-pdfs/ \
  --xlsx-path /kaggle/input/business-rules/PA_Business_Rules.xlsx

# 4. Output: result.csv in working directory
```

**Notebook stub** for `notebook.ipynb` (to include in submission ZIP):
- Cell 1: `!pip install -q groq pymupdf pandas openpyxl`
- Cell 2: `import os; os.environ["GROQ_API_KEY"] = "<key>"` (judge fills in)
- Cell 3: `!python pipeline.py --pdf-dir ./pdfs/ --xlsx-path ./PA_Business_Rules.xlsx`
- Cell 4: `import pandas as pd; pd.read_csv("result.csv").head(10)`

**Cost transparency**: print expected token cost and wall-clock estimate before processing starts. Judges should see "Predicted ~2.5M tokens, ~3 hours wall-clock" so they don't kill the run mid-execution.

### 11.16 D1 morning readiness fixes — applied

The five blockers and seven specification gaps from the 29 May readiness review are applied to [03_PIPELINE_ARCHITECTURE.md](./03_PIPELINE_ARCHITECTURE.md):

| Issue | Fix location | Status |
|---|---|---|
| B1 — COLUMN_MAP key mismatch | Block 9 | ✅ Renamed `step_therapy_text` → `combined_step_text`. `format_row` raises on missing keys. |
| B2 — Pre-flight burns LLM budget | Block 8 | ✅ Cheap-only preflight (raw text scan, no vision). Shares `pdf_cache` with batch. |
| B3 — Empty-outline fallback unspecified | Block 8 | ✅ If `len(outline) < 5`, fall through to small-doc path (use full_text as context). |
| B4 — `slice_by_anchors` multi-occurrence bug | Block 3 | ✅ LLM returns `{heading, page}` tuples; lookup keys on both. |
| B5 — Pass 1/3 inconsistent prompts | Block 5 | ✅ All three passes use `SYSTEM_PROMPT_EXTRACTOR` + pass `retry_prompt`. |
| S1 — manual_labels.csv schema | §11.14 above | ✅ Schema + 5-row selection locked. |
| S2 — Reference tab columns | Block 9 | ✅ Reference tab is TRANSPOSED (`Sno., Params, Values`). `load_reference_tab_transposed()` reads it row-by-row and maps informal Params labels to canonical CSV columns. No startup halt — informational log only. |
| S3 — Additional Extracted Data tab | Block 9 | ✅ Already handled by `inspect_additional_data_tab()` returning None on unusable. |
| S4 — Daily quota vs minute quota | Block 4 | ✅ `retry()` detects daily-quota error and exits with code 2 (resume tomorrow). |
| S5 — `log_debug` is O(N²) | Block 9 | ✅ Switched to JSONL append-only. |
| S6 — `doc_handle` not closed | Block 2 + 8 | ✅ `ingest_pdf` returns open handle; `process_all_rows` closes all in `finally`. |
| S7 — Indication normalisation on validation rows | Block 9 | ✅ Applied to validation set in `load_validation_set()`. |
| M1–M8 — Minor code gaps | Various | ✅ All applied in architecture file. |

**Codex review (29 May) additional fixes applied:**

| Issue | Decision | Fix location |
|---|---|---|
| Groq vs Gemini compliance | **Stay with Groq** (partner direction). All Gemini references removed from docs. | All 5 docs |
| Target universe broader than TREMFYA/STELARA | Extraction pipeline is brand-agnostic — no change. FDA-baseline expansion deferred to Phase 2 access-score module. | [01](./01_CONTEXT_DISCUSSION.md), [02](./02_CONTEXT_PROBLEM_AND_DOCS.md), [05 Param 13](./05_CONSTRAINTS.md) |
| Reference tab is transposed (`Sno., Params, Values`) | Rewrote loader to parse row-by-row, map informal Params labels to canonical CSV columns. | [03 Block 9 `load_reference_tab_transposed`](./03_PIPELINE_ARCHITECTURE.md) |
| Additional Extracted Data tab has zero filename overlap | Already handled by `inspect_additional_data_tab` returning None. Validation now relies on Reference + manual labels. | [03 Block 9](./03_PIPELINE_ARCHITECTURE.md) |
| Sentinel inconsistency (No vs NA, etc.) | Single source-of-truth table in [05 §A](./05_CONSTRAINTS.md). Per-param sections updated to match. | [05 §A](./05_CONSTRAINTS.md), [02 params table](./02_CONTEXT_PROBLEM_AND_DOCS.md) |
| Rule counter undercounts generics | Added `GENERIC_STEP_PATTERNS` covering topical CS, other topicals, conventional systemics, NSAIDs, unnamed conventional steps. Cluster by 80-char proximity. | [03 Block 6](./03_PIPELINE_ARCHITECTURE.md), [§11.5 above](#115-critical-5-rule-based-step-counter-in-parallel-with-pass-3) |
| PDF size framing wrong ("long tail") | Updated to "core path on ~30 of 70 PDFs (~40%)". | [§11.1 above](#111-critical-1-outline-driven-sectioning--replaces-all-prior-sectioning-tiers) |
| `287728-4459856.pdf` (hematology policy expecting STELARA) | Phase 1: produces `CRITICAL_DRUG_NOT_FOUND` row with NAs. Phase 2: must map this category to Bucket 0 in access score. | [05 Param 13 Phase 2 obligations](./05_CONSTRAINTS.md) |

---

## 12. Updated 4-day timeline (reflecting §11)

| Day | Build | Done = |
|---|---|---|
| **D1 (28 May → 29 May)** | Block 1 + Block 4 (with `TokenBudget`) + Block 2 ingestion + `extract_outline()` + determinism test + Additional Extracted Data tab inspection + pre-flight cross-reference | `ingest_pdf` + `extract_outline` work on Oregon Medicaid; determinism known; pre-flight halts on missing files; ADE tab content known |
| **D2 (29 May → 30 May)** | Block 3 outline-driven sectioning (Stages A+B+C) + Pass 1 + Pass 2 (single blob) + Block 6 verbatim check with calibrated thresholds + manual labelling of 5 spot-check rows | Reference row passes ≥9/12; calibrated per-field token-recall thresholds saved; 5 manually-labelled validation rows ready |
| **D3 (30 May → 31 May)** | Pass 3 (step-text-only input) + `rule_based_step_count()` + reconciliation + semantic contradiction rules + Block 8 orchestrator + checkpointing + full 79-row dry run | `result.csv` exists with 79 rows; `_warnings` summary shows which need manual review; smoke-test gate hit ≥85% |
| **D4 AM (31 May → 1 Jun morning)** | Fix highest-impact errors from D3 dry run; final column-name snapshot test; README; requirements.txt; submission ZIP | Submission package matches exact spec; reproducible from fresh clone with `GROQ_API_KEY` env var |
| **D4 PM (1 Jun afternoon, post-deadline)** | Phase 2 (Access Score) — explicitly out of scope per partner direction | n/a |

Note: D2 calibration step (token-recall thresholds) is the only new D2 work vs the original timeline. Pre-flight + outline extraction + determinism test slot into D1's existing budget.

---

## 13. Implementation order — function-by-function build sequence

> Read top-to-bottom and execute in order. Each phase builds only on previous phases. Each phase ends with a **checkpoint** — a concrete test that demonstrates the phase works before moving on.
> 
> Single file: `pipeline.py`. Module-level globals (`BRANDED_DRUGS`, `GENERIC_DRUGS`, `groq_client`, `token_budget`) are declared at top, populated at startup. No side effects at import time — `main()` is the only entry point that touches API or filesystem.

### Phase 0 — Bootstrap (no LLM, no network) | ~15 min

**Intent:** module scaffolding + the one utility that every other function uses for logging.

| # | Function / item | Block | Notes |
|---|---|---|---|
| 1 | Imports + `__future__` annotations | top | `os`, `json`, `re`, `time`, `base64`, `io`, `tempfile`, `hashlib`, `argparse`, `pathlib.Path`, `collections.deque`, `statistics`. Plus `pandas`, `fitz`, `groq`. |
| 2 | Block 1 constants | Block 1 | All paths via `os.environ.get()`; model names; rate limits; `KNOWN_GENERICS_IN_MARKET_BASKET`, `INN_TO_BRAND`, `TARGETED_SYNTHETICS_NOT_IN_BASKET`, `PHOTOTHERAPY_TERMS`. Empty `BRANDED_DRUGS = set()` / `GENERIC_DRUGS = set()`. |
| 3 | `log_debug(entry: dict)` — JSONL append-only | Block 9 | **Must exist before anything else** — every function below calls it. |

**Depends on:** nothing.
**Checkpoint:** `python pipeline.py --help` prints the argparse usage. No crash, no API call, no file write.

### Phase 1 — Drug + spreadsheet loaders | ~20 min

**Intent:** read the inputs that don't need a PDF.

| # | Function | Block | Notes |
|---|---|---|---|
| 4 | `load_drug_classifications(xlsx_path)` | Block 9 | Reads `PsO Brands- For Ground Truth` tab, returns `(branded_set, generic_set)`. Adds INN aliases + targeted-synthetics-not-in-basket. |
| 5 | `normalise_columns(df)` | Block 9 | Maps variants (filename/file/pdf → Filename, drug/brand/medication → Brand, condition/disease → Indication). Indication normaliser inline. |
| 6 | `load_submissions(path)` | Block 9 | Reads XLSX `Submissions` tab or CSV. Calls `normalise_columns`. |
| 7 | `get_drug_aliases(drug_name)` | Block 3 | Pure Python — brand → set of {brand, INN, INN+biosimilar_suffixes}. |
| 8 | `get_other_brands_in_doc(source_text, target_drug)` | Block 3 | Pure Python — for multi-brand disambiguation. Needs `BRANDED_DRUGS`/`GENERIC_DRUGS` populated. |

**Depends on:** Phase 0.
**Checkpoint:** in a `__main__` test stub, load XLSX, print `len(BRANDED_DRUGS)`, `len(GENERIC_DRUGS)`, `len(submissions_df)`. Expect ~30 / ~5 / 79.

### Phase 2 — PDF ingestion (text-only, vision stubbed) | ~30 min

**Intent:** get text out of a PDF deterministically. Vision fallback is stubbed to return the original sparse text — backfilled in Phase 4 after LLM plumbing works.

| # | Function | Block | Notes |
|---|---|---|---|
| 9 | `_render_page_png(page, dpi=200) → bytes` | Block 2 | Pure PyMuPDF — needed by Phase 4 vision fallback, write now while we're in Block 2. |
| 10 | `_ocr_page_with_vision(page, page_num, fallback_text) → str` | Block 2 | **Stub for now**: just `return fallback_text` + a log line. Backfilled Phase 4. |
| 11 | `ingest_pdf(filepath) → dict` | Block 2 | Open with PyMuPDF, per-page `get_text`, vision fallback if < `MIN_CHARS_PER_PAGE`, page markers, return dict with open `doc_handle`. |
| 12 | `_ingest_pdf_text_only(filepath) → dict` | Block 8 | Cheap variant for preflight — same output shape but `_text_only: True`, no vision. |
| 13 | `_ensure_vision_aware(doc_info)` | Block 8 | **Stub for now**: `return doc_info` unchanged. Backfilled Phase 4. |
| 14 | `close_pdf_cache(pdf_cache)` | Block 2 | Closes all `doc_handle`s defensively. |

**Depends on:** Phase 0.
**Checkpoint:** `python -c "from pipeline import ingest_pdf; d = ingest_pdf(Path('Sample_PsO_ADS_Track/66156-4274314.pdf')); print(d['n_pages'], 'Humira' in d['full_text'].lower(), 'tuberculosis' in d['full_text'].lower())"` — expect `441 True True`. Close the handle after.

### Phase 3 — LLM plumbing | ~45 min

**Intent:** Groq talks back, JSON parses cleanly, retries behave.

| # | Function / class | Block | Notes |
|---|---|---|---|
| 15 | `TokenBudget` class | Block 4 | Sliding 60s window. Module-level `token_budget = TokenBudget()` instance. |
| 16 | `_estimate_tokens(prompt, system_prompt, max_output_tokens) → int` | Block 4 | Simple chars/4 + max_output. |
| 17 | `DailyQuotaExceeded(Exception)` | Block 4 | Sentinel for clean batch halt. |
| 18 | `retry(func, *args, **kwargs)` | Block 4 | 429 detection (minute vs daily), 5xx/connection backoff, non-retryable raise. |
| 19 | `groq_client = Groq(api_key=GROQ_API_KEY)` | Block 4 | Module-level. |
| 20 | `_call_groq_text(prompt, system_prompt) → str` | Block 4 | Calls `token_budget.consume` first. |
| 21 | `_call_groq_vision(prompt, image_b64) → str` | Block 4 | Same — single-image OpenAI-style `image_url` content. |
| 22 | `call_llm(prompt, system_prompt='', vision=False, image_b64=None) → str` | Block 4 | Router. |
| 23 | `parse_json_safe(response, retry_prompt=None) → dict` | Block 4 | Fence-strip, trailing-comma fix, reprompt-on-fail using `retry_prompt`. |
| 24 | `determinism_test() → str` | Block 9 | 3 identical prompts, hash, return STABLE / MINOR_DRIFT / SIGNIFICANT_DRIFT. |

**Depends on:** Phase 0 + Phase 1 (for token_budget setup — actually only Phase 0 needed).
**Checkpoint:** test script: `determinism_test()` prints STABLE (or known drift); a manual `parse_json_safe(call_llm('Return JSON: {"x": 1}'))` returns `{'x': 1}`. **Important:** also verify `meta-llama/llama-4-scout-17b-16e-instruct` is reachable — send a dummy 1x1 PNG and check the response shape. If it 404s, halt and switch to a different vision model **before** Phase 4.

### Phase 4 — Backfill vision fallback | ~15 min

**Intent:** complete what Phase 2 stubbed.

| # | Function | Block | Notes |
|---|---|---|---|
| 25 | Replace `_ocr_page_with_vision` stub | Block 2 | Real implementation: render PNG, base64, `retry(call_llm, vision=True)`, quality check (refusal markers + length floor). |
| 26 | Replace `_ensure_vision_aware` stub | Block 8 | Walk pages, for any sparse page invoke `_ocr_page_with_vision`, mutate `doc_info`. |

**Depends on:** Phase 2 + Phase 3.
**Checkpoint:** ingest the same Oregon Medicaid PDF — should NOT fire vision (audit shows zero sparse pages). Now synthesize a deliberately-sparse single-page PDF and confirm vision fires + returns text.

### Phase 5 — Outline & sectioning | ~60 min

**Intent:** turn 688K chars of Oregon Medicaid into an 8K-char assembled context with the right four parts.

| # | Function | Block | Notes |
|---|---|---|---|
| 27 | Heading regexes: `HEADING_NUMBERED`, `HEADING_KEYWORDS`, `HEADING_LIST_ITEM`, `PAGE_MARKER` | Block 3 | Module-level compiled. |
| 28 | `_locate_heading_offset(full_text, heading_text, page_start_offset, page_offsets, page_num)` | Block 3 | Single-page-window constrained. |
| 29 | `_heading_level(span, median_size) → int` | Block 3 | Pure helper. |
| 30 | `_outline_from_fonts(doc, full_text, page_offsets) → list[dict]` | Block 3 | Font/style scan. |
| 31 | `extract_outline(doc, full_text) → list[dict]` | Block 3 | TOC first, fonts fallback. |
| 32 | `map_outline_to_sections(outline, drug, indication) → dict` | Block 3 | **One LLM call** — depends on `call_llm`. |
| 33 | `slice_by_anchors(full_text, outline, anchors) → dict` | Block 3 | Deterministic. `(heading, page)`-keyed lookup. |
| 34 | `recursive_zoom(section_text, doc, section_key, drug, indication) → str` | Block 3 | Stage C — one LLM call for sub-outline filter. |
| 35 | `assemble_context(sections, drug, indication, doc=None) → str` | Block 3 | Labels + recursive_zoom triggers + largest-section truncation if over cap. |

**Depends on:** Phase 2 (ingest_pdf) + Phase 3 (call_llm).
**Checkpoint:** for `(66156-4274314.pdf, CIMZIA)`: outline > 50 headings, anchors return 4 non-null heading-text+page values, assembled context contains the strings "tuberculosis" (universal Step 4), "Humira" or "Enbrel" (drug Step 11), and "Renewal" (reauth). Size < 60K chars.

### Phase 6 — Extraction passes | ~45 min

**Intent:** turn assembled context into 12 parameter values.

| # | Function | Block | Notes |
|---|---|---|---|
| 36 | `SYSTEM_PROMPT_EXTRACTOR` constant | Block 5 | Verbatim + multi-brand rule. |
| 37 | `_multi_brand_directive(drug, other_brands) → str` | Block 5 | Shared snippet. |
| 38 | `extract_simple_params(context, drug, indication, other_brands=None) → dict` | Block 5 | Pass 1 — 7 fields. |
| 39 | `extract_step_therapy_text(context, drug, indication, other_brands=None) → dict` | Block 5 | Pass 2 — single blob. |
| 40 | `_extract_section_markers(assembled_context) → str` | Block 5 | Pull `[XXX]` lines for Pass 3. |
| 41 | `extract_step_counts(combined_step_text, assembled_context, drug, indication) → dict` | Block 5 | Pass 3 — CoT with worked examples. |

**Depends on:** Phase 3 + Phase 5.
**Checkpoint:** run all three passes on the Reference row's (Filename, Brand) — assuming it has those anchors. Print results. Eyeball Age, TB Test, step counts vs the Reference tab's ground truth. Expect ≥9/12 match.

### Phase 7 — Validation | ~45 min

**Intent:** format-correct, hallucination-detect, contradiction-flag, rule-counter cross-check.

| # | Function | Block | Notes |
|---|---|---|---|
| 42 | `_tokenise(text) → set[str]` + `token_recall(extracted, source) → float` | Block 6 | |
| 43 | `VERBATIM_THRESHOLDS` dict | Block 6 | Module-level defaults. |
| 44 | `critical_verbatim_check(params, source_text) → list[str]` | Block 6 | |
| 45 | `critical_step_extraction_check(params) → list[str]` | Block 6 | |
| 46 | `_cluster_by_or_proximity(brand_hits, text, window) → list[set]` | Block 6 | |
| 47 | `GENERIC_STEP_PATTERNS` list | Block 6 | Codex fix #7 — class-level generic patterns. |
| 48 | `rule_based_step_count(combined_step_text) → dict` | Block 6 | Named drugs + pattern hits, 80-char proximity cluster. |
| 49 | `reconcile_step_counts(llm_counts, rule_counts, llm_failed=False) → tuple` | Block 6 | |
| 50 | `rule_reauth_required`, `rule_auth_duration`, `rule_quantity_limits_strict`, `rule_age_format`, `rule_step_na_format` | Block 6 | All mutate-in-place. |
| 51 | `semantic_contradiction_checks(params, source_text) → list[str]` | Block 6 | 6 advisory rules. |
| 52 | `multi_brand_ambiguity_check(params, source_text, target_drug) → list[str]` | Block 6 | |
| 53 | `calibrate_verbatim_thresholds(validation_set)` | Block 6 | D2 calibration helper. |
| 54 | `validate_all(params, source_text='', target_drug='') → tuple` | Block 6 | Integrator. |

**Depends on:** Phase 1 (`BRANDED_DRUGS` / `GENERIC_DRUGS`).
**Checkpoint:** feed validate_all the Phase-6 Reference output + the full PDF text. `critical_failures` should be empty, `_warnings` should be either empty or contain only intentional advisory tags. Manually verify `rule_based_step_count` finds at least 3 generic-step matches on Oregon Medicaid Step 11 text (topical CS + another topical + systemic).

### Phase 8 — Orchestration | ~30 min

**Intent:** stitch ingest → section → pass-1-2-3 → validate → row, with caches and checkpoints.

| # | Function | Block | Notes |
|---|---|---|---|
| 55 | `_atomic_checkpoint_write(results)` | Block 8 | tempfile + os.replace. |
| 56 | `_na_row_with_warning(warning) → dict` | Block 8 | Empty row template. |
| 57 | `process_single_row(filename, drug, indication, pdf_cache, outline_cache, section_cache, rerun_count=0) → dict` | Block 8 | The main per-row pipeline. Includes drug-not-found loud raise, rule-counter parallel call, rerun loop, DailyQuotaExceeded propagation. |
| 58 | `process_all_rows(submissions_df, pdf_cache=None, indication_default='Plaque Psoriasis') → list[dict]` | Block 8 | Loop + checkpoint + DailyQuotaExceeded catch + `finally: close_pdf_cache`. |
| 59 | `print_end_of_batch_summary(results)` | Block 8 | Diagnostics. |
| 60 | `preflight_check(submissions_df, pdf_cache)` | Block 8 | Cheap-only — populates pdf_cache, drug-presence scan, no LLM. |

**Depends on:** Phases 1, 2, 5, 6, 7.
**Checkpoint:** run `process_all_rows` on the first 3 rows of Submissions. Confirm `checkpoint.json` exists with 3 entries, `result.csv` not yet written (that's Phase 9), no exceptions. Re-run — should skip all 3 from cache instantly.

### Phase 9 — Output, validation set, entry point | ~30 min

**Intent:** make the program runnable end-to-end with all the CLI niceties.

| # | Function / item | Block | Notes |
|---|---|---|---|
| 61 | `COLUMN_MAP` dict + `PHASE2_PARAMS` set | Block 9 | **`combined_step_text` is the key, not `step_therapy_text`.** |
| 62 | `format_row(params) → dict` | Block 9 | Raises on missing keys (except Phase 2). |
| 63 | `save_csv(results, path=OUTPUT_PATH)` | Block 9 | |
| 64 | `load_reference_tab_transposed(xlsx_path) → dict` | Block 9 | Parses transposed `Sno., Params, Values` layout. |
| 65 | `inspect_additional_data_tab(xlsx_path, submissions_df)` | Block 9 | |
| 66 | `per_param_match(pred, truth, param_name) → bool` | Block 9 | Per-param tolerance rules. |
| 67 | `load_validation_set(xlsx_path, manual_labels_path) → DataFrame` | Block 9 | Combines Reference + manual labels (+ ADE if usable). |
| 68 | `run_smoke_test(xlsx_path) → bool` | Block 9 | ≥85% gate. |
| 69 | `main()` | Block 9 | argparse, GROQ_API_KEY guard, path guards, startup diagnostics, smoke-test mode, preflight, smoke gate, batch, summary. |
| 70 | `if __name__ == "__main__": main()` | bottom | |

**Depends on:** all previous phases.
**Checkpoint:** `GROQ_API_KEY=... python pipeline.py --smoke-test` runs end-to-end. If no `manual_labels.csv` exists, it prints a clear warning and runs on whatever Reference yields.

### Phase 10 — Manual labels + smoke gate green | ~60 min (mostly your time, not coding)

**Intent:** create the validation oracle and prove the pipeline is correct on it.

| # | Step | Notes |
|---|---|---|
| 71 | Hand-label `manual_labels.csv` | 5 rows per §11.14 stratification — small TREMFYA, small STELARA, medium multi-drug, Oregon Medicaid, wildcard. I can pre-fill a draft by running the pipeline on those 5 rows and emitting a draft CSV; you correct what's wrong. |
| 72 | `python pipeline.py --smoke-test` | Iterate prompts in Block 5 until ≥85% per-cell agreement. |
| 73 | Calibrate `VERBATIM_THRESHOLDS` | Run `calibrate_verbatim_thresholds(validation_set)` once thresholds have ≥3 samples. |

**Depends on:** Phase 9.
**Checkpoint:** smoke gate passes ≥85%, calibrated thresholds logged.

### Phase 11 — Full batch + iterate | variable

**Intent:** ship.

| # | Step | Notes |
|---|---|---|
| 74 | Pre-flight token-budget dry run | Print predicted total tokens + wall-clock. |
| 75 | `python pipeline.py` | Full 79 rows. Expect ~3 hours with the TokenBudget throttle. |
| 76 | Review `debug_log.jsonl` end-of-batch summary | Triage rows tagged `MULTI_BRAND_AMBIGUOUS_*`, `STEP_COUNT_MAJOR_DISAGREE_*`, `CRITICAL_DRUG_NOT_FOUND`. |
| 77 | Force-rerun bad rows | `python pipeline.py --force-rerun "<file>:<brand>"` after fixing prompts. |
| 78 | Final `result.csv` column snapshot | Compare header to Submissions tab template. |
| 79 | Assemble submission ZIP | `pipeline.py`, `result.csv`, `requirements.txt`, `README.md`, `manual_labels.csv`. |

**Depends on:** Phase 10.
**Checkpoint:** submission ZIP ready, smoke-test reproducible from fresh clone.

---

### Dependency graph (one glance)

```
Phase 0 (constants + log_debug)
  └─ Phase 1 (drug + spreadsheet loaders)
       └─ Phase 2 (ingestion, vision stubbed)
            └─ Phase 3 (LLM plumbing)
                 ├─ Phase 4 (backfill vision)
                 └─ Phase 5 (sectioning)
                      └─ Phase 6 (extraction passes)
                           └─ Phase 7 (validation)
                                └─ Phase 8 (orchestration)
                                     └─ Phase 9 (output + entry)
                                          └─ Phase 10 (manual labels + smoke gate)
                                               └─ Phase 11 (full batch + ship)
```

### Rules of engagement while building

1. **No detours.** If a phase's checkpoint fails, fix that phase before starting the next.
2. **No premature optimisation.** Phases 2 + 5 are written first to be correct, not fast.
3. **Stub when blocked.** Phase 2 ships with vision stubbed; Phase 4 backfills. Never leave a stub past its backfill phase.
4. **One commit per phase.** Each phase's checkpoint is the commit boundary.
5. **Test artefacts are throwaway** — small inline `__main__` blocks for ad-hoc verification, deleted before submission.
6. **If a prompt regression appears in Phase 10**, do not iterate the prompt mid-batch. `--force-rerun` only the affected rows after the prompt change.

**Total writing time estimate: ~5.5 hours of focused coding. Plus ~3-5 hours of testing/iteration. Realistic green-smoke-test target: end of D3.**
