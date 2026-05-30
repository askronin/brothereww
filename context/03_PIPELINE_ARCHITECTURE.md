# Pipeline Architecture: pipeline.py
> Complete architecture for the extraction pipeline.
> Access score module to be added AFTER extraction is validated.
> Single file. All logic here.

---

## File Structure Overview

```
pipeline.py
│
├── BLOCK 1 — CONFIG & CONSTANTS
├── BLOCK 2 — FILE INGESTION (3 format handlers)
├── BLOCK 3 — DOCUMENT SECTIONING
├── BLOCK 4 — LLM INTERFACE (abstracted, swappable)
├── BLOCK 5 — EXTRACTION PASSES (3 passes)
├── BLOCK 6 — BUSINESS RULES VALIDATION
├── BLOCK 7 — ACCESS SCORE [PLACEHOLDER — add after extraction confirmed]
├── BLOCK 8 — ORCHESTRATOR
└── BLOCK 9 — OUTPUT
```

---

## BLOCK 1 — CONFIG & CONSTANTS

```python
# ─────────────────────────────────────────────────────────────────
# BLOCK 1 — CONFIG & CONSTANTS
# ─────────────────────────────────────────────────────────────────

import os
import json
import zipfile
import pandas as pd
from pathlib import Path

# ── API Keys (Groq only) ──────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
# GROQ_API_KEY presence is enforced at startup in main() — fail-fast with a
# clear error rather than letting an empty key produce a confusing 401 later.

# ── Model Names ───────────────────────────────────────────────────
GROQ_TEXT_MODEL   = "llama-3.3-70b-versatile"
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# ── File Paths (overridable via env or CLI) ───────────────────────
# Resolution order: CLI flag > env var > default. Set in main() before any
# block reads them. Default values reflect the local dev layout; Kaggle and
# Colab judges should set --pdf-dir / --xlsx-path or the env vars.
PDF_DIR = Path(os.environ.get("PIPELINE_PDF_DIR", "Sample_PsO_ADS_Track/"))
XLSX_PATH = Path(os.environ.get("PIPELINE_XLSX_PATH",
                                "pre-context/PA_Business_Rules.xlsx"))
OUTPUT_PATH = Path(os.environ.get("PIPELINE_OUTPUT_PATH", "result.csv"))
CHECKPOINT_PATH = Path(os.environ.get("PIPELINE_CHECKPOINT_PATH", "checkpoint.json"))
DEBUG_LOG_PATH = Path(os.environ.get("PIPELINE_DEBUG_LOG_PATH", "debug_log.jsonl"))

# ── Processing Constants ──────────────────────────────────────────
MAX_RETRIES = 3
MAX_PIPELINE_RERUNS = 2          # max reruns per row when critical validation fails
BACKOFF_BASE = 2                 # seconds, doubles each retry
CHECKPOINT_INTERVAL = 10         # save checkpoint every N rows
MAX_TOKENS_TEXT = 4096
MAX_TOKENS_VISION = 4096
TEMPERATURE = 0.0                # reproducibility — never raise

# Vision fallback trigger: per-page text density threshold.
# Pages with extracted text below this many characters are re-OCR'd via the
# vision model. On the audited 70-PDF sample no page falls below 100; this
# is defensive for the judges' held-out test set.
MIN_CHARS_PER_PAGE = 100

# Section-assembly token budgets (chars; ~4 chars/token).
# When a single assembled section exceeds MAX_SECTION_CHARS we run Stage C
# (recursive zoom) on that section's outline. When the full assembled context
# exceeds MAX_CONTEXT_CHARS we force recursive zoom on the largest section.
MAX_SECTION_CHARS = 60_000     # ~15K tokens
MAX_CONTEXT_CHARS = 100_000    # ~25K tokens

# Token-budget throttle (Groq llama-3.3-70b-versatile free-tier ceiling).
# Conservative under the documented 6,000 TPM cap.
TPM_LIMIT = 5_500

# Determinism: hash N identical prompts at startup to detect non-stable serving.
DETERMINISM_TEST_RUNS = 3

# ── Drug Classification — loaded from XLSX at startup ─────────────
# NOT hardcoded. Loaded by load_drug_classifications() from
# PA_Business_Rules.xlsx "PsO Brands- For Ground Truth" tab.
# 
# The tab contains brand names only. We maintain a static INN→Brand
# mapping dict for matching INNs found in policy text to brand names.
# This dict only needs updating when genuinely new INNs are approved
# (rare) — it does NOT control which drugs are classified as branded.
#
# Classification logic:
#   branded_drugs = all drugs in the PsO Brands tab EXCEPT known generics
#   generic_drugs = Acitretin, Cyclosporine, Methotrexate, Vtama, Zoryve
#                  (conventional non-biologic drugs in the tab)

KNOWN_GENERICS_IN_MARKET_BASKET = {
    "acitretin", "cyclosporine", "methotrexate", "vtama", "zoryve"
}

# INN → Brand name mapping for policy text matching only
# (used when policy names the INN instead of brand name)
INN_TO_BRAND = {
    "guselkumab": "tremfya",
    "ustekinumab": "stelara",
    "adalimumab": "humira",
    "etanercept": "enbrel",
    "infliximab": "remicade",
    "certolizumab": "cimzia",
    "secukinumab": "cosentyx",
    "ixekizumab": "taltz",
    "risankizumab": "skyrizi",
    "brodalumab": "siliq",
    "tildrakizumab": "ilumya",
    "deucravacitinib": "sotyktu",
    "apremilast": "otezla",
    "bimekizumab": "bimzelx",
    # JAK inhibitors / targeted synthetics — branded step per business rules
    # Not in PsO market basket tab, handled here explicitly
    "tofacitinib": "xeljanz",
    "upadacitinib": "rinvoq",
    "baricitinib": "olumiant",
}

# Targeted synthetics that are branded steps but NOT in PsO market basket.
# If a policy requires these as steps, they count as BRANDED.
# Source: business rules "biologic or targeted synthetic drug = branded step"
TARGETED_SYNTHETICS_NOT_IN_BASKET = {
    "xeljanz", "tofacitinib",
    "rinvoq", "upadacitinib",
    "olumiant", "baricitinib",
}

PHOTOTHERAPY_TERMS = {
    "phototherapy", "puva", "uvb", "uva", "narrowband uvb",
    "psoralen", "light therapy", "photochemotherapy"
}

# Populated at runtime by load_drug_classifications()
BRANDED_DRUGS = set()
GENERIC_DRUGS = set()
```

---

## BLOCK 2 — FILE INGESTION

Single path: PyMuPDF text extraction per page, with per-page vision OCR fallback for sparse pages. Always returns a clean concatenated string with page markers preserved for downstream sectioning.

```python
# ─────────────────────────────────────────────────────────────────
# BLOCK 2 — FILE INGESTION
# ─────────────────────────────────────────────────────────────────

import base64
import io
import fitz  # pymupdf


def _render_page_png(page: "fitz.Page", dpi: int = 200) -> bytes:
    """
    Render a PDF page to PNG bytes for vision-model OCR fallback.
    200 DPI is the sweet spot: legible for tables, ~250 KB per page.
    """
    pix = page.get_pixmap(dpi=dpi)
    return pix.tobytes("png")


def _ocr_page_with_vision(page: "fitz.Page", page_num: int,
                          fallback_text: str = "") -> str:
    """
    Fallback when text extraction returns < MIN_CHARS_PER_PAGE.
    Renders the page, sends to Groq vision model (Llama 4 Scout), returns text.
    
    Quality check (Resolved §M4): if the vision model returns "I cannot read
    this image" / "Sorry" / very short output, treat the call as failed and
    fall back to the original (sparse) extracted text.
    """
    png_bytes = _render_page_png(page)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    prompt = (
        "Extract ALL text visible on this page exactly as it appears. "
        "Preserve numbered lists, bullets, table rows (use ' | ' as a column "
        "separator), and paragraph breaks. Do NOT summarise. Do NOT add "
        "commentary. Return only the page text."
    )
    try:
        text = retry(call_llm, prompt=prompt, vision=True, image_b64=b64) or ""
    except Exception as e:
        log_debug({"event": "vision_fallback_failed", "page_num": page_num,
                   "error": str(e)})
        return fallback_text
    
    # Quality check — refuse-strings or suspiciously short output
    lowered = text.strip().lower()
    refusal_markers = ("i cannot", "i can't", "i'm sorry", "unable to",
                       "no text", "no readable text", "cannot read")
    if len(text.strip()) < 50 or any(m in lowered[:200] for m in refusal_markers):
        log_debug({"event": "vision_fallback_low_quality", "page_num": page_num,
                   "vision_output_preview": text[:200]})
        return fallback_text
    
    log_debug({"event": "vision_fallback_used", "page_num": page_num,
               "chars_returned": len(text)})
    return text


def ingest_pdf(filepath: Path) -> dict:
    """
    Single ingestion path. All files in this dataset are real, born-digital PDFs.
    
    Strategy:
      1. Open with PyMuPDF — leave the handle OPEN (caller is responsible
         for closing via close_pdf_cache() in finally).
      2. For each page: extract text. If < MIN_CHARS_PER_PAGE, render and OCR
         via the Groq vision model (with quality check).
      3. Concatenate with explicit page markers — downstream sectioning uses
         these to localise findings and to keep table boundaries crisp.
    
    Returns:
    {
        "full_text":      str,        # all pages concatenated with markers
        "pages":          list[str],  # per-page text (parallel to page_num)
        "vision_pages":   list[int],  # 1-indexed pages where vision OCR ran
        "n_pages":        int,
        "filepath":       str,
        "doc_handle":     fitz.Document,  # OPEN — caller must close
    }
    
    Resolved §S6: doc handle stays open and is reused by extract_outline /
    recursive_zoom. Closed in process_all_rows's finally block.
    """
    try:
        doc = fitz.open(filepath)
    except Exception as e:
        raise RuntimeError(f"PyMuPDF cannot open {filepath}: {e}") from e
    
    pages, vision_pages = [], []
    
    for i, page in enumerate(doc, start=1):
        text = page.get_text("text") or ""
        if len(text.strip()) < MIN_CHARS_PER_PAGE:
            text = _ocr_page_with_vision(page, page_num=i, fallback_text=text)
            vision_pages.append(i)
        pages.append(text)
    
    full_text_parts = [f"\n===== PAGE {i} =====\n{p}" for i, p in enumerate(pages, start=1)]
    return {
        "full_text":    "".join(full_text_parts),
        "pages":        pages,
        "vision_pages": vision_pages,
        "n_pages":      len(pages),
        "filepath":     str(filepath),
        "doc_handle":   doc,
    }


def close_pdf_cache(pdf_cache: dict) -> None:
    """Close all open PyMuPDF doc handles. Called in process_all_rows finally."""
    for entry in pdf_cache.values():
        try:
            doc = entry.get("doc_handle")
            if doc is not None:
                doc.close()
        except Exception:
            pass
```

**Page markers (`===== PAGE N =====`)** survive into the assembled context. They cost ~6 tokens per page (negligible) and make two downstream things easier: (a) heuristic section-split anchors on page boundaries when the document doesn't use textual headers; (b) when verbatim-check fails, the debug log can point at the exact page the extracted text was supposed to live on.

---

## BLOCK 3 — DOCUMENT SECTIONING (Outline-Driven)

**Design philosophy.** Build a complete document outline first (local, deterministic). Map outline headings to our 4 target section types via ONE small LLM call. Slice the full text by heading char-offset deterministically. Recursive zoom only when sections exceed the budget. The LLM never sees policy body text during sectioning.

Replaces all prior sectioning tiers (heuristic windowing + LLM-anchor + truncated fallback). See [06_DEVELOPER_PLAN.md §11.1](./06_DEVELOPER_PLAN.md) for the full design rationale.

```python
# ─────────────────────────────────────────────────────────────────
# BLOCK 3 — OUTLINE-DRIVEN SECTIONING
# ─────────────────────────────────────────────────────────────────

import re
from statistics import median


# ── STAGE A — Local outline extraction ────────────────────────────

# Compiled patterns for heading detection in the no-TOC path.
HEADING_NUMBERED   = re.compile(r"^\s*(?:Step|Section|Part|Criterion)\s+\d+", re.I)
HEADING_KEYWORDS   = re.compile(
    r"^\s*(?:General|Universal|Initial|Renewal|Reauthorization|"
    r"Continuation|Quantity|Diagnosis|Approval|Coverage|Preferred|"
    r"Non-Preferred|Targeted|Plaque|Psoriasis|Step Therapy)\b", re.I)
HEADING_LIST_ITEM  = re.compile(r"^\s*\d+\.\s")
PAGE_MARKER        = re.compile(r"===== PAGE (\d+) =====")


def extract_outline(doc: "fitz.Document", full_text: str) -> list[dict]:
    """
    Return ordered list of headings spanning the document.
    
    Each heading: {
        "page":        int,        # 1-indexed page number
        "level":       int,        # 1 = top, deeper = larger numbers
        "text":        str,        # heading text (stripped)
        "char_offset": int,        # offset into full_text where this heading lives
    }
    
    Strategy:
      1. Try doc.get_toc() — PDF bookmarks if present (~30% of dataset).
      2. Else: per-page font/style scan via page.get_text("dict").
    
    Output is the ONLY thing Stage B's LLM call sees. Typical size: 5-10K
    tokens for large docs, 500-1500 tokens for small docs.
    """
    # Build page → char-offset map from PAGE markers inserted by ingest_pdf
    page_offsets = {int(m.group(1)): m.end() for m in PAGE_MARKER.finditer(full_text)}
    
    # Path 1: PDF bookmarks
    toc = doc.get_toc()  # list of [level, title, page_num]
    if toc and len(toc) >= 3:
        outline = []
        for level, title, page_num in toc:
            if page_num <= 0:
                continue
            outline.append({
                "page":        page_num,
                "level":       level,
                "text":        title.strip(),
                "char_offset": _locate_heading_offset(
                    full_text, title, page_offsets.get(page_num, 0),
                    page_offsets, page_num
                ),
            })
        log_debug({"event": "outline_from_toc", "n_headings": len(outline)})
        return outline
    
    # Path 2: font/style scan
    outline = _outline_from_fonts(doc, full_text, page_offsets)
    log_debug({"event": "outline_from_fonts", "n_headings": len(outline)})
    return outline


def _outline_from_fonts(doc, full_text: str, page_offsets: dict[int, int]) -> list[dict]:
    """Heading detection by font/style/pattern heuristics. Per-page."""
    outline = []
    for page_idx, page in enumerate(doc, start=1):
        page_dict = page.get_text("dict")
        # Collect font sizes on this page to compute the median
        sizes = []
        spans_with_meta = []
        for block in page_dict.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if not text:
                        continue
                    sizes.append(span["size"])
                    spans_with_meta.append({
                        "text":  text,
                        "size":  span["size"],
                        "flags": span.get("flags", 0),  # bit 16 = bold
                        "bbox":  span.get("bbox", (0, 0, 0, 0)),
                    })
        if not sizes:
            continue
        median_size = median(sizes)
        size_threshold = median_size * 1.15
        
        # Pass over spans, emit candidates that look like headings
        for span in spans_with_meta:
            text = span["text"]
            is_bold = bool(span["flags"] & 16)
            is_large = span["size"] > size_threshold
            is_all_caps = text.isupper() and len(text) >= 3
            
            looks_like_heading = (
                (is_large and len(text) < 150)
                or (is_bold and len(text) < 100)
                or (is_all_caps and len(text) < 100)
                or bool(HEADING_NUMBERED.match(text))
                or bool(HEADING_KEYWORDS.match(text))
                or bool(HEADING_LIST_ITEM.match(text) and len(text) < 150)
            )
            if not looks_like_heading:
                continue
            
            outline.append({
                "page":        page_idx,
                "level":       _heading_level(span, median_size),
                "text":        text,
                "char_offset": _locate_heading_offset(
                    full_text, text, page_offsets.get(page_idx, 0),
                    page_offsets, page_idx
                ),
            })
    return outline


def _heading_level(span: dict, median_size: float) -> int:
    """Coarse heading-level estimate. 1 = top, larger = deeper."""
    ratio = span["size"] / max(median_size, 1)
    if ratio > 1.5:   return 1
    if ratio > 1.25:  return 2
    if ratio > 1.10:  return 3
    return 4


def _locate_heading_offset(full_text: str, heading_text: str,
                           page_start_offset: int,
                           page_offsets: dict[int, int],
                           page_num: int) -> int:
    """
    Find char offset of heading_text within full_text, anchored to the page
    where it appears.
    
    Resolved §M1: search window is restricted to the SINGLE page where the
    heading lives (page_start_offset to start of next page), not 20K chars.
    Prevents the search bleeding into adjacent pages where a similar heading
    might exist.
    """
    page_end = page_offsets.get(page_num + 1, page_start_offset + 20_000)
    search_window = full_text[page_start_offset:page_end]
    needle = heading_text.strip()[:80]
    idx = search_window.find(needle)
    return page_start_offset + idx if idx >= 0 else page_start_offset


# ── STAGE B — Outline → section anchors (one LLM call) ───────────

SECTION_KEYS = ("universal_criteria", "classification_tables",
                "drug_specific_criteria", "reauth_criteria")


def map_outline_to_sections(outline: list[dict], drug: str, indication: str) -> dict:
    """
    ONE LLM call. Input = outline only (~5-10K tokens). Output = which
    {heading, page} pair begins each of our 4 target sections.
    
    Resolved §B4: anchor is (heading, page) not just heading. Multi-occurrence
    heading names ("Step 1" appears in many drug sections of Oregon Medicaid)
    are disambiguated by page number.
    
    Returns: {
        "universal_criteria":     {"heading": "...", "page": N} | None,
        "classification_tables":  {"heading": "...", "page": N} | None,
        "drug_specific_criteria": {"heading": "...", "page": N} | None,
        "reauth_criteria":        {"heading": "...", "page": N} | None,
    }
    """
    outline_blob = "\n".join(
        f"  page {h['page']:>3} | L{h['level']} | {h['text']}"
        for h in outline
    )
    
    prompt = f"""You are scanning the OUTLINE of a payer Prior Authorization policy.
Identify the heading (EXACT text + page number, copied from the outline below)
that begins each of these four sections for drug "{drug}" and indication "{indication}":

  1. universal_criteria      : criteria applying to ALL drugs in the policy
                               (e.g. "General Authorization Guidelines",
                                TB screening, diagnosis confirmation)
  2. classification_tables   : preferred / non-preferred agent tables
                               (often required to derive class-level steps)
  3. drug_specific_criteria  : section for {drug}, its INN, biosimilars,
                               OR the {indication} indication block
  4. reauth_criteria         : renewal / continuation / reauthorization

IMPORTANT: Heading text alone is NOT unique — many policies repeat headings
like "Step 1" across drug sections. ALWAYS include the page number.

OUTLINE (format: "page N | L<level> | <heading text>"):
{outline_blob}

Return ONLY this JSON. Use null for any section that does not appear:

{{
  "universal_criteria":     {{"heading": "<exact text>", "page": N}} or null,
  "classification_tables":  {{"heading": "<exact text>", "page": N}} or null,
  "drug_specific_criteria": {{"heading": "<exact text>", "page": N}} or null,
  "reauth_criteria":        {{"heading": "<exact text>", "page": N}} or null
}}
"""
    response = retry(call_llm, prompt, system_prompt=SYSTEM_PROMPT_EXTRACTOR)
    anchors = parse_json_safe(response, retry_prompt=prompt)
    # Normalise keys to expected set; drop unknowns
    return {k: anchors.get(k) for k in SECTION_KEYS}


# ── Slicing — deterministic, no LLM ──────────────────────────────

def slice_by_anchors(full_text: str, outline: list[dict], anchors: dict) -> dict:
    """
    Given anchor {heading, page} pairs, find each in the outline and slice
    full_text. Each section spans from its anchor offset to the next outline
    entry's offset (any section, in document order), or end-of-doc.
    
    Resolved §B4: lookup keys on (heading_text, page) — multi-occurrence safe.
    """
    sorted_headings = sorted(outline, key=lambda h: h["char_offset"])
    offsets_sorted = [h["char_offset"] for h in sorted_headings]
    
    sections = {k: "" for k in SECTION_KEYS}
    for key, anchor in anchors.items():
        if not anchor or not isinstance(anchor, dict):
            continue
        heading = (anchor.get("heading") or "").strip().lower()
        page = anchor.get("page")
        if not heading or page is None:
            continue
        
        # Exact (heading, page) match first
        match = next(
            (h for h in outline
             if h["text"].strip().lower() == heading and h["page"] == page),
            None
        )
        # Fallback: starts-with on same page
        if not match:
            match = next(
                (h for h in outline
                 if h["page"] == page
                 and h["text"].strip().lower().startswith(heading[:50])),
                None
            )
        # Last resort: exact heading text on the closest page
        if not match:
            candidates = [h for h in outline
                          if h["text"].strip().lower() == heading]
            if candidates:
                match = min(candidates, key=lambda h: abs(h["page"] - page))
        
        if not match:
            log_debug({"event": "anchor_not_in_outline", "section": key,
                       "anchor": anchor})
            continue
        
        start = match["char_offset"]
        next_offsets = [o for o in offsets_sorted if o > start]
        end = next_offsets[0] if next_offsets else len(full_text)
        sections[key] = full_text[start:end]
    
    return sections


# ── Stage C — Recursive zoom for huge sections ───────────────────

def recursive_zoom(section_text: str, doc: "fitz.Document", section_key: str,
                   drug: str, indication: str) -> str:
    """
    If an assembled section exceeds MAX_SECTION_CHARS, re-extract a sub-outline
    of just that section and ask the LLM which sub-headings are relevant.
    
    Used for the universal block on Oregon Medicaid (which spans 50+ pages of
    Targeted Immune Modulator criteria, not all relevant to PsO).
    """
    if len(section_text) <= MAX_SECTION_CHARS:
        return section_text
    
    # Re-build outline restricted to this section's char range
    page_offsets = {int(m.group(1)): m.end() for m in PAGE_MARKER.finditer(section_text)}
    sub_outline = []
    for line in section_text.split("\n"):
        line = line.strip()
        if HEADING_NUMBERED.match(line) or HEADING_KEYWORDS.match(line):
            sub_outline.append({"text": line[:150], "level": 3})
    
    if not sub_outline:
        log_debug({"event": "recursive_zoom_no_subheadings", "section": section_key,
                   "size": len(section_text)})
        return section_text[:MAX_SECTION_CHARS]  # hard truncate as last resort
    
    outline_blob = "\n".join(f"  - {h['text']}" for h in sub_outline)
    relevance_topics = {
        "universal_criteria":     "TB screening, diagnosis confirmation, severity, age tables",
        "classification_tables":  "preferred / non-preferred agent listings",
        "drug_specific_criteria": f"{drug} or {indication} steps and eligibility",
        "reauth_criteria":        "continuation / renewal requirements",
    }.get(section_key, "")
    
    prompt = f"""This section is too large. From these sub-headings, return
the ones RELEVANT to: {relevance_topics}.

SUB-HEADINGS:
{outline_blob}

Return ONLY this JSON:
{{ "keep": ["heading text 1", "heading text 2", ...] }}
"""
    try:
        response = retry(call_llm, prompt)
        keep = parse_json_safe(response).get("keep", [])
    except Exception:
        return section_text[:MAX_SECTION_CHARS]
    
    # Concatenate text under each kept heading (±2000 chars after the heading)
    pieces = []
    for heading in keep:
        idx = section_text.find(heading)
        if idx >= 0:
            pieces.append(section_text[idx : idx + 4000])
    
    return "\n\n".join(pieces) if pieces else section_text[:MAX_SECTION_CHARS]


# ── Assembly — labelled context for downstream extraction ────────

def assemble_context(sections: dict, drug: str, indication: str,
                     doc: "fitz.Document" = None) -> str:
    """
    Build the labelled context block from the 4 sliced sections.
    Recursive-zoom any oversized section. Output is what Pass 1 / Pass 2 see.
    
    Section labels are INTERNAL — Pass 2's extracted text in result.csv must
    not contain these labels (Pass 2 prompt enforces this).
    """
    parts = []
    
    universal = sections.get("universal_criteria") or ""
    if doc and len(universal) > MAX_SECTION_CHARS:
        universal = recursive_zoom(universal, doc, "universal_criteria", drug, indication)
    if universal:
        parts.append(f"[UNIVERSAL CRITERIA — applies to all drugs]\n{universal}")
    else:
        parts.append("[UNIVERSAL CRITERIA — NOT FOUND]")
    
    tables = sections.get("classification_tables") or ""
    if doc and len(tables) > MAX_SECTION_CHARS:
        tables = recursive_zoom(tables, doc, "classification_tables", drug, indication)
    if tables:
        parts.append(f"[CLASSIFICATION TABLES — preferred / non-preferred agents]\n{tables}")
    
    drug_specific = sections.get("drug_specific_criteria") or ""
    if doc and len(drug_specific) > MAX_SECTION_CHARS:
        drug_specific = recursive_zoom(drug_specific, doc, "drug_specific_criteria", drug, indication)
    if drug_specific:
        parts.append(f"[DRUG-SPECIFIC CRITERIA — {drug} for {indication}]\n{drug_specific}")
    else:
        parts.append(f"[DRUG-SPECIFIC CRITERIA — NOT FOUND FOR {drug}]")
    
    reauth = sections.get("reauth_criteria") or ""
    if doc and len(reauth) > MAX_SECTION_CHARS:
        reauth = recursive_zoom(reauth, doc, "reauth_criteria", drug, indication)
    if reauth:
        parts.append(f"[REAUTHORIZATION / RENEWAL CRITERIA]\n{reauth}")
    
    context = "\n\n".join(parts)
    
    # Resolved §M3: if over hard cap, truncate the LARGEST section first,
    # not always the universal block. Universal is often the most important
    # (contains TB / age table); the drug section can be the bloated one.
    if len(context) > MAX_CONTEXT_CHARS:
        log_debug({"event": "context_over_cap", "size": len(context),
                   "drug": drug})
        sizes = {
            "universal": len(universal),
            "tables":    len(tables),
            "drug":      len(drug_specific),
            "reauth":    len(reauth),
        }
        largest = max(sizes, key=sizes.get)
        overshoot = len(context) - MAX_CONTEXT_CHARS
        # Cap the largest at (its size - overshoot)
        target_size = max(2000, sizes[largest] - overshoot)
        if largest == "universal":   universal = universal[:target_size]
        elif largest == "tables":    tables = tables[:target_size]
        elif largest == "drug":      drug_specific = drug_specific[:target_size]
        else:                        reauth = reauth[:target_size]
        # Re-assemble
        parts = []
        if universal:     parts.append(f"[UNIVERSAL CRITERIA — applies to all drugs]\n{universal}")
        if tables:        parts.append(f"[CLASSIFICATION TABLES — preferred / non-preferred agents]\n{tables}")
        if drug_specific: parts.append(f"[DRUG-SPECIFIC CRITERIA — {drug} for {indication}]\n{drug_specific}")
        if reauth:        parts.append(f"[REAUTHORIZATION / RENEWAL CRITERIA]\n{reauth}")
        context = "\n\n".join(parts)
    
    return context


# ── Drug alias lookup (loaded from INN_TO_BRAND + extras) ────────

def get_drug_aliases(drug_name: str) -> list[str]:
    """
    Brand → all known matching strings. Used by preflight drug-not-found
    check. Combines brand, INN, common biosimilar suffixes.
    """
    inn_reverse = {v: k for k, v in INN_TO_BRAND.items()}
    aliases = {drug_name.lower()}
    inn = inn_reverse.get(drug_name.lower())
    if inn:
        aliases.add(inn)
        # Common biosimilar suffix patterns
        for suffix in ("-aekn", "-kfce", "-rdt", "-aauz", "-anbm", "-asbf"):
            aliases.add(f"{inn}{suffix}")
    return sorted(aliases)


# ── Multi-brand disambiguation helper (Resolved §11.13) ───────────

def get_other_brands_in_doc(source_text: str, target_drug: str) -> list[str]:
    """
    For multi-brand disambiguation. Returns the list of OTHER PsO brands
    that actually appear in source_text — these are the brands the LLM must
    be told to IGNORE when extracting params for {target_drug}.
    
    Scope-limited to what's in the doc (not all 35 brands) to keep prompt
    size bounded.
    """
    target_aliases = set(get_drug_aliases(target_drug))
    text_lower = source_text.lower()
    
    found = set()
    for brand in BRANDED_DRUGS | GENERIC_DRUGS:
        if brand in target_aliases:
            continue
        if re.search(rf"\b{re.escape(brand)}\b", text_lower):
            found.add(brand)
    
    # Cap at top-20 by frequency to keep prompts bounded on very-multi-brand docs
    if len(found) > 20:
        scored = sorted(
            found,
            key=lambda b: len(re.findall(rf"\b{re.escape(b)}\b", text_lower)),
            reverse=True
        )
        found = set(scored[:20])
    
    return sorted(found)
```

---

## BLOCK 4 — LLM INTERFACE

```python
# ─────────────────────────────────────────────────────────────────
# BLOCK 4 — LLM INTERFACE (Groq only — text + vision + TPM throttle)
# ─────────────────────────────────────────────────────────────────

import time
from collections import deque
from groq import Groq

groq_client = Groq(api_key=GROQ_API_KEY)


# ── TokenBudget — adaptive TPM throttle ───────────────────────────

class TokenBudget:
    """
    Sliding-window token accounting under Groq's TPM ceiling.
    
    Before sending a call we estimate (prompt + max_tokens), drop entries
    older than 60s from the window, sum what's left, and sleep until budget
    frees if the sum + estimate would breach TPM_LIMIT. We never send a call
    that would 429, so retry logic stays simple.
    """
    def __init__(self, tpm: int = TPM_LIMIT):
        self.tpm = tpm
        self.window: deque = deque()   # (timestamp, tokens)
    
    def consume(self, est_tokens: int) -> None:
        now = time.time()
        # Drop entries older than 60s
        while self.window and now - self.window[0][0] > 60:
            self.window.popleft()
        used = sum(t for _, t in self.window)
        if used + est_tokens > self.tpm:
            # Sleep until the oldest entry ages out
            oldest_ts = self.window[0][0]
            sleep_for = max(0.0, 60 - (now - oldest_ts) + 0.5)
            log_debug({"event": "tpm_throttle_sleep",
                       "seconds": round(sleep_for, 2),
                       "tokens_in_window": used,
                       "would_add": est_tokens})
            time.sleep(sleep_for)
            now = time.time()
            while self.window and now - self.window[0][0] > 60:
                self.window.popleft()
        self.window.append((now, est_tokens))


# Single shared budget instance
token_budget = TokenBudget()


def _estimate_tokens(prompt: str, system_prompt: str, max_output_tokens: int) -> int:
    """Conservative estimate: 1 token ≈ 4 chars for English text."""
    return (len(prompt) + len(system_prompt)) // 4 + max_output_tokens


# ── Provider callers ──────────────────────────────────────────────

def _call_groq_text(prompt: str, system_prompt: str = "") -> str:
    """Call Groq Llama 3.3 70B for text-only extraction / reasoning."""
    token_budget.consume(_estimate_tokens(prompt, system_prompt, MAX_TOKENS_TEXT))
    
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    
    response = groq_client.chat.completions.create(
        model=GROQ_TEXT_MODEL,
        messages=messages,
        max_tokens=MAX_TOKENS_TEXT,
        temperature=TEMPERATURE,
    )
    return response.choices[0].message.content


def _call_groq_vision(prompt: str, image_b64: str) -> str:
    """
    Call Groq Llama 4 Scout (multimodal) with one page image.
    Single-image per call to keep request payloads small.
    """
    # Vision image counts as ~1500 tokens regardless of size (Groq estimate)
    token_budget.consume(len(prompt) // 4 + 1500 + MAX_TOKENS_VISION)
    
    response = groq_client.chat.completions.create(
        model=GROQ_VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                },
            ],
        }],
        max_tokens=MAX_TOKENS_VISION,
        temperature=TEMPERATURE,
    )
    return response.choices[0].message.content


def call_llm(prompt: str, system_prompt: str = "", vision: bool = False,
             image_b64: str | None = None) -> str:
    """
    Master LLM router. One provider (Groq), two models.
    
    All calls pass through token_budget — we sleep before the call if needed.
    
    vision=False (default) → text model for extraction / CoT
    vision=True            → vision model for per-page image OCR fallback
                             (requires image_b64)
    """
    if vision:
        if not image_b64:
            raise ValueError("call_llm(vision=True) requires image_b64")
        return _call_groq_vision(prompt=prompt, image_b64=image_b64)
    return _call_groq_text(prompt=prompt, system_prompt=system_prompt)


class DailyQuotaExceeded(Exception):
    """
    Raised by retry() when Groq returns a daily-quota error (vs a minute
    rate-limit error). Caught at the orchestrator level — checkpoint saved,
    process exits with code 2 so a wrapper script / cron can resume tomorrow.
    """
    pass


def retry(func, *args, max_attempts: int = MAX_RETRIES, **kwargs):
    """
    Retry with exponential backoff. Distinguishes:
      - Daily quota exceeded   → raise DailyQuotaExceeded (no retry, halt batch)
      - 429 minute rate limit  → sleep 30s and retry
      - 5xx / connection error → exponential backoff
      - Other 4xx              → no retry, raise immediately
    
    Resolved §S4: daily vs minute quota are both 429 from Groq but the
    'Retry-After' header / error body differs. We sniff the error string.
    """
    for attempt in range(max_attempts):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            err_str = str(e)
            err_lower = err_str.lower()
            
            # Daily quota markers (Groq returns these in the error body)
            is_daily_quota = (
                "daily" in err_lower and "quota" in err_lower
            ) or (
                "rate_limit_exceeded" in err_lower
                and any(m in err_lower for m in ("day", "rpd", "tpd"))
            )
            is_429 = "429" in err_str or "rate_limit" in err_lower
            is_5xx = any(code in err_str for code in ("500", "502", "503", "504"))
            is_connection = any(kw in err_lower for kw in
                                ("connection", "timeout", "read timed out"))
            
            if is_daily_quota:
                log_debug({"event": "daily_quota_exceeded", "error": err_str})
                raise DailyQuotaExceeded(err_str) from e
            
            if is_429:
                wait = 30
            elif is_5xx or is_connection:
                wait = BACKOFF_BASE ** attempt
            else:
                # Non-retryable: bad payload, auth error, etc.
                log_debug({"event": "retry_giving_up_non_retryable",
                           "error": err_str})
                raise
            
            log_debug({"event": "retry", "attempt": attempt+1,
                       "error": err_str, "wait_seconds": wait,
                       "reason": "429" if is_429 else "5xx_or_conn"})
            if attempt == max_attempts - 1:
                raise
            time.sleep(wait)


def parse_json_safe(response: str, retry_prompt: str | None = None) -> dict:
    """
    Parse LLM JSON response safely. Handles fences, trailing commas, and
    extra prose. If parsing fails AND retry_prompt is provided, one reprompt
    asks the model to return ONLY valid JSON.
    
    Standard json.loads does NOT handle trailing commas — we strip them.
    """
    import re
    
    def _attempt(text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        if start == -1 or end == 0:
            raise ValueError(f"No JSON object found: {text[:200]}")
        text = text[start:end]
        # Strip trailing commas before } or ]
        text = re.sub(r',\s*([}\]])', r'\1', text)
        return json.loads(text)
    
    try:
        return _attempt(response)
    except (json.JSONDecodeError, ValueError) as first_err:
        log_debug({"event": "json_parse_error_first", "error": str(first_err),
                   "raw": response[:500]})
        if not retry_prompt:
            raise
        
        # One reprompt — ask for clean JSON only
        strict_prompt = (
            f"Your previous response was not valid JSON. "
            f"Return ONLY the JSON object that answers this prompt, no other text:\n\n"
            f"{retry_prompt}"
        )
        try:
            retry_response = retry(call_llm, strict_prompt)
            return _attempt(retry_response)
        except Exception as second_err:
            log_debug({"event": "json_parse_error_after_retry",
                       "error": str(second_err), "raw": retry_response[:500] if 'retry_response' in dir() else ""})
            raise first_err
```

---

## BLOCK 5 — EXTRACTION PASSES

```python
# ─────────────────────────────────────────────────────────────────
# BLOCK 5 — EXTRACTION PASSES
# ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_EXTRACTOR = """
You are a precise medical policy extraction assistant.
Rules:
- For verbatim fields: copy the EXACT words from the document — do NOT paraphrase
- For formatted fields (age, durations): convert to the specified format
- If information is not found, return "NA" for that field
- Return valid JSON only, no explanation text outside the JSON
- When a policy lists multiple drugs with different values in the same field,
  return ONLY the value associated with the TARGET drug.
"""


def _multi_brand_directive(drug: str, other_brands: list[str]) -> str:
    """
    Shared snippet appended to Pass 1 and Pass 2 prompts. Tells the model
    which OTHER brands appear in this policy so it can disambiguate.
    """
    if not other_brands:
        return ""
    others = ", ".join(other_brands)
    return f"""
TARGET DRUG: {drug}
OTHER DRUGS THAT MAY APPEAR IN THIS POLICY: {others}

CRITICAL — MULTI-BRAND DISAMBIGUATION:
If the policy lists separate values for different drugs in the SAME field
(e.g. "Age: >=18 (TREMFYA, HUMIRA), >=4 (ENBREL), >=6 (COSENTYX, STELARA)"),
return ONLY the value associated with {drug}.

Do NOT return values stated for the OTHER DRUGS listed above. If the policy
gives ONE value that applies to ALL drugs in the class or section (e.g.
"All biologics require TB screening"), use that value.
"""


# ── PASS 1 — Simple Parameters ────────────────────────────────────

def extract_simple_params(context: str, drug: str, indication: str,
                          other_brands: list[str] | None = None) -> dict:
    """
    Extract params: Age(1), TB Test(6), Initial Auth(7), Reauth Duration(8),
    Reauth Requirements(10), Specialist Types(11), Quantity Limits(12)
    
    NOTE: reauth_required(9) is NOT extracted here — it is fully derived
    by rule_reauth_required() in validation. Do not ask LLM for it.
    
    Age is the ONLY param with format standardisation (not verbatim).
    All other params: verbatim from policy.
    
    Auth durations: return as plain number string only ("6", "12").
    Convert "6 months" → "6", "one year" → "12", "up to 6 months" → "6".
    """
    
    multi_brand = _multi_brand_directive(drug, other_brands or [])
    
    prompt = f"""
From the following payer PA policy document, extract information for {drug} 
for {indication} (moderate to severe Plaque Psoriasis).
{multi_brand}

POLICY TEXT:
{context}

IMPORTANT RULES:
- For age: standardise to format like ">=18" or ">=6". Convert whatever the policy 
  says ("18 years of age or older" → ">=18", "6 years or older" → ">=6").
  If policy says "FDA approved age" or similar without a number → return "FDA approved age"
  If age not mentioned → return "NA"
- For durations: return plain number only ("6" not "6 months", "12" not "one year")
- For all other fields: copy EXACT verbatim text from policy
- Return "NA" (not empty string, not null) when field not found
- For quantity_limits: ONLY capture text explicitly labelled "quantity limit" —
  REJECT if labelled "dosage", "dosing limit", "dosing information", "recommended dose"

Return ONLY this JSON:
{{
  "age": ">=N format, or 'FDA approved age', or 'NA' if not mentioned",
  "tb_test_required": "Yes or No",
  "initial_auth_duration_months": "number only e.g. '6' or '12', or 'Unspecified'",
  "reauth_duration_months": "number only e.g. '12', or 'Unspecified' if required but unstated, or 'NA' if not mentioned",
  "reauth_requirements_text": "exact verbatim continuation criteria text, or 'NA' if not mentioned",
  "specialist_types": "comma-separated specialties, or 'NA' if none specified",
  "quantity_limits_text": "exact verbatim text if explicitly labelled quantity limit, else 'NA'"
}}
"""
    response = retry(call_llm, prompt, system_prompt=SYSTEM_PROMPT_EXTRACTOR)
    return parse_json_safe(response, retry_prompt=prompt)


# ── PASS 2 — Step Therapy Verbatim Text (single blob) ────────────

def extract_step_therapy_text(context: str, drug: str, indication: str,
                              other_brands: list[str] | None = None) -> dict:
    """
    Extract step therapy text as ONE verbatim blob.
    
    Why one blob (changed from previous split design): forcing Pass 2 to label
    each step as universal vs indication-specific is a judgment call the model
    gets wrong on Aetna-style policies. Pass 3 absorbs that classification
    using the [UNIVERSAL CRITERIA] / [DRUG-SPECIFIC CRITERIA] markers that
    Block 3 already inserted into the assembled context.
    """
    multi_brand = _multi_brand_directive(drug, other_brands or [])
    
    prompt = f"""
From the following payer PA policy document, copy VERBATIM all step therapy
requirements for {drug} ({indication} — moderate to severe plaque psoriasis).
{multi_brand}

Step therapy = prior treatments the patient must have tried and failed before
this drug can be approved.

POLICY TEXT:
{context}

RULES (strict):
1. Copy text EXACTLY as written. No paraphrasing, no summarising, no rewording.
2. Preserve AND / OR connectors and any bullet/numbering structure.
3. Include both general/universal step language AND drug- or indication-
   specific step language. Do NOT split them — concatenate into one block,
   preserving the order they appear in the policy.
4. Do NOT include eligibility criteria (TB test, diagnosis, age), quantity
   limits, or reauthorization criteria.
5. Do NOT include criteria stated for OTHER indications (PsA, Crohn's, etc.).
6. Do NOT include step requirements stated specifically for OTHER DRUGS
   (only include requirements that apply to {drug} or to the whole drug class).
7. If no step therapy exists for {drug} / {indication}: return "NA".

Return ONLY this JSON:
{{
  "combined_step_text": "the verbatim block, or 'NA' if no step therapy exists",
  "has_step_therapy": true or false
}}
"""
    response = retry(call_llm, prompt, system_prompt=SYSTEM_PROMPT_EXTRACTOR)
    result = parse_json_safe(response, retry_prompt=prompt)
    
    # Normalise "NA" sentinel
    text = result.get("combined_step_text", "") or ""
    if text.strip().upper() in ("NA", "N/A", "NONE", ""):
        result["combined_step_text"] = "NA"
        result["has_step_therapy"] = False
    
    return result


# ── PASS 3 — Step Counting CoT (input = step text only) ──────────

def extract_step_counts(combined_step_text: str, assembled_context: str,
                        drug: str, indication: str) -> dict:
    """
    Pass 3 takes the SMALL combined_step_text from Pass 2 (~1-2K tokens),
    not the full assembled context. The assembled_context is referenced only
    to give the model the [UNIVERSAL CRITERIA] / [DRUG-SPECIFIC CRITERIA] /
    [CLASSIFICATION TABLES] section markers — we slice short anchors out of
    it, we do NOT pass the whole thing.
    
    This change keeps Pass 3 ~3K tokens per call instead of ~12K.
    """
    if not combined_step_text or combined_step_text.strip() in ("", "NA"):
        return {
            "steps_brands":      "NA",
            "steps_generic":     "NA",
            "step_phototherapy": "NA",
            "reasoning":         "No step therapy text from Pass 2",
        }
    
    branded_list = ", ".join(sorted(BRANDED_DRUGS)[:30])
    generic_list = ", ".join(sorted(GENERIC_DRUGS))
    
    # Extract just the SECTION-MARKER LINES from assembled_context so Pass 3
    # can tell which part of the step text was universal vs drug-specific.
    section_markers = _extract_section_markers(assembled_context)
    
    prompt = f"""
You are counting step therapy requirements for {drug} ({indication}).

STEP THERAPY TEXT (verbatim from the policy):
{combined_step_text}

SECTION MARKERS from the assembled context (use these to classify each step
as universal vs indication-specific):
{section_markers}

CLASSIFICATION REFERENCE:
- Branded / biologic / targeted synthetic drugs: {branded_list}
  Also: any drug described as "biologic", "targeted synthetic",
  "biologic immunomodulator", or any drug class the target drug belongs to.
- Generic / conventional drugs: {generic_list}
  Also: any topical corticosteroid, any non-biologic systemic, any unnamed
  conventional step ("must try one conventional systemic").
- Phototherapy: phototherapy, UVB, PUVA, narrowband UVB, light therapy.

WORKED EXAMPLES (anchor your reasoning on these):

Example 1 — Oregon Medicaid TREMFYA (all-AND chain):
  Universal: TB test, diagnosis confirmation
  Indication: topical CS AND another topical AND phototherapy AND systemic
              AND (Humira OR Enbrel for ≥3 months)
  → step_a: [TB test, diagnosis]      (not therapy — exclude from counts)
  → step_b: [topical CS, other topical, photo, systemic, Humira-or-Enbrel]
  → step_c: same as step_b joined by AND (no OR between steps)
  → step_d: "Humira OR Enbrel" is OR WITHIN a step → 1 branded step
  → step_e: [topical CS=generic, other topical=generic, photo=photo,
             systemic=generic, Humira/Enbrel=branded]
  → counts: branded=1, generic=3, phototherapy=Yes

Example 2 — Reference tab (OR resolution):
  Universal: must try/fail Yesintek (1 branded — AND)
  Indication: previously received biologic (1 branded) OR failed
              MTX/cyclosporine/acitretin (1 generic)
  → step_d: indication has OR between steps → take LEAST RESTRICTIVE path
            = the 1-generic path
  → final: universal branded (1) AND indication generic (1)
  → counts: branded=1, generic=1, phototherapy=No (only appeared in OR)

COUNTING PROCEDURE:
  STEP A — list UNIVERSAL therapy steps   (excluding TB, diagnosis, age, etc.)
  STEP B — list INDICATION-SPECIFIC steps for {indication} only
            (ignore steps stated solely for other indications)
  STEP C — combine A and B with AND  (universal AND indication are additive)
  STEP D — within the combined set, where there are OR conditions BETWEEN
           steps, choose the path with FEWEST steps. Show the chosen path.
  STEP E — classify each resolved-path step as branded / generic / photo
  STEP F — count totals

OUTPUT RULES:
- "Humira OR Enbrel" = 1 branded step (OR within a step is not a path choice)
- "Previously received a biologic or targeted synthetic" = 1 branded step
- Unnamed step ("must try a conventional") defaults to generic
- 0 branded → output "NA" (not "0")
- 0 generic → output "NA" (not "0")
- Phototherapy: "Yes" only if mandatory AND (not in any OR); "No" if only in OR;
  "NA" if no step therapy at all

Return ONLY this JSON:
{{
  "step_a_universal":             ["…", "…"],
  "step_b_indication_specific":   ["…", "…"],
  "step_c_combined":              ["after AND merge"],
  "step_d_or_resolution":         "explain OR paths and the path chosen",
  "step_e_classification":        [{{"step": "…", "type": "branded|generic|phototherapy"}}],
  "steps_brands":                 "count as string, or 'NA'",
  "steps_generic":                "count as string, or 'NA'",
  "step_phototherapy":            "Yes | No | NA",
  "reasoning":                    "one-line summary"
}}
"""
    response = retry(call_llm, prompt, system_prompt=SYSTEM_PROMPT_EXTRACTOR)
    return parse_json_safe(response, retry_prompt=prompt)


def _extract_section_markers(assembled_context: str) -> str:
    """
    Pull the [SECTION] marker lines out of the assembled context so Pass 3
    can reference them when classifying universal vs indication-specific
    without us sending the whole context body.
    """
    marker_lines = [
        line for line in assembled_context.split("\n")
        if line.strip().startswith("[") and line.strip().endswith("]")
    ]
    return "\n".join(marker_lines) if marker_lines else "(no section markers found)"
```

---

## BLOCK 6 — BUSINESS RULES VALIDATION

```python
# ─────────────────────────────────────────────────────────────────
# BLOCK 6 — VALIDATION (verbatim + step-count reconciliation
#                       + semantic contradictions + format rules)
# ─────────────────────────────────────────────────────────────────

# Per-field token-recall thresholds. Calibrated on D2 against Reference row
# + manually-labelled spot checks (see verb_calibrate()). Until calibration
# runs, these are the conservative defaults.
VERBATIM_THRESHOLDS = {
    "combined_step_text":        0.70,
    "reauth_requirements_text":  0.70,
    "quantity_limits_text":      0.80,  # short field, less PDF noise
}


def _tokenise(text: str) -> set[str]:
    """Lowercase, strip punctuation, return set of tokens length >= 2."""
    import re
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if len(t) >= 2}


def token_recall(extracted: str, source: str) -> float:
    """
    Fraction of extracted-text tokens that also appear in source text.
    Robust to PDF ligatures, soft hyphens, and whitespace variations that
    break substring matching. Used in place of literal substring match.
    """
    ext = _tokenise(extracted)
    if not ext:
        return 1.0
    src = _tokenise(source)
    return len(ext & src) / len(ext)


# ── CRITICAL VALIDATIONS (failure triggers rerun, max 2) ──────────

def critical_verbatim_check(params: dict, source_text: str) -> list[str]:
    """
    Verify verbatim fields are token-recall-supported by the source.
    Uses calibrated per-field thresholds (token recall ≥ threshold).
    
    Failure = likely hallucination or paraphrase → trigger rerun.
    """
    failures = []
    for field, threshold in VERBATIM_THRESHOLDS.items():
        val = params.get(field)
        if not val or str(val).strip().upper() in ("NA", ""):
            continue
        if len(str(val)) < 15:
            continue
        recall = token_recall(str(val), source_text)
        if recall < threshold:
            log_debug({"event": "verbatim_failed", "field": field,
                       "recall": round(recall, 3), "threshold": threshold,
                       "val_preview": str(val)[:200]})
            failures.append(field)
    return failures


def critical_step_extraction_check(params: dict) -> list[str]:
    """Step text non-empty but BOTH counts NA → Pass 3 failed → rerun."""
    failures = []
    step_text = params.get("combined_step_text", "")
    brands = params.get("steps_brands", "NA")
    generic = params.get("steps_generic", "NA")
    
    if step_text and len(step_text) > 20 and str(step_text).upper() != "NA" \
            and brands == "NA" and generic == "NA":
        failures.append("step_count_extraction_failed")
    return failures


# ── RULE-BASED STEP COUNTER (parallel Plan B for Pass 3) ─────────

# Resolved Codex finding #7: business rules count many things as generic steps
# beyond the named drugs in GENERIC_DRUGS. Topical corticosteroids, other
# topicals, conventional systemics, NSAIDs, and unnamed conventional steps
# all qualify. Without these patterns the rule counter would undercount most
# Oregon-Medicaid-style policies.
GENERIC_STEP_PATTERNS = [
    # Topical corticosteroids — class + named members
    r"\btopical\s+(?:high[\s-]?potency\s+)?(?:cortico)?steroid",
    r"\b(?:betamethasone|clobetasol|fluocinonide|halcinonide|halobetasol|"
      r"triamcinolone|hydrocortisone|mometasone|desonide|fluticasone)\b",
    # Other topicals (vitamin D analogs, retinoids, anthralin)
    r"\b(?:calcipotriene|calcipotriol|calcitriol|tazarotene|anthralin|"
      r"crisaborole|tapinarof|roflumilast)\b",
    r"\banother\s+topical\b",
    # Conventional systemics — class + named members
    r"\b(?:conventional|non[\s-]?biologic)\s+(?:systemic|agent|therapy|treatment|dmard)",
    r"\bone\s+(?:conventional|generic|oral|non[\s-]?biologic)\s+(?:systemic|agent|therapy)",
    # NSAIDs as a category
    r"\bNSAID(?:s)?\b",
    # Abbreviations commonly used in policies
    r"\b(?:MTX|CYC)\b",
    # Conventional DMARDs not in the basket
    r"\b(?:sulfasalazine|leflunomide|hydroxychloroquine|azathioprine|"
      r"6[\s-]?mercaptopurine|6[\s-]?MP)\b",
    # Phototherapy is intentionally NOT in this list (separate param)
]


def _count_generic_pattern_hits(text: str) -> int:
    """
    Count distinct generic-step matches, clustering matches that are close
    together (within 80 chars) as one step. This avoids double-counting when
    multiple patterns overlap on the same step description (e.g.
    "topical high-potency corticosteroid (betamethasone)" matches both
    the topical-class pattern and the betamethasone pattern).
    """
    hit_offsets: list[int] = []
    for pattern in GENERIC_STEP_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            hit_offsets.append(m.start())
    if not hit_offsets:
        return 0
    hit_offsets.sort()
    # Cluster: each cluster spans hits within 80 chars of each other
    clusters = 1
    last = hit_offsets[0]
    for o in hit_offsets[1:]:
        if o - last > 80:
            clusters += 1
        last = o
    return clusters


def rule_based_step_count(combined_step_text: str) -> dict:
    """
    Deterministic step counter — runs alongside Pass 3 on every row.
    
    Generic counter (Resolved Codex #7): combines (a) named-drug hits against
    GENERIC_DRUGS, and (b) pattern-based hits against GENERIC_STEP_PATTERNS.
    The union is clustered by char-proximity so overlapping patterns don't
    double-count.
    
    Used three ways:
      1. Sanity check: if LLM and rule agree → high confidence
      2. Discrepancy flag: if they disagree by ≥2 → manual review
      3. Hard fallback: if Pass 3 LLM call fails entirely → emit rule counts
    """
    if not combined_step_text or str(combined_step_text).strip().upper() in ("", "NA"):
        return {"brands_rule": "NA", "generic_rule": "NA", "photo_rule": "NA"}
    
    text = combined_step_text.lower()
    
    # Brand hits — named only (no class-level patterns for branded; biologics
    # are typically called out by name)
    branded_hits = {
        b for b in BRANDED_DRUGS
        if re.search(rf"\b{re.escape(b.lower())}\b", text)
    }
    
    # Generic count: named-drug hits + pattern hits, clustered
    generic_offsets: list[int] = []
    for g in GENERIC_DRUGS:
        for m in re.finditer(rf"\b{re.escape(g.lower())}\b", text):
            generic_offsets.append(m.start())
    for pattern in GENERIC_STEP_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            generic_offsets.append(m.start())
    
    generic_count = 0
    if generic_offsets:
        generic_offsets.sort()
        generic_count = 1
        last = generic_offsets[0]
        for o in generic_offsets[1:]:
            if o - last > 80:
                generic_count += 1
            last = o
    
    # Phototherapy presence + OR-context
    photo_mentioned = any(t in text for t in PHOTOTHERAPY_TERMS)
    photo_in_or = bool(re.search(
        r"\bor\b[^.]{0,80}(phototherapy|puva|uvb|narrowband|light therapy)",
        text
    ))
    
    # Cluster brand mentions joined by "or" within 50 chars → one step
    brand_clusters = _cluster_by_or_proximity(branded_hits, text, window=50)
    
    return {
        "brands_rule":  str(len(brand_clusters)) if brand_clusters else "NA",
        "generic_rule": str(generic_count)      if generic_count   else "NA",
        "photo_rule":   "Yes" if (photo_mentioned and not photo_in_or)
                        else "No"  if photo_mentioned
                        else "NA",
    }


def _cluster_by_or_proximity(brand_hits: set[str], text: str, window: int) -> list[set]:
    """
    Two brand mentions within `window` chars joined by "or" → same cluster.
    Each cluster counts as ONE branded step.
    """
    if not brand_hits:
        return []
    # Map each brand to its first occurrence offset
    positions = []
    for brand in brand_hits:
        m = re.search(rf"\b{re.escape(brand.lower())}\b", text)
        if m:
            positions.append((m.start(), brand))
    positions.sort()
    
    clusters: list[set] = []
    current: set = set()
    last_end = -1
    for pos, brand in positions:
        if last_end >= 0 and (pos - last_end) <= window \
                and " or " in text[last_end:pos]:
            current.add(brand)
        else:
            if current:
                clusters.append(current)
            current = {brand}
        last_end = pos + len(brand)
    if current:
        clusters.append(current)
    return clusters


def reconcile_step_counts(llm_counts: dict, rule_counts: dict,
                          llm_failed: bool = False) -> tuple[dict, list[str]]:
    """
    Merge LLM + rule counts. Returns (final_counts, flags).
    
    - LLM == rule: high confidence, no flag.
    - |LLM - rule| <= 1: minor disagree, emit LLM, flag MINOR.
    - |LLM - rule| >= 2: major disagree, emit LLM, flag MAJOR for manual review.
    - LLM failed entirely: emit rule, flag RULE_FALLBACK.
    """
    flags: list[str] = []
    if llm_failed:
        return ({
            "steps_brands":      rule_counts["brands_rule"],
            "steps_generic":     rule_counts["generic_rule"],
            "step_phototherapy": rule_counts["photo_rule"],
        }, ["STEP_COUNT_RULE_FALLBACK"])
    
    def _to_int(v):
        try: return int(str(v))
        except Exception: return None
    
    for field, llm_key, rule_key in [
        ("brands",  "steps_brands",  "brands_rule"),
        ("generic", "steps_generic", "generic_rule"),
    ]:
        l = _to_int(llm_counts.get(llm_key))
        r = _to_int(rule_counts.get(rule_key))
        if l is None or r is None:
            continue
        diff = abs(l - r)
        if diff == 0:
            continue
        elif diff <= 1:
            flags.append(f"STEP_COUNT_MINOR_DISAGREE_{field}_llm{l}_rule{r}")
        else:
            flags.append(f"STEP_COUNT_MAJOR_DISAGREE_{field}_llm{l}_rule{r}")
    
    # Photo: simple disagreement flag
    if llm_counts.get("step_phototherapy") != rule_counts.get("photo_rule") \
            and rule_counts.get("photo_rule") != "NA":
        flags.append(f"PHOTO_DISAGREE_llm{llm_counts.get('step_phototherapy')}_rule{rule_counts.get('photo_rule')}")
    
    return ({
        "steps_brands":      llm_counts.get("steps_brands", "NA"),
        "steps_generic":     llm_counts.get("steps_generic", "NA"),
        "step_phototherapy": llm_counts.get("step_phototherapy", "NA"),
    }, flags)


# ── ADVISORY VALIDATIONS (flag only, no rerun) ────────────────────

def rule_reauth_required(params: dict) -> dict:
    """
    Derived rule: if reauth_duration or reauth_requirements is non-null/non-NA
    → set reauth_required = "Yes"
    Do NOT ask LLM for this — it is 100% derived.
    """
    duration = params.get("reauth_duration_months")
    requirements = params.get("reauth_requirements_text")
    
    has_duration = duration and str(duration).upper() not in ("NA", "NULL", "NONE", "N/A", "")
    has_requirements = requirements and str(requirements).upper() not in ("NA", "NULL", "NONE", "N/A", "")
    
    params["reauth_required"] = "Yes" if (has_duration or has_requirements) else "No"
    return params


def rule_auth_duration(params: dict) -> dict:
    """
    If PA required for PsO (always in this dataset):
    initial_auth_duration must be a stated number or "Unspecified". Never blank.
    """
    duration = params.get("initial_auth_duration_months")
    if not duration or str(duration).strip().upper() in ("", "NULL", "NONE", "NA", "N/A"):
        params["initial_auth_duration_months"] = "Unspecified"
    return params


def rule_quantity_limits_strict(params: dict) -> dict:
    """
    Quantity limits: reject if labelled as dosage/dosing/administration.
    Keep only if explicitly labelled "quantity limit".
    """
    ql = params.get("quantity_limits_text", "")
    if not ql:
        return params
    
    reject_terms = ["dosage", "dosing limit", "dosing information",
                    "recommended dose", "administration", "dose is",
                    "quantity limits exist"]  # generic statement without specifics
    
    if any(term in ql.lower() for term in reject_terms):
        params["quantity_limits_text"] = None
        params.setdefault("_flags", []).append("quantity_limit_rejected_wrong_label")
    
    return params


def rule_age_format(params: dict) -> dict:
    """
    Age standardisation and edge case handling.
    
    Output format rules (confirmed against Additional Extracted Data ground truth):
    - Age found with number → ">=N" format (e.g. ">=18", ">=6")
    - Policy says "FDA approved age" or similar → "FDA approved age"
    - Age not mentioned → "NA"
    
    NOTE: Partner requested "NA" for not mentioned. Ground truth (Additional
    Extracted Data tab, 337 rows) uses "No". Partner confirmed "NA" — partner instruction takes precedence.
    Flagged for partner confirmation before final submission.
    """
    age = params.get("age", "")
    if not age or str(age).strip().upper() in ("", "NULL", "NONE", "NA", "N/A", "NO"):
        params["age"] = "NA"
    elif "fda" in str(age).lower() and not any(c.isdigit() for c in str(age)):
        params["age"] = "FDA approved age"
    # If LLM returned a sentence instead of >=N format, attempt to parse
    elif str(age)[0].isdigit():
        params["age"] = f">={age}"
    return params


def rule_step_na_format(params: dict) -> dict:
    """Step counts of 0 must output "NA" not "0"."""
    for field in ["steps_brands", "steps_generic"]:
        val = params.get(field)
        if val in (0, "0", 0.0):
            params[field] = "NA"
    return params


def semantic_contradiction_checks(params: dict, source_text: str = "") -> list[str]:
    """
    Advisory rules detecting internal contradictions. Tag rows for manual
    review. No rerun — these don't fix themselves on retry.
    """
    warnings = []
    
    # Reauth contradiction
    reauth_req = params.get("reauth_required", "")
    reauth_dur = params.get("reauth_duration_months")
    if reauth_req == "No" and reauth_dur and \
            str(reauth_dur).upper() not in ("NA", "NULL", "NONE", "N/A", ""):
        warnings.append("REAUTH_CONTRADICTION")
    
    # Brand missed: step text mentions a known brand but brand count = NA
    step_text = (params.get("combined_step_text") or "").lower()
    brands = params.get("steps_brands", "NA")
    if step_text and step_text != "na" and brands == "NA":
        brand_in_text = any(
            re.search(rf"\b{re.escape(b.lower())}\b", step_text)
            for b in BRANDED_DRUGS
        )
        if brand_in_text:
            warnings.append("BRAND_MISSED")
    
    # TB contradiction
    tb = params.get("tb_test_required", "")
    if tb == "Yes" and source_text:
        if re.search(r"no\s+tb\s+testing|tb\s+test(ing)?\s+not\s+required",
                     source_text.lower()):
            warnings.append("TB_CONTRADICTION")
    
    # Age out of range
    age = str(params.get("age", ""))
    m = re.search(r">=(\d+)", age)
    if m:
        n = int(m.group(1))
        if n < 1 or n > 99:
            warnings.append("AGE_OUT_OF_RANGE")
    
    # Specialist vague
    spec = (params.get("specialist_types") or "").lower()
    if spec and spec != "na":
        if any(w in spec for w in ("appropriate", "qualified", "licensed prescriber")):
            warnings.append("SPECIALIST_VAGUE")
    
    # Auth duration outlier
    init_auth = params.get("initial_auth_duration_months", "")
    try:
        if init_auth and str(init_auth).upper() != "UNSPECIFIED":
            d = int(str(init_auth))
            if d < 1 or d > 24:
                warnings.append("AUTH_DURATION_OUTLIER")
    except ValueError:
        pass
    
    return warnings


def multi_brand_ambiguity_check(params: dict, source_text: str,
                                target_drug: str) -> list[str]:
    """
    Resolved §11.13 — advisory check. For at-risk per-drug fields (Age,
    Quantity Limits, Specialist Types), scan a window around the extracted
    value in the source. If ANOTHER brand name appears in that window,
    flag MULTI_BRAND_AMBIGUOUS_<field>.
    
    Doesn't auto-rerun — surfaces ambiguous rows in the end-of-batch summary
    for manual review.
    """
    warnings = []
    if not source_text:
        return warnings
    
    text_lower = source_text.lower()
    target_aliases = set(get_drug_aliases(target_drug))
    other_brands = (BRANDED_DRUGS | GENERIC_DRUGS) - target_aliases
    
    # Field → (extraction logic, window size around match)
    risk_fields = {
        "age":                  300,
        "quantity_limits_text": 500,
        "specialist_types":     300,
    }
    
    for field, window in risk_fields.items():
        val = params.get(field)
        if not val or str(val).strip().upper() in ("NA", "", "FDA APPROVED AGE"):
            continue
        
        # Locate the value's footprint in source
        if field == "age":
            m = re.search(r">=(\d+)", str(val))
            if not m:
                continue
            n = m.group(1)
            # Find occurrences of this number near an age keyword
            for found in re.finditer(rf"\b{n}\b", text_lower):
                start = max(0, found.start() - window // 2)
                end   = min(len(text_lower), found.end() + window // 2)
                snip  = text_lower[start:end]
                if not any(kw in snip for kw in ("age", "years", "year of")):
                    continue
                # Check for another brand name in the same window
                if any(re.search(rf"\b{re.escape(b)}\b", snip) for b in other_brands):
                    warnings.append(f"MULTI_BRAND_AMBIGUOUS_age")
                    break
        else:
            # For text fields: search for first ~50 chars of the extracted value
            needle = str(val)[:50].lower().strip()
            if len(needle) < 10:
                continue
            idx = text_lower.find(needle)
            if idx < 0:
                continue
            start = max(0, idx - window // 2)
            end   = min(len(text_lower), idx + len(needle) + window // 2)
            snip  = text_lower[start:end]
            if any(re.search(rf"\b{re.escape(b)}\b", snip) for b in other_brands):
                warnings.append(f"MULTI_BRAND_AMBIGUOUS_{field}")
    
    return warnings


def calibrate_verbatim_thresholds(validation_set: list[dict]) -> None:
    """
    D2 calibration step. After running Pass 2 on Reference + spot-check rows,
    compute the actual token-recall distribution per field across known-good
    extractions. Set VERBATIM_THRESHOLDS[field] = 5th percentile of that
    distribution.
    
    If fewer than 3 calibration rows are available: stay at the conservative
    defaults. Log what was used.
    """
    from statistics import quantiles
    global VERBATIM_THRESHOLDS
    
    if len(validation_set) < 3:
        log_debug({"event": "verbatim_calibration_skipped",
                   "reason": "too few rows", "rows": len(validation_set)})
        return
    
    for field in list(VERBATIM_THRESHOLDS.keys()):
        recalls = [
            token_recall(row.get(field, ""), row.get("_source_text", ""))
            for row in validation_set
            if row.get(field) and str(row.get(field)).upper() != "NA"
        ]
        if len(recalls) < 3:
            continue
        # 5th percentile via quantiles
        q = quantiles(recalls, n=20)  # q[0] is the 5th percentile
        new_threshold = max(0.5, round(q[0], 2))
        log_debug({"event": "verbatim_calibrated", "field": field,
                   "samples": len(recalls), "new_threshold": new_threshold,
                   "previous": VERBATIM_THRESHOLDS[field]})
        VERBATIM_THRESHOLDS[field] = new_threshold


def validate_all(params: dict, source_text: str = "",
                 target_drug: str = "") -> tuple[dict, list[str]]:
    """
    Run all validation. Returns (validated_params, critical_failures).
    
    Layers:
      1. Format/derivation rules (always mutate in place)
      2. Critical checks (return failures → caller reruns row)
      3. Semantic contradiction checks (advisory — flag only)
      4. Multi-brand ambiguity checks (advisory — flag only, Resolved §11.13)
    """
    # Format / derivation rules
    params = rule_reauth_required(params)
    params = rule_auth_duration(params)
    params = rule_quantity_limits_strict(params)
    params = rule_age_format(params)
    params = rule_step_na_format(params)
    
    # Critical checks
    critical_failures = []
    if source_text:
        critical_failures.extend(critical_verbatim_check(params, source_text))
    critical_failures.extend(critical_step_extraction_check(params))
    
    # Semantic contradictions (advisory)
    warnings = semantic_contradiction_checks(params, source_text)
    params.setdefault("_warnings", []).extend(warnings)
    
    # Multi-brand ambiguity (advisory)
    if target_drug and source_text:
        params["_warnings"].extend(
            multi_brand_ambiguity_check(params, source_text, target_drug)
        )
    
    return params, critical_failures
```

---

## BLOCK 7 — ACCESS SCORE (DEFERRED TO PHASE 2)

**Not built in this phase.** Logic lives in [05_CONSTRAINTS.md §Param 13](./05_CONSTRAINTS.md) as the spec; the function bodies stay empty until extraction is validated against the Reference tab and spot-checked rows. For now `params["access_score"] = ""` flows through the orchestrator and `format_row()` writes an empty cell. The submission contract requires the column to exist — it does not require it to be populated during extraction-only builds.

```python
# Stubs only. Phase 2 will implement these.
def classify_bucket(params: dict) -> int: ...
def compute_within_bucket_score(params: dict, bucket: int) -> float: ...
def compute_access_score(params: dict) -> int: ...
```

---

## BLOCK 8 — ORCHESTRATOR

```python
# ─────────────────────────────────────────────────────────────────
# BLOCK 8 — ORCHESTRATOR
#   - Pre-flight (cross-reference + drug presence scan)
#   - Three-tier cache: pdf_cache, outline_cache, section_cache
#   - Per-row pipeline with rerun loop + rule-based step fallback
#   - Atomic checkpoint writes keyed by (filename, brand)
# ─────────────────────────────────────────────────────────────────

import os
import tempfile


# ── PRE-FLIGHT CHECK (Resolved §B2, §11.2, §11.10) ────────────────

def preflight_check(submissions_df: pd.DataFrame, pdf_cache: dict) -> None:
    """
    CHEAP-ONLY pre-flight. No LLM calls, no vision OCR.
    
    Resolved §B2: previous design called full ingest_pdf which could fire
    vision-fallback LLM calls. Now we only:
      1. Cross-reference Submissions filenames against PDF_DIR. Missing → halt.
      2. For each unique filename: do a CHEAP text-only scan (PyMuPDF
         page.get_text, no vision fallback) and check for drug aliases.
      3. Populate pdf_cache with the cheap-extracted text so process_all_rows
         doesn't re-ingest.
    
    If a row needs vision fallback, that happens lazily during process_single_row
    on first cache miss — NOT during preflight.
    """
    submissions_files = set(submissions_df["Filename"].astype(str))
    disk_files = {f.name for f in PDF_DIR.glob("*.pdf")}
    missing = submissions_files - disk_files
    unused = disk_files - submissions_files
    
    if missing:
        raise RuntimeError(
            f"Submissions references {len(missing)} missing PDFs:\n  "
            + "\n  ".join(sorted(missing))
        )
    if unused:
        print(f"INFO: {len(unused)} PDFs on disk not referenced by Submissions")
        for f in sorted(unused)[:10]:
            print(f"   {f}")
        if len(unused) > 10:
            print(f"   ... and {len(unused) - 10} more")
    
    # Drug-presence scan — text-only, no vision
    zero_match = []
    for filename, group in submissions_df.groupby("Filename"):
        doc_info = _ingest_pdf_text_only(PDF_DIR / filename)
        pdf_cache[filename] = doc_info  # populate cache for batch run
        full_text_lower = doc_info["full_text"].lower()
        for drug in group["Brand"].unique():
            aliases = get_drug_aliases(drug)
            if not any(a in full_text_lower for a in aliases):
                zero_match.append((filename, drug))
                first_pages = "\n".join(doc_info["pages"][:2])[:5000]
                log_debug({"event": "drug_not_found_preflight",
                           "filename": filename, "drug": drug,
                           "first_2_pages_preview": first_pages})
    
    if zero_match:
        print(f"WARNING: {len(zero_match)} (filename, drug) pairs have no "
              f"alias matches anywhere in the PDF. These will produce "
              f"CRITICAL_DRUG_NOT_FOUND rows. Inspect debug_log.jsonl.")
        for fn, dg in zero_match[:10]:
            print(f"   {fn} — {dg}")


def _ingest_pdf_text_only(filepath: Path) -> dict:
    """
    Cheap text-only ingestion for preflight — NO vision fallback.
    Returns the same dict shape as ingest_pdf but with a `_text_only` flag
    so the orchestrator knows to upgrade to vision-aware ingestion if it
    actually finds sparse pages it needs.
    """
    try:
        doc = fitz.open(filepath)
    except Exception as e:
        raise RuntimeError(f"PyMuPDF cannot open {filepath}: {e}") from e
    
    pages = [page.get_text("text") or "" for page in doc]
    full_text = "".join(
        f"\n===== PAGE {i} =====\n{p}" for i, p in enumerate(pages, start=1)
    )
    return {
        "full_text":    full_text,
        "pages":        pages,
        "vision_pages": [],
        "n_pages":      len(pages),
        "filepath":     str(filepath),
        "doc_handle":   doc,
        "_text_only":   True,
    }


def _ensure_vision_aware(doc_info: dict) -> dict:
    """
    Upgrade a text-only ingestion to vision-aware. Called lazily by
    process_single_row only if we actually find sparse pages that need OCR.
    For the audited 70-PDF dataset this should never fire.
    """
    if not doc_info.get("_text_only"):
        return doc_info
    
    doc = doc_info["doc_handle"]
    pages, vision_pages = [], []
    for i, page in enumerate(doc, start=1):
        text = doc_info["pages"][i - 1]
        if len(text.strip()) < MIN_CHARS_PER_PAGE:
            text = _ocr_page_with_vision(page, page_num=i, fallback_text=text)
            vision_pages.append(i)
        pages.append(text)
    
    full_text = "".join(
        f"\n===== PAGE {i} =====\n{p}" for i, p in enumerate(pages, start=1)
    )
    doc_info["pages"] = pages
    doc_info["full_text"] = full_text
    doc_info["vision_pages"] = vision_pages
    doc_info["_text_only"] = False
    return doc_info


# ── PER-ROW PIPELINE ──────────────────────────────────────────────

def process_single_row(filename: str, drug: str, indication: str,
                       pdf_cache: dict, outline_cache: dict,
                       section_cache: dict, rerun_count: int = 0) -> dict:
    """
    Full pipeline for one row. Three caches, three passes, rerun on critical.
    
    Cache tiers:
      1. pdf_cache[filename]       — ingested text + open doc handle
      2. outline_cache[filename]   — extracted outline
      3. section_cache[(filename, drug)] — assembled context per drug
    """
    try:
        # ── Cache tier 1: ingestion ──
        # Preflight may have populated this with text-only entries.
        # Upgrade to vision-aware lazily if any page is sparse.
        if filename not in pdf_cache:
            pdf_cache[filename] = ingest_pdf(PDF_DIR / filename)
        else:
            pdf_cache[filename] = _ensure_vision_aware(pdf_cache[filename])
        doc_info = pdf_cache[filename]
        full_text = doc_info["full_text"]
        doc = doc_info["doc_handle"]
        
        # ── Cache tier 2: outline ──
        if filename not in outline_cache:
            outline_cache[filename] = extract_outline(doc, full_text)
        outline = outline_cache[filename]
        
        # ── Cache tier 3: assembled context (per (filename, drug)) ──
        # Resolved §B3: small docs OR empty/tiny outlines → use full_text directly.
        cache_key = (filename, drug)
        if cache_key not in section_cache:
            if len(full_text) < 50_000 or len(outline) < 5:
                log_debug({"event": "section_full_doc_path",
                           "filename": filename, "drug": drug,
                           "outline_size": len(outline),
                           "text_size": len(full_text)})
                section_cache[cache_key] = full_text[:MAX_CONTEXT_CHARS]
            else:
                anchors = map_outline_to_sections(outline, drug, indication)
                sections = slice_by_anchors(full_text, outline, anchors)
                # If sectioning produced NOTHING usable, fall back to full doc
                if not any(sections.values()):
                    log_debug({"event": "sectioning_empty_falling_back",
                               "filename": filename, "drug": drug})
                    section_cache[cache_key] = full_text[:MAX_CONTEXT_CHARS]
                else:
                    section_cache[cache_key] = assemble_context(
                        sections, drug, indication, doc=doc
                    )
        context = section_cache[cache_key]
        
        # ── Drug-not-found loud raise (Resolved §11.2) ──
        aliases = get_drug_aliases(drug)
        if not any(a in context.lower() for a in aliases) \
                and not any(a in full_text.lower() for a in aliases):
            log_debug({"event": "drug_not_found_in_row",
                       "filename": filename, "drug": drug})
            return _na_row_with_warning("CRITICAL_DRUG_NOT_FOUND")
        
        # ── Compute OTHER_BRANDS list for multi-brand disambiguation ──
        other_brands = get_other_brands_in_doc(context, drug)
        
        # ── Pass 1 — Simple params ──
        simple = retry(extract_simple_params, context, drug, indication, other_brands)
        
        # ── Pass 2 — Step therapy verbatim (single blob) ──
        step_pass2 = retry(extract_step_therapy_text, context, drug, indication, other_brands)
        combined_step_text = step_pass2.get("combined_step_text", "NA")
        
        # ── Pass 3 — Step counting (input = step text only) ──
        llm_failed = False
        try:
            llm_counts = extract_step_counts(combined_step_text, context, drug, indication)
        except DailyQuotaExceeded:
            raise  # propagate so process_all_rows can save + exit cleanly
        except Exception as e:
            log_debug({"event": "pass3_failed_using_rule_fallback",
                       "error": str(e), "filename": filename, "drug": drug})
            llm_counts = {"steps_brands": "NA", "steps_generic": "NA",
                          "step_phototherapy": "NA"}
            llm_failed = True
        
        # ── Rule-based counter (parallel Plan B) ──
        rule_counts = rule_based_step_count(combined_step_text)
        final_counts, count_flags = reconcile_step_counts(
            llm_counts, rule_counts, llm_failed=llm_failed
        )
        
        # ── Merge ──
        params = {**simple, **final_counts}
        params["combined_step_text"] = combined_step_text
        params.setdefault("_warnings", []).extend(count_flags)
        
        # ── Validate (may trigger rerun) ──
        params, critical_failures = validate_all(
            params, source_text=full_text, target_drug=drug
        )
        
        if critical_failures and rerun_count < MAX_PIPELINE_RERUNS:
            log_debug({"event": "rerun", "filename": filename, "drug": drug,
                       "rerun_count": rerun_count + 1,
                       "critical_failures": critical_failures})
            section_cache.pop(cache_key, None)
            return process_single_row(filename, drug, indication, pdf_cache,
                                      outline_cache, section_cache,
                                      rerun_count=rerun_count + 1)
        
        if critical_failures:
            params.setdefault("_warnings", []).append(
                f"CRITICAL_VALIDATION_FAILED_AFTER_{MAX_PIPELINE_RERUNS}_RERUNS:"
                f"{critical_failures}"
            )
        
        # Phase 2 placeholder
        params["access_score"] = ""
        
        if params.get("_warnings"):
            log_debug({"event": "row_warnings", "filename": filename,
                       "drug": drug, "warnings": params["_warnings"]})
        
        return params
        
    except DailyQuotaExceeded:
        raise
    except Exception as e:
        log_debug({"event": "row_failed", "filename": filename,
                   "drug": drug, "error": str(e)})
        return _na_row_with_warning(f"EXTRACTION_EXCEPTION: {e}")


def _na_row_with_warning(warning: str) -> dict:
    """Empty row with all NAs + a warning tag. Used for unrecoverable cases."""
    return {
        "age": "NA", "combined_step_text": "NA",
        "steps_brands": "NA", "steps_generic": "NA",
        "step_phototherapy": "NA", "tb_test_required": "No",
        "quantity_limits_text": "NA", "specialist_types": "NA",
        "initial_auth_duration_months": "Unspecified",
        "reauth_duration_months": "NA", "reauth_required": "No",
        "reauth_requirements_text": "NA",
        "access_score": "",
        "_warnings": [warning],
    }


# ── BATCH ORCHESTRATION + CHECKPOINTING ──────────────────────────

def process_all_rows(submissions_df: pd.DataFrame,
                     pdf_cache: dict | None = None,
                     indication_default: str = "Plaque Psoriasis") -> list[dict]:
    """
    Loop all rows. Cache PDFs + outlines + assembled contexts.
    Checkpoint atomically every CHECKPOINT_INTERVAL rows.
    Resume by (filename, brand) key — survives spreadsheet edits.
    
    pdf_cache may be pre-populated by preflight_check() — we reuse it.
    
    On DailyQuotaExceeded: save checkpoint, exit with code 2 — wrapper script
    or cron can resume tomorrow.
    Closes all PyMuPDF doc handles in finally — no leaked file descriptors.
    """
    if pdf_cache is None:
        pdf_cache = {}
    outline_cache: dict = {}
    section_cache: dict = {}
    
    # Load checkpoint, keyed by (filename, brand)
    completed: dict[tuple[str, str], dict] = {}
    if CHECKPOINT_PATH.exists():
        with open(CHECKPOINT_PATH) as f:
            ckpt = json.load(f)
            for entry in ckpt.get("results", []):
                key = (entry.get("Filename"), entry.get("Brand"))
                completed[key] = entry
        print(f"Resuming with {len(completed)} rows already done.")
    
    results: list[dict] = []
    total = len(submissions_df)
    
    try:
        for idx, row in submissions_df.iterrows():
            filename = str(row["Filename"])
            drug = str(row["Brand"])
            indication = str(row.get("Indication") or indication_default)
            key = (filename, drug)
            
            if key in completed:
                results.append(completed[key])
                continue
            
            print(f"Processing row {idx+1}/{total}: {filename} — {drug}")
            try:
                result = process_single_row(
                    filename, drug, indication,
                    pdf_cache, outline_cache, section_cache
                )
            except DailyQuotaExceeded as e:
                print(f"\nDAILY QUOTA EXCEEDED. Saving checkpoint and exiting.")
                print(f"Resume tomorrow with: python pipeline.py")
                _atomic_checkpoint_write(results)
                close_pdf_cache(pdf_cache)
                import sys
                sys.exit(2)
            
            result["Filename"] = filename
            result["Brand"] = drug
            results.append(result)
            
            if len(results) % CHECKPOINT_INTERVAL == 0:
                _atomic_checkpoint_write(results)
                print(f"  Checkpoint saved ({len(results)} rows)")
        
        _atomic_checkpoint_write(results)
        return results
    finally:
        close_pdf_cache(pdf_cache)


def _atomic_checkpoint_write(results: list[dict]) -> None:
    """Write to temp file then os.replace — survives mid-write crashes."""
    fd, tmp_path = tempfile.mkstemp(prefix="checkpoint_", suffix=".json",
                                    dir=str(CHECKPOINT_PATH.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"results": results}, f)
        os.replace(tmp_path, CHECKPOINT_PATH)
    except Exception:
        os.unlink(tmp_path)
        raise


def print_end_of_batch_summary(results: list[dict]) -> None:
    """End-of-batch diagnostics — surface rows needing manual review."""
    flag_counts: dict[str, int] = {}
    critical_rows: list[tuple[str, str, list[str]]] = []
    
    for r in results:
        warnings = r.get("_warnings", []) or []
        for w in warnings:
            tag = w.split("_")[0] + "_" + (w.split("_")[1] if "_" in w else "")
            flag_counts[tag] = flag_counts.get(tag, 0) + 1
            if any(w.startswith(p) for p in
                   ("CRITICAL", "STEP_COUNT_MAJOR", "TB_CONTRADICTION",
                    "REAUTH_CONTRADICTION", "BRAND_MISSED")):
                critical_rows.append((r.get("Filename"), r.get("Brand"), warnings))
    
    print("\n===== END-OF-BATCH SUMMARY =====")
    print(f"Total rows: {len(results)}")
    print(f"\nWarning tag counts:")
    for tag, n in sorted(flag_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {tag}")
    print(f"\nRows needing manual review: {len(critical_rows)}")
    for fn, dg, ws in critical_rows[:20]:
        print(f"  {fn} — {dg}: {ws}")
    if len(critical_rows) > 20:
        print(f"  ... and {len(critical_rows) - 20} more")
```

---

## BLOCK 9 — OUTPUT + STARTUP

```python
# ─────────────────────────────────────────────────────────────────
# BLOCK 9 — OUTPUT
# ─────────────────────────────────────────────────────────────────

# Resolved §B1: param keys MUST match what process_single_row sets.
# `combined_step_text` is the param name (was incorrectly `step_therapy_text`).
COLUMN_MAP = {
    "Filename": "Filename",
    "Brand": "Brand",
    "age": "Age",
    "combined_step_text": "Step Therapy Requirements Documented in Policy",
    "steps_brands": "Number of Steps through Brands",
    "steps_generic": "Number of Steps through Generic",
    "step_phototherapy": "Step through-Phototherapy",
    "tb_test_required": "TB Test required",
    "quantity_limits_text": "Quantity Limits",
    "specialist_types": "Specialist Types",
    "initial_auth_duration_months": "Initial Authorization Duration(in-months)",
    "reauth_duration_months": "Reauthorization Duration(in-months)",
    "reauth_required": "Reauthorization Required",
    "reauth_requirements_text": "Reauthorization Requirements Documented in Policy",
    "access_score": "Access Score"
}

# Param keys that are allowed to be empty (Phase 2 deferral)
PHASE2_PARAMS = {"access_score"}


def format_row(params: dict) -> dict:
    """
    Map internal param keys to CSV column names.
    Resolved §B1: raise on missing keys (other than Phase 2 deferrals) to
    catch wiring bugs immediately instead of producing silent empty columns.
    """
    missing = [k for k in COLUMN_MAP
               if k not in params and k not in PHASE2_PARAMS
               and k not in ("Filename", "Brand")]
    if missing:
        raise KeyError(f"format_row: params is missing keys: {missing} "
                       f"(params has: {list(params.keys())})")
    return {COLUMN_MAP[k]: params.get(k, "") for k in COLUMN_MAP}


def save_csv(results: list[dict], path: Path = OUTPUT_PATH):
    formatted = [format_row(r) for r in results]
    df = pd.DataFrame(formatted)
    df = df[list(COLUMN_MAP.values())]
    df.to_csv(path, index=False)
    print(f"Saved {len(results)} rows to {path}")


def log_debug(entry: dict):
    """
    Append-only JSONL log. Resolved §S5: previous O(N²) read-all-then-write
    pattern is gone. One line = one event.
    """
    with open(DEBUG_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ─────────────────────────────────────────────────────────────────
# STARTUP FUNCTIONS
# ─────────────────────────────────────────────────────────────────

def load_drug_classifications(xlsx_path: Path) -> tuple[set, set]:
    """
    Load branded vs generic drug classification from XLSX.
    Reads "PsO Brands- For Ground Truth" tab.
    
    Drugs in tab = entire PsO market basket.
    Drugs in KNOWN_GENERICS_IN_MARKET_BASKET = generic/conventional.
    Everything else in the tab = branded/targeted.
    
    Returns (branded_set, generic_set) — both lowercase.
    
    Also adds INN aliases from INN_TO_BRAND for matching.
    """
    try:
        df = pd.read_excel(xlsx_path, sheet_name="PsO Brands- For Ground Truth", header=0)
        all_drugs = {str(v).lower().strip() for v in df.iloc[:, 0] if str(v) not in ('nan', '')}
    except Exception:
        # Fallback if sheet not found
        all_drugs = set()
    
    generic_set = {d for d in all_drugs if d in KNOWN_GENERICS_IN_MARKET_BASKET}
    branded_set = all_drugs - generic_set
    
    # Add INNs to branded set for policy text matching
    for inn, brand in INN_TO_BRAND.items():
        if brand.lower() in branded_set:
            branded_set.add(inn.lower())
    
    # Add targeted synthetics not in market basket (JAK inhibitors etc.)
    # These are branded steps per business rules even if not in the tab
    branded_set.update(TARGETED_SYNTHETICS_NOT_IN_BASKET)
    
    return branded_set, generic_set


def load_submissions(path: Path) -> pd.DataFrame:
    """
    Load input spreadsheet. Handles .xlsx and .csv.
    Always returns DataFrame with normalised Filename and Brand columns.
    Indication column passes through if present.
    """
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
    else:
        try:
            df = pd.read_excel(path, sheet_name="Submissions")
        except Exception:
            df = pd.read_excel(path, sheet_name=0)
    
    return normalise_columns(df)


def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Map common column name variants to Filename, Brand, Indication."""
    col_map = {
        "filename": "Filename", "file_name": "Filename", "file": "Filename",
        "pdf": "Filename", "pdf_name": "Filename",
        "brand": "Brand", "drug": "Brand", "brand_name": "Brand",
        "drug_name": "Brand", "product": "Brand", "medication": "Brand",
        "indication": "Indication", "disease": "Indication",
        "condition": "Indication",
    }
    df.columns = [col_map.get(c.lower().strip(), c) for c in df.columns]
    
    missing = [c for c in ["Filename", "Brand"] if c not in df.columns]
    if missing:
        raise ValueError(f"Required columns not found: {missing}\n"
                         f"Available: {list(df.columns)}")
    
    # Indication normalisation (variants → "Plaque Psoriasis")
    if "Indication" in df.columns:
        canonical_pso = {"pso", "psoriasis", "plaque psoriasis",
                         "moderate-to-severe plaque psoriasis",
                         "moderate to severe plaque psoriasis"}
        df["Indication"] = df["Indication"].astype(str).apply(
            lambda v: "Plaque Psoriasis" if v.strip().lower() in canonical_pso else v
        )
        print(f"INFO: Indication column present, normalised to canonical strings.")
    else:
        print(f"INFO: No Indication column — hardcoding 'Plaque Psoriasis'.")
    
    return df


# ─────────────────────────────────────────────────────────────────
# STARTUP DIAGNOSTICS (Resolved §11.11 + §11.12)
# ─────────────────────────────────────────────────────────────────

def load_reference_tab_transposed(xlsx_path: Path) -> dict:
    """
    Resolved §S2 (Codex 29 May): The Reference tab is TRANSPOSED — its columns
    are `Sno., Params, Values` and each row is one parameter, not one
    (Filename, Brand). This loader reads the transposed sheet and produces a
    single dict mapping CSV-column-name → value, suitable for use as one
    ground-truth row by the smoke test.
    
    Returns: {
        "Filename":   "<filename if recorded as a Params row>",
        "Brand":      "<brand>",
        "Age":        ">=N",
        "Step Therapy Requirements Documented in Policy": "...",
        ...
    }
    
    If the Params column uses informal labels (e.g. "Steps - Branded"), we map
    them onto canonical CSV column names. Unrecognised params are kept under
    their literal label and logged for review.
    """
    try:
        df = pd.read_excel(xlsx_path, sheet_name="Reference")
    except Exception as e:
        raise RuntimeError(f"Cannot load Reference tab: {e}") from e
    
    # Locate Params and Values columns flexibly
    cols_lc = {c.lower().strip(): c for c in df.columns}
    params_col = cols_lc.get("params") or cols_lc.get("parameter")
    values_col = cols_lc.get("values") or cols_lc.get("value")
    if not params_col or not values_col:
        raise RuntimeError(
            f"Reference tab columns not as expected. Got: {list(df.columns)}.\n"
            f"Expected a 'Params' column and a 'Values' column."
        )
    
    # Build informal-label → canonical-CSV-name mapping
    label_to_csv = {
        "filename":                                "Filename",
        "file name":                               "Filename",
        "brand":                                   "Brand",
        "drug":                                    "Brand",
        "age":                                     "Age",
        "step therapy":                            "Step Therapy Requirements Documented in Policy",
        "step therapy requirements":               "Step Therapy Requirements Documented in Policy",
        "steps - branded":                         "Number of Steps through Brands",
        "number of steps through brands":          "Number of Steps through Brands",
        "steps - generic":                         "Number of Steps through Generic",
        "number of steps through generic":         "Number of Steps through Generic",
        "step through phototherapy":               "Step through-Phototherapy",
        "step through-phototherapy":               "Step through-Phototherapy",
        "phototherapy":                            "Step through-Phototherapy",
        "tb test":                                 "TB Test required",
        "tb test required":                        "TB Test required",
        "quantity limits":                         "Quantity Limits",
        "specialist":                              "Specialist Types",
        "specialist types":                        "Specialist Types",
        "initial authorization duration":          "Initial Authorization Duration(in-months)",
        "initial auth duration":                   "Initial Authorization Duration(in-months)",
        "initial auth duration (in months)":       "Initial Authorization Duration(in-months)",
        "reauth duration":                         "Reauthorization Duration(in-months)",
        "reauthorization duration":                "Reauthorization Duration(in-months)",
        "reauth required":                         "Reauthorization Required",
        "reauthorization required":                "Reauthorization Required",
        "reauth requirements":                     "Reauthorization Requirements Documented in Policy",
        "reauthorization requirements":            "Reauthorization Requirements Documented in Policy",
    }
    
    row: dict = {}
    unrecognised = []
    for _, r in df.iterrows():
        label_raw = str(r[params_col] or "").strip()
        if not label_raw or label_raw.lower() in ("nan", "none"):
            continue
        value = r[values_col]
        if pd.isna(value):
            continue
        csv_col = label_to_csv.get(label_raw.lower())
        if csv_col:
            row[csv_col] = str(value).strip()
        else:
            unrecognised.append(label_raw)
            row[label_raw] = str(value).strip()
    
    if unrecognised:
        log_debug({"event": "reference_tab_unrecognised_params",
                   "labels": unrecognised})
        print(f"INFO: Reference tab has {len(unrecognised)} param labels we "
              f"don't auto-map: {unrecognised[:5]}{'...' if len(unrecognised) > 5 else ''}")
    
    print(f"INFO: Reference tab parsed. {len(row)} fields extracted "
          f"(filename={row.get('Filename')!r}, brand={row.get('Brand')!r}).")
    return row


def inspect_additional_data_tab(xlsx_path: Path,
                                submissions_df: pd.DataFrame) -> pd.DataFrame | None:
    """
    Load the Additional Extracted Data tab. Verify its content. Compute
    overlap with our 79 Submissions rows. Returns the overlap DataFrame if
    usable as silver-standard validation, else None.
    """
    try:
        df = pd.read_excel(xlsx_path, sheet_name="Additional Extracted Data")
    except Exception as e:
        print(f"INFO: Additional Extracted Data tab not loadable: {e}. "
              f"Dropping from validation plan.")
        return None
    
    print(f"\n=== Additional Extracted Data tab inspection ===")
    print(f"  Rows: {len(df)}")
    print(f"  Columns: {list(df.columns)}")
    
    # Try to locate a Filename column (variant-aware)
    filename_col = next((c for c in df.columns
                         if c.lower().strip() in ("filename", "file_name", "file")),
                        None)
    if not filename_col:
        print(f"  No Filename column → not usable for validation overlap.")
        return None
    
    print(f"  Unique filenames: {df[filename_col].nunique()}")
    overlap = set(df[filename_col].astype(str)) & set(submissions_df["Filename"].astype(str))
    print(f"  Overlap with Submissions: {len(overlap)} filenames")
    
    if not overlap:
        print(f"  Zero overlap → not usable. Dropping from validation plan.")
        return None
    
    overlap_df = df[df[filename_col].astype(str).isin(overlap)].copy()
    print(f"  Using {len(overlap_df)} overlapping rows as silver-standard.\n")
    return overlap_df


def determinism_test() -> str:
    """
    Run identical prompt 3× at temperature 0. Hash responses. Report.
    
    Returns: "STABLE" | "MINOR_DRIFT" | "SIGNIFICANT_DRIFT"
    """
    import hashlib
    prompt = ('Extract the minimum age from this sentence: '
              '"Member must be 18 years or older". '
              'Return ONLY JSON: {"age": ">=N"}')
    
    outputs = []
    for _ in range(DETERMINISM_TEST_RUNS):
        try:
            outputs.append(call_llm(prompt))
        except Exception as e:
            log_debug({"event": "determinism_test_error", "error": str(e)})
            return "UNKNOWN"
    
    hashes = {hashlib.md5(o.encode()).hexdigest() for o in outputs}
    if len(hashes) == 1:
        result = "STABLE"
    elif len(hashes) == 2:
        result = "MINOR_DRIFT"
    else:
        result = "SIGNIFICANT_DRIFT"
    
    log_debug({"event": "determinism_test", "result": result,
               "n_unique": len(hashes), "outputs": outputs})
    print(f"INFO: Determinism test = {result} ({len(hashes)} unique outputs over "
          f"{DETERMINISM_TEST_RUNS} runs)")
    return result


# ─────────────────────────────────────────────────────────────────
# SMOKE TEST (Resolved §11.4)
# ─────────────────────────────────────────────────────────────────

def load_validation_set(xlsx_path: Path,
                        manual_labels_path: Path = Path("manual_labels.csv")) -> pd.DataFrame:
    """
    Combine validation sources into one DataFrame keyed by CSV column names.
    
    Sources (Resolved §11.4 + Codex 29 May):
      1. Reference tab — TRANSPOSED layout (one row per param). Produces a
         single ground-truth row when filename + brand are recorded as Params.
         If those are missing (Reference is a worked-example with no filename
         anchor), the row is still loaded but the smoke test will skip it
         because it cannot run the pipeline without a (Filename, Brand) key.
      2. Manual labels CSV — 5 rows hand-labelled D1-D2, exact CSV column
         schema per §11.14.
      3. Additional Extracted Data tab — only if `inspect_additional_data_tab`
         confirmed usable overlap (returned non-None). NOT loaded here; that
         function returns the overlap DataFrame and main() merges it.
    """
    parts: list[pd.DataFrame] = []
    
    # Reference tab — transposed parser
    try:
        ref_row = load_reference_tab_transposed(xlsx_path)
        if ref_row.get("Filename") and ref_row.get("Brand"):
            ref_df = pd.DataFrame([ref_row])
            ref_df["_source"] = "reference"
            parts.append(ref_df)
        else:
            print(f"INFO: Reference tab parsed but has no Filename/Brand anchor. "
                  f"Skipping from validation set (use it as comparison only).")
    except Exception as e:
        print(f"WARN: Reference tab not parseable: {e}")
    
    # Manual labels
    if manual_labels_path.exists():
        manual = pd.read_csv(manual_labels_path)
        manual["_source"] = "manual"
        parts.append(manual)
    else:
        print(f"WARN: {manual_labels_path} not found — manual labels missing. "
              f"Smoke gate will run with whatever else loaded.")
    
    if not parts:
        raise RuntimeError(
            "No validation sources loaded. Either add manual_labels.csv or "
            "make Reference tab provide a Filename+Brand row. Use --skip-smoke-gate "
            "to bypass."
        )
    
    combined = pd.concat(parts, ignore_index=True)
    
    # Resolved §S7: apply same Indication normalisation as Submissions
    if "Indication" in combined.columns:
        canonical_pso = {"pso", "psoriasis", "plaque psoriasis",
                         "moderate-to-severe plaque psoriasis",
                         "moderate to severe plaque psoriasis"}
        combined["Indication"] = combined["Indication"].astype(str).apply(
            lambda v: "Plaque Psoriasis" if v.strip().lower() in canonical_pso else v
        )
    
    print(f"INFO: validation set assembled — {len(combined)} rows from "
          f"{combined['_source'].value_counts().to_dict()}.")
    return combined


def per_param_match(pred: str, truth: str, param_name: str) -> bool:
    """
    Per-param tolerance rules.
    - Exact: age, durations, counts, Yes/No flags, reauth_required
    - Token-recall ≥ 0.70: free-text fields
    - Set equality: specialist_types
    """
    if pred is None: pred = ""
    if truth is None: truth = ""
    pred, truth = str(pred).strip(), str(truth).strip()
    
    text_fields = {"combined_step_text", "reauth_requirements_text",
                   "quantity_limits_text"}
    if param_name in text_fields:
        return token_recall(pred, truth) >= 0.70
    if param_name == "specialist_types":
        return ({s.strip().lower() for s in pred.split(",") if s.strip()}
                == {s.strip().lower() for s in truth.split(",") if s.strip()})
    return pred.lower() == truth.lower()


def run_smoke_test(xlsx_path: Path) -> bool:
    """
    Build validation set, run the pipeline on each row, score per param.
    Returns True if ≥85% per-cell agreement. False halts the batch.
    """
    validation_df = load_validation_set(xlsx_path)
    print(f"\n=== Smoke test on {len(validation_df)} validation rows ===")
    
    correct = 0
    total = 0
    by_param = {}
    
    pdf_cache: dict = {}
    outline_cache: dict = {}
    section_cache: dict = {}
    
    for _, row in validation_df.iterrows():
        filename = row.get("Filename") or row.get("File")
        drug = row.get("Brand") or row.get("Drug")
        if not filename or not drug:
            continue
        indication = row.get("Indication", "Plaque Psoriasis")
        pred = process_single_row(filename, drug, indication,
                                  pdf_cache, outline_cache, section_cache)
        for param_key, csv_col in COLUMN_MAP.items():
            if param_key in ("Filename", "Brand", "access_score"):
                continue
            truth = row.get(csv_col)
            if pd.isna(truth) or truth == "":
                continue
            total += 1
            if per_param_match(pred.get(param_key), truth, param_key):
                correct += 1
                by_param[param_key] = by_param.get(param_key, [0, 0])
                by_param[param_key][0] += 1
                by_param[param_key][1] += 1
            else:
                by_param[param_key] = by_param.get(param_key, [0, 0])
                by_param[param_key][1] += 1
    
    rate = correct / total if total else 0
    print(f"\nOverall: {correct}/{total} = {rate:.1%}")
    print(f"Per-param:")
    for param, (c, t) in sorted(by_param.items()):
        print(f"  {param:>40}: {c}/{t}")
    
    threshold = 0.85
    passed = rate >= threshold
    print(f"\nSmoke test: {'PASS' if passed else 'FAIL'} (threshold {threshold:.0%})")
    return passed


# ─────────────────────────────────────────────────────────────────
# MAIN (with CLI flags)
# ─────────────────────────────────────────────────────────────────

def main():
    import argparse
    import sys
    global BRANDED_DRUGS, GENERIC_DRUGS, PDF_DIR, XLSX_PATH, OUTPUT_PATH
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true",
                        help="Run smoke test only, exit. Halts on failure.")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Skip pre-flight check (NOT recommended).")
    parser.add_argument("--skip-smoke-gate", action="store_true",
                        help="Skip the pre-batch smoke gate. Use only when "
                        "smoke set is empty or known-broken.")
    parser.add_argument("--force-rerun", nargs="*", default=[],
                        help="Force rerun for specific 'filename:brand' keys.")
    parser.add_argument("--pdf-dir", type=Path, default=None,
                        help="Override PDF_DIR (env: PIPELINE_PDF_DIR).")
    parser.add_argument("--xlsx-path", type=Path, default=None,
                        help="Override XLSX_PATH (env: PIPELINE_XLSX_PATH).")
    parser.add_argument("--output-path", type=Path, default=None,
                        help="Override OUTPUT_PATH (env: PIPELINE_OUTPUT_PATH).")
    args = parser.parse_args()
    
    # CLI overrides take precedence over env vars take precedence over defaults
    if args.pdf_dir:     PDF_DIR = args.pdf_dir
    if args.xlsx_path:   XLSX_PATH = args.xlsx_path
    if args.output_path: OUTPUT_PATH = args.output_path
    
    # Resolved §M6: fail-fast on missing GROQ_API_KEY
    if not GROQ_API_KEY:
        print("ERROR: GROQ_API_KEY environment variable is required.")
        print("       export GROQ_API_KEY=<your_groq_api_key>")
        sys.exit(1)
    
    # Resolved §M5: confirm paths exist before we do anything else
    if not PDF_DIR.exists():
        print(f"ERROR: PDF_DIR does not exist: {PDF_DIR}")
        sys.exit(1)
    if not XLSX_PATH.exists():
        print(f"ERROR: XLSX_PATH does not exist: {XLSX_PATH}")
        sys.exit(1)
    
    # Startup
    print("Loading drug classifications from XLSX...")
    BRANDED_DRUGS, GENERIC_DRUGS = load_drug_classifications(XLSX_PATH)
    print(f"  Branded: {len(BRANDED_DRUGS)}, Generic: {len(GENERIC_DRUGS)}")
    
    print("Loading submissions...")
    submissions_df = load_submissions(XLSX_PATH)
    print(f"  {len(submissions_df)} rows to process")
    
    # Reference-tab parse (transposed — see load_reference_tab_transposed).
    # We parse it once here for inspection; load_validation_set re-parses it
    # when building the smoke-test set. Cheap on a one-tab read.
    try:
        ref_row = load_reference_tab_transposed(XLSX_PATH)
        log_debug({"event": "reference_tab_parsed",
                   "n_fields": len(ref_row),
                   "has_filename": bool(ref_row.get("Filename")),
                   "has_brand": bool(ref_row.get("Brand"))})
    except Exception as e:
        print(f"WARN: Reference tab parse failed at startup: {e}")
    
    # Diagnostics
    determinism_test()
    inspect_additional_data_tab(XLSX_PATH, submissions_df)
    
    # Smoke test mode (run-only, exit with status)
    if args.smoke_test:
        passed = run_smoke_test(XLSX_PATH)
        sys.exit(0 if passed else 1)
    
    # Pre-flight (cheap-only, populates pdf_cache for batch)
    pdf_cache: dict = {}
    if not args.skip_preflight:
        preflight_check(submissions_df, pdf_cache)
    
    # Force-rerun handling
    if args.force_rerun:
        rerun_keys = {tuple(k.split(":", 1)) for k in args.force_rerun}
        if CHECKPOINT_PATH.exists():
            with open(CHECKPOINT_PATH) as f:
                ckpt = json.load(f)
            ckpt["results"] = [
                r for r in ckpt.get("results", [])
                if (r.get("Filename"), r.get("Brand")) not in rerun_keys
            ]
            _atomic_checkpoint_write(ckpt["results"])
            print(f"Cleared {len(rerun_keys)} rows from checkpoint for rerun.")
    
    # Smoke gate before full batch (Resolved §11.4)
    if not args.skip_smoke_gate:
        print("\n=== Pre-batch smoke gate ===")
        if not run_smoke_test(XLSX_PATH):
            print("HALT: smoke test failed. Not running batch — fix first.")
            sys.exit(1)
    
    # Full batch (pdf_cache shared with preflight)
    results = process_all_rows(submissions_df, pdf_cache=pdf_cache)
    save_csv(results)
    
    # End-of-batch diagnostics
    print_end_of_batch_summary(results)


if __name__ == "__main__":
    main()
```

---

## Data Flow Summary

```
STARTUP
  load_drug_classifications()         → BRANDED_DRUGS, GENERIC_DRUGS
  load_submissions()                  → 79 rows of (Filename, Brand[, Indication])
  determinism_test()                  → STABLE | MINOR_DRIFT | SIGNIFICANT_DRIFT
  inspect_additional_data_tab()       → silver-standard overlap (or None)
  preflight_check()                   → HALT on missing PDFs; warn on zero-match
  run_smoke_test() [gate]             → HALT batch if < 85% on validation set
       ↓
PER ROW (with 3-tier caching)
  Cache 1 — ingest_pdf()              [per filename]   → full_text + pages + doc
  Cache 2 — extract_outline()         [per filename]   → headings list
  Cache 3 — assemble_context():       [per (file, drug)]
              map_outline_to_sections()    → 1 LLM call returning heading anchors
              slice_by_anchors()           → deterministic slice
              recursive_zoom() if needed   → 1 LLM call only when section > 60K chars
       ↓
  Drug-not-found guard                → CRITICAL_DRUG_NOT_FOUND row + warning
       ↓
  Pass 1: extract_simple_params()     → 7 params (1 LLM call, ~10K tokens)
  Pass 2: extract_step_therapy_text() → 1 verbatim blob (1 LLM call, ~10K tokens)
  Pass 3: extract_step_counts()       → 3 counts + CoT (1 LLM call, ~3K tokens)
            ║
            ╠═ rule_based_step_count()  → deterministic Plan B (LOCAL)
            ╚═ reconcile_step_counts()  → flags MINOR/MAJOR/RULE_FALLBACK
       ↓
  validate_all():
       rule_age_format, rule_auth_duration, etc.    (mutate)
       critical_verbatim_check (token-recall)        → rerun on fail (max 2)
       critical_step_extraction_check                → rerun on fail
       semantic_contradiction_checks                 → advisory flags
       ↓
  access_score = "" (Phase 2, deferred)
  format_row() → result.csv
       ↓
  Checkpoint every 10 rows (atomic write, keyed by (Filename, Brand))
       ↓
END-OF-BATCH
  print_end_of_batch_summary()         → counts of warnings + rows needing review
```

**Per-row API budget:** 1 (section mapping, amortised across drugs sharing a PDF) + 1 (Pass 1) + 1 (Pass 2) + 1 (Pass 3) = **~4 LLM calls per row**, ~25–30K tokens total. With `TokenBudget` throttle and the 3-tier cache, predicted batch wall-clock is ~3 hours.

---

## Dependencies

```
groq               # Llama 3.3 70B + Llama 4 Scout vision via one client
pymupdf            # text extraction + page rendering for vision fallback
pandas             # XLSX/CSV I/O
openpyxl           # XLSX reader backend for pandas
# stdlib: pathlib, json, re, base64, io, time
```

No `zipfile` (real PDFs only). Groq is the sole provider — one SDK covers text + vision.
