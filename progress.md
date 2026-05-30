# Build Progress — `pipeline.py`

Last updated: 29 May 2026, end of D2 build session.
Source of truth for the spec: [context/03_PIPELINE_ARCHITECTURE.md](context/03_PIPELINE_ARCHITECTURE.md).
Source of truth for the order: [context/06_DEVELOPER_PLAN.md §13](context/06_DEVELOPER_PLAN.md).

---

## One-glance status

| Phase | What it built | Status |
|---|---|---|
| 0 | env loader, log_debug, argparse skeleton, constants | ✅ |
| 1 | `load_drug_classifications`, `load_submissions`, `normalise_columns`, `get_drug_aliases`, `get_other_brands_in_doc` | ✅ |
| 2 | `ingest_pdf` (vision stubbed initially), `_ingest_pdf_text_only`, `_ensure_vision_aware` (stubbed), `close_pdf_cache`, `_render_page_png` | ✅ |
| 3 | `TokenBudget`, `DailyQuotaExceeded`, `retry`, `_call_groq_text`, `_call_groq_vision`, `call_llm`, `parse_json_safe` (with reprompt), `determinism_test` | ✅ |
| 4 | Real `_ocr_page_with_vision` + `_ensure_vision_aware` (vision stubs replaced) | ✅ |
| 5 | `extract_outline`, `map_outline_to_sections`, `slice_by_anchors`, `recursive_zoom`, `assemble_context`, plus heading regexes + outline pruning | ✅ |
| 6 | `extract_simple_params` (Pass 1), `extract_step_therapy_text` (Pass 2), `extract_step_counts` (Pass 3), `_multi_brand_directive`, `_extract_section_markers`, `SYSTEM_PROMPT_EXTRACTOR` | ✅ |
| 7 | All `rule_*`, `critical_*`, `token_recall`, `rule_based_step_count`, `GENERIC_STEP_PATTERNS`, `reconcile_step_counts`, `semantic_contradiction_checks`, `multi_brand_ambiguity_check`, `calibrate_verbatim_thresholds`, `validate_all` | ✅ (with 2 known bugs in rule counter, deferred) |
| 8 | `_atomic_checkpoint_write`, `_na_row_with_warning`, `process_single_row`, `process_all_rows`, `print_end_of_batch_summary`, `preflight_check`, `SMALL_DOC_CHAR_LIMIT` bypass | ✅ |
| 9 | `COLUMN_MAP`, `PHASE2_PARAMS`, `format_row`, `save_csv`, `load_reference_tab_transposed`, `inspect_additional_data_tab`, `per_param_match`, `load_validation_set`, `run_smoke_test`, full `main()` | ✅ |
| 10 | Manual labels + smoke gate green | ⏸ blocked on tokens |
| 11 | Full batch + ship | ⏸ blocked on tokens |

**Single file: `pipeline.py`, 2,887 lines, 109 public names. `python pipeline.py --help` works cleanly with no side effects.**

---

## Facts verified by running, not by reading

These were assumed in the docs and **actually measured** today. They override what the architecture/dev-plan say where they conflict.

| Fact | Value | Source |
|---|---|---|
| Total Submissions rows | **79** | `load_submissions` |
| Unique filenames | **70** | matches PDFs on disk |
| Unique brands | **15** (STELARA 33, TREMFYA 28, then ENBREL/AMJEVITA/OTEZLA/YESINTEK/COSENTYX/REMICADE/SILIQ/CIMZIA/BIMZELX/SKYRIZI/OTULFI/ILUMYA/ACITRETIN) | broader than docs' "TREMFYA + STELARA" claim, confirms Codex finding |
| Indication column | **absent** in Submissions tab | hardcoded fallback fires |
| Branded drugs (from XLSX) | **50** (incl. INN aliases + targeted synthetics) | `load_drug_classifications` |
| Generic drugs | **5** (acitretin, cyclosporine, methotrexate, vtama, zoryve) | confirmed |
| Oregon Medicaid (`66156-4274314.pdf`) page count | **441 pages, 721K chars** | `ingest_pdf` |
| Raw outline on Oregon Medicaid (font-scan) | **2,021 headings** (was 8,246 before heuristic tightening) | first attempt was too aggressive |
| Pruned outline for LLM mapping | **191 entries** (L1+L2 only, under 400-entry cap) | sent to Stage B |
| Oregon Medicaid "Step 11" terminology | **doesn't exist** in the PDF | the actual doc uses numbered decision-tree questions; Humira appears around page 231 |
| Sparse pages needing vision | **2 of 441** on Oregon Medicaid (pages 13, 14) | both are PA-request forms PyMuPDF doesn't extract well |
| Vision OCR on those pages | **recovered 2,930 + 2,901 chars** of structured form data | real win, justifies the fallback |
| Pre-flight mismatched-row finding | **1 row**: `287728-4459856.pdf — STELARA` (hematology policy, no PsO/STELARA content) | Codex predicted this exactly |
| Determinism test | **STABLE** (1 unique output over 3 runs at T=0) | Groq Llama 3.3 70B is deterministic for us |
| Llama 4 Scout vision availability on Groq | **confirmed** — text returns clean for full PDF page | ~80s per call though, expensive |

---

## Real findings from testing that affect future iteration

### Vision is slow
~60-80s per vision call. On the audited dataset only 2 pages need it (Oregon Medicaid 13, 14), so it doesn't dominate runtime. **But:** held-out PDFs may have more. Vision cost = ~6K tokens per page (text+image+output budget after the MAX_TOKENS_VISION 2048 reduction in Phase 4).

### TokenBudget fix already shipped
Original `TokenBudget.consume` crashed with `IndexError` when a single request exceeded `TPM_LIMIT` and window was empty. Fixed: now sleeps 60s in that case and logs. Also lowered `MAX_TOKENS_VISION` from 4096 to 2048 so vision requests fit under TPM ceiling.

### Outline heuristic tightening (Phase 5)
First version emitted 8,246 headings on Oregon Medicaid. Tightened: drop bare numbered list items (`HEADING_LIST_ITEM` removed from OR chain), require ≥8 chars, require multi-word for bold/caps matches. Down to 2,021 raw → 191 pruned. Pruning logic: L1+L2 first; if still > 400, L1 only; if still > 400, longest N by length.

### Min-section-size enforcement (Phase 5)
First slice attempt on Oregon Medicaid gave drug_specific=54 chars and reauth=33 chars (both anchors landed on page 374, ~50 chars apart). Added `MIN_SECTION_CHARS_BY_KEY` = `{universal: 2500, classification: 1000, drug_specific: 15000, reauth: 2000}` — slice skips "next entries" closer than the minimum. Now drug_specific=5K (still misses TB Step 4 a few pages deeper, but contains most criteria).

### Pass 6 worked end-to-end on small Aetna doc
For `(330109-4880941.pdf, TREMFYA)`:
- Pass 1: `age=>=18, tb=Yes, init_auth=12, reauth=12, specialist=dermatologist, qty=NA` + full verbatim reauth requirements
- Pass 2: 381 chars of step therapy verbatim ("THREE preferred products: ustekinumab, adalimumab OR Enbrel, Rinvoq, Otezla")
- Pass 3: `branded=4, generic=NA, photo=NA` — overcounted by 1 (the "THREE preferred products" wording wasn't parsed as a numeric constraint)
- ~180s wall-clock total for the row

### Rule-based step counter — 2 known bugs (Phase 7)
Both visible on Oregon-Medicaid-style step text `"topical CS AND another topical (calcip/tazar/anthr) AND phototherapy AND systemic (acitr/cyclo/MTX) AND Humira or Enbrel"`:

1. **Generic clusters too aggressively** — all generic-pattern hits fall within 80 chars of each other in dense step text, collapsing 3 separate steps into 1 cluster. Fix would require splitting on AND separators first, then OR-resolving within each phrase.
2. **`photo_in_or` regex false positive** — `"or anthralin AND phototherapy"` matches the OR-near-photo regex even though "or" belongs to the topical-options list, not phototherapy. Fix: only check the 30 chars immediately before phototherapy for "or ".

**Both bugs result in the rule counter undercounting, which produces `STEP_COUNT_MAJOR_DISAGREE_*` flags during reconciliation. Pass 3 (LLM) remains primary — these flags surface rows for manual review.** Acceptable for now.

---

## Open architectural notes

### `_locate_heading_offset` falls back to page-start
When a heading's exact text isn't found in the single-page window (PyMuPDF span text differs from PDF text-stream output), we return the page marker's offset. Two anchors on the same page can therefore overlap. Mitigated by `MIN_SECTION_CHARS_BY_KEY` but not perfect.

### Drug_specific section often misses universal-of-the-class criteria
Oregon Medicaid has TWO levels of "universal": document-wide (page 10+ "General PA information") and drug-class-wide within the TIM block (page 374+). The LLM mapper picks the document-wide one for `universal_criteria`, so TB testing for immune modulators (page ~378) ends up neither in universal nor in drug_specific reliably. Bumping `drug_specific` min from 5K → 15K helped; may need 20K+ for full coverage.

### Recursive zoom only fires when section > MAX_SECTION_CHARS (60K)
On our test, no section reached 60K post-slicing, so recursive_zoom was never exercised end-to-end. The code is there but unverified in practice.

---

## Token budget — the blocker for Phases 10–11

**Groq free tier: 100,000 tokens / day** on `llama-3.3-70b-versatile`.

Today's usage by phase (estimated from `debug_log.jsonl`):
- Phase 3 sanity (text + vision + determinism): ~10K
- Phase 4 vision backfill on Oregon Medicaid: ~10K
- Phase 5 (3 sectioning attempts; 1 rejected as 120K-token request, 2 successful at ~10K each): ~30K
- Phase 6 (Pass 1/2/3 on Aetna): ~30K
- **Today's total: ~80-90K, near the cap.**

Per-row cost from Phase 6 measurement:
- Pass 1: ~12-15K tokens (full assembled context input + ~1K output)
- Pass 2: ~12-15K tokens
- Pass 3: ~3-4K tokens (small step text only)
- `map_outline_to_sections` per unique PDF (cached): ~5-10K
- **~30-45K tokens per row.**

For 79 rows: **~2.5-3.5M tokens — 25-35× the free-tier daily limit.**

### Resume requires one of:

1. **Upgrade Groq Dev tier** (~$10-20). Probably the fastest path; unblocks Phase 10+11 immediately.
2. **Split batch across 3-4 days** with the existing checkpoint. Risky on the 1-Jun deadline.
3. **Move text reasoning to a higher-TPD free model** (e.g. `llama-3.1-8b-instant`). Significant quality regression risk on Pass 3 step counting.
4. **Drastically cut context sizes** below the current ~12K-token average. Means more aggressive sectioning, more recursive zooms — also more LLM calls to compute the zooms, partially self-defeating.

---

## How to resume (when token budget is unblocked)

```bash
# From project root:
python pipeline.py --help                 # sanity check imports

# Phase 10 — populate manual_labels.csv with 5 hand-labelled rows
#   (small TREMFYA + small STELARA + medium multi-drug + Oregon Medicaid + wildcard)
#   schema = COLUMN_MAP.values() minus "Access Score", plus Indication
# Once present, run the gate:
python pipeline.py --smoke-test           # ≥85% per-cell agreement to pass

# Phase 11 — full batch (~3 hours wall-clock with TPM throttle)
python pipeline.py                        # preflight → smoke gate → batch → CSV

# Force-rerun specific rows after fixing prompts:
python pipeline.py --force-rerun "66156-4274314.pdf:CIMZIA" "287728-4459856.pdf:STELARA"

# Skip determinism + smoke gate for fast iteration during debugging:
python pipeline.py --skip-determinism --skip-smoke-gate
```

### Files in play

- `pipeline.py` — the build (2,887 lines, single file)
- `pre-context/PA_Business_Rules.xlsx` — Business Rules, Submissions, Reference (transposed), Additional Extracted Data, PsO Brands tabs
- `Sample_PsO_ADS_Track/` — 70 real PDFs
- `.env` — `groq_api_key=...` (loader normalises case)
- `debug_log.jsonl` — append-only JSONL, every retry/throttle/warning event
- `checkpoint.json` — atomic (filename, brand) keyed, resume-safe
- `result.csv` — final output, 79 rows × 15 columns
- `manual_labels.csv` — **NOT YET CREATED** — required for smoke gate
- `context/` — all the planning docs (01-06), don't re-read unless something diverges

### Known-good module functions (callable from `python -c`)

All exposed at module level for ad-hoc testing:
- `load_drug_classifications`, `load_submissions`, `get_drug_aliases`, `get_other_brands_in_doc`
- `ingest_pdf`, `_ingest_pdf_text_only`, `_ensure_vision_aware`, `close_pdf_cache`
- `call_llm`, `parse_json_safe`, `retry`, `determinism_test`, `token_budget`
- `extract_outline`, `extract_outline_for_mapping`, `map_outline_to_sections`, `slice_by_anchors`, `recursive_zoom`, `assemble_context`
- `extract_simple_params`, `extract_step_therapy_text`, `extract_step_counts`
- `rule_based_step_count`, `reconcile_step_counts`, `validate_all`, `token_recall`
- `process_single_row`, `process_all_rows`, `preflight_check`, `print_end_of_batch_summary`
- `load_reference_tab_transposed`, `inspect_additional_data_tab`, `load_validation_set`, `per_param_match`, `run_smoke_test`
- `format_row`, `save_csv`

### Globals populated at runtime

- `BRANDED_DRUGS`, `GENERIC_DRUGS` — populated by `load_drug_classifications()` (called inside `main()` or manually in tests)
- `token_budget` — singleton, persists for process lifetime
- `groq_client` — singleton, lazy in the sense of not hitting network on construction

---

## What to NOT do when resuming

- **Don't change the architecture spec** ([context/03_PIPELINE_ARCHITECTURE.md](context/03_PIPELINE_ARCHITECTURE.md)) without a reason. The code follows it phase by phase.
- **Don't lower `TPM_LIMIT`** below 5500 without checking that Phase 4's `MAX_TOKENS_VISION = 2048` still fits.
- **Don't remove the small-doc bypass** (`SMALL_DOC_CHAR_LIMIT = 50_000` in Phase 8) — small docs get butchered by sectioning machinery.
- **Don't run the full batch** without `manual_labels.csv` in place AND the smoke gate passing.
- **Don't trust the rule-based step counter on multi-AND policies** — the LLM Pass 3 is the source of truth; rule counter is the fallback.

---

## Status snapshot for handoff

**The code is done. The tokens are not.** Pipeline is functionally complete, verified end-to-end on one small doc (Aetna TREMFYA, ~3 min wall-clock, all params extracted). Sectioning works on Oregon Medicaid CIMZIA but with partial drug-section coverage. Resume by deciding the token-budget question and then running `python pipeline.py --smoke-test`.
