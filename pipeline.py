"""
pipeline.py — PA Policy Extraction Pipeline (PsO Hackathon)

Single-file extraction pipeline that reads payer Prior Authorization PDFs
from Sample_PsO_ADS_Track/, fills the Submissions tab of
pre-context/PA_Business_Rules.xlsx, and writes result.csv.

Build sequence per context/06_DEVELOPER_PLAN.md §13 (function-level order).
Spec per context/03_PIPELINE_ARCHITECTURE.md.

Phase 0 — Bootstrap (constants, env loader, log_debug, argparse stub).
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from statistics import median, quantiles
from typing import Optional

import pandas as pd
import fitz  # pymupdf
from groq import Groq


# ─────────────────────────────────────────────────────────────────
# .ENV LOADER (must run BEFORE Block 1 reads env vars)
# ─────────────────────────────────────────────────────────────────

def _load_env_file(path: Path = Path(".env")) -> None:
    """
    Load .env at project root. Keys are uppercased on read so a file with
    `groq_api_key=xxx` produces `GROQ_API_KEY=xxx` in the process env.
    External env vars take precedence (setdefault, not assignment).
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().upper()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_env_file()


# ─────────────────────────────────────────────────────────────────
# BLOCK 1 — CONFIG & CONSTANTS
# ─────────────────────────────────────────────────────────────────

# ── API Keys (Groq, multi-key TPD pooling) ───────────────────────
# Loads GROQ_API_KEY (base) plus GROQ_API_KEY_1, _2, ... up to _9.
# Each key is an independent Groq organization with its own daily TPD
# per model. When a (key, model) combo hits TPD we rotate to the next
# model in the fallback chain (same key). When all models on a key are
# exhausted we rotate to the next key, restarting from the preferred
# model. Lets us pool 5× TPD across 5 keys without changing routing.
def _load_groq_keys() -> list:
    """Return all Groq API keys from env in priority order, deduplicated."""
    keys: list = []
    seen: set = set()
    base = os.environ.get("GROQ_API_KEY", "").strip()
    if base and base not in seen:
        keys.append(base)
        seen.add(base)
    for i in range(1, 10):
        k = os.environ.get(f"GROQ_API_KEY_{i}", "").strip()
        if k and k not in seen:
            keys.append(k)
            seen.add(k)
    return keys


GROQ_API_KEYS: list = _load_groq_keys()
GROQ_API_KEY = GROQ_API_KEYS[0] if GROQ_API_KEYS else ""  # backward compat
# Presence is enforced in main() — fail fast with a clear message rather
# than letting Groq return a confusing 401 later.

# ── Model Names ───────────────────────────────────────────────────
# Primary text model + automatic-fallback chain. When the current model hits
# its daily TPD limit, _call_groq_text advances to the next entry. The chain
# is consumed in order; once a model is exhausted in this process, we don't
# go back to it (no point — quota resets at midnight UTC anyway).
#
#   llama-3.3-70b-versatile  — 70B, clean JSON output, primary
#   qwen/qwen3-32b           — 32B, partner-requested jugad fallback
#                              NOTE: Qwen3 is a "thinking model" that emits
#                              <think>…</think> blocks before its answer.
#                              _strip_thinking_blocks() handles this.
#   openai/gpt-oss-120b      — 120B, clean JSON, last resort
GROQ_TEXT_MODEL = "llama-3.3-70b-versatile"  # kept for backward compatibility
GROQ_TEXT_MODEL_FALLBACKS = [  # default chain (used by determinism_test + tiny calls)
    "llama-3.3-70b-versatile",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "qwen/qwen3-32b",
    "openai/gpt-oss-120b",
]
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# Per-call-site model preference (Scout has 5× the daily TPD of llama-3.3-70b).
# Pass 1 + Pass 2: verbatim-extraction tasks, heavy token cost — Scout's
#   500K TPD lets us run far more per day at acceptable extraction quality.
# Pass 3 + outline_map + recursive_zoom: judgment/CoT tasks where 70B's
#   stronger reasoning matters most. Llama-3.3-70b primary.
GROQ_TEXT_MODELS_FOR_PASS_1_2 = [
    "meta-llama/llama-4-scout-17b-16e-instruct",   # 500K TPD, 30K TPM
    "llama-3.3-70b-versatile",                     # 100K TPD, 12K TPM
    "qwen/qwen3-32b",                              # 500K TPD, 6K TPM (TPM too small for many calls)
    "openai/gpt-oss-120b",                         # 200K TPD, 8K TPM
]
GROQ_TEXT_MODELS_FOR_REASONING = [
    "llama-3.3-70b-versatile",                     # primary — quality
    "meta-llama/llama-4-scout-17b-16e-instruct",   # quality fallback
    "qwen/qwen3-32b",
    "openai/gpt-oss-120b",
]

# Approx per-minute TPM ceilings per model (free tier, observed from 413
# error responses). Used by _classify_tpm_error to distinguish "request
# itself is bigger than the model's TPM" (skip to next model) from
# "60-second window is full" (wait 120s and retry).
GROQ_MODEL_TPM_CAP = {
    "llama-3.3-70b-versatile":                    12_000,
    "meta-llama/llama-4-scout-17b-16e-instruct":  30_000,
    "qwen/qwen3-32b":                              6_000,
    "openai/gpt-oss-120b":                         8_000,
    "openai/gpt-oss-20b":                          8_000,
    "llama-3.1-8b-instant":                        6_000,
}

# Seconds to wait when a TPM-window-full (not request-too-big) hit is
# detected. Free-tier TPM window is 60s; we wait 120s for safety margin.
TPM_WINDOW_WAIT_SECONDS = 120

# ── File Paths (env-overridable, CLI-overridable) ─────────────────
PDF_DIR = Path(os.environ.get("PIPELINE_PDF_DIR", "Sample_PsO_ADS_Track/"))
XLSX_PATH = Path(os.environ.get("PIPELINE_XLSX_PATH",
                                "pre-context/PA_Business_Rules.xlsx"))
OUTPUT_PATH = Path(os.environ.get("PIPELINE_OUTPUT_PATH", "result.csv"))
CHECKPOINT_PATH = Path(os.environ.get("PIPELINE_CHECKPOINT_PATH",
                                      "checkpoint.json"))
DEBUG_LOG_PATH = Path(os.environ.get("PIPELINE_DEBUG_LOG_PATH",
                                     "debug_log.jsonl"))

# ── Processing Constants ──────────────────────────────────────────
MAX_RETRIES = 3
MAX_PIPELINE_RERUNS = 2
BACKOFF_BASE = 2
CHECKPOINT_INTERVAL = 10
MAX_TOKENS_TEXT = 4096    # default — only used if a caller doesn't override
# Vision OCR outputs are page-text dumps, typically 200-2000 chars.
# 2048 is plenty; lower keeps each vision request under TPM_LIMIT.
MAX_TOKENS_VISION = 2048

# Per-call max_tokens ceilings (Fix A).
# Set per call site to the real maximum output we actually emit, NOT some
# huge global default. Groq counts max_tokens against the per-minute TPM
# window even if the model produces less, so a 4096 reservation we never
# use costs us 4K of TPM budget on every call. Measured ceilings from
# observed completions during testing:
MAX_TOKENS_OUTLINE_MAP    = 512    # JSON with 4 anchor objects, ~200 tok seen
MAX_TOKENS_RECURSIVE_ZOOM = 1024   # JSON list of headings, ~500 tok seen
MAX_TOKENS_PASS_1         = 1024   # 7-field JSON, ~500 tok seen
MAX_TOKENS_PASS_2         = 2048   # verbatim step text, can be longer
MAX_TOKENS_PASS_3         = 1500   # CoT JSON with reasoning, ~800 tok seen

# Context truncation cap for Pass 1 / Pass 2.
#
# Sized for Scout (the primary Pass 1/2 model), which has TPM=30_000 and a
# 131K-token context window. We previously used 30_000 chars (~7.5K tokens)
# — sized for llama-70B's 12K TPM ceiling. With Scout-first routing, that
# left Scout's headroom unused AND silently dropped policy text far from
# the drug-name cluster (e.g. the "THREE preferred products" universal
# block we had to add the deterministic anchor scanner to recover).
#
# New sizing: 70_000 chars context (~17.5K tok) + max_tokens 2048 +
# scaffolding ~1.5K ≈ ~21K total tokens per request. Fits Scout's 30K TPM
# with comfortable margin (~85% utilization). On llama-70B fallback, this
# WILL overflow the per-request 12K — the fallback path will log
# request_too_big and try Scout on the next key or a different model. We
# accept that trade-off: Scout-first routing is now the hot path.
MAX_CONTEXT_CHARS_FOR_PASS = 70_000
TEMPERATURE = 0.0

# Vision fallback trigger: per-page text density threshold.
MIN_CHARS_PER_PAGE = 100

# Section-assembly budgets (chars; ~4 chars/token).
MAX_SECTION_CHARS = 60_000     # ~15K tokens — triggers Stage C recursive zoom
MAX_CONTEXT_CHARS = 100_000    # ~25K tokens — hard ceiling, truncate largest

# Token-budget throttle (conservative under documented 6,000 TPM).
TPM_LIMIT = 5_500

# Hard ceiling per single API call. Below Groq's free-tier daily TPD (100K)
# AND below typical per-request 413 thresholds. Prevents the kind of
# 120,633-token request that triggered our organization_restricted lockout
# on 2026-05-29 (an unpruned 8,246-entry outline blob sent to
# map_outline_to_sections). Realistic max per call with current pruning /
# context caps is ~30K tokens — 90K leaves comfortable headroom.
MAX_REQUEST_TOKENS = 90_000

# Determinism: hash N identical prompts at startup to detect serving drift.
DETERMINISM_TEST_RUNS = 3

# ── Drug classification — hardcoded for submission portability ────
#
# Mirror of the "PsO Brands- For Ground Truth" sheet from the dev workbook.
# Hardcoded so the pipeline can run against submission environments that
# only ship the Submissions sheet + a PDF folder. load_drug_classifications()
# uses this list as the primary source; if the XLSX has an updated sheet
# during dev, that takes precedence.
#
# Last synced from PA_Business_Rules.xlsx on 2026-05-31 (35 entries).
PSO_MARKET_BASKET = (
    "Acitretin", "Amjevita", "Avsola", "Bimzelx", "Cimzia", "Cosentyx",
    "Cyclosporine", "Cyltezo", "Enbrel", "Humira", "Hyrimoz", "Idacio",
    "Ilumya", "Inflectra", "Hulio", "Methotrexate", "Otezla", "Otulfi",
    "Psychiva / Quallent", "Remicade", "Renflexis", "Selarsdi", "Siliq",
    "Skyrizi", "Sotyktu", "Stelara", "Steqeyma", "Taltz", "Tremfya",
    "Vtama", "Wezlana", "Yesintek", "Yuflyma", "Yusimry", "Zoryve",
)

# Generic conventionals that appear in the PsO market basket tab.
KNOWN_GENERICS_IN_MARKET_BASKET = {
    "acitretin", "cyclosporine", "methotrexate", "vtama", "zoryve",
}

# INN → brand mapping for policy text matching. Used by get_drug_aliases
# to scan for INN mentions when only the brand is in BRANDED_DRUGS.
INN_TO_BRAND = {
    "guselkumab":      "tremfya",
    "ustekinumab":     "stelara",
    "adalimumab":      "humira",
    "etanercept":      "enbrel",
    "infliximab":      "remicade",
    "certolizumab":    "cimzia",
    "secukinumab":     "cosentyx",
    "ixekizumab":      "taltz",
    "risankizumab":    "skyrizi",
    "brodalumab":      "siliq",
    "tildrakizumab":   "ilumya",
    "deucravacitinib": "sotyktu",
    "apremilast":      "otezla",
    "bimekizumab":     "bimzelx",
    # JAK inhibitors / targeted synthetics — branded steps per business rules.
    "tofacitinib":     "xeljanz",
    "upadacitinib":    "rinvoq",
    "baricitinib":     "olumiant",
}

# Targeted synthetics that are branded steps but NOT in PsO market basket tab.
TARGETED_SYNTHETICS_NOT_IN_BASKET = {
    "xeljanz", "tofacitinib",
    "rinvoq", "upadacitinib",
    "olumiant", "baricitinib",
}

# Biosimilar brand → primary INN. Used by get_drug_aliases to recognize
# INN-suffix forms (e.g. policies written as "ustekinumab-kfce" should
# match a YESINTEK target). Keys are biosimilar brand names; values are
# the primary INN that, combined with biosimilar suffixes like "-kfce",
# "-aauz", "-atto", etc., cover the alternate forms policies may use.
BIOSIMILAR_TO_PRIMARY_INN = {
    # adalimumab biosimilars
    "amjevita":  "adalimumab",
    "cyltezo":   "adalimumab",
    "hyrimoz":   "adalimumab",
    "hadlima":   "adalimumab",
    "idacio":    "adalimumab",
    "hulio":     "adalimumab",
    "yuflyma":   "adalimumab",
    "yusimry":   "adalimumab",
    "abrilada":  "adalimumab",
    # infliximab biosimilars
    "avsola":    "infliximab",
    "inflectra": "infliximab",
    "renflexis": "infliximab",
    "ixifi":     "infliximab",
    # ustekinumab biosimilars
    "wezlana":   "ustekinumab",
    "selarsdi":  "ustekinumab",
    "yesintek":  "ustekinumab",
    "otulfi":    "ustekinumab",
    "steqeyma":  "ustekinumab",
    "imuldosa":  "ustekinumab",
    "pyzchiva":  "ustekinumab",
}

PHOTOTHERAPY_TERMS = {
    "phototherapy", "puva", "uvb", "uva", "narrowband uvb",
    "psoralen", "light therapy", "photochemotherapy",
}

# Populated at startup by load_drug_classifications() in main().
BRANDED_DRUGS: set = set()
GENERIC_DRUGS: set = set()


# ─────────────────────────────────────────────────────────────────
# BLOCK 9 (early) — log_debug
# Defined here because virtually every function below logs. Append-only
# JSONL avoids the O(N²) read-then-write pattern of the earlier draft.
# ─────────────────────────────────────────────────────────────────

def log_debug(entry: dict) -> None:
    """Append one JSON line to DEBUG_LOG_PATH. Always takes a dict."""
    with open(DEBUG_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ─────────────────────────────────────────────────────────────────
# BLOCK 9 (Phase 1) — Drug classification + submissions loaders
# ─────────────────────────────────────────────────────────────────

def load_drug_classifications(xlsx_path: Optional[Path] = None) -> tuple[set, set]:
    """
    Load branded vs generic drug classification.

    Primary source: hardcoded PSO_MARKET_BASKET (35 entries, mirrors the
    "PsO Brands- For Ground Truth" sheet). Submission environments that
    only ship Submissions + PDFs work without any external sheet.

    Optional override: if xlsx_path is provided AND has the "PsO Brands-
    For Ground Truth" sheet, that sheet's contents REPLACE the hardcoded
    list. Lets dev iterate on the basket without code changes.

    In both cases: KNOWN_GENERICS_IN_MARKET_BASKET split into the generic
    set, the rest into branded. INN aliases + targeted-synthetic JAK
    inhibitors added explicitly.

    Returns (branded_set, generic_set), both lowercase.
    """
    # Start with the hardcoded list (always available, never raises).
    all_drugs = {d.lower().strip() for d in PSO_MARKET_BASKET}
    source = "hardcoded"

    # If an XLSX with the sheet exists, override.
    if xlsx_path is not None and xlsx_path.exists() and xlsx_path.suffix.lower() in (".xlsx", ".xls"):
        try:
            df = pd.read_excel(
                xlsx_path,
                sheet_name="PsO Brands- For Ground Truth",
                header=0,
            )
            xlsx_drugs = {
                str(v).lower().strip()
                for v in df.iloc[:, 0]
                if str(v).lower().strip() not in ("nan", "", "none")
            }
            if xlsx_drugs:
                all_drugs = xlsx_drugs
                source = "xlsx_override"
        except Exception as e:
            log_debug({
                "event": "drug_classification_xlsx_sheet_missing",
                "note": "falling back to hardcoded PSO_MARKET_BASKET",
                "error": str(e),
            })

    log_debug({
        "event": "drug_classification_loaded",
        "source": source,
        "n_drugs": len(all_drugs),
    })

    generic_set = {d for d in all_drugs if d in KNOWN_GENERICS_IN_MARKET_BASKET}
    branded_set = all_drugs - generic_set

    # Add INNs to branded set so we can match policy text that names the INN
    # instead of the brand (e.g. "ustekinumab" instead of "STELARA").
    for inn, brand in INN_TO_BRAND.items():
        if brand.lower() in branded_set:
            branded_set.add(inn.lower())

    # Targeted synthetics that aren't in the market basket tab but count as
    # branded per business rules (JAK inhibitors etc).
    branded_set.update(TARGETED_SYNTHETICS_NOT_IN_BASKET)

    return branded_set, generic_set


def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Map common column-name variants in the Submissions tab to canonical
    {Filename, Brand, Indication}. Indication values are normalised to
    "Plaque Psoriasis" for PsO variants. Raises ValueError if Filename
    or Brand cannot be resolved.
    """
    col_map = {
        "filename":   "Filename",
        "file_name":  "Filename",
        "file":       "Filename",
        "pdf":        "Filename",
        "pdf_name":   "Filename",
        "brand":      "Brand",
        "drug":       "Brand",
        "brand_name": "Brand",
        "drug_name":  "Brand",
        "product":    "Brand",
        "medication": "Brand",
        "indication": "Indication",
        "disease":    "Indication",
        "condition":  "Indication",
    }
    df.columns = [col_map.get(c.lower().strip(), c) for c in df.columns]

    missing = [c for c in ("Filename", "Brand") if c not in df.columns]
    if missing:
        raise ValueError(
            f"Required columns not found: {missing}\n"
            f"Available: {list(df.columns)}"
        )

    if "Indication" in df.columns:
        canonical_pso = {
            "pso", "psoriasis", "plaque psoriasis",
            "moderate-to-severe plaque psoriasis",
            "moderate to severe plaque psoriasis",
        }
        df["Indication"] = df["Indication"].astype(str).apply(
            lambda v: "Plaque Psoriasis"
            if v.strip().lower() in canonical_pso else v
        )
        print("INFO: Indication column present, normalised to canonical strings.")
    else:
        print("INFO: No Indication column — will default to 'Plaque Psoriasis'.")

    return df


def load_submissions(path: Path) -> pd.DataFrame:
    """
    Load the Submissions tab (XLSX) or a CSV equivalent. Always returns a
    DataFrame with normalised Filename/Brand/(Indication) columns.
    """
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
    else:
        try:
            df = pd.read_excel(path, sheet_name="Submissions")
        except Exception:
            df = pd.read_excel(path, sheet_name=0)
    return normalise_columns(df)


# ─────────────────────────────────────────────────────────────────
# BLOCK 3 (Phase 1 fragment) — Drug alias helpers
# ─────────────────────────────────────────────────────────────────

def get_drug_aliases(drug_name: str) -> list[str]:
    """
    Brand → all strings to scan for in policy text.
    Combines brand, INN, and common biosimilar INN suffixes.

    Biosimilar brand names (YESINTEK, OTULFI, AMJEVITA, etc.) also map to
    their primary INN + every observed biosimilar suffix, so policies
    written with the INN-suffix form (e.g. 'ustekinumab-kfce') match.
    """
    inn_reverse = {v: k for k, v in INN_TO_BRAND.items()}
    aliases = {drug_name.lower()}

    # Direct brand → INN
    inn = inn_reverse.get(drug_name.lower())

    # Biosimilar brand → primary INN (not in INN_TO_BRAND directly)
    if not inn:
        inn = BIOSIMILAR_TO_PRIMARY_INN.get(drug_name.lower())

    if inn:
        aliases.add(inn)
        # All observed biosimilar suffix forms — pipeline scans for any
        # of these in policy text when matching the target drug.
        for suffix in ("-aekn", "-kfce", "-rdt", "-aauz", "-anbm", "-asbf",
                       "-atto", "-adbm", "-adaz", "-aqvh", "-bwwd", "-fkjp",
                       "-aaly", "-fbwm"):
            aliases.add(f"{inn}{suffix}")
    return sorted(aliases)


def _truncate_context_for_pass(
    context: str,
    drug: str,
    max_chars: int = MAX_CONTEXT_CHARS_FOR_PASS,
) -> str:
    """Fix B: keep Pass 1 / Pass 2 requests under free-tier TPM by slicing
    a window around the densest drug-name cluster.

    Deterministic. No LLM call. Returns the original context unchanged if
    already within max_chars — so small/medium docs pay zero cost.

    Strategy when over budget:
      1. Find every drug-alias mention in the context.
      2. For each mention, count other mentions within ±half_window chars.
      3. Centre the slice on the mention with the highest local count.
      4. Slice [center - half_window, center + half_window] (clamped to doc).

    If the drug isn't found at all (shouldn't happen post-preflight, but
    defensive), fall back to the head of the context.
    """
    if len(context) <= max_chars:
        return context

    aliases = get_drug_aliases(drug)
    text_lower = context.lower()
    positions: list[int] = []
    for alias in aliases:
        for m in re.finditer(rf"\b{re.escape(alias)}\b", text_lower):
            positions.append(m.start())

    if not positions:
        log_debug({
            "event": "context_truncated_no_drug_hits",
            "drug": drug,
            "from_chars": len(context),
            "to_chars": max_chars,
        })
        return context[:max_chars]

    positions.sort()
    half = max_chars // 2

    best_center = positions[0]
    best_count = 0
    for p in positions:
        count = sum(1 for q in positions if abs(q - p) <= half)
        if count > best_count:
            best_count = count
            best_center = p

    start = max(0, best_center - half)
    end = min(len(context), start + max_chars)
    # If we clipped at the right edge, shift start left so we keep full max_chars
    if end - start < max_chars and start > 0:
        start = max(0, end - max_chars)
    truncated = context[start:end]

    log_debug({
        "event": "context_truncated_for_tpm",
        "drug": drug,
        "from_chars": len(context),
        "to_chars": len(truncated),
        "drug_hits_total": len(positions),
        "drug_hits_kept": sum(1 for p in positions if start <= p < end),
        "slice_start": start,
        "slice_end": end,
    })
    return truncated


# ── Deterministic step-therapy keyword scanner ───────────────────
#
# Insurance against outline-mapping missing the universal "preferred
# products" / "Documentation for all indications" block. Outline
# anchoring is unreliable for Aetna-style policies where the
# step-therapy gate lives in an un-headed paragraph between two TOC
# sections. This scanner runs on the full PDF text every time, pulls
# any paragraph containing step-therapy language, and feeds the result
# to Pass 2 + Pass 3 as a guaranteed-included anchor section.
#
# Patterns are intentionally broad on the right side (preferred /
# additional / must try) and tight on the left (no false-positive on
# "preferred provider", "preferred pharmacy"). Tested across Aetna,
# Cigna, UHC, BCBS phrasings.
STEP_THERAPY_KEYWORD_PATTERNS = [
    r"\bpreferred\s+products?\b",
    r"\bnon[-\s]preferred\b",
    r"\b(?:THREE|TWO|ONE|FOUR|FIVE|1|2|3|4|5)\s+(?:additional\s+)?preferred\b",
    r"\bDocumentation\s+for\s+all\s+indications\b",
    r"\bstep[-\s]therapy\b",
    r"\bmust\s+(?:have\s+)?(?:tried|try|fail|failed)\b",
    r"\bpreviously\s+received\s+(?:a\s+)?(?:biologic|targeted)\b",
    r"\binadequate\s+(?:response|treatment\s+response)\b",
    r"\bpreferred\s+(?:ustekinumab|adalimumab|etanercept|infliximab|biologic|agent)\b",
    r"\bunable\s+to\s+take\b[^\n]{0,80}\bpreferred\b",
    r"\bfailed\s+trial\s+of\b",
    r"\btrial\s+and\s+inadequate\b",
]

_STEP_THERAPY_KEYWORD_RE = re.compile(
    "|".join(STEP_THERAPY_KEYWORD_PATTERNS),
    re.IGNORECASE,
)


def _extract_step_therapy_anchor(full_text: str, max_chars: int = 6_000) -> str:
    """Deterministic scanner over full PDF text. Pulls paragraphs that
    mention preferred-product / step-therapy language. Returns a stitched
    blob (up to max_chars) intended as an additional Pass 2 / Pass 3
    context section.

    Returns "" if no matches (no overhead added downstream).

    Strategy:
      - Split on blank-line paragraph boundaries.
      - For each matching paragraph, also include the next 2 paragraphs
        (bulleted lists frequently follow a header paragraph and would
        otherwise be cut off).
      - Stitch in document order, deduplicate consecutive paras, cap at
        max_chars.
    """
    if not full_text:
        return ""

    paras = re.split(r"\n\s*\n", full_text)
    matched: set = set()
    for i, p in enumerate(paras):
        if _STEP_THERAPY_KEYWORD_RE.search(p):
            for j in range(i, min(i + 3, len(paras))):
                matched.add(j)

    if not matched:
        return ""

    sorted_idx = sorted(matched)
    pieces: list = []
    last_idx = -2
    total = 0
    for i in sorted_idx:
        if i != last_idx + 1 and pieces:
            pieces.append("— — —")  # gap marker
        para_text = paras[i].strip()
        if not para_text:
            continue
        if total + len(para_text) > max_chars:
            break
        pieces.append(para_text)
        total += len(para_text) + 4
        last_idx = i

    return "\n\n".join(pieces)


def get_other_brands_in_doc(source_text: str, target_drug: str) -> list[str]:
    """
    For multi-brand disambiguation (§11.13). Returns the list of OTHER PsO
    brands present in source_text — these are the brands the LLM must be
    told to ignore when extracting params for target_drug.

    Scope-limited to brands actually in the doc, not all 30+ from the basket.
    Capped at top-20 by frequency to keep prompts bounded on very-multi-brand
    docs.
    """
    target_aliases = set(get_drug_aliases(target_drug))
    text_lower = source_text.lower()

    found: set[str] = set()
    for brand in BRANDED_DRUGS | GENERIC_DRUGS:
        if brand in target_aliases:
            continue
        if re.search(rf"\b{re.escape(brand)}\b", text_lower):
            found.add(brand)

    if len(found) > 20:
        scored = sorted(
            found,
            key=lambda b: len(re.findall(rf"\b{re.escape(b)}\b", text_lower)),
            reverse=True,
        )
        found = set(scored[:20])

    return sorted(found)


# ─────────────────────────────────────────────────────────────────
# BLOCK 2 — PDF INGESTION
# Single path. PyMuPDF text per page; sparse pages route through a vision
# OCR fallback. Vision is stubbed in Phase 2 and backfilled in Phase 4 once
# Block 4 (LLM plumbing) is online — on the audited 70-PDF dataset no page
# falls below MIN_CHARS_PER_PAGE, so the stub is exercised only on held-out
# PDFs the judges might add.
# ─────────────────────────────────────────────────────────────────

def _render_page_png(page: "fitz.Page", dpi: int = 200) -> bytes:
    """Render a PDF page to PNG bytes for vision-model OCR fallback.

    200 DPI is the sweet spot: legible for tables, ~250-500 KB per page.
    """
    pix = page.get_pixmap(dpi=dpi)
    return pix.tobytes("png")


def _ocr_page_with_vision(
    page: "fitz.Page",
    page_num: int,
    fallback_text: str = "",
) -> str:
    """Vision OCR for sparse pages (Phase 4 backfill).

    Renders the page to PNG, sends to Groq Llama 4 Scout, returns the
    extracted text. If vision fails OR the model refuses OR returns
    suspiciously short output, we fall back to the original sparse text
    rather than poisoning downstream with a useless string.
    """
    png_bytes = _render_page_png(page)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    prompt = (
        "Extract ALL text visible on this page exactly as it appears. "
        "Preserve numbered lists, bullets, table rows (use ' | ' as a "
        "column separator), and paragraph breaks. Do NOT summarise. "
        "Do NOT add commentary. Return only the page text."
    )
    try:
        text = retry(call_llm, prompt=prompt, vision=True, image_b64=b64) or ""
    except DailyQuotaExceeded:
        # Quota mid-batch must halt + checkpoint; swallowing it would
        # silently degrade OCR for every remaining sparse page in the batch.
        raise
    except Exception as e:
        log_debug({
            "event": "vision_fallback_failed",
            "page_num": page_num,
            "error": str(e),
        })
        return fallback_text

    # Quality gate — refusal markers or implausibly short returns
    lowered = text.strip().lower()
    refusal_markers = (
        "i cannot", "i can't", "i'm sorry", "unable to",
        "no text", "no readable text", "cannot read",
    )
    if len(text.strip()) < 50 or any(m in lowered[:200] for m in refusal_markers):
        log_debug({
            "event": "vision_fallback_low_quality",
            "page_num": page_num,
            "vision_output_preview": text[:200],
        })
        return fallback_text

    log_debug({
        "event": "vision_fallback_used",
        "page_num": page_num,
        "chars_returned": len(text),
    })
    return text


def ingest_pdf(filepath: Path) -> dict:
    """Open a PDF with PyMuPDF, extract text per page, OCR-fallback sparse
    pages, and return the full concatenated text with page markers.

    The PyMuPDF doc handle is left OPEN — outline extraction and recursive
    zoom (Block 3) reuse it. Caller is responsible for closing via
    close_pdf_cache() in a finally block.

    Returns:
        {
            "full_text":    str    # all pages joined with ===== PAGE N =====
            "pages":        list[str]
            "vision_pages": list[int]   # 1-indexed pages where vision fired
            "n_pages":      int
            "filepath":     str
            "doc_handle":   fitz.Document  # OPEN
        }
    """
    try:
        doc = fitz.open(filepath)
    except Exception as e:
        raise RuntimeError(f"PyMuPDF cannot open {filepath}: {e}") from e

    pages: list[str] = []
    vision_pages: list[int] = []

    for i, page in enumerate(doc, start=1):
        text = page.get_text("text") or ""
        if len(text.strip()) < MIN_CHARS_PER_PAGE:
            text = _ocr_page_with_vision(page, page_num=i, fallback_text=text)
            vision_pages.append(i)
        pages.append(text)

    full_text_parts = [
        f"\n===== PAGE {i} =====\n{p}"
        for i, p in enumerate(pages, start=1)
    ]
    return {
        "full_text":    "".join(full_text_parts),
        "pages":        pages,
        "vision_pages": vision_pages,
        "n_pages":      len(pages),
        "filepath":     str(filepath),
        "doc_handle":   doc,
    }


def _ingest_pdf_text_only(filepath: Path) -> dict:
    """Cheap text-only ingestion for preflight — NO vision regardless of
    sparsity. Same output shape as ingest_pdf, plus a `_text_only` flag so
    _ensure_vision_aware() can upgrade later if a sparse page is found
    during real processing.
    """
    try:
        doc = fitz.open(filepath)
    except Exception as e:
        raise RuntimeError(f"PyMuPDF cannot open {filepath}: {e}") from e

    pages = [page.get_text("text") or "" for page in doc]
    full_text = "".join(
        f"\n===== PAGE {i} =====\n{p}"
        for i, p in enumerate(pages, start=1)
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
    """Upgrade a text-only ingestion (from preflight) to vision-aware
    by re-OCRing any sparse pages (Phase 4 backfill).

    Idempotent — returns immediately if already upgraded. Mutates the
    incoming dict in place (and returns it) so cache entries stay
    consistent.
    """
    if not doc_info.get("_text_only"):
        return doc_info

    doc = doc_info["doc_handle"]
    pages: list[str] = []
    vision_pages: list[int] = []

    for i, page in enumerate(doc, start=1):
        text = doc_info["pages"][i - 1]
        if len(text.strip()) < MIN_CHARS_PER_PAGE:
            text = _ocr_page_with_vision(page, page_num=i, fallback_text=text)
            vision_pages.append(i)
        pages.append(text)

    full_text = "".join(
        f"\n===== PAGE {i} =====\n{p}"
        for i, p in enumerate(pages, start=1)
    )
    doc_info["pages"] = pages
    doc_info["full_text"] = full_text
    doc_info["vision_pages"] = vision_pages
    doc_info["_text_only"] = False
    return doc_info


def close_pdf_cache(pdf_cache: dict) -> None:
    """Close every PyMuPDF doc handle in the cache. Defensive: ignores
    individual close errors so one bad handle doesn't block the rest.
    Called in the finally block of process_all_rows.
    """
    for entry in pdf_cache.values():
        try:
            doc = entry.get("doc_handle")
            if doc is not None:
                doc.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────
# BLOCK 4 — LLM INTERFACE (Groq only — text + vision + TPM throttle)
# One provider, two models, one SDK. Every call is throttled by the
# shared TokenBudget so we never breach Groq's 6K TPM ceiling (we run
# under TPM_LIMIT = 5500 to keep headroom).
# ─────────────────────────────────────────────────────────────────


class DailyQuotaExceeded(Exception):
    """Raised by retry() when Groq returns a daily-quota error (vs a minute
    rate-limit). Caught by process_all_rows so we can save the checkpoint
    and exit cleanly with status code 2 for tomorrow's resume.
    """


class RequestTooLarge(Exception):
    """Raised by _call_groq_* when the estimated request size would exceed
    MAX_REQUEST_TOKENS. Non-retryable — caller must reduce input size
    (smaller context, smaller outline) before re-sending.

    This is a defensive pre-flight check, not an after-the-fact API error.
    We refuse to send the request rather than risk another 413 / org block.
    """


class TokenBudget:
    """Sliding 60-second token-usage window, tracked PER (key_idx, model).

    Earlier version used a single global window with hard-coded
    TPM_LIMIT=5_500, which throttled Scout calls (real TPM=30_000) the
    same as llama-70B (real TPM=12_000). Result: Scout artificially
    bottlenecked, debug log showed 101 throttle sleeps + ~99 min wasted
    sitting at sub-5K token requests that Scout could handle trivially.

    The fix: each (key, model) combo gets its own 60s window, and the
    cap consulted at sleep-decision time is GROQ_MODEL_TPM_CAP[model]
    minus a safety margin. Different models on the same key share zero
    state — accurate to how Groq actually meters TPM (per-model per-key).
    """

    # Safety margin below documented TPM so we never breach the 429 line.
    # Groq reports the TPM as the daily cap; in practice the rolling window
    # accepts ~95% of that. 0.85 leaves comfortable headroom for response
    # overhead and clock skew.
    SAFETY_FRACTION = 0.85
    # Fallback for models not in GROQ_MODEL_TPM_CAP (defensive).
    DEFAULT_TPM = 5_500

    def __init__(self) -> None:
        # key: (key_idx, model_name) → deque of (timestamp, tokens)
        self._windows: dict = {}

    def _cap_for(self, model: str) -> int:
        raw = GROQ_MODEL_TPM_CAP.get(model, self.DEFAULT_TPM)
        return int(raw * self.SAFETY_FRACTION)

    def consume(self, est_tokens: int, model: str, key_idx: int = 0) -> None:
        """Throttle on the per-(key, model) sliding window. Sleep until the
        window has room, then record this call."""
        cap = self._cap_for(model)
        win_key = (key_idx, model)
        window = self._windows.setdefault(win_key, deque())

        now = time.time()
        while window and now - window[0][0] > 60:
            window.popleft()
        used = sum(t for _, t in window)

        if used + est_tokens > cap:
            if window:
                oldest_ts = window[0][0]
                sleep_for = max(0.0, 60 - (now - oldest_ts) + 0.5)
            else:
                # Single request larger than this model's TPM cap.
                # Caller should route to a bigger-TPM model rather than wait.
                sleep_for = 60.0
                log_debug({
                    "event": "tpm_request_exceeds_budget",
                    "est_tokens": est_tokens,
                    "tpm_cap": cap,
                    "model": model,
                    "key_idx": key_idx,
                })
            log_debug({
                "event": "tpm_throttle_sleep",
                "seconds": round(sleep_for, 2),
                "model": model,
                "key_idx": key_idx,
                "tokens_in_window": used,
                "would_add": est_tokens,
                "tpm_cap": cap,
            })
            time.sleep(sleep_for)
            now = time.time()
            while window and now - window[0][0] > 60:
                window.popleft()
        window.append((now, est_tokens))


# Module-level shared state. Groq() does not hit the network on
# construction, so building the client list at import time is safe.
token_budget = TokenBudget()
_groq_clients: list = [Groq(api_key=k) for k in GROQ_API_KEYS]
# Backward compatibility — single-client references (vision uses this).
groq_client = _groq_clients[0] if _groq_clients else None

# Per-(key_idx, model) exhaustion tracking. A combo is added when that
# key+model returns a TPD-style error today. Reset only on process
# restart — that's fine because TPD resets at midnight UTC anyway, and
# a fresh process discovers exhaustion again with one failed attempt.
_exhausted_combos: set = set()  # set of (key_idx, model_name)


def _is_combo_exhausted(key_idx: int, model: str) -> bool:
    return (key_idx, model) in _exhausted_combos


def _mark_combo_exhausted(key_idx: int, model: str, reason: str = "tpd") -> None:
    if (key_idx, model) not in _exhausted_combos:
        _exhausted_combos.add((key_idx, model))
        log_debug({
            "event": "combo_exhausted",
            "key_idx": key_idx,
            "model": model,
            "reason": reason,
        })
        print(f"INFO: Combo exhausted: key[{key_idx}] + {model} ({reason})")


def _classify_tpm_error(err_str: str) -> str:
    """Parse a Groq TPM error to distinguish 'request_too_big' (the single
    request exceeds the model's TPM ceiling — no amount of waiting helps,
    must try a model with bigger TPM) from 'window_full' (the 60-second
    sliding window has filled up — wait and retry).
    Returns 'request_too_big' | 'window_full' | 'unknown'.
    """
    m_req = re.search(r"Requested\s+(\d+)", err_str)
    m_lim = re.search(r"Limit\s+(\d+)", err_str)
    if m_req and m_lim:
        try:
            req = int(m_req.group(1))
            lim = int(m_lim.group(1))
            return "request_too_big" if req > lim else "window_full"
        except ValueError:
            pass
    # Ambiguous → assume window_full (safe: triggers wait+retry once).
    return "unknown"


# Compiled once. Strips Qwen3's <think>…</think> reasoning trace (and any
# similar thinking-model output) from the response before parse_json_safe.
_THINK_BLOCK = re.compile(r"<think\b.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_thinking_blocks(text: str) -> str:
    """Remove <think>…</think> reasoning blocks. No-op for models that don't
    emit them. Qwen3-32B emits these by default before its final answer.
    """
    return _THINK_BLOCK.sub("", text or "").strip()


def _estimate_tokens(prompt: str, system_prompt: str,
                     max_output_tokens: int) -> int:
    """Conservative tokens estimate: 1 token ≈ 4 chars for English text."""
    return (len(prompt) + len(system_prompt)) // 4 + max_output_tokens


def _call_groq_text(
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = MAX_TOKENS_TEXT,
    model_chain: Optional[list] = None,
) -> str:
    """Groq text completion with multi-key × multi-model fallback.

    Walks the (key_idx, model) grid:
      - Outer loop: each Groq API key in GROQ_API_KEYS order.
      - Inner loop: each model in `model_chain` (caller's preference).
      - Skip combos already marked exhausted today.
      - TPD error: mark combo exhausted (today), continue to next combo.
      - TPM "request_too_big": continue to next model (bigger TPM may help).
      - TPM "window_full": wait TPM_WINDOW_WAIT_SECONDS, retry SAME combo
        ONCE. If still fails, give up on this combo.
      - Other errors: raise immediately (don't burn quota walking the grid).

    `model_chain` defaults to GROQ_TEXT_MODEL_FALLBACKS for backward compat
    with callers that don't specify. Strips Qwen-style <think>…</think>
    blocks. Temperature 0 throughout.
    """
    if not _groq_clients:
        raise RuntimeError("No Groq API keys configured — set GROQ_API_KEY in .env")
    if model_chain is None:
        model_chain = GROQ_TEXT_MODEL_FALLBACKS

    est = _estimate_tokens(prompt, system_prompt, max_tokens)
    if est > MAX_REQUEST_TOKENS:
        log_debug({
            "event": "request_too_large_aborted",
            "kind": "text",
            "est_tokens": est,
            "limit": MAX_REQUEST_TOKENS,
            "prompt_chars": len(prompt),
            "system_chars": len(system_prompt),
            "max_tokens_used": max_tokens,
        })
        raise RequestTooLarge(
            f"Text request would be ~{est:,} tokens, exceeds "
            f"MAX_REQUEST_TOKENS={MAX_REQUEST_TOKENS:,}. Refusing to send."
        )

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    last_error: Optional[Exception] = None

    for key_idx, client in enumerate(_groq_clients):
        for model in model_chain:
            if _is_combo_exhausted(key_idx, model):
                continue

            # Each combo gets up to 2 attempts (one initial + one post-wait
            # retry on TPM window-full). TPD and other errors break out
            # after the first attempt.
            for attempt in range(2):
                token_budget.consume(est, model=model, key_idx=key_idx)
                started_at_ts = time.time()
                started_at_iso = datetime.datetime.fromtimestamp(
                    started_at_ts, tz=datetime.timezone.utc,
                ).isoformat(timespec="milliseconds")
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=TEMPERATURE,
                    )
                    duration = time.time() - started_at_ts
                    usage = getattr(response, "usage", None)
                    log_debug({
                        "event": "llm_call",
                        "kind": "text",
                        "model": model,
                        "key_idx": key_idx,
                        "attempt": attempt,
                        "started_at": started_at_iso,
                        "duration_sec": round(duration, 3),
                        "est_tokens": est,
                        "max_tokens_param": max_tokens,
                        "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                        "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                        "total_tokens": getattr(usage, "total_tokens", None) if usage else None,
                        "prompt_chars": len(prompt),
                        "system_chars": len(system_prompt),
                    })
                    content = response.choices[0].message.content or ""
                    return _strip_thinking_blocks(content)
                except Exception as e:
                    duration = time.time() - started_at_ts
                    err_str = str(e)
                    err_lower = err_str.lower()
                    last_error = e

                    is_daily_quota = (
                        ("daily quota" in err_lower)
                        or ("tpd" in err_lower)
                        or ("rpd" in err_lower)
                        or ("tokens per day" in err_lower)
                        or ("requests per day" in err_lower)
                    )
                    is_tpm = (
                        ("tpm" in err_lower)
                        or ("tokens per minute" in err_lower)
                    )
                    # Organization-level restrictions are permanent for that
                    # key (Groq has banned/locked the org). Treat as TPD-
                    # equivalent: mark combo exhausted + move to next key,
                    # don't propagate as "other" (which would kill the row).
                    is_org_restricted = (
                        ("organization has been restricted" in err_lower)
                        or ("organization_restricted" in err_lower)
                        or ("org_restricted" in err_lower)
                    )
                    classification = (
                        "tpd" if is_daily_quota
                        else ("tpm" if is_tpm
                              else ("org_restricted" if is_org_restricted
                                    else "other"))
                    )

                    log_debug({
                        "event": "llm_call_failed",
                        "kind": "text",
                        "model": model,
                        "key_idx": key_idx,
                        "attempt": attempt,
                        "started_at": started_at_iso,
                        "duration_sec": round(duration, 3),
                        "est_tokens": est,
                        "classified_as": classification,
                        "error": err_str[:400],
                    })

                    if is_daily_quota:
                        _mark_combo_exhausted(key_idx, model, reason="tpd")
                        break  # break attempt loop, walk to next model

                    if is_org_restricted:
                        # Whole organization is locked — mark EVERY model
                        # exhausted on this key so we don't waste attempts
                        # on llama/qwen/gpt-oss which will all return the
                        # same 400. Then break to walk to the next key.
                        for m in model_chain:
                            _mark_combo_exhausted(key_idx, m, reason="org_restricted")
                        break  # break attempt loop, walk to next model

                    if is_tpm:
                        tpm_kind = _classify_tpm_error(err_str)
                        if tpm_kind == "request_too_big":
                            # This combo's TPM ceiling is below the request
                            # size — no wait helps. Try next model (may have
                            # higher TPM).
                            log_debug({
                                "event": "tpm_request_too_big_skip",
                                "model": model,
                                "key_idx": key_idx,
                            })
                            break  # walk to next model
                        # window_full or unknown → wait & retry ONCE
                        if attempt == 0:
                            print(f"INFO: TPM window full on key[{key_idx}] + "
                                  f"{model} — waiting {TPM_WINDOW_WAIT_SECONDS}s")
                            log_debug({
                                "event": "tpm_window_full_waiting",
                                "model": model,
                                "key_idx": key_idx,
                                "wait_sec": TPM_WINDOW_WAIT_SECONDS,
                            })
                            time.sleep(TPM_WINDOW_WAIT_SECONDS)
                            continue  # attempt 1 — same combo retry
                        # attempt 1 also TPM → give up on this combo
                        break

                    # Other errors: not retryable here. Raise immediately
                    # so retry() (5xx/connection) or the caller can decide.
                    raise

    # All (key, model) combos in the chain are exhausted.
    msg = (
        f"All Groq combos exhausted: {len(_groq_clients)} keys × "
        f"{len(model_chain)} models. Last error: {last_error}"
    )
    log_debug({
        "event": "all_combos_exhausted",
        "n_keys": len(_groq_clients),
        "model_chain": model_chain,
    })
    raise DailyQuotaExceeded(msg) from last_error


def _call_groq_vision(
    prompt: str,
    image_b64: str,
    max_tokens: int = MAX_TOKENS_VISION,
) -> str:
    """Groq Llama 4 Scout (multimodal) with ONE page image per call.
    Single-image keeps payload small — Scout's vision endpoint accepts the
    OpenAI-compatible `image_url` content with a base64 data URL.

    Uses the same multi-key fallback as _call_groq_text — vision OCR fires
    rarely on this dataset, but if Scout's TPD on key[0] is exhausted by
    text traffic (Pass 1+2 also use Scout), vision will need the next key.
    """
    if not _groq_clients:
        raise RuntimeError("No Groq API keys configured")
    # Image ≈ 1000 tokens at typical PDF-page sizes (Llama 4 Scout is on
    # the lower end of vision tokenisers; OpenAI's high-detail bucket is
    # ~765-1105 per tile). Plus the prompt text + reserved output.
    est = len(prompt) // 4 + 1000 + max_tokens
    if est > MAX_REQUEST_TOKENS:
        log_debug({
            "event": "request_too_large_aborted",
            "kind": "vision",
            "est_tokens": est,
            "limit": MAX_REQUEST_TOKENS,
        })
        raise RequestTooLarge(
            f"Vision request would be ~{est:,} tokens, exceeds "
            f"MAX_REQUEST_TOKENS={MAX_REQUEST_TOKENS:,}. Refusing to send."
        )
    last_error: Optional[Exception] = None
    # Vision is single-model (Llama 4 Scout) — walk keys only for TPD fallback
    for key_idx, client in enumerate(_groq_clients):
        if _is_combo_exhausted(key_idx, GROQ_VISION_MODEL):
            continue

        for attempt in range(2):  # one wait+retry on TPM window-full
            token_budget.consume(est, model=GROQ_VISION_MODEL, key_idx=key_idx)
            started_at_ts = time.time()
            started_at_iso = datetime.datetime.fromtimestamp(
                started_at_ts, tz=datetime.timezone.utc,
            ).isoformat(timespec="milliseconds")
            try:
                response = client.chat.completions.create(
                    model=GROQ_VISION_MODEL,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_b64}",
                                },
                            },
                        ],
                    }],
                    max_tokens=max_tokens,
                    temperature=TEMPERATURE,
                )
                duration = time.time() - started_at_ts
                usage = getattr(response, "usage", None)
                log_debug({
                    "event": "llm_call",
                    "kind": "vision",
                    "model": GROQ_VISION_MODEL,
                    "key_idx": key_idx,
                    "attempt": attempt,
                    "started_at": started_at_iso,
                    "duration_sec": round(duration, 3),
                    "est_tokens": est,
                    "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                    "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                    "total_tokens": getattr(usage, "total_tokens", None) if usage else None,
                    "prompt_chars": len(prompt),
                    "image_b64_chars": len(image_b64),
                })
                return response.choices[0].message.content
            except Exception as e:
                duration = time.time() - started_at_ts
                err_str = str(e)
                err_lower = err_str.lower()
                last_error = e
                is_daily_quota = (
                    ("daily quota" in err_lower)
                    or ("tpd" in err_lower)
                    or ("tokens per day" in err_lower)
                )
                is_tpm = ("tpm" in err_lower) or ("tokens per minute" in err_lower)
                log_debug({
                    "event": "llm_call_failed",
                    "kind": "vision",
                    "model": GROQ_VISION_MODEL,
                    "key_idx": key_idx,
                    "attempt": attempt,
                    "started_at": started_at_iso,
                    "duration_sec": round(duration, 3),
                    "est_tokens": est,
                    "classified_as": "tpd" if is_daily_quota else ("tpm" if is_tpm else "other"),
                    "error": err_str[:400],
                })
                if is_daily_quota:
                    _mark_combo_exhausted(key_idx, GROQ_VISION_MODEL, reason="tpd")
                    break  # walk to next key
                if is_tpm:
                    if _classify_tpm_error(err_str) == "request_too_big":
                        break  # next key
                    if attempt == 0:
                        print(f"INFO: Vision TPM window full on key[{key_idx}] — "
                              f"waiting {TPM_WINDOW_WAIT_SECONDS}s")
                        time.sleep(TPM_WINDOW_WAIT_SECONDS)
                        continue
                    break
                raise

    msg = f"All Groq keys exhausted for vision. Last error: {last_error}"
    log_debug({"event": "all_combos_exhausted_vision", "n_keys": len(_groq_clients)})
    raise DailyQuotaExceeded(msg) from last_error


def call_llm(prompt: str, system_prompt: str = "",
             vision: bool = False, image_b64: Optional[str] = None,
             max_tokens: Optional[int] = None,
             model_chain: Optional[list] = None) -> str:
    """Master LLM router. Groq, text or vision.

    `max_tokens` right-sizes the output reservation per call type (Fix A).
    `model_chain` lets each call site declare its preferred fallback order
    (Pass 1+2 → Scout-first; Pass 3+map+zoom → llama-first). Both default
    to safe values for backward compat with retry() and tiny smoke calls.
    """
    if vision:
        if not image_b64:
            raise ValueError("call_llm(vision=True) requires image_b64")
        return _call_groq_vision(
            prompt=prompt, image_b64=image_b64,
            max_tokens=max_tokens if max_tokens is not None else MAX_TOKENS_VISION,
        )
    return _call_groq_text(
        prompt=prompt, system_prompt=system_prompt,
        max_tokens=max_tokens if max_tokens is not None else MAX_TOKENS_TEXT,
        model_chain=model_chain,
    )


def retry(func, *args, max_attempts: int = MAX_RETRIES, **kwargs):
    """Retry with class-aware backoff.

    - Daily quota exceeded → raise DailyQuotaExceeded (no retry; halt batch)
    - 429 minute rate-limit → sleep 30s and retry
    - 5xx / connection      → exponential backoff (BACKOFF_BASE ** attempt)
    - Other 4xx             → no retry, raise immediately

    Logs every failure as a dict.
    """
    for attempt in range(max_attempts):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            err_str = str(e)
            err_lower = err_str.lower()

            # Same fix as in _call_groq_text — match TPD-specific markers,
            # NOT "day" (which matches Groq's "Upgrade today" CTA).
            is_daily_quota = (
                ("daily quota" in err_lower)
                or ("tpd" in err_lower)
                or ("rpd" in err_lower)
                or ("tokens per day" in err_lower)
                or ("requests per day" in err_lower)
            )
            is_tpm = (
                ("tpm" in err_lower)
                or ("tokens per minute" in err_lower)
            )
            is_429 = "429" in err_str or "rate_limit" in err_lower
            is_5xx = any(c in err_str for c in ("500", "502", "503", "504"))
            is_conn = any(kw in err_lower for kw in
                          ("connection", "timeout", "read timed out"))

            if is_daily_quota:
                log_debug({"event": "daily_quota_exceeded", "error": err_str})
                raise DailyQuotaExceeded(err_str) from e

            if is_tpm:
                # TPM = request too large for per-minute window. Retrying
                # without shrinking the request just fails again. Surface
                # immediately so caller can act (smaller context / smaller
                # max_tokens).
                log_debug({"event": "tpm_request_too_large", "error": err_str})
                raise

            if is_429:
                wait = 30
            elif is_5xx or is_conn:
                wait = BACKOFF_BASE ** attempt
            else:
                log_debug({"event": "retry_giving_up_non_retryable",
                           "error": err_str})
                raise

            log_debug({
                "event": "retry",
                "attempt": attempt + 1,
                "error": err_str,
                "wait_seconds": wait,
                "reason": "429" if is_429 else "5xx_or_conn",
            })
            if attempt == max_attempts - 1:
                raise
            time.sleep(wait)


def parse_json_safe(
    response: str,
    retry_prompt: Optional[str] = None,
    retry_model_chain: Optional[list] = None,
    retry_max_tokens: Optional[int] = None,
    retry_system_prompt: Optional[str] = None,
) -> dict:
    """Parse LLM JSON output safely. Handles markdown fences, trailing
    commas, and extra prose around the JSON. If retry_prompt is provided
    and the first parse fails, reprompts the model once for clean JSON.

    retry_model_chain / retry_max_tokens / retry_system_prompt: pass the
    SAME chain/budget/system prompt that was used for the original call.
    Without these, the retry would silently fall back to the default
    (max_tokens=4096 + default model chain), which can burn quota and
    pick a different model than the caller intended.
    """

    def _attempt(text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError(f"No JSON object found: {text[:200]}")
        text = text[start:end]
        text = re.sub(r",\s*([}\]])", r"\1", text)
        return json.loads(text)

    try:
        return _attempt(response)
    except (json.JSONDecodeError, ValueError) as first_err:
        log_debug({
            "event": "json_parse_error_first",
            "error": str(first_err),
            "raw": (response or "")[:500],
        })
        if not retry_prompt:
            raise

        strict_prompt = (
            "Your previous response was not valid JSON. "
            "Return ONLY the JSON object that answers this prompt, "
            "no other text:\n\n" + retry_prompt
        )
        retry_response = ""
        try:
            # Preserve original model chain + budget on retry so we don't
            # silently fall back to defaults (which burn quota and may
            # route to a different model than the caller chose).
            call_kwargs = {}
            if retry_model_chain is not None:
                call_kwargs["model_chain"] = retry_model_chain
            if retry_max_tokens is not None:
                call_kwargs["max_tokens"] = retry_max_tokens
            if retry_system_prompt is not None:
                call_kwargs["system_prompt"] = retry_system_prompt
            retry_response = retry(call_llm, strict_prompt, **call_kwargs)
            return _attempt(retry_response)
        except Exception as second_err:
            log_debug({
                "event": "json_parse_error_after_retry",
                "error": str(second_err),
                "raw": (retry_response or "")[:500],
            })
            raise first_err


# Shared system prompt used by sectioning (Block 3) AND the three
# extraction passes (Block 5). Defined here so Phase 5 can reference it
# before Phase 6 fully populates Block 5.
SYSTEM_PROMPT_EXTRACTOR = """\
You are a precise medical policy extraction assistant.
Rules:
- For verbatim fields: copy the EXACT words from the document — do NOT paraphrase.
- For formatted fields (age, durations): convert to the specified format.
- If information is not found, return "NA" for that field.
- Return valid JSON only, no explanation text outside the JSON.
- When a policy lists multiple drugs with different values in the same field,
  return ONLY the value associated with the TARGET drug.
"""


# ─────────────────────────────────────────────────────────────────
# BLOCK 3 — OUTLINE-DRIVEN SECTIONING (Phase 5)
#
# Stage A — Build a complete document outline (local, no API).
# Stage B — One LLM call to map outline → 4 target-section anchors.
# Stage C — Recursive zoom on any section that exceeds MAX_SECTION_CHARS.
# Slicing — Deterministic char-offset slicing using outline anchors.
# ─────────────────────────────────────────────────────────────────

# Compiled heading-detection patterns.
HEADING_NUMBERED = re.compile(
    r"^\s*(?:Step|Section|Part|Criterion)\s+\d+", re.I
)
HEADING_KEYWORDS = re.compile(
    r"^\s*(?:General|Universal|Initial|Renewal|Reauthorization|"
    r"Continuation|Quantity|Diagnosis|Approval|Coverage|Preferred|"
    r"Non-Preferred|Targeted|Plaque|Psoriasis|Step Therapy)\b",
    re.I,
)
HEADING_LIST_ITEM = re.compile(r"^\s*\d+\.\s")
PAGE_MARKER = re.compile(r"===== PAGE (\d+) =====")

SECTION_KEYS = (
    "universal_criteria",
    "classification_tables",
    "drug_specific_criteria",
    "reauth_criteria",
)


def _heading_level(span: dict, median_size: float) -> int:
    """Coarse heading-level estimate. 1 = top, larger = deeper."""
    ratio = span["size"] / max(median_size, 1)
    if ratio > 1.5:
        return 1
    if ratio > 1.25:
        return 2
    if ratio > 1.10:
        return 3
    return 4


def _locate_heading_offset(
    full_text: str,
    heading_text: str,
    page_start_offset: int,
    page_offsets: dict,
    page_num: int,
) -> int:
    """Find char offset of heading_text within full_text, restricted to a
    single-page window (M1 fix: prior code searched ±20K chars which could
    bleed across pages and resolve to the wrong occurrence)."""
    page_end = page_offsets.get(page_num + 1, page_start_offset + 20_000)
    search_window = full_text[page_start_offset:page_end]
    needle = heading_text.strip()[:80]
    idx = search_window.find(needle)
    return page_start_offset + idx if idx >= 0 else page_start_offset


def _outline_from_fonts(
    doc: "fitz.Document",
    full_text: str,
    page_offsets: dict,
) -> list:
    """Heading detection by font/style/pattern heuristics, per page.

    Tightened after Phase 5 testing showed the first-cut heuristic emitted
    8,246 headings for Oregon Medicaid (every "3. Is the patient..." in a
    decision-tree was matching). Rules now require multiple positive signals:

      • Big font (>1.25 × page median) AND ≥8 chars AND <150 chars
      • OR bold AND multi-word AND ≥8 chars AND <100 chars
      • OR all-caps AND multi-word AND ≥8 chars AND <100 chars
      • OR explicit "Step N / Section N / Criterion N" numbered pattern
      • OR a medical-policy keyword anchor (General / Renewal / Preferred / ...)

    Bare numbered-list items ("3. " / "1. ") and short labels ("Yes:" / "No:")
    are intentionally NOT in the OR chain — they create the most noise in
    decision-tree-style documents like Oregon Medicaid.
    """
    outline: list = []
    for page_idx, page in enumerate(doc, start=1):
        page_dict = page.get_text("dict")
        sizes: list = []
        spans_with_meta: list = []
        for block in page_dict.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = (span.get("text") or "").strip()
                    if not text:
                        continue
                    sizes.append(span["size"])
                    spans_with_meta.append({
                        "text":  text,
                        "size":  span["size"],
                        "flags": span.get("flags", 0),
                        "bbox":  span.get("bbox", (0, 0, 0, 0)),
                    })
        if not sizes:
            continue
        median_size = median(sizes)
        size_threshold = median_size * 1.25  # stricter (was 1.15)

        for span in spans_with_meta:
            text = span["text"]
            text_len = len(text)
            if text_len < 8:
                continue  # filters "Yes:" / "No:" / single-word labels

            multi_word = " " in text
            is_bold = bool(span["flags"] & 16)
            is_large = span["size"] > size_threshold
            is_all_caps = text.isupper()

            looks_like_heading = (
                (is_large and text_len < 150)
                or (is_bold and multi_word and text_len < 100)
                or (is_all_caps and multi_word and text_len < 100)
                or bool(HEADING_NUMBERED.match(text))
                or bool(HEADING_KEYWORDS.match(text))
            )
            if not looks_like_heading:
                continue

            outline.append({
                "page":        page_idx,
                "level":       _heading_level(span, median_size),
                "text":        text,
                "char_offset": _locate_heading_offset(
                    full_text, text, page_offsets.get(page_idx, 0),
                    page_offsets, page_idx,
                ),
            })
    return outline


# Maximum outline entries sent to map_outline_to_sections. Above this we
# prune to top-level (L1+L2) headings to keep the LLM call under the per-
# request size limit and the daily token quota.
MAX_OUTLINE_ENTRIES = 400


def _prune_outline(outline: list) -> list:
    """If outline is too large, prune to top-level headings only.
    Returns outline unchanged if already within MAX_OUTLINE_ENTRIES."""
    if len(outline) <= MAX_OUTLINE_ENTRIES:
        return outline

    # Keep L1+L2; if still too many, L1 only; if STILL too many, top N by
    # length (longer headings tend to be more semantically meaningful).
    pruned = [h for h in outline if h["level"] <= 2]
    if len(pruned) > MAX_OUTLINE_ENTRIES:
        pruned = [h for h in outline if h["level"] == 1]
    if len(pruned) > MAX_OUTLINE_ENTRIES:
        pruned = sorted(pruned, key=lambda h: -len(h["text"]))[:MAX_OUTLINE_ENTRIES]
        pruned = sorted(pruned, key=lambda h: h["char_offset"])

    log_debug({
        "event": "outline_pruned",
        "before": len(outline),
        "after": len(pruned),
    })
    return pruned


def extract_outline(doc: "fitz.Document", full_text: str) -> list:
    """Build an ordered outline of the document.

    Tries PDF bookmarks (doc.get_toc()) first — about 30% of audited PDFs
    have them. Falls back to font/style scanning.

    Each entry: {page, level, text, char_offset}. The outline is the ONLY
    input to Stage B's LLM call — typical size 5-10K tokens for large docs,
    500-1500 for small ones.
    """
    page_offsets = {
        int(m.group(1)): m.end()
        for m in PAGE_MARKER.finditer(full_text)
    }

    # Path 1: PDF bookmarks
    toc = doc.get_toc()
    if toc and len(toc) >= 3:
        outline: list = []
        for level, title, page_num in toc:
            if page_num <= 0:
                continue
            outline.append({
                "page":        page_num,
                "level":       level,
                "text":        title.strip(),
                "char_offset": _locate_heading_offset(
                    full_text, title, page_offsets.get(page_num, 0),
                    page_offsets, page_num,
                ),
            })
        log_debug({"event": "outline_from_toc", "n_headings": len(outline)})
        return outline

    # Path 2: font/style scan
    outline = _outline_from_fonts(doc, full_text, page_offsets)
    log_debug({"event": "outline_from_fonts", "n_headings": len(outline)})
    return outline


def extract_outline_for_mapping(doc, full_text) -> list:
    """Wrapper that returns a size-bounded outline suitable for sending
    to Stage B's LLM call. extract_outline() returns the FULL outline
    (used by slice_by_anchors); this one prunes for the prompt."""
    return _prune_outline(extract_outline(doc, full_text))


# ── Stage B — Outline → section anchors (one LLM call) ───────────

def map_outline_to_sections(
    outline: list,
    drug: str,
    indication: str,
) -> dict:
    """ONE LLM call. Input = outline only (~5-10K tokens). Output = which
    {heading, page} pair begins each of the 4 target sections.

    The (heading, page) tuple is the lookup key — multi-occurrence headings
    like "Step 1" (which Oregon Medicaid repeats across many drug sections)
    are disambiguated by page number (Codex fix B4).
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
    response = retry(
        call_llm, prompt,
        system_prompt=SYSTEM_PROMPT_EXTRACTOR,
        max_tokens=MAX_TOKENS_OUTLINE_MAP,
        model_chain=GROQ_TEXT_MODELS_FOR_REASONING,
    )
    anchors = parse_json_safe(
        response, retry_prompt=prompt,
        retry_model_chain=GROQ_TEXT_MODELS_FOR_REASONING,
        retry_max_tokens=MAX_TOKENS_OUTLINE_MAP,
        retry_system_prompt=SYSTEM_PROMPT_EXTRACTOR,
    )
    return {k: anchors.get(k) for k in SECTION_KEYS}


# ── Slicing — deterministic, no LLM ──────────────────────────────

# Minimum section size per type. If the literal next outline entry is too
# close to the anchor (e.g. an immediate sub-heading on the same page), we
# keep skipping until the slice is at least this many chars. Stops anchors
# from accidentally bounding each other when they sit on the same page
# (Oregon Medicaid TIM section header + "Length of Authorization:" are
# both at page 374, ~50 chars apart).
MIN_SECTION_CHARS_BY_KEY = {
    "universal_criteria":     2_500,
    "classification_tables":  1_000,
    # Drug section needs to span enough to include class-level criteria like
    # TB screening (Oregon Medicaid TIM block: Step 4 is ~5 pages deep into
    # the TIM section header) plus the actual step-therapy gates. 15K chars
    # ≈ 10 pages, covers TIM + drug subsection comfortably. Small-doc bypass
    # in the orchestrator (Phase 8) handles docs where this would over-slice.
    "drug_specific_criteria": 15_000,
    "reauth_criteria":        2_000,
}


def slice_by_anchors(full_text: str, outline: list, anchors: dict) -> dict:
    """Given (heading, page) anchors, slice full_text into the 4 target
    sections. Each section spans from its anchor offset to the next outline
    entry whose offset is at least MIN_SECTION_CHARS_BY_KEY chars later —
    closer entries are treated as sub-headings of the current section and
    skipped over.

    Lookup keys on (heading_text, page) — multi-occurrence safe.
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
             if h["text"].strip().lower() == heading
             and h["page"] == page),
            None,
        )
        # Fallback: starts-with on same page
        if not match:
            match = next(
                (h for h in outline
                 if h["page"] == page
                 and h["text"].strip().lower().startswith(heading[:50])),
                None,
            )
        # Last resort: exact heading text on the closest page
        if not match:
            candidates = [
                h for h in outline
                if h["text"].strip().lower() == heading
            ]
            if candidates:
                match = min(candidates, key=lambda h: abs(h["page"] - page))

        if not match:
            log_debug({
                "event": "anchor_not_in_outline",
                "section": key,
                "anchor": anchor,
            })
            continue

        start = match["char_offset"]
        min_size = MIN_SECTION_CHARS_BY_KEY.get(key, 1_000)
        # Find the first "next entry" that is at least min_size chars away;
        # closer entries are treated as sub-headings of this section.
        end = len(full_text)
        for o in offsets_sorted:
            if o > start and (o - start) >= min_size:
                end = o
                break
        sections[key] = full_text[start:end]

    return sections


# ── Stage C — Recursive zoom for huge sections ───────────────────

def recursive_zoom(
    section_text: str,
    doc: "fitz.Document",
    section_key: str,
    drug: str,
    indication: str,
) -> str:
    """If a section exceeds MAX_SECTION_CHARS, re-extract a sub-outline
    of just that section and ask the LLM which sub-headings are relevant.

    Used for the universal block on Oregon Medicaid (50+ pages of immune
    modulator criteria, most not relevant to PsO).
    """
    if len(section_text) <= MAX_SECTION_CHARS:
        return section_text

    sub_outline: list = []
    for line in section_text.split("\n"):
        line = line.strip()
        if HEADING_NUMBERED.match(line) or HEADING_KEYWORDS.match(line):
            sub_outline.append({"text": line[:150], "level": 3})

    # Fix C: cap sub_outline. Unbounded growth was responsible for the
    # 412K-char prompt that burned llama's TPD in one shot on 2026-05-29
    # (Oregon Medicaid had 2000+ matching lines). Sort by content length —
    # longer headings tend to carry more semantic information than terse
    # navigation labels.
    MAX_SUB_OUTLINE_ENTRIES = 100
    if len(sub_outline) > MAX_SUB_OUTLINE_ENTRIES:
        log_debug({
            "event": "recursive_zoom_sub_outline_capped",
            "section": section_key,
            "from_entries": len(sub_outline),
            "to_entries": MAX_SUB_OUTLINE_ENTRIES,
        })
        sub_outline.sort(key=lambda h: -len(h["text"]))
        sub_outline = sub_outline[:MAX_SUB_OUTLINE_ENTRIES]

    if not sub_outline:
        log_debug({
            "event": "recursive_zoom_no_subheadings",
            "section": section_key,
            "size": len(section_text),
        })
        return section_text[:MAX_SECTION_CHARS]

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
        response = retry(
            call_llm, prompt,
            max_tokens=MAX_TOKENS_RECURSIVE_ZOOM,
            model_chain=GROQ_TEXT_MODELS_FOR_REASONING,
        )
        keep = parse_json_safe(
            response, retry_prompt=prompt,
            retry_model_chain=GROQ_TEXT_MODELS_FOR_REASONING,
            retry_max_tokens=MAX_TOKENS_RECURSIVE_ZOOM,
        ).get("keep", [])
    except Exception as e:
        log_debug({
            "event": "recursive_zoom_failed",
            "section": section_key,
            "error": str(e),
        })
        return section_text[:MAX_SECTION_CHARS]

    pieces: list = []
    for heading in keep:
        idx = section_text.find(heading)
        if idx >= 0:
            pieces.append(section_text[idx: idx + 4000])

    return "\n\n".join(pieces) if pieces else section_text[:MAX_SECTION_CHARS]


# ── Assembly — labelled context for downstream extraction ────────

def assemble_context(
    sections: dict,
    drug: str,
    indication: str,
    doc: Optional["fitz.Document"] = None,
) -> str:
    """Build the labelled context block from the 4 sliced sections.
    Recursively zooms any oversized section. Final hard cap truncates the
    LARGEST section (M3 fix: prior code always truncated universal).
    """
    universal     = sections.get("universal_criteria") or ""
    tables        = sections.get("classification_tables") or ""
    drug_specific = sections.get("drug_specific_criteria") or ""
    reauth        = sections.get("reauth_criteria") or ""

    if doc and len(universal) > MAX_SECTION_CHARS:
        universal = recursive_zoom(universal, doc, "universal_criteria",
                                   drug, indication)
    if doc and len(tables) > MAX_SECTION_CHARS:
        tables = recursive_zoom(tables, doc, "classification_tables",
                                drug, indication)
    if doc and len(drug_specific) > MAX_SECTION_CHARS:
        drug_specific = recursive_zoom(drug_specific, doc,
                                       "drug_specific_criteria",
                                       drug, indication)
    if doc and len(reauth) > MAX_SECTION_CHARS:
        reauth = recursive_zoom(reauth, doc, "reauth_criteria",
                                drug, indication)

    parts: list = []
    if universal:
        parts.append(f"[UNIVERSAL CRITERIA — applies to all drugs]\n{universal}")
    else:
        parts.append("[UNIVERSAL CRITERIA — NOT FOUND]")
    if tables:
        parts.append(f"[CLASSIFICATION TABLES — preferred / non-preferred agents]\n{tables}")
    if drug_specific:
        parts.append(f"[DRUG-SPECIFIC CRITERIA — {drug} for {indication}]\n{drug_specific}")
    else:
        parts.append(f"[DRUG-SPECIFIC CRITERIA — NOT FOUND FOR {drug}]")
    if reauth:
        parts.append(f"[REAUTHORIZATION / RENEWAL CRITERIA]\n{reauth}")

    context = "\n\n".join(parts)

    # Hard cap — truncate the largest section if over budget
    if len(context) > MAX_CONTEXT_CHARS:
        log_debug({
            "event": "context_over_cap",
            "size": len(context),
            "drug": drug,
        })
        sizes = {
            "universal": len(universal),
            "tables":    len(tables),
            "drug":      len(drug_specific),
            "reauth":    len(reauth),
        }
        largest = max(sizes, key=sizes.get)
        overshoot = len(context) - MAX_CONTEXT_CHARS
        target_size = max(2000, sizes[largest] - overshoot)
        if largest == "universal":
            universal = universal[:target_size]
        elif largest == "tables":
            tables = tables[:target_size]
        elif largest == "drug":
            drug_specific = drug_specific[:target_size]
        else:
            reauth = reauth[:target_size]

        parts = []
        if universal:
            parts.append(f"[UNIVERSAL CRITERIA — applies to all drugs]\n{universal}")
        if tables:
            parts.append(f"[CLASSIFICATION TABLES — preferred / non-preferred agents]\n{tables}")
        if drug_specific:
            parts.append(f"[DRUG-SPECIFIC CRITERIA — {drug} for {indication}]\n{drug_specific}")
        if reauth:
            parts.append(f"[REAUTHORIZATION / RENEWAL CRITERIA]\n{reauth}")
        context = "\n\n".join(parts)

    return context


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def determinism_test() -> str:
    """Run an identical prompt N times at temperature 0. Hash responses.
    Returns "STABLE" | "MINOR_DRIFT" | "SIGNIFICANT_DRIFT" | "UNKNOWN".
    Logs the result. Plan-of-action depends on the outcome (see dev plan
    §11.12).
    """
    prompt = (
        'Extract the minimum age from this sentence: '
        '"Member must be 18 years or older". '
        'Return ONLY JSON: {"age": ">=N"}'
    )
    outputs: list[str] = []
    for _ in range(DETERMINISM_TEST_RUNS):
        try:
            outputs.append(call_llm(prompt))
        except Exception as e:
            log_debug({"event": "determinism_test_error", "error": str(e)})
            return "UNKNOWN"

    hashes = {hashlib.md5((o or "").encode()).hexdigest() for o in outputs}
    if len(hashes) == 1:
        result = "STABLE"
    elif len(hashes) == 2:
        result = "MINOR_DRIFT"
    else:
        result = "SIGNIFICANT_DRIFT"

    log_debug({
        "event": "determinism_test",
        "result": result,
        "n_unique": len(hashes),
        "outputs": outputs,
    })
    print(f"INFO: Determinism test = {result} "
          f"({len(hashes)} unique outputs over {DETERMINISM_TEST_RUNS} runs)")
    return result


# ─────────────────────────────────────────────────────────────────
# BLOCK 5 — EXTRACTION PASSES (Phase 6)
# Three small prompts beat one big prompt — separation of concerns
# stops the model conflating fields and cuts wasted output tokens.
# ─────────────────────────────────────────────────────────────────


def _multi_brand_directive(drug: str, other_brands: list) -> str:
    """Shared snippet appended to Pass 1 and Pass 2 prompts. Tells the model
    which OTHER brands appear in this policy so it can disambiguate (per
    §11.13). Empty string if no other brands present (avoids prompt noise
    on single-drug policies).
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


# ── PASS 1 — Simple parameters ────────────────────────────────────

def extract_simple_params(
    context: str,
    drug: str,
    indication: str,
    other_brands: Optional[list] = None,
) -> dict:
    """Pass 1 — 7 fields with format conversion.

    Age becomes >=N; durations become plain numbers. Other text fields are
    verbatim. Reauthorization Required (Param 9) is NOT asked here — it's
    100% derived by rule_reauth_required() in Block 6.

    Context is truncated around drug mentions if it exceeds
    MAX_CONTEXT_CHARS_FOR_PASS (Fix B). Reserves only MAX_TOKENS_PASS_1
    output tokens instead of the legacy 4096 (Fix A) — Groq counts the
    reservation against per-minute TPM.
    """
    context = _truncate_context_for_pass(context, drug)
    multi_brand = _multi_brand_directive(drug, other_brands or [])

    prompt = f"""
From the following payer PA policy document, extract information for {drug}
for {indication} (moderate to severe Plaque Psoriasis).
{multi_brand}

POLICY TEXT:
{context}

IMPORTANT RULES:
- For age: standardise to format like ">=18" or ">=6". Convert whatever the
  policy says ("18 years of age or older" → ">=18", "6 years or older" → ">=6").
  If policy ONLY says "adult", "adults", "adult member(s)", or "adult patient(s)"
  without a numeric threshold → return ">=18" (adult = 18+ by US payer convention).
  If policy says "FDA approved age" or similar without a number → return
  "FDA approved age". If age not mentioned → return "NA".
- For durations: return plain number only ("6" not "6 months", "12" not "one year").
- For all other fields: copy EXACT verbatim text from policy.
- Return "NA" (not empty string, not null) when field not found.

OUTPUT FORMAT — VERY IMPORTANT:
- EVERY field value in the returned JSON MUST be a plain string (or "NA").
- Do NOT return a dict, object, list, or nested JSON for any field value.
- Even if the policy presents quantity-limit info or reauth-requirements
  as a TABLE or per-strength-list in the source, render the WHOLE thing as
  ONE multi-line string with newlines (\n) and bullet markers preserved.
  Example WRONG: "quantity_limits_text": {{"Tremfya 100mg": ["1 per 56d"]}}
  Example RIGHT: "quantity_limits_text": "Tremfya 100mg/mL: 1 syringe per 56 days\\nTremfya 200mg/2mL: 1 syringe per 28 days"
- For VERBATIM text fields (reauth_requirements_text, quantity_limits_text):
  preserve the bullet/numbering structure as it appears in the source.
  Keep bullet markers exactly (•, *, -, o, ◦, ▪) and numbered list markers
  ("1.", "2.", "a)", "i.") on their own lines (separated by \n) within the
  string. Do NOT flatten a bulleted list into a comma-separated paragraph.
- For quantity_limits_text: capture text labelled with ANY variant of
  "Quantity Limit", "Quantity Level Limit", "Quantity Limit Program", "QLL",
  or "Quantity Restrictions" — these all mean the same thing.
  REJECT only if the label is exclusively "dosage", "dosing limit",
  "dosing information", "recommended dose", "administration", or "dose is".
  The text must contain drug-specific quantity / strength / days-supply info
  (e.g. "Stelara 90mg/mL: 1 syringe per 56 days"); the generic statement
  "Quantity limits exist" with no specifics is NOT a match.

- For specialist_types: if the policy lists prescriber specialty BY
  INDICATION (e.g. a "Prescriber Specialties" section with sub-bullets
  per indication), return ONLY the specialty for {indication}.
  Example policy text:
    "1. Prescriber Specialties — must be prescribed by:
       1. Plaque psoriasis: dermatologist
       2. Psoriatic arthritis: rheumatologist or dermatologist
       3. Ulcerative colitis and Crohn's disease: gastroenterologist"
  → For indication="Plaque Psoriasis", return "dermatologist" ONLY.
  → Do NOT return "dermatologist, rheumatologist, gastroenterologist" —
    rheumatologist and gastroenterologist apply to OTHER indications.
  If the policy lists ONE specialty applying to all indications (or no
  per-indication breakdown), return that single value or comma-list as
  written. Capitalisation as in source ("Dermatologist" vs "dermatologist").

Return ONLY this JSON:
{{
  "age": ">=N format, or 'FDA approved age', or 'NA' if not mentioned",
  "tb_test_required": "Yes or No",
  "initial_auth_duration_months": "number only e.g. '6' or '12', or 'Unspecified'",
  "reauth_duration_months": "number only e.g. '12', or 'Unspecified' if required but unstated, or 'NA' if not mentioned",
  "reauth_requirements_text": "exact verbatim continuation criteria text WITH bullet structure preserved, or 'NA' if not mentioned",
  "specialist_types": "comma-separated specialties, or 'NA' if none specified",
  "quantity_limits_text": "exact verbatim text WITH bullet structure preserved if labelled with any quantity-limit variant; else 'NA'"
}}
"""
    response = retry(
        call_llm, prompt,
        system_prompt=SYSTEM_PROMPT_EXTRACTOR,
        max_tokens=MAX_TOKENS_PASS_1,
        model_chain=GROQ_TEXT_MODELS_FOR_PASS_1_2,
    )
    return parse_json_safe(
        response, retry_prompt=prompt,
        retry_model_chain=GROQ_TEXT_MODELS_FOR_PASS_1_2,
        retry_max_tokens=MAX_TOKENS_PASS_1,
        retry_system_prompt=SYSTEM_PROMPT_EXTRACTOR,
    )


# ── PASS 2 — Step therapy verbatim (single blob) ─────────────────

def extract_step_therapy_text(
    context: str,
    drug: str,
    indication: str,
    other_brands: Optional[list] = None,
    step_therapy_anchor: str = "",
) -> dict:
    """Pass 2 — verbatim step therapy text, ONE blob.

    Earlier draft split into universal vs indication-specific; that turned
    out to be a judgment call the model gets wrong on multi-brand policies.
    Pass 3 absorbs that classification using the [UNIVERSAL CRITERIA] /
    [DRUG-SPECIFIC CRITERIA] markers that Block 3 inserted into the
    assembled context.

    Same TPM-safety wrappers as Pass 1: drug-window truncation (Fix B)
    + per-call max_tokens=MAX_TOKENS_PASS_2 (Fix A).

    step_therapy_anchor (optional): deterministic keyword-scanned blob
    of step-therapy / preferred-product paragraphs from the full PDF
    text. Guaranteed-included before context truncation — insurance
    against outline mapping missing the universal section.
    """
    # Reserve budget for anchor so total prompt size stays bounded
    anchor_size = len(step_therapy_anchor or "")
    truncation_budget = max(
        15_000,
        MAX_CONTEXT_CHARS_FOR_PASS - anchor_size - 1_000,  # 1K for headers
    )
    context = _truncate_context_for_pass(context, drug, max_chars=truncation_budget)
    multi_brand = _multi_brand_directive(drug, other_brands or [])

    if step_therapy_anchor:
        anchor_block = (
            "== STEP THERAPY ANCHOR (keyword-scanned from full document — "
            "INCLUDE ANY MATCHING TEXT FROM HERE IN YOUR OUTPUT) ==\n"
            f"{step_therapy_anchor}\n\n"
        )
    else:
        anchor_block = ""

    prompt = f"""
From the following payer PA policy document, copy VERBATIM all text that
describes WHICH PRIOR TREATMENTS the patient must have tried/failed (or be
unable to take) to qualify {drug} for {indication}.

{multi_brand}

{anchor_block}IMPORTANT — the STEP THERAPY ANCHOR block above (if present)
is keyword-extracted from the full policy and is GUARANTEED to contain the
step-therapy gate language. Always include matching text from there in
your output, even if it does not appear in the main POLICY TEXT below.

== WHAT TO INCLUDE (the rule that decides) ==

Include any sentence or bullet whose APPROVAL DEPENDS on a specific named
product or treatment. The criterion does NOT have to be labelled "step
therapy" — coverage-criteria bullets that mention specific products are
step-therapy-equivalent and MUST be included.

Specifically include any sentence/bullet that mentions:
- A brand name (TREMFYA, HUMIRA, ENBREL, COSENTYX, OTEZLA, SOTYKTU, etc.)
- An INN (adalimumab, methotrexate, cyclosporine, acitretin, MTX, etc.)
- A treatment modality with a name (phototherapy, UVB, PUVA, narrowband
  UVB, light therapy)
- A drug class label (biologic, targeted synthetic drug, TNF inhibitor,
  JAK inhibitor, conventional systemic DMARD, topical corticosteroid)
- A "previously received" / "tried and failed" / "inadequate response to"
  / "intolerance to" / "contraindication to" phrase paired with any of the
  above

== INCLUSION EXAMPLES (these all MUST be in the output) ==

✓ "The patient is unable to take THREE preferred products … A preferred
  ustekinumab product …"
✓ "Member has had an inadequate response or intolerance to either
  phototherapy (e.g., UVB, PUVA) or pharmacologic treatment with
  methotrexate, cyclosporine, or acitretin."
✓ "For members 6 years of age or older who have previously received a
  biologic or targeted synthetic drug (e.g., Sotyktu, Otezla) indicated
  for the treatment of moderate to severe plaque psoriasis"
   ← INCLUDE this even when the target drug is STELARA. The criterion
     applies to STELARA — Sotyktu / Otezla are listed as EXAMPLES of the
     biologic class, not as exclusions. Generic-criterion-with-examples
     is NOT a multi-brand conflict.
✓ "At least 3% BSA is affected and the member has had inadequate response
  to methotrexate, cyclosporine, or acitretin."
   ← INCLUDE. The BSA part alone wouldn't qualify, but the MTX/cyclo/acitretin
     mention makes it step-therapy-equivalent.

== EXCLUSION EXAMPLES (do NOT include these) ==

✗ "Member has a confirmed diagnosis of moderate-to-severe plaque psoriasis"
  — no product mentioned.
✗ "At least 10% body surface area affected" (when this bullet stands alone
  with no product mention) — BSA threshold alone is not a step.
✗ "Member has been screened for tuberculosis (TB)" — eligibility, no product.
✗ "Initial authorization period is 12 months" — auth duration, not step.
✗ "Authorization may be granted for plaque psoriasis ONLY when prescribed
   by a dermatologist" — specialist restriction, no product.
✗ Step requirements stated EXCLUSIVELY for a different drug (e.g. a section
  labelled "TREMFYA-specific requirements" when the target drug is STELARA).
   But generic criteria that mention multiple brands as alternatives or
   examples → INCLUDE per above.

== POLICY TEXT ==
{context}

== RULES ==
1. Copy text EXACTLY as written. No paraphrasing, no summarising.
2. Preserve AND/OR connectors and bullet/numbering structure exactly.
3. Include EVERY sentence/bullet that passes the inclusion rule, in the
   order they appear in the policy. If they come from different blocks
   (e.g. a "step therapy" block AND a "coverage criteria" block), join
   the blocks with " AND " between them.
4. Multi-brand directive in the OTHER_DRUGS list above means: when a
   sentence is exclusively about a DIFFERENT drug's criteria, skip it.
   It does NOT mean to skip generic criteria that mention multiple drugs
   as examples / alternatives — those still apply to {drug}.
5. Return "NA" ONLY if you find NO sentence/bullet matching the inclusion
   rule above. If even ONE matches, include it.

Return ONLY this JSON (the value MUST be a plain string, never a dict/list):
{{
  "combined_step_text": "the verbatim block — preserve bullets, AND/OR, and line breaks (use \\n) — or 'NA' if no qualifying content exists",
  "has_step_therapy": true or false
}}
"""
    response = retry(
        call_llm, prompt,
        system_prompt=SYSTEM_PROMPT_EXTRACTOR,
        max_tokens=MAX_TOKENS_PASS_2,
        model_chain=GROQ_TEXT_MODELS_FOR_PASS_1_2,
    )
    result = parse_json_safe(
        response, retry_prompt=prompt,
        retry_model_chain=GROQ_TEXT_MODELS_FOR_PASS_1_2,
        retry_max_tokens=MAX_TOKENS_PASS_2,
        retry_system_prompt=SYSTEM_PROMPT_EXTRACTOR,
    )

    text = result.get("combined_step_text", "") or ""
    if text.strip().upper() in ("NA", "N/A", "NONE", ""):
        result["combined_step_text"] = "NA"
        result["has_step_therapy"] = False

    return result


# ── PASS 3 — Step counting CoT (small input — step text only) ────

def _extract_section_markers(assembled_context: str) -> str:
    """Pull the [SECTION] marker lines from the assembled context so Pass 3
    can reference them when classifying universal vs indication-specific
    without us re-sending the entire context body."""
    marker_lines = [
        line for line in assembled_context.split("\n")
        if line.strip().startswith("[") and line.strip().endswith("]")
    ]
    return "\n".join(marker_lines) if marker_lines else "(no section markers found)"


def extract_step_counts(
    combined_step_text: str,
    assembled_context: str,
    drug: str,
    indication: str,
) -> dict:
    """Pass 3 — count branded / generic / phototherapy steps via CoT.

    Takes the SMALL combined_step_text from Pass 2 (~1-2K tokens), NOT the
    full assembled context. Section markers from assembled_context are
    extracted separately so the model can classify universal vs indication-
    specific without paying for the full context's body.
    """
    if not combined_step_text or combined_step_text.strip() in ("", "NA"):
        return {
            "steps_brands":      "NA",
            "steps_generic":     "NA",
            "step_phototherapy": "NA",
            "reasoning":         "No step therapy text from Pass 2",
        }

    # Use FULL branded list — earlier code sliced [:30] alphabetically which
    # silently omitted common PsO targets (SKYRIZI, STELARA, SOTYKTU, TALTZ,
    # TREMFYA, YESINTEK) past the alphabetical cutoff. The full PsO market
    # basket is ~50 entries — ~400 chars added to the prompt is negligible
    # for Scout's 30K TPM budget and prevents the LLM from mis-classifying
    # these brands when they appear in step-therapy text.
    branded_list = ", ".join(sorted(BRANDED_DRUGS))
    generic_list = ", ".join(sorted(GENERIC_DRUGS))
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

═══════════════════════════════════════════════════════════════════════
DECISION TREE — APPLY THIS FIRST, BEFORE ANYTHING ELSE
═══════════════════════════════════════════════════════════════════════

Step ① — Does the policy have a UNIVERSAL MULTI-BRAND GATE?
  Examples of universal multi-brand gates:
    - "must have failed THREE preferred products: A, B, C"
    - "contraindication, intolerance or ineffective response to all of:
       Ilumya, Stelara, Avsola, Inflectra, Renflexis"
    - "must try and fail two preferred biologics"

  ── YES, universal gate exists:
       brand_count = STATED NUMBER from the gate.
       Apply TARGET DRUG EXCLUSION: if {drug} is in the listed brands,
       subtract it (you cannot require failure of {drug} as a step
       for {drug} itself).
       DO NOT add Path A "previously received biologic" on top of the
       universal count — the universal already requires biologic failures,
       so Path A is REDUNDANT (already counted in universal).
       Proceed to Step ③ for the generic / phototherapy count.

  ── NO universal multi-brand gate → proceed to Step ②.

Step ② — Connector between Path A and Path B in the indication block:
  Path A is typically "previously received a biologic or targeted synthetic
  drug (e.g., Sotyktu, Otezla)" — a history-style sentence.
  Path B is typically "for treatment when ANY of the following criteria is
  met: ..." — a criteria-and-step-therapy section.

  ── EXPLICIT OR connector ("; or", "or:", " OR ", numbered "1. X; or 2. Y"):
       Path A is HISTORY CHECK only → brand_count = 0 from Path A.
       Only Path B's standard step path contributes (see Step ③).

  ── IMPLICIT (no explicit OR — paragraphs separated only by newlines,
     or two consecutive "Authorization may be granted" sentences with no
     connector word):
       Path A counts as 1 brand step (the biologic requirement).
       Add Path B's standard step path (Step ③).

Step ③ — Path B's STANDARD step path (the one that actually contributes
generic / phototherapy counts):
  Path B is typically "for treatment when ANY of: (a) crucial body areas;
  (b) 10% BSA; (c) 3% BSA + (inadequate response to phototherapy OR
  methotrexate/cyclosporine/acitretin; or clinical reason to avoid)".
  Sub-bullets (a) and (b) are clinical-severity criteria → 0 steps.
  Sub-bullet (c) has the actual step requirement:
    "(inadequate response to phototherapy OR MTX/CYC/ACI; or clinical
     reason to avoid)" is ONE OR-cluster → counts as 1 GENERIC step.
  → Generic count from Path B = 1 step.
  → Phototherapy is INSIDE the OR-cluster (one of the alternatives) →
    Phototherapy = "No" (it's only an OR alternative, not mandatory).

═══════════════════════════════════════════════════════════════════════
EXPECTED OUTPUTS FOR THE 5 CANONICAL AETNA-FAMILY PATTERNS
═══════════════════════════════════════════════════════════════════════

PATTERN 1 — Universal "THREE preferred products" + indication block (Row 1):
  → Step ①: universal exists, stated count = 3 → brand_count = 3.
  → Path A is REDUNDANT (subsumed by universal). Generic from Path B = 1.
  → branded=3, generic=1, phototherapy=No.

PATTERN 2 — Universal "contraindication to all of [list including {drug}]"
            + indication block (Row 2-style for target=STELARA):
  → Step ①: universal exists, listed brands = [Ilumya, Stelara, Avsola/
    Inflectra/Renflexis cluster]. After excluding target {drug}=STELARA:
    [Ilumya, Avsola/Inflectra/Renflexis cluster] = 2 brand steps.
  → Path A REDUNDANT. Generic from Path B = 1.
  → branded=2, generic=1, phototherapy=No.

PATTERN 3 — No universal + EXPLICIT OR between Path A and Path B (Row 3):
  → Step ①: no universal.
  → Step ②: explicit "; or" → Path A is history check = 0 brand.
  → Generic from Path B = 1.
  → branded=NA (0), generic=1, phototherapy=No.

PATTERN 4 — No universal + IMPLICIT (Path A and Path B separated only by
            newlines, no explicit OR connector) (Rows 4 and 5):
  → Step ①: no universal.
  → Step ②: implicit → Path A = 1 brand step (biologic).
  → Generic from Path B = 1.
  → branded=1, generic=1, phototherapy=No.

═══════════════════════════════════════════════════════════════════════

The decision tree above OVERRIDES anything in the worked examples below.
Use the worked examples only when the input policy doesn't fit any of the
4 canonical patterns above.

WORKED EXAMPLES (anchor your reasoning on these):

Example 1 — Oregon-Medicaid-style (all-AND chain):
  Universal: TB test, diagnosis confirmation
  Indication: topical CS AND another topical AND phototherapy AND systemic
              AND (Humira OR Enbrel for >=3 months)
  → step_a: [TB test, diagnosis]      (not therapy — exclude from counts)
  → step_b: [topical CS, other topical, photo, systemic, Humira-or-Enbrel]
  → step_c: same as step_b joined by AND (no OR between steps)
  → step_d: "Humira OR Enbrel" is OR WITHIN a step → 1 branded step
  → step_e: [topical CS=generic, other topical=generic, photo=photo,
             systemic=generic, Humira/Enbrel=branded]
  → counts: branded=1, generic=3, phototherapy=Yes

Example 2 — Reference-tab-style (OR resolution):
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

NUMERIC PATTERNS TO RECOGNIZE (very important):
- "must try THREE preferred products" → count = 3 (use the stated NUMBER,
  not the length of any list that follows)
- "must try TWO additional preferred products" (from a list of 3 options)
  → count = 2 — the patient picks ANY 2 of the 3, so count the STATED 2,
  not the 3 list items
- "any one of the following: A, B, C" → 1 step (OR-cluster — patient picks 1)
- "either of the following: ..." → 1 step
- "all of the following: A, B, C" → 3 steps (each is required)
- If a block says "THREE preferred products: 1) X, 2) TWO additional from
  {{a, b, c}}" then total = 1 + 2 = 3 steps (the literal numbers stated),
  NOT 1 + 3 = 4 (don't count the inner list length).

WORKED EXAMPLE 3 — Aetna-TREMFYA-style (additive AND between named drug
  + N-additional-from-list, with an EXPLICITLY STATED count):
  Policy text: "Previously received ustekinumab (Stelara) AND has tried TWO
                additional preferred products from the following list:
                Skyrizi, Cosentyx, Otezla"
  → step_a: []  (none of these are universal in this example)
  → step_b: [ustekinumab/Stelara, TWO additional preferred products]
  → step_c: [ustekinumab AND 2-additional-preferred]  (AND joins them)
  → step_d: no OR between steps — the "two additional" is a SELECT-2-of-3,
            which counts as 2 separate required steps (patient must satisfy 2)
  → step_e: ustekinumab=branded(1), each additional preferred=branded(1 each)
  → counts: branded = 1 + 2 = 3, generic=NA, phototherapy=NA
  KEY RULE: "X additional from {{a, b, c}}" with an EXPLICIT stated count X
             → count = X (the policy's stated number). Total branded =
             explicit named drug (1) + X additional = 1 + X.
             This rule applies ONLY when the policy explicitly says a number
             like "TWO additional", "ONE of the following", "must try THREE".
             It does NOT apply to plain comma-lists without a count (see EX 4).

WORKED EXAMPLE 4 — OR-cluster within ONE bullet (do NOT flatten):
  Policy text: "Member has had inadequate response or intolerance to either
                phototherapy (e.g., UVB, PUVA) or pharmacologic treatment
                with methotrexate, cyclosporine, or acitretin."
  This is ONE criterion. The policy does NOT state a count. The "or"s join
  ALTERNATIVE ways to satisfy the same single criterion. Patient picks ONE.
  → step_b: [inadequate-response criterion]  (ONE entry, not four)
  → step_c: [the same single criterion]
  → step_d: within this 1 step, alternatives are: photo / methotrexate /
            cyclosporine / acitretin. Pick LEAST RESTRICTIVE single alt.
            The criterion stays 1 step regardless of how many alternatives.
  → step_e: classify by the chosen alternative:
              if photo path → 1 phototherapy step
              if generic path → 1 generic step (the mtx/cyclo/acit OR-cluster
                                                 is STILL 1 step, NOT 3)
  → counts (taking generic path): branded=NA, generic=1, phototherapy=No
  KEY RULE: "A, B, or C" inside ONE bullet/criterion = 1 step.
             ONLY count multiple steps when the policy explicitly says a
             number ("TWO of", "all of the following", "three additional").
             Comma-lists without a stated count are ALWAYS 1 OR-cluster step.

DECISION TREE for "list-like" criteria (apply per criterion):
  Q: Does the policy state an explicit NUMBER for this list?
    ("TWO additional", "ONE of", "ALL of the following", "THREE preferred")
    YES → flatten to that number (use Worked Example 3 logic)
    NO  → it is a single OR-cluster — count = 1 step (use Worked Example 4)

ANCHOR RULE (very important — applies to Aetna / Cigna preferred-product gates):
  When the UNIVERSAL section states an explicit total count for branded steps
  (e.g. "THREE preferred products", "FOUR preferred", "FIVE preferred",
   "the patient is unable to take TWO preferred biologics"), the universal
  branded count is EXACTLY that stated number. Do NOT:
    - recount sub-bullets that elaborate what the N preferred products are
    - add extra branded steps from later "previously received a biologic"
      sentences in the indication block (those are alternative qualification
      paths, NOT additions on top of the universal gate)
    - count "(e.g., Sotyktu, Otezla)" mentions inside parentheses as extra
      branded steps — they are examples of the class, not separate steps

OR-PATH TIE-BREAKER (when two indication paths have equal step counts):
  If the indication block has multiple OR-alternative paths and they tie on
  step count, prefer the path that adds the FEWEST branded steps (canonical
  "least restrictive" interpretation). Example for Aetna Tremfya:
    Path A: "previously received a biologic" → 1 branded step
    Path B: "3% BSA + (photo OR methotrexate/cyclosporine/acitretin)"
            → 1 step (treat inner OR as 1 OR-cluster generic)
  Both = 1 step. Path B adds 0 branded; Path A adds 1 branded.
  → CHOOSE Path B. Final: universal-branded + 1 generic.

WORKED EXAMPLE 5 — Aetna-Tremfya canonical pattern (full doc structure):
  Universal: "unable to take THREE preferred products: ustekinumab + TWO
              additional from {{adalimumab/Enbrel, Rinvoq, Otezla}}"
  Indication: "Authorization may be granted for adult members who have
              previously received a biologic (e.g., Sotyktu, Otezla)" — Path A
              OR
              "Authorization may be granted for adult members when ANY of:
                • Crucial body areas affected (0 steps — clinical only)
                • 10% BSA affected (0 steps — clinical only)
                • 3% BSA + (inadequate response to photo OR mtx/cyclo/acit)
                  (1 step — OR-cluster within bullet)" — Path B
  → Universal branded count: STATED as THREE → 3 (anchor rule)
  → Indication: OR-tie-breaker prefers Path B with generic path → 1 generic
  → Final: branded=3, generic=1, phototherapy=No
  (Phototherapy = No because photo appears only inside the OR-cluster we
   resolved to the methotrexate alternative.)

WORKED EXAMPLE 6 — Path A "history check" vs Path B "step path":
  Aetna-style policies commonly have an indication block with two paths:
    Path A: "previously received a biologic or targeted synthetic drug"
            (a HISTORY CHECK — confirming patient already took a biologic)
    Path B: "for treatment when ANY of: crucial body / 10% BSA /
            3% BSA + (inadequate response to MTX/CYC/ACI; or clinical reason
            to avoid)"
            (CRITERIA + STEP THERAPY — the actual qualification gate)

  How to count steps:
    Path B always contributes its STANDARD step path → 1 generic step
    (the 3% BSA + MTX/CYC/ACI OR-cluster). The "clinical reason to avoid"
    sub-clause is an OR alternative WITHIN the cluster — it doesn't change
    the count; the cluster is still 1 step.

  Path A's contribution depends on the connector between Path A and Path B:
    EXPLICIT OR connector ("; or", "or:", " OR ", numbered "1. ... ; or 2. ..."):
      → Path A is a HISTORY CHECK only — count 0 brand steps from it.
      → Only Path B's standard path counts.
      → Result: Brands=NA (or 0), Generic=1, Photo=No.

    IMPLICIT (no explicit OR — paragraphs separated only by newlines, or
    just listed sequentially as "Authorization may be granted... Authorization
    may be granted ..."):
      → Path A counts as 1 brand step (the biologic requirement).
      → Path B's standard path adds 1 generic step.
      → Result: Brands=1, Generic=1, Photo=No.

  WORKED EXAMPLE — Aetna explicit-OR (Row 3 pattern):
    Policy text: "1. Plaque psoriasis (PsO)
                   1. For adult members who have previously received a
                      biologic or targeted synthetic drug; or
                   2. For adult members for treatment of moderate-to-severe
                      plaque psoriasis when any of the following criteria
                      is met: ..."
    The numbered "1. ...; or 2. ..." structure is EXPLICIT OR.
    → Path A is history-only (0 brand).
    → Path B standard path = 3% BSA + MTX/CYC/ACI cluster = 1 generic.
    → Counts: branded=NA, generic=1, phototherapy=No.

  WORKED EXAMPLE — Aetna implicit (Row 5 pattern):
    Policy text: "Authorization of 12 months may be granted for members 6
                   years of age and older who have previously received a
                   biologic or targeted synthetic drug.

                   Authorization of 12 months may be granted for members 6
                   years of age and older for treatment of moderate to severe
                   plaque psoriasis when any of the following criteria is
                   met: ..."
    No explicit "or" between the two "Authorization may be granted" sentences.
    Treat as additive.
    → Path A: 1 brand step (biologic).
    → Path B standard: 1 generic step.
    → Counts: branded=1, generic=1, phototherapy=No.

  CRITICAL: The "clinical reason to avoid" / "contraindication to" carve-outs
  inside Path B's 3% BSA sub-bullet do NOT change the step count. They are
  one OR alternative within the OR-cluster (which is already counted as 1
  step). The cluster stays = 1 generic step regardless.

WORKED EXAMPLE 7 — TARGET DRUG EXCLUSION in universal gates:
  When a universal/multi-brand gate explicitly LISTS specific brand names
  that the patient must have failed, and the TARGET drug ({drug}) appears
  in that list, the target drug requirement does NOT count as a step
  (you cannot require failure of the target drug as a step for itself).

  Example policy text (Row 2 / 148593 STELARA pattern):
    "Member has a contraindication, intolerance or ineffective response to
     all of the following available equivalent alternative targeted immune
     modulators: both Ilumya, Stelara, and either Avsola, Inflectra, or
     Renflexis."
    For target={drug}=STELARA, EXCLUDE Stelara from the count:
      Listed brand groups: [Ilumya, Stelara, (Avsola OR Inflectra OR Renflexis)]
      After target exclusion: [Ilumya, (Avsola OR Inflectra OR Renflexis)]
      → 2 brand steps (Ilumya = 1, OR-cluster = 1).

    Then add the indication block's standard step path → 1 generic step.
    → Final: branded=2, generic=1, phototherapy=No.

  KEY RULE: ALWAYS check if the target drug appears in the listed brand
  names. If YES, subtract it from the step count.
  (For target=TREMFYA, the same gate would NOT exclude any of Ilumya/Stelara/
  Avsola/Inflectra/Renflexis since TREMFYA itself isn't in the list →
  count would be 3 brand steps from the gate.)

OUTPUT RULES:
- "Humira OR Enbrel" = 1 branded step (OR within a step is not a path choice)
- "Previously received a biologic or targeted synthetic" — counts depend on
  Path A/Path B connector (see Worked Example 6):
    explicit OR connector with Path B → 0 brand step (history check only)
    implicit (no explicit OR) → 1 brand step (additive with Path B)
- "methotrexate, cyclosporine, or acitretin" presented as alternatives within
  one criterion = 1 generic step (one OR-cluster). The carve-out
  "clinical reason to avoid {{mtx, cyc, aci}}" inside the same cluster does
  NOT change the count — the cluster is still 1 step.
- TARGET DRUG EXCLUSION (Worked Example 7): if the target drug ({drug}) is
  listed in a multi-brand universal gate, subtract it from the count.
- Unnamed step ("must try a conventional") defaults to generic
- 0 branded → output "NA" (not "0")
- 0 generic → output "NA" (not "0")
- Phototherapy: "Yes" only if mandatory AND (not in any OR);
                "No"  if it appears only as an OR alternative;
                "NA"  if no step therapy at all (or phototherapy not mentioned)

Return ONLY this JSON:
{{
  "step_a_universal":           ["…", "…"],
  "step_b_indication_specific": ["…", "…"],
  "step_c_combined":            ["after AND merge"],
  "step_d_or_resolution":       "explain OR paths and the path chosen",
  "step_e_classification":      [{{"step": "…", "type": "branded|generic|phototherapy"}}],
  "steps_brands":               "count as string, or 'NA'",
  "steps_generic":              "count as string, or 'NA'",
  "step_phototherapy":          "Yes | No | NA",
  "reasoning":                  "one-line summary"
}}
"""
    response = retry(
        call_llm, prompt,
        system_prompt=SYSTEM_PROMPT_EXTRACTOR,
        max_tokens=MAX_TOKENS_PASS_3,
        model_chain=GROQ_TEXT_MODELS_FOR_REASONING,
    )
    return parse_json_safe(
        response, retry_prompt=prompt,
        retry_model_chain=GROQ_TEXT_MODELS_FOR_REASONING,
        retry_max_tokens=MAX_TOKENS_PASS_3,
        retry_system_prompt=SYSTEM_PROMPT_EXTRACTOR,
    )


# ─────────────────────────────────────────────────────────────────
# BLOCK 6 — VALIDATION (Phase 7)
# Three layers:
#  1. Format / derivation rules (always mutate in place)
#  2. Critical checks (return failures → caller reruns row, max 2)
#  3. Advisory checks (semantic contradictions, multi-brand ambiguity)
# Plus the rule-based step counter (parallel Plan B for Pass 3).
# ─────────────────────────────────────────────────────────────────


# ── Verbatim token-recall (replaces brittle substring match) ─────

# Per-field thresholds. Calibrated on D2 against Reference + manual labels
# via calibrate_verbatim_thresholds(). Defaults are conservative.
VERBATIM_THRESHOLDS = {
    "combined_step_text":        0.70,
    "reauth_requirements_text":  0.70,
    "quantity_limits_text":      0.80,  # short, less PDF noise
}


def _tokenise(text: str) -> set:
    """Lowercase, strip punctuation, return set of tokens length >= 2."""
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 2}


def token_recall(extracted: str, source: str) -> float:
    """Fraction of extracted-text tokens that also appear in source text.
    Robust to PDF ligatures, soft hyphens, whitespace variations that break
    substring matching. Returns 1.0 for empty extracted (vacuously true).
    """
    ext = _tokenise(extracted)
    if not ext:
        return 1.0
    src = _tokenise(source)
    return len(ext & src) / len(ext)


# ── CRITICAL VALIDATIONS (failure triggers rerun, max 2) ──────────

def critical_verbatim_check(params: dict, source_text: str) -> list:
    """Verify verbatim fields are token-recall-supported by source.
    Failure = likely hallucination → trigger rerun."""
    failures = []
    for field, threshold in VERBATIM_THRESHOLDS.items():
        val = params.get(field)
        if not val or str(val).strip().upper() in ("NA", ""):
            continue
        if len(str(val)) < 15:
            continue
        recall = token_recall(str(val), source_text)
        if recall < threshold:
            log_debug({
                "event": "verbatim_failed",
                "field": field,
                "recall": round(recall, 3),
                "threshold": threshold,
                "val_preview": str(val)[:200],
            })
            failures.append(field)
    return failures


def critical_step_extraction_check(params: dict) -> list:
    """Step text non-empty but BOTH counts NA → Pass 3 failed → rerun."""
    failures = []
    step_text = params.get("combined_step_text", "")
    brands = params.get("steps_brands", "NA")
    generic = params.get("steps_generic", "NA")

    if (step_text and len(step_text) > 20
            and str(step_text).upper() != "NA"
            and brands == "NA" and generic == "NA"):
        failures.append("step_count_extraction_failed")
    return failures


# ── RULE-BASED STEP COUNTER (parallel Plan B for Pass 3) ─────────

# Class-level patterns for generic steps. Without these, the counter only
# catches NAMED drugs in GENERIC_DRUGS (acitretin/cyclosporine/MTX/vtama/
# zoryve) and misses the topical-corticosteroid / NSAID / "another topical"
# requirements that are typically not named (Codex finding #7).
GENERIC_STEP_PATTERNS = [
    r"\btopical\s+(?:high[\s-]?potency\s+)?(?:cortico)?steroid",
    r"\b(?:betamethasone|clobetasol|fluocinonide|halcinonide|halobetasol|"
    r"triamcinolone|hydrocortisone|mometasone|desonide|fluticasone)\b",
    r"\b(?:calcipotriene|calcipotriol|calcitriol|tazarotene|anthralin|"
    r"crisaborole|tapinarof|roflumilast)\b",
    r"\banother\s+topical\b",
    r"\b(?:conventional|non[\s-]?biologic)\s+(?:systemic|agent|therapy|treatment|dmard)",
    r"\bone\s+(?:conventional|generic|oral|non[\s-]?biologic)\s+(?:systemic|agent|therapy)",
    r"\bNSAID(?:s)?\b",
    r"\b(?:MTX|CYC)\b",
    r"\b(?:sulfasalazine|leflunomide|hydroxychloroquine|azathioprine|"
    r"6[\s-]?mercaptopurine|6[\s-]?MP)\b",
]


def _cluster_by_or_proximity(brand_hits: set, text: str, window: int) -> list:
    """Two brand mentions within `window` chars joined by "or" → one cluster.
    Each cluster counts as ONE branded step ("Humira OR Enbrel" = 1 step)."""
    if not brand_hits:
        return []
    positions = []
    for brand in brand_hits:
        m = re.search(rf"\b{re.escape(brand.lower())}\b", text)
        if m:
            positions.append((m.start(), brand))
    positions.sort()

    clusters: list = []
    current: set = set()
    last_end = -1
    for pos, brand in positions:
        if (last_end >= 0
                and (pos - last_end) <= window
                and " or " in text[last_end:pos]):
            current.add(brand)
        else:
            if current:
                clusters.append(current)
            current = {brand}
        last_end = pos + len(brand)
    if current:
        clusters.append(current)
    return clusters


def rule_based_step_count(combined_step_text: str) -> dict:
    """Deterministic step counter — runs alongside Pass 3 on every row.

    Generic count combines (a) named-drug hits against GENERIC_DRUGS and
    (b) pattern hits against GENERIC_STEP_PATTERNS, clustered by 80-char
    proximity to avoid double-counting overlapping matches.

    Used three ways:
      1. Sanity check (agreement → high confidence)
      2. Discrepancy flag (disagree ≥2 → manual review)
      3. Hard fallback (Pass 3 LLM call fails → emit rule counts)
    """
    if not combined_step_text or str(combined_step_text).strip().upper() in ("", "NA"):
        return {"brands_rule": "NA", "generic_rule": "NA", "photo_rule": "NA"}

    text = combined_step_text.lower()

    branded_hits = {
        b for b in BRANDED_DRUGS
        if re.search(rf"\b{re.escape(b.lower())}\b", text)
    }

    # Generic: named hits + pattern hits, clustered by proximity
    generic_offsets: list = []
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

    photo_mentioned = any(t in text for t in PHOTOTHERAPY_TERMS)
    photo_in_or = bool(re.search(
        r"\bor\b[^.]{0,80}(phototherapy|puva|uvb|narrowband|light therapy)",
        text,
    ))

    brand_clusters = _cluster_by_or_proximity(branded_hits, text, window=50)

    return {
        "brands_rule":  str(len(brand_clusters)) if brand_clusters else "NA",
        "generic_rule": str(generic_count)      if generic_count   else "NA",
        "photo_rule":   "Yes" if (photo_mentioned and not photo_in_or)
                        else "No"  if photo_mentioned
                        else "NA",
    }


def reconcile_step_counts(
    llm_counts: dict,
    rule_counts: dict,
    llm_failed: bool = False,
) -> tuple:
    """Merge LLM + rule counts. Returns (final_counts, flags).

    - LLM == rule: high confidence, no flag.
    - |LLM - rule| <= 1: minor disagree, emit LLM, flag MINOR.
    - |LLM - rule| >= 2: major disagree, emit LLM, flag MAJOR (manual review).
    - LLM failed: emit rule, flag RULE_FALLBACK.
    """
    flags: list = []
    if llm_failed:
        return ({
            "steps_brands":      rule_counts["brands_rule"],
            "steps_generic":     rule_counts["generic_rule"],
            "step_phototherapy": rule_counts["photo_rule"],
        }, ["STEP_COUNT_RULE_FALLBACK"])

    def _to_int(v):
        try:
            return int(str(v))
        except Exception:
            return None

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

    if (llm_counts.get("step_phototherapy") != rule_counts.get("photo_rule")
            and rule_counts.get("photo_rule") != "NA"):
        flags.append(
            f"PHOTO_DISAGREE_llm{llm_counts.get('step_phototherapy')}"
            f"_rule{rule_counts.get('photo_rule')}"
        )

    return ({
        "steps_brands":      llm_counts.get("steps_brands", "NA"),
        "steps_generic":     llm_counts.get("steps_generic", "NA"),
        "step_phototherapy": llm_counts.get("step_phototherapy", "NA"),
    }, flags)


# ── Format / derivation rules (mutate in place) ──────────────────

def rule_reauth_required(params: dict) -> dict:
    """Param 9 — fully derived. Never asked to the LLM."""
    duration = params.get("reauth_duration_months")
    requirements = params.get("reauth_requirements_text")
    has_duration = (
        duration and str(duration).upper()
        not in ("NA", "NULL", "NONE", "N/A", "")
    )
    has_requirements = (
        requirements and str(requirements).upper()
        not in ("NA", "NULL", "NONE", "N/A", "")
    )
    params["reauth_required"] = "Yes" if (has_duration or has_requirements) else "No"
    return params


def rule_auth_duration(params: dict) -> dict:
    """Initial auth: always required for PsO in this dataset → must be a
    number or "Unspecified". Never blank or NA."""
    duration = params.get("initial_auth_duration_months")
    if not duration or str(duration).strip().upper() in ("", "NULL", "NONE", "NA", "N/A"):
        params["initial_auth_duration_months"] = "Unspecified"
    return params


def rule_quantity_limits_strict(params: dict) -> dict:
    """Quantity limits — label-aware validation.

    Accept ANY of the common label variants Groq has seen in payer PDFs:
      - "Quantity Limit" / "Quantity Limits"
      - "Quantity Level Limit" / "Quantity Level Limits"   ← the one we missed
      - "Quantity Limit Program"
      - "QLL"
      - "Quantity Restriction(s)"

    Reject only when the text is EXCLUSIVELY about dosing (a dose schedule
    or administration instruction without per-day/per-package quantity
    info) or when it's the generic statement "Quantity limits exist" with
    no drug-specific specifics.

    The Pass 1 prompt should have produced an "NA" already if no quantity
    label was found; this rule guards against false positives where the
    LLM accidentally captured a dosing block.
    """
    ql = params.get("quantity_limits_text", "")
    if not ql or str(ql).upper() == "NA":
        return params

    ql_lower = str(ql).lower()
    ACCEPT_LABEL_PATTERNS = (
        "quantity limit",        # covers "quantity limit", "quantity limits", "quantity limit program"
        "quantity level limit",  # covers "quantity level limit(s)"
        "quantity restriction",  # covers "quantity restriction(s)"
        "qll",
    )
    REJECT_IF_ONLY_LABEL = (
        "dosage",
        "dosing limit",
        "dosing information",
        "recommended dose",
        "dose is ",
        "administration",
    )
    GENERIC_NO_SPECIFICS = "quantity limits exist"  # boilerplate phrase

    has_quantity_label = any(p in ql_lower for p in ACCEPT_LABEL_PATTERNS)
    # Generic statement without specifics is not useful as a quantity limit.
    if GENERIC_NO_SPECIFICS in ql_lower and not any(c.isdigit() for c in ql_lower):
        params["quantity_limits_text"] = "NA"
        params.setdefault("_warnings", []).append("quantity_limit_too_generic")
        return params

    # Only reject if the text has dosing language AND no quantity-limit
    # variant label at all.
    if (any(t in ql_lower for t in REJECT_IF_ONLY_LABEL)
            and not has_quantity_label):
        params["quantity_limits_text"] = "NA"
        params.setdefault("_warnings", []).append("quantity_limit_rejected_dosing_label")

    return params


def rule_age_format(params: dict) -> dict:
    """Age — locks to the §A sentinel contract.
    Outputs: ">=N" | "FDA approved age" | "NA".
    """
    age = params.get("age", "")
    if not age or str(age).strip().upper() in ("", "NULL", "NONE", "NA", "N/A", "NO"):
        params["age"] = "NA"
    elif "fda" in str(age).lower() and not any(c.isdigit() for c in str(age)):
        params["age"] = "FDA approved age"
    elif str(age)[0].isdigit():
        params["age"] = f">={age}"
    # Already correct format (">=N" or "FDA approved age") → leave alone
    return params


def rule_step_na_format(params: dict) -> dict:
    """Step counts of 0 must output "NA" not "0" (per §A sentinel contract)."""
    for field in ("steps_brands", "steps_generic"):
        val = params.get(field)
        if val in (0, "0", 0.0):
            params[field] = "NA"
    return params


# ── ADVISORY: semantic contradictions ─────────────────────────────

def semantic_contradiction_checks(params: dict, source_text: str = "") -> list:
    """Advisory rules detecting internal contradictions. No rerun — tag
    rows for manual review."""
    warnings: list = []

    reauth_req = params.get("reauth_required", "")
    reauth_dur = params.get("reauth_duration_months")
    if (reauth_req == "No" and reauth_dur
            and str(reauth_dur).upper() not in ("NA", "NULL", "NONE", "N/A", "")):
        warnings.append("REAUTH_CONTRADICTION")

    step_text = (params.get("combined_step_text") or "").lower()
    brands = params.get("steps_brands", "NA")
    if step_text and step_text != "na" and brands == "NA":
        brand_in_text = any(
            re.search(rf"\b{re.escape(b.lower())}\b", step_text)
            for b in BRANDED_DRUGS
        )
        if brand_in_text:
            warnings.append("BRAND_MISSED")

    tb = params.get("tb_test_required", "")
    if tb == "Yes" and source_text:
        if re.search(
            r"no\s+tb\s+testing|tb\s+test(ing)?\s+not\s+required",
            source_text.lower(),
        ):
            warnings.append("TB_CONTRADICTION")

    age = str(params.get("age", ""))
    m = re.search(r">=(\d+)", age)
    if m:
        n = int(m.group(1))
        if n < 1 or n > 99:
            warnings.append("AGE_OUT_OF_RANGE")

    spec = (params.get("specialist_types") or "").lower()
    if spec and spec != "na":
        if any(w in spec for w in ("appropriate", "qualified", "licensed prescriber")):
            warnings.append("SPECIALIST_VAGUE")

    init_auth = params.get("initial_auth_duration_months", "")
    try:
        if init_auth and str(init_auth).upper() != "UNSPECIFIED":
            d = int(str(init_auth))
            if d < 1 or d > 24:
                warnings.append("AUTH_DURATION_OUTLIER")
    except ValueError:
        pass

    return warnings


# ── ADVISORY: multi-brand ambiguity (§11.13) ─────────────────────

def multi_brand_ambiguity_check(
    params: dict,
    source_text: str,
    target_drug: str,
) -> list:
    """For at-risk per-drug fields (Age, Quantity Limits, Specialist Types),
    scan a window around the extracted value in source. If ANOTHER brand
    name appears in that window, flag MULTI_BRAND_AMBIGUOUS_<field>.
    Doesn't auto-rerun — surfaces ambiguous rows for manual review.
    """
    warnings: list = []
    if not source_text:
        return warnings

    text_lower = source_text.lower()
    target_aliases = set(get_drug_aliases(target_drug))
    other_brands = (BRANDED_DRUGS | GENERIC_DRUGS) - target_aliases

    risk_fields = {
        "age":                  300,
        "quantity_limits_text": 500,
        "specialist_types":     300,
    }

    for field, window in risk_fields.items():
        val = params.get(field)
        if not val or str(val).strip().upper() in ("NA", "", "FDA APPROVED AGE"):
            continue

        if field == "age":
            m = re.search(r">=(\d+)", str(val))
            if not m:
                continue
            n = m.group(1)
            for found in re.finditer(rf"\b{n}\b", text_lower):
                start = max(0, found.start() - window // 2)
                end = min(len(text_lower), found.end() + window // 2)
                snip = text_lower[start:end]
                if not any(kw in snip for kw in ("age", "years", "year of")):
                    continue
                if any(re.search(rf"\b{re.escape(b)}\b", snip) for b in other_brands):
                    warnings.append(f"MULTI_BRAND_AMBIGUOUS_age")
                    break
        else:
            needle = str(val)[:50].lower().strip()
            if len(needle) < 10:
                continue
            idx = text_lower.find(needle)
            if idx < 0:
                continue
            start = max(0, idx - window // 2)
            end = min(len(text_lower), idx + len(needle) + window // 2)
            snip = text_lower[start:end]
            if any(re.search(rf"\b{re.escape(b)}\b", snip) for b in other_brands):
                warnings.append(f"MULTI_BRAND_AMBIGUOUS_{field}")

    return warnings


def calibrate_verbatim_thresholds(validation_set: list) -> None:
    """D2 calibration. After Pass 2 runs on Reference + spot-check rows,
    set per-field threshold to the 5th percentile of token-recall across
    known-good extractions. If < 3 samples: leave defaults.
    """
    global VERBATIM_THRESHOLDS
    if len(validation_set) < 3:
        log_debug({
            "event": "verbatim_calibration_skipped",
            "reason": "too few rows",
            "rows": len(validation_set),
        })
        return

    for field in list(VERBATIM_THRESHOLDS.keys()):
        recalls = [
            token_recall(row.get(field, ""), row.get("_source_text", ""))
            for row in validation_set
            if row.get(field) and str(row.get(field)).upper() != "NA"
        ]
        if len(recalls) < 3:
            continue
        q = quantiles(recalls, n=20)  # q[0] is the 5th percentile
        new_threshold = max(0.5, round(q[0], 2))
        log_debug({
            "event": "verbatim_calibrated",
            "field": field,
            "samples": len(recalls),
            "new_threshold": new_threshold,
            "previous": VERBATIM_THRESHOLDS[field],
        })
        VERBATIM_THRESHOLDS[field] = new_threshold


# ── Integrator ────────────────────────────────────────────────────

def validate_all(
    params: dict,
    source_text: str = "",
    target_drug: str = "",
) -> tuple:
    """Run all validation. Returns (validated_params, critical_failures).

    Layers:
      1. Format/derivation rules (always mutate in place)
      2. Critical checks (return failures → caller reruns row)
      3. Semantic contradiction checks (advisory — flag only)
      4. Multi-brand ambiguity checks (advisory — flag only)
    """
    params = rule_reauth_required(params)
    params = rule_auth_duration(params)
    params = rule_quantity_limits_strict(params)
    params = rule_age_format(params)
    params = rule_step_na_format(params)

    critical_failures: list = []
    if source_text:
        critical_failures.extend(critical_verbatim_check(params, source_text))
    critical_failures.extend(critical_step_extraction_check(params))

    warnings = semantic_contradiction_checks(params, source_text)
    params.setdefault("_warnings", []).extend(warnings)

    if target_drug and source_text:
        params["_warnings"].extend(
            multi_brand_ambiguity_check(params, source_text, target_drug)
        )

    return params, critical_failures


# ─────────────────────────────────────────────────────────────────
# BLOCK 7 — ACCESS SCORE (Phase 9)
# Pure-Python, deterministic, zero LLM calls.
#
# Implements partner spec §07_ACCESS_SCORE_COMPLETE + delta update:
#   Layer A — Pre-checks (run BEFORE FDA comparison):
#     1. apply_reauth_inference()        — Rules 1 & 2 normalise null + No
#     2. PRODUCT_NOT_FOUND short-circuit — all 6 FDA fields null → bucket 0
#     3. INSUFFICIENT_DATA short-circuit — only 0-1 FDA fields known → 25
#
#   Layer B — Six FDA-baseline comparisons (P1/P3/P4/P5/P6/P11):
#     - compare_age handles "FDA labelled/approved age" → EQUIVALENT
#     - compare_brand_steps / generic_steps / phototherapy / tb_test / specialist
#     - Each returns (state, severity)
#
#   Layer C — Three access-modifier features (run AFTER FDA aggregation):
#     - Feature A: initial_auth_duration_months
#     - Feature B: reauth_required (post-inference)
#     - Feature C: reauth_duration_months (only when B == Yes)
#     - Consistency check: reauth_dur < initial_dur / 2 → +1 minor
#
#   Layer D — Bucket assignment (existing rules from §07 spec):
#     - First-match-wins ladder over Severe/Major/Moderate/Minor/Improvement
#     - Returns one of {0, 15, 25, 50, 75, 100}
#
# FDA baselines fetched from openFDA at startup, cached to
# fda_baselines_cache.json. SILIQ-style API errors handled gracefully.
# ─────────────────────────────────────────────────────────────────


# ── FDA baseline cache + API ─────────────────────────────────────

FDA_API_BASE = "https://api.fda.gov/drug/label.json"
FDA_BASELINE_CACHE_PATH = Path(
    os.environ.get("PIPELINE_FDA_CACHE_PATH", "fda_baselines_cache.json")
)
FDA_BASELINE: dict = {}  # populated by load_all_fda_baselines() at startup


def _fetch_openfda_baseline(brand_name: str) -> Optional[dict]:
    """Try openFDA first. Returns parsed baseline dict tagged with
    source='openfda', or None on any failure mode (missing/network/error)."""
    try:
        import urllib.parse
        import urllib.request

        url = (
            f"{FDA_API_BASE}?search="
            f"openfda.brand_name:%22{urllib.parse.quote(brand_name)}%22&limit=1"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "pso-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # SILIQ-style: top-level "error" key, no meta
        if "error" in data:
            log_debug({
                "event": "fda_api_no_result",
                "drug": brand_name,
                "source": "openfda",
                "error_code": data["error"].get("code"),
                "error_message": data["error"].get("message"),
            })
            return None

        if data.get("meta", {}).get("results", {}).get("total", 0) == 0:
            log_debug({"event": "fda_api_no_result", "drug": brand_name, "source": "openfda"})
            return None

        result = parse_fda_label(data["results"][0], brand_name)
        result["source"] = "openfda"
        return result

    except Exception as e:
        log_debug({"event": "fda_api_error", "drug": brand_name, "source": "openfda", "error": str(e)})
        return None


# DailyMed API — NIH public service, used as fallback when openFDA has no
# label for a brand (e.g. SILIQ as of 2026-05). Two-step protocol:
#   1. Search by drug_name → JSON list of (setid, title, spl_version)
#   2. Fetch SPL XML by setid → parse INDICATIONS / DESCRIPTION / effectiveTime
# Output is mapped to the same openFDA-shaped dict that parse_fda_label()
# consumes, so the rest of the access-score pipeline is identical.
DAILYMED_SEARCH_BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json"
DAILYMED_SPL_BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls"
_DAILYMED_NS = "{urn:hl7-org:v3}"

# LOINC section codes we care about in DailyMed SPL XML.
_LOINC_INDICATIONS = "34067-9"   # 1 INDICATIONS AND USAGE
_LOINC_DESCRIPTION = "34089-3"   # 11 DESCRIPTION (used for pharm_class hint)


def _extract_pharm_class_from_description(desc_text: str) -> Optional[str]:
    """Pull a pharm-class-like string from the DESCRIPTION/INDICATIONS text.
    Looks for the canonical '... interleukin-X [receptor Y] antagonist'
    phrasing in biologic labels — accepts optional parenthetical
    abbreviation (e.g. '(IL-17RA)') between the receptor letter and the
    word 'antagonist'. Returns 'Interleukin-17 Receptor A Antagonist [EPC]'
    style string, or None if no match."""
    m = re.search(
        r"(interleukin[-\s][\w\-]+(?:\s+receptor\s+[A-Za-z]+)?)"
        r"(?:\s*\([^)]{0,30}\))?"   # optional "(IL-17RA)"-style abbrev
        r"\s+antagonist",
        desc_text, re.IGNORECASE,
    )
    if not m:
        return None
    base = re.sub(r"\s+", " ", m.group(1).strip())
    # Title-case while keeping single-letter receptor codes (A/B/C) upper.
    parts = []
    for tok in re.split(r"(\s+)", base):
        if tok.strip() == "":
            parts.append(tok)
        elif tok.lower() in ("a", "b", "c"):
            parts.append(tok.upper())
        else:
            parts.append(tok[:1].upper() + tok[1:].lower())
    return f"{''.join(parts)} Antagonist [EPC]"


def _fetch_dailymed_baseline(brand_name: str) -> Optional[dict]:
    """Fallback to DailyMed when openFDA has no label. Two API calls:
    search → pick best match → fetch SPL XML → map to openFDA shape →
    parse_fda_label(). Returns dict tagged with source='dailymed', or None.
    """
    try:
        import urllib.parse
        import urllib.request
        import xml.etree.ElementTree as ET

        # --- Step 1: search by drug name ---
        search_url = (
            f"{DAILYMED_SEARCH_BASE}"
            f"?drug_name={urllib.parse.quote(brand_name)}&pagesize=10"
        )
        req = urllib.request.Request(search_url, headers={"User-Agent": "pso-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            search_data = json.loads(resp.read().decode("utf-8"))

        rows = search_data.get("data") or []
        if not rows:
            log_debug({
                "event": "dailymed_no_result",
                "drug": brand_name,
                "reason": "empty_data",
            })
            return None

        # Filter: title must (a) contain the brand and (b) look like a drug
        # label — has "(GENERIC_NAME)" pattern. This excludes false positives
        # like "G-11 (CERCIS SILIQUASTRUM WHOLE)" homonym for SILIQ search.
        brand_upper = brand_name.upper().strip()
        candidates = []
        for row in rows:
            title = (row.get("title") or "").upper()
            if brand_upper not in title:
                continue
            # Look for "(SOMETHING)" — the INN in parens
            gen_match = re.search(r"\(([A-Z][A-Z0-9\-]{3,})\)", title)
            if not gen_match:
                continue
            # Additional sanity: brand should appear before the parens
            # (the brand is the first token in real drug titles)
            paren_pos = title.find("(")
            if paren_pos > 0 and brand_upper not in title[:paren_pos + len(brand_upper) + 2]:
                continue
            candidates.append((row, gen_match.group(1)))

        if not candidates:
            log_debug({
                "event": "dailymed_no_result",
                "drug": brand_name,
                "reason": "no_matching_title",
                "n_rows": len(rows),
            })
            return None

        # Take the highest spl_version (most recent label revision)
        candidates.sort(key=lambda c: c[0].get("spl_version", 0), reverse=True)
        chosen_row, generic_name = candidates[0]
        setid = chosen_row["setid"]
        title = chosen_row.get("title", "")

        # --- Step 2: fetch SPL XML ---
        xml_url = f"{DAILYMED_SPL_BASE}/{setid}.xml"
        req = urllib.request.Request(xml_url, headers={"User-Agent": "pso-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_bytes = resp.read()
        root = ET.fromstring(xml_bytes)

        # effective_time — root-level <effectiveTime value="YYYYMMDD"/>
        eff_time_raw = ""
        et_elem = root.find(f"{_DAILYMED_NS}effectiveTime")
        if et_elem is not None:
            eff_time_raw = et_elem.attrib.get("value", "")

        # Sections — walk all <section> and pick INDICATIONS + DESCRIPTION.
        # Use section.itertext() (not just <text>) so nested <excerpt>/
        # <highlight> content is included. The pharm-class phrasing
        # ("human interleukin-X receptor Y antagonist") lives in the
        # excerpt/highlight under INDICATIONS, not the main <text>.
        indications_text = ""
        description_text = ""
        for section in root.iter(f"{_DAILYMED_NS}section"):
            code = section.find(f"{_DAILYMED_NS}code")
            if code is None:
                continue
            section_code = code.attrib.get("code")
            if section_code == _LOINC_INDICATIONS and not indications_text:
                indications_text = re.sub(
                    r"\s+", " ",
                    "".join(section.itertext()).strip(),
                )
            elif section_code == _LOINC_DESCRIPTION and not description_text:
                description_text = "".join(section.itertext()).strip()

        # Pharm class — best-effort. Check INDICATIONS excerpt first (where
        # "human ... antagonist" phrasing typically sits), then DESCRIPTION.
        pharm_class = (
            _extract_pharm_class_from_description(indications_text)
            or _extract_pharm_class_from_description(description_text)
        )

        # Map to openFDA-shaped dict so parse_fda_label() works unchanged
        synthetic_label = {
            "indications_and_usage": [indications_text],
            "effective_time": eff_time_raw,
            "openfda": {
                "generic_name": [generic_name] if generic_name else [],
                "pharm_class_epc": [pharm_class] if pharm_class else [],
            },
        }

        result = parse_fda_label(synthetic_label, brand_name)
        result["source"] = "dailymed"
        log_debug({
            "event": "dailymed_baseline_loaded",
            "drug": brand_name,
            "setid": setid,
            "title": title,
            "spl_version": chosen_row.get("spl_version"),
        })
        return result

    except Exception as e:
        log_debug({"event": "dailymed_api_error", "drug": brand_name, "error": str(e)})
        return None


def fetch_fda_baseline(brand_name: str) -> Optional[dict]:
    """Fetch FDA label baseline for a brand. Tries openFDA first; falls
    back to DailyMed if openFDA has no data (404 / NOT_FOUND / network
    error). Returns parsed baseline dict (tagged with source field) or
    None if BOTH sources fail.
    """
    result = _fetch_openfda_baseline(brand_name)
    if result:
        return result

    log_debug({"event": "fda_fallback_dailymed_try", "drug": brand_name})
    result = _fetch_dailymed_baseline(brand_name)
    if result:
        log_debug({
            "event": "fda_fallback_dailymed_success",
            "drug": brand_name,
            "min_age": result.get("min_age"),
            "inn": result.get("inn"),
        })
        return result

    log_debug({"event": "fda_both_sources_failed", "drug": brand_name})
    return None


# Other major indication keywords. If we can't find a PsO subsection and any
# of these are mentioned, we refuse to guess an age — the label is multi-
# indication and a bare regex would grab a different indication's age.
_OTHER_INDICATIONS_PATTERN = re.compile(
    r"\b(?:psoriatic\s+arthritis|ulcerative\s+colitis|crohn|"
    r"rheumatoid\s+arthritis|ankylosing|juvenile|uveitis|"
    r"hidradenitis|atopic\s+dermatitis|polyarticular|"
    r"non[-\s]radiographic|axial\s+spondyloarthritis)\b",
    re.IGNORECASE,
)


def _extract_pso_indication_section(indications: str) -> Optional[str]:
    """Slice indications_and_usage to the Plaque-Psoriasis-specific subsection.

    FDA labels list multiple indications in one big text block, each under
    a numbered subsection (1.1, 1.2, ...). Naive regex on the full text
    picks the FIRST age mentioned, which is often pJIA (2y) or PsA (2y),
    not PsO. We must bound to the PsO subsection before extracting age.

    Strategy:
      1. Find "X.Y ... Plaque Psoriasis" section header (lookahead/behind
         excludes "( 1.1 )" summary cross-references).
      2. Slice from header start to the NEXT "X.Y" section start (or EOF).
      3. If no PsO header found AND the label is single-indication, return
         the full text. If multi-indication without a PsO section header,
         return None — safer than picking the wrong age.
    """
    # PsO section header: "X.Y [optional title prefix] Plaque Psoriasis"
    # Negative lookbehind for "(" and lookahead for non-")" filter out the
    # "( 1.1 )" cross-references in the summary preamble.
    pso_header = re.search(
        r"(?<!\()\b\d+\.\d+\s+(?!\))[^.]{0,80}?Plaque\s+Psoriasis\b",
        indications, re.IGNORECASE,
    )

    if not pso_header:
        # No PsO subsection header. Two valid cases:
        #   (a) Single-indication PsO label — use full text.
        #   (b) Multi-indication label where PsO didn't get a subsection
        #       header (shouldn't happen for current FDA-PsO-approved
        #       biologics) → refuse to guess.
        if not _OTHER_INDICATIONS_PATTERN.search(indications):
            return indications
        return None

    pso_start = pso_header.start()
    # Bound by the NEXT numbered section header (same exclusion rules).
    next_section = re.search(
        r"(?<!\()\s+\d+\.\d+\s+(?!\))[A-Z]",
        indications[pso_header.end():],
    )
    if next_section:
        pso_end = pso_header.end() + next_section.start()
        return indications[pso_start:pso_end]
    return indications[pso_start:]


def _extract_age_from_pso_section(pso_section: str) -> Optional[int]:
    """Extract minimum age from a PsO-bounded indication section.

    Tries multiple phrasing patterns, then falls back to 18 if the section
    explicitly says 'adult' without any pediatric mention (FDA approved for
    adults only → comparison baseline should be 18, not None/UNKNOWN)."""
    age_patterns = [
        # "6 years of age and older" / "4 years of age or older"  (TREMFYA, ENBREL)
        r"(\d+)\s+years?\s+of\s+age\s+(?:and|or)\s+older",
        # "6 years and older"  (COSENTYX — drops "of age")
        r"(\d+)\s+years?\s+(?:and|or)\s+older",
        # "pediatric patients X years"  (rare variant)
        r"pediatric\s+patients\s+(?:aged\s+)?(\d+)\s+years?",
        # "age X years and older"  (defensive)
        r"\bage[ds]?\s+(\d+)\s+years?\s+(?:and|or)\s+older",
    ]
    for pat in age_patterns:
        m = re.search(pat, pso_section, re.IGNORECASE)
        if m:
            return int(m.group(1))

    # Adult-only fallback: section mentions "adult" but no pediatric/years
    # → FDA approves adults only → baseline is >=18.
    has_adult = re.search(
        r"\badults?\b|\badult\s+(?:patients|members)\b",
        pso_section, re.IGNORECASE,
    )
    has_pediatric = re.search(
        r"\bpediatric\b|\bchildren\b|\bage[ds]?\s+\d+\b|\b\d+\s+years?\b",
        pso_section, re.IGNORECASE,
    )
    if has_adult and not has_pediatric:
        return 18
    return None


def _extract_weight_from_pso_section(pso_section: str) -> Optional[int]:
    """Same PsO-section bounding for weight. Avoids picking up a different
    indication's weight condition (e.g. some labels have weight cutoffs
    only for non-PsO indications)."""
    m = re.search(
        r"weigh(?:ing|s)?\s+at\s+least\s+(\d+)\s*kg",
        pso_section, re.IGNORECASE,
    )
    return int(m.group(1)) if m else None


def parse_fda_label(label: dict, brand_name: str) -> dict:
    """Parse raw FDA label into baseline dict.

    Critical: extract min_age + min_weight from the PLAQUE-PSORIASIS-SPECIFIC
    subsection only, not the first match in the full indications_and_usage
    text. Multi-indication labels list PsA / pJIA / UC / CD ages alongside
    PsO; naive regex picks whichever appears first (usually 2y from pJIA or
    PsA), giving wrong PsO baseline.

    Verified ages after fix (vs raw FDA label PsO subsections):
      TREMFYA=6, STELARA=6, ENBREL=4, COSENTYX=6, AMJEVITA=18 (adult-only),
      SKYRIZI=18, ILUMYA=18, BIMZELX=18, OTEZLA=6, REMICADE=18 (adult-only),
      CIMZIA=18 (adult-only), YESINTEK=6, OTULFI=6, ACITRETIN=None.
    """
    indications = (label.get("indications_and_usage") or [""])[0]
    eff_time = label.get("effective_time", "")

    # YYYYMMDD → YYYY-MM-DD
    eff_date = None
    if isinstance(eff_time, str) and len(eff_time) == 8 and eff_time.isdigit():
        eff_date = f"{eff_time[:4]}-{eff_time[4:6]}-{eff_time[6:]}"

    # PsO-bounded age + weight (the actual fix — see helper docstrings)
    pso_section = _extract_pso_indication_section(indications)
    if pso_section is not None:
        min_age = _extract_age_from_pso_section(pso_section)
        min_weight = _extract_weight_from_pso_section(pso_section)
    else:
        min_age = None
        min_weight = None
        log_debug({
            "event": "fda_label_no_pso_section",
            "drug": brand_name,
            "indications_chars": len(indications),
        })

    # Severity — handle "moderate-to-severe" OR "moderate to severe"
    severity = None
    section_for_severity = pso_section or indications
    if re.search(r"moderate[-\s]to[-\s]severe", section_for_severity, re.IGNORECASE):
        severity = "moderate-to-severe"

    openfda = label.get("openfda", {})
    return {
        "brand_name":                  brand_name,
        "min_age":                     min_age,
        "min_weight_kg":               min_weight,
        "indication_severity":         severity,
        "brand_steps":                 0,     # FDA never requires steps
        "generic_steps":               0,
        "phototherapy_required":       False,
        "tb_test_required_as_pa_gate": False,  # FDA recommends evaluation, not PA gate
        "tb_evaluation_recommended":   True,
        "specialist_restriction":      None,
        "label_effective_date":        eff_date,
        "inn":         (openfda.get("generic_name")    or [None])[0],
        "pharm_class": (openfda.get("pharm_class_epc") or [None])[0],
    }


def load_all_fda_baselines(brands: list) -> dict:
    """Load FDA baselines for the given brand list. Disk-cached:
    fda_baselines_cache.json on second run avoids any network calls.

    Brands that fail to fetch are NOT cached as null — they are simply
    absent from the returned dict. This lets future runs retry on
    transient failures.
    """
    if FDA_BASELINE_CACHE_PATH.exists():
        try:
            with open(FDA_BASELINE_CACHE_PATH) as f:
                cached = json.load(f)
            log_debug({
                "event": "fda_baseline_cache_loaded",
                "path": str(FDA_BASELINE_CACHE_PATH),
                "n_brands": len(cached),
            })
            return cached
        except Exception as e:
            log_debug({"event": "fda_baseline_cache_corrupt", "error": str(e)})

    baselines: dict = {}
    for brand in sorted({b.upper().strip() for b in brands if b}):
        baseline = fetch_fda_baseline(brand)
        if baseline:
            baselines[brand] = baseline
        else:
            log_debug({"event": "fda_baseline_missing", "drug": brand})

    try:
        with open(FDA_BASELINE_CACHE_PATH, "w") as f:
            json.dump(baselines, f, indent=2)
        log_debug({
            "event": "fda_baseline_cache_written",
            "n_brands": len(baselines),
        })
    except Exception as e:
        log_debug({"event": "fda_baseline_cache_write_failed", "error": str(e)})

    return baselines


# ── Null / value helpers ─────────────────────────────────────────

_NULL_TOKENS = {"NA", "N/A", "NONE", "NULL", "", "UNSPECIFIED"}


def _is_null_value(v) -> bool:
    """Treat NA / NULL / empty / Unspecified / None as null for access score.
    Note: 'Unspecified' counts as null for duration features (we don't know
    the number), but treated as KNOWN for the PolicyCoverageCount check
    when applied to text fields — that gating is done elsewhere."""
    if v is None:
        return True
    s = str(v).strip().upper()
    return s in _NULL_TOKENS


def _to_int_or_none(v) -> Optional[int]:
    """Parse integer from a value that may include 'months', '>=', '12.0', etc.
    Returns None for null tokens or unparseable values."""
    if _is_null_value(v):
        return None
    s = str(v).strip()
    m = re.search(r"-?\d+", s)
    if not m:
        return None
    try:
        return int(m.group())
    except ValueError:
        return None


def _parse_age_value(age_str) -> Optional[int]:
    """Extract numeric age from '>=18' / '6 years or older' format.
    Returns None for null tokens OR for 'FDA labelled/approved age' (the
    caller handles that special case separately)."""
    if _is_null_value(age_str):
        return None
    s = str(age_str).strip().upper()
    if "FDA" in s and ("AGE" in s or "LABEL" in s or "APPROV" in s):
        return None
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def _is_fda_labelled_age(age_str) -> bool:
    """Per delta UPDATE 3: policy_age 'FDA labelled age' / 'FDA approved age'
    means the policy defers to FDA → comparison must be EQUIVALENT."""
    if _is_null_value(age_str):
        return False
    s = str(age_str).strip().upper()
    return ("FDA" in s
            and ("LABEL" in s or "APPROV" in s)
            and "AGE" in s)


# ── Layer A — Reauth inference + pre-check short-circuits ───────

def apply_reauth_inference(params: dict) -> dict:
    """Delta UPDATE: reauthorization inference rules. Applied BEFORE any
    Feature B evaluation, mutates a COPY and returns it.

      Rule 1: ReauthRequired NULL + ReauthDuration NOT NULL → Yes
              (presence of a duration implies reauth exists)
      Rule 2: ReauthRequired = No + ReauthDuration NOT NULL → Yes (override)
              (duration is stronger evidence than the No flag)
    """
    out = dict(params)
    req = out.get("reauth_required")
    dur = out.get("reauth_duration_months")

    req_null = _is_null_value(req)
    dur_null = _is_null_value(dur)
    req_no = (not req_null) and str(req).strip().lower() == "no"

    if req_null and not dur_null:
        out["reauth_required"] = "Yes"
        out["_reauth_inferred_rule"] = "RULE_1_NULL_TO_YES"
    elif req_no and not dur_null:
        out["reauth_required"] = "Yes"
        out["_reauth_inferred_rule"] = "RULE_2_NO_OVERRIDDEN_TO_YES"
    return out


# Fields participating in the policy-coverage-count check (the six FDA
# comparison fields). Order matters for reasoning output only.
FDA_COMPARISON_FIELDS = (
    "age",
    "steps_brands",
    "steps_generic",
    "step_phototherapy",
    "tb_test_required",
    "specialist_types",
)


def count_policy_coverage_fields(params: dict) -> int:
    """Delta UPDATE 1 + 2: count how many of the 6 FDA-comparison fields
    are non-null in the extracted params.

    Note: 'No' counts as a real value (e.g. tb_test_required='No' is the
    policy affirmatively saying no TB test is required → it IS known)."""
    count = 0
    for field in FDA_COMPARISON_FIELDS:
        if not _is_null_value(params.get(field)):
            count += 1
    return count


# ── Layer B — The six FDA-baseline comparisons ──────────────────

def compare_age(fda_age, policy_age) -> tuple:
    """P1 — age comparison with delta UPDATE 3.

    If policy says 'FDA labelled age' → EQUIVALENT regardless of FDA age.
    Else numeric delta = policy - FDA:
      delta <= 0 → EQUIVALENT or LESS_RESTRICTIVE
      0 < delta <= 4  → MORE_RESTRICTIVE / MINOR
      5 <= delta <= 9 → MORE_RESTRICTIVE / MODERATE
      delta >= 10     → MORE_RESTRICTIVE / MAJOR
    """
    # Delta UPDATE 3: FDA labelled age → EQUIVALENT
    if _is_fda_labelled_age(policy_age):
        return "EQUIVALENT", None

    if fda_age is None or _is_null_value(policy_age):
        return "UNKNOWN", None

    fda_num = fda_age if isinstance(fda_age, int) else _parse_age_value(fda_age)
    policy_num = _parse_age_value(policy_age)
    if fda_num is None or policy_num is None:
        return "UNKNOWN", None

    delta = policy_num - fda_num
    if delta > 0:
        if delta <= 4:   sev = "MINOR"
        elif delta <= 9: sev = "MODERATE"
        else:            sev = "MAJOR"
        return "MORE_RESTRICTIVE", sev
    elif delta == 0:
        return "EQUIVALENT", None
    return "LESS_RESTRICTIVE", None


def _compare_step_count(fda_steps, policy_steps, severity_ladder) -> tuple:
    """Shared logic for brand + generic step comparisons.
    severity_ladder maps delta -> severity, with the third entry used
    for delta >= 3."""
    fda_n = 0 if _is_null_value(fda_steps) else (_to_int_or_none(fda_steps) or 0)
    policy_n = 0 if _is_null_value(policy_steps) else (_to_int_or_none(policy_steps) or 0)

    delta = policy_n - fda_n
    if delta > 0:
        if delta == 1:   sev = severity_ladder[0]
        elif delta == 2: sev = severity_ladder[1]
        else:            sev = severity_ladder[2]
        return "MORE_RESTRICTIVE", sev
    elif delta == 0:
        return "EQUIVALENT", None
    return "LESS_RESTRICTIVE", None


def compare_brand_steps(fda_steps, policy_steps) -> tuple:
    """P3 — branded step count.
      delta 1 → MODERATE, 2 → MAJOR, >=3 → SEVERE."""
    return _compare_step_count(
        fda_steps, policy_steps,
        severity_ladder=("MODERATE", "MAJOR", "SEVERE"),
    )


def compare_generic_steps(fda_steps, policy_steps) -> tuple:
    """P4 — generic step count.
      delta 1 → MINOR, 2 → MODERATE, >=3 → MAJOR."""
    return _compare_step_count(
        fda_steps, policy_steps,
        severity_ladder=("MINOR", "MODERATE", "MAJOR"),
    )


def compare_phototherapy(fda_val, policy_val) -> tuple:
    """P5 — phototherapy step. Yes/No only. Null on either side → UNKNOWN."""
    if _is_null_value(fda_val) or _is_null_value(policy_val):
        # FDA baseline always has phototherapy_required=False for PsO → not null
        # but defensively handle a None FDA.
        if _is_null_value(policy_val):
            return "UNKNOWN", None

    fda_yes = (fda_val is True) or (str(fda_val).strip().lower() == "yes")
    policy_yes = str(policy_val).strip().lower() == "yes"

    if not fda_yes and policy_yes:
        return "MORE_RESTRICTIVE", "MODERATE"
    elif fda_yes and not policy_yes:
        return "LESS_RESTRICTIVE", None
    return "EQUIVALENT", None


def compare_tb_test(fda_val, policy_val) -> tuple:
    """P6 — TB test as PA gate.
    FDA recommends evaluation but does NOT make it a PA gate, so
    policy=Yes → MORE_RESTRICTIVE MINOR."""
    if _is_null_value(policy_val):
        return "UNKNOWN", None
    fda_yes = (fda_val is True) or (str(fda_val).strip().lower() == "yes")
    policy_yes = str(policy_val).strip().lower() == "yes"

    if not fda_yes and policy_yes:
        return "MORE_RESTRICTIVE", "MINOR"
    elif fda_yes and not policy_yes:
        return "LESS_RESTRICTIVE", None
    return "EQUIVALENT", None


def compare_specialist(fda_val, policy_val) -> tuple:
    """P11 — specialist type restriction. Presence vs absence only;
    dermatologist vs rheumatologist treated equally (both are restrictions)."""
    if _is_null_value(policy_val):
        # If policy explicitly says NA/None, that means no restriction.
        policy_has = False
    else:
        policy_has = True

    fda_has = (
        fda_val is not None and not _is_null_value(fda_val)
        and str(fda_val).strip().lower() not in ("false", "no")
    )

    if not fda_has and policy_has:
        return "MORE_RESTRICTIVE", "MINOR"
    elif fda_has and not policy_has:
        return "LESS_RESTRICTIVE", None
    return "EQUIVALENT", None


def aggregate_severity_counts(comparisons: dict) -> dict:
    """Tally MORE_RESTRICTIVE severities + improvement + unknown counts."""
    counts = {
        "severe": 0, "major": 0, "moderate": 0,
        "minor": 0, "improvement": 0, "unknown": 0,
    }
    for _, (state, severity) in comparisons.items():
        if state == "MORE_RESTRICTIVE" and severity:
            counts[severity.lower()] = counts.get(severity.lower(), 0) + 1
        elif state == "LESS_RESTRICTIVE":
            counts["improvement"] += 1
        elif state == "UNKNOWN":
            counts["unknown"] += 1
    return counts


# ── Layer C — Access modifier features (A, B, C) + consistency ──

def _classify_duration_months(months: Optional[int]) -> str:
    """Shared bucket for Feature A and Feature C:
      >= 12 → IMPROVEMENT
      6-11  → NEUTRAL
      < 6   → MINOR_RESTRICTION
      None  → UNKNOWN
    """
    if months is None:
        return "UNKNOWN"
    if months >= 12:
        return "IMPROVEMENT"
    if months >= 6:
        return "NEUTRAL"
    return "MINOR_RESTRICTION"


def evaluate_access_modifiers(params: dict) -> dict:
    """Delta NEW: features A/B/C + consistency check.

    Assumes apply_reauth_inference() has already been called on params.
    Returns dict with per-feature outcome + roll-up counts to add to the
    main severity counters.
    """
    initial_dur = _to_int_or_none(params.get("initial_auth_duration_months"))
    reauth_dur = _to_int_or_none(params.get("reauth_duration_months"))
    reauth_req_raw = params.get("reauth_required")
    reauth_req_null = _is_null_value(reauth_req_raw)
    reauth_req_yes = (not reauth_req_null) and str(reauth_req_raw).strip().lower() == "yes"
    reauth_req_no = (not reauth_req_null) and str(reauth_req_raw).strip().lower() == "no"

    # Feature A — Initial Authorization Duration
    feature_a = _classify_duration_months(initial_dur)

    # Feature B — Reauthorization Required
    if reauth_req_no:
        feature_b = "IMPROVEMENT"
    elif reauth_req_yes:
        feature_b = "EVALUATE_C"
    else:
        feature_b = "UNKNOWN"

    # Feature C — Reauthorization Duration (only when B == EVALUATE_C)
    if feature_b == "EVALUATE_C":
        feature_c = _classify_duration_months(reauth_dur)
    else:
        feature_c = "NOT_APPLICABLE"

    # Consistency check — applies only when BOTH durations are real numbers.
    # Reauth < Initial/2 → +1 minor restriction.
    consistency_penalty = 0
    if initial_dur is not None and reauth_dur is not None:
        if reauth_dur < initial_dur / 2:
            consistency_penalty = 1

    # Roll up to severity counters
    improvements = 0
    minor_restrictions = 0
    for outcome in (feature_a, feature_b, feature_c):
        if outcome == "IMPROVEMENT":
            improvements += 1
        elif outcome == "MINOR_RESTRICTION":
            minor_restrictions += 1
    minor_restrictions += consistency_penalty

    return {
        "feature_a": feature_a,
        "feature_b": feature_b,
        "feature_c": feature_c,
        "consistency_penalty": consistency_penalty,
        "improvements": improvements,
        "minor_restrictions": minor_restrictions,
    }


# ── Layer D — Bucket assignment ─────────────────────────────────

def assign_bucket(counts: dict) -> int:
    """First-match-wins ladder over the existing §07 spec."""
    severe      = counts.get("severe", 0)
    major       = counts.get("major", 0)
    moderate    = counts.get("moderate", 0)
    minor       = counts.get("minor", 0)
    improvement = counts.get("improvement", 0)

    # Bucket 0 — Near-impossible
    if (severe >= 1 and major >= 2) or major >= 3 or severe >= 2:
        return 0
    # Bucket 15 — Very restrictive
    if (major >= 2 or severe >= 1) or (severe >= 1 and major >= 1):
        return 15
    # Bucket 25 — Restrictive
    if major == 1 or moderate >= 2 or minor >= 3:
        return 25
    # Bucket 100 — Best access
    if (improvement >= 2 and severe == 0 and major == 0
            and moderate == 0 and minor == 0):
        return 100
    # Bucket 75 — Better than FDA
    if improvement >= 1 and severe == 0 and major == 0:
        return 75
    # Bucket 50 — FDA parity
    if severe == 0 and major == 0 and moderate == 0 and minor <= 1:
        return 50
    # Fallback — should not reach if rules are exhaustive
    return 25


# ── Master orchestrator ──────────────────────────────────────────

def compute_access_score(params: dict, drug: str) -> dict:
    """Master access-score function. Pure Python, zero LLM calls.

    Pipeline:
      0. Pull FDA baseline (cached). Missing → bucket 25.
      1. apply_reauth_inference()      — normalise reauth_required
      2. PolicyCoverageCount short-circuit (delta UPDATE 1+2)
      3. Six FDA comparisons → aggregate severity counts
      4. evaluate_access_modifiers()   — Features A/B/C + consistency
         → add to improvement / minor counters
      5. assign_bucket()
      6. Build reasoning trace
    """
    fda = FDA_BASELINE.get(drug.upper().strip()) if drug else None
    if not fda:
        return {
            "bucket": 25,
            "score": 25,
            "reason": "FDA_BASELINE_MISSING",
            "reasoning": [f"FDA baseline not found for {drug} — default Bucket 25"],
        }

    # Layer A — inference + pre-check shorts
    params = apply_reauth_inference(params)

    coverage = count_policy_coverage_fields(params)
    if coverage == 0:
        return {
            "bucket": 0,
            "score": 0,
            "reason": "PRODUCT_NOT_FOUND",
            "coverage_count": 0,
            "reasoning": ["All 6 FDA-comparison fields null → PRODUCT_NOT_FOUND"],
        }
    if coverage <= 1:
        return {
            "bucket": 25,
            "score": 25,
            "reason": "INSUFFICIENT_DATA",
            "coverage_count": coverage,
            "reasoning": [f"Only {coverage}/6 FDA-comparison fields known → INSUFFICIENT_DATA"],
        }

    # Layer B — six FDA comparisons
    comparisons = {
        "age":           compare_age(fda.get("min_age"),                params.get("age")),
        "brand_steps":   compare_brand_steps(fda.get("brand_steps"),    params.get("steps_brands")),
        "generic_steps": compare_generic_steps(fda.get("generic_steps"), params.get("steps_generic")),
        "phototherapy":  compare_phototherapy(fda.get("phototherapy_required"), params.get("step_phototherapy")),
        "tb_test":       compare_tb_test(fda.get("tb_test_required_as_pa_gate"), params.get("tb_test_required")),
        "specialist":    compare_specialist(fda.get("specialist_restriction"),   params.get("specialist_types")),
    }
    counts = aggregate_severity_counts(comparisons)

    # Layer C — access modifier features
    modifiers = evaluate_access_modifiers(params)
    counts["improvement"] += modifiers["improvements"]
    counts["minor"]       += modifiers["minor_restrictions"]

    # Layer D — bucket assignment
    bucket = assign_bucket(counts)

    return {
        "bucket": bucket,
        "score": bucket,  # Score == bucket until Layer 2 within-bucket scoring lands
        "reason": "OK",
        "coverage_count": coverage,
        "restriction_summary": counts,
        "comparisons": {
            k: {"state": v[0], "severity": v[1]}
            for k, v in comparisons.items()
        },
        "modifiers": modifiers,
        "reauth_inference": params.get("_reauth_inferred_rule"),
        "reasoning": _build_access_score_reasoning(comparisons, modifiers, counts, bucket),
    }


def _build_access_score_reasoning(comparisons, modifiers, counts, bucket) -> list:
    """Human-readable trace for debug log + manual review."""
    lines: list = []
    for param, (state, severity) in comparisons.items():
        if state == "MORE_RESTRICTIVE":
            lines.append(f"{param}: MORE_RESTRICTIVE ({severity})")
        elif state == "LESS_RESTRICTIVE":
            lines.append(f"{param}: LESS_RESTRICTIVE (improvement)")
        elif state == "UNKNOWN":
            lines.append(f"{param}: UNKNOWN (ignored)")
    lines.append(
        f"modifiers: A={modifiers['feature_a']}, B={modifiers['feature_b']}, "
        f"C={modifiers['feature_c']}, consistency_penalty={modifiers['consistency_penalty']}"
    )
    lines.append(
        f"Bucket: {bucket} | Severe={counts['severe']}, Major={counts['major']}, "
        f"Moderate={counts['moderate']}, Minor={counts['minor']}, "
        f"Improvements={counts['improvement']}"
    )
    return lines


# ─────────────────────────────────────────────────────────────────
# BLOCK 8 — ORCHESTRATOR (Phase 8)
# Pre-flight, three-tier cache (pdf / outline / section), per-row
# pipeline with rerun loop + rule-based step fallback, atomic checkpoint
# writes keyed by (filename, brand) — survives spreadsheet edits and
# DailyQuotaExceeded mid-batch.
# ─────────────────────────────────────────────────────────────────


def _atomic_checkpoint_write(results: list) -> None:
    """Write to temp file then os.replace — survives mid-write crashes."""
    fd, tmp_path = tempfile.mkstemp(
        prefix="checkpoint_", suffix=".json",
        dir=str(CHECKPOINT_PATH.parent or "."),
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"results": results}, f)
        os.replace(tmp_path, CHECKPOINT_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def _na_row_with_warning(warning: str) -> dict:
    """Empty row with all-NA sentinels (§A contract) + a single warning tag.
    Used for unrecoverable rows: drug not found, PDF missing, exception.

    Access score: 0 when drug-not-found (PRODUCT_NOT_FOUND per delta spec),
    else 25 (default for unscorable rows). NA-fields are explicit so the
    coverage_count check in compute_access_score wouldn't help — we set
    the score directly here.
    """
    is_product_not_found = "DRUG_NOT_FOUND" in warning
    return {
        "age": "NA",
        "combined_step_text": "NA",
        "steps_brands": "NA",
        "steps_generic": "NA",
        "step_phototherapy": "NA",
        "tb_test_required": "NA",
        "quantity_limits_text": "NA",
        "specialist_types": "NA",
        "initial_auth_duration_months": "NA",
        "reauth_duration_months": "NA",
        "reauth_required": "NA",
        "reauth_requirements_text": "NA",
        "access_score": 0 if is_product_not_found else 25,
        "_warnings": [warning],
    }


# Threshold for "small doc" bypass — under this many chars we skip the
# outline-driven sectioning entirely and use full_text directly. Block 3's
# sectioning machinery is wasted effort on docs that already fit in the
# context window.
SMALL_DOC_CHAR_LIMIT = 50_000


def process_single_row(
    filename: str,
    drug: str,
    indication: str,
    pdf_cache: dict,
    outline_cache: dict,
    section_cache: dict,
    rerun_count: int = 0,
) -> dict:
    """Full per-row pipeline.

    Cache tiers:
      1. pdf_cache[filename]            — ingested text + open doc handle
      2. outline_cache[filename]        — extracted outline (pruned)
      3. section_cache[(filename, drug)] — assembled context per drug

    Rerun semantics:
      - critical_failures from validate_all → rerun (max 2)
      - DailyQuotaExceeded propagates so process_all_rows can halt cleanly
      - Any other exception → emit NA row with warning, batch continues
    """
    try:
        # Cache tier 1: ingestion (preflight may have pre-populated)
        if filename not in pdf_cache:
            pdf_cache[filename] = ingest_pdf(PDF_DIR / filename)
        else:
            pdf_cache[filename] = _ensure_vision_aware(pdf_cache[filename])
        doc_info = pdf_cache[filename]
        full_text = doc_info["full_text"]
        doc = doc_info["doc_handle"]

        # Cache tier 2: outline
        if filename not in outline_cache:
            outline_cache[filename] = extract_outline(doc, full_text)
        outline = outline_cache[filename]

        # Cache tier 3: assembled context (per (filename, drug))
        cache_key = (filename, drug)
        if cache_key not in section_cache:
            if len(full_text) < SMALL_DOC_CHAR_LIMIT or len(outline) < 5:
                # Small docs / empty outlines → use full text directly
                log_debug({
                    "event": "section_full_doc_path",
                    "filename": filename,
                    "drug": drug,
                    "outline_size": len(outline),
                    "text_size": len(full_text),
                })
                section_cache[cache_key] = full_text[:MAX_CONTEXT_CHARS]
            else:
                pruned = _prune_outline(outline)
                anchors = map_outline_to_sections(pruned, drug, indication)
                # Use the FULL outline (not pruned) for slicing — pruning is
                # only for the LLM map call (token cost). Slicing needs every
                # heading to correctly bound section boundaries; missing
                # intermediate headings can make sections over-broad.
                sections = slice_by_anchors(full_text, outline, anchors)
                if not any(sections.values()):
                    log_debug({
                        "event": "sectioning_empty_falling_back",
                        "filename": filename,
                        "drug": drug,
                    })
                    section_cache[cache_key] = full_text[:MAX_CONTEXT_CHARS]
                else:
                    section_cache[cache_key] = assemble_context(
                        sections, drug, indication, doc=doc,
                    )
        context = section_cache[cache_key]

        # Drug-not-found loud raise (§11.2)
        aliases = get_drug_aliases(drug)
        if (not any(a in context.lower() for a in aliases)
                and not any(a in full_text.lower() for a in aliases)):
            log_debug({
                "event": "drug_not_found_in_row",
                "filename": filename,
                "drug": drug,
            })
            return _na_row_with_warning("CRITICAL_DRUG_NOT_FOUND")

        # Multi-brand disambiguation directive input
        other_brands = get_other_brands_in_doc(context, drug)

        # Deterministic step-therapy anchor — keyword scanner over full PDF text.
        # Insurance against outline-mapping missing the universal "preferred
        # products" / "Documentation for all indications" block. Empty string
        # if no matches (zero overhead).
        step_anchor = _extract_step_therapy_anchor(full_text)
        if step_anchor:
            log_debug({
                "event": "step_anchor_extracted",
                "filename": filename,
                "drug": drug,
                "anchor_chars": len(step_anchor),
            })

        # Pass 1
        simple = retry(extract_simple_params, context, drug, indication, other_brands)

        # Pass 2
        step_pass2 = retry(
            extract_step_therapy_text, context, drug, indication, other_brands,
            step_anchor,
        )
        pass2_step_text = step_pass2.get("combined_step_text", "NA")

        # Safety net for Pass 3: if Pass 2 STILL dropped the preferred-product
        # language (anchor returned non-empty but combined_step_text missed it),
        # build an AUGMENTED version of the step text for Pass 3's reasoning
        # input. Keep pass2_step_text CLEAN — it's what ends up in the
        # partner-facing CSV (Step Therapy column) and must not contain
        # internal "[FROM STEP THERAPY ANCHOR — keyword-scanned]" markers.
        pass3_step_text = pass2_step_text
        if step_anchor and pass2_step_text and pass2_step_text != "NA":
            anchor_has_preferred = bool(
                re.search(r"preferred\s+products?|THREE\s+preferred|TWO\s+additional",
                          step_anchor, re.IGNORECASE)
            )
            pass2_has_preferred = bool(
                re.search(r"preferred\s+products?|THREE\s+preferred|TWO\s+additional",
                          pass2_step_text, re.IGNORECASE)
            )
            if anchor_has_preferred and not pass2_has_preferred:
                log_debug({
                    "event": "step_anchor_safety_net_prepended",
                    "filename": filename,
                    "drug": drug,
                })
                # Pass 3 sees both sources with explicit provenance tags.
                pass3_step_text = (
                    f"[FROM STEP THERAPY ANCHOR — keyword-scanned]\n"
                    f"{step_anchor}\n\n"
                    f"[FROM PASS 2 — outline-derived]\n"
                    f"{pass2_step_text}"
                )

        # combined_step_text is the canonical name downstream code expects;
        # use the augmented version for the rest of this row's processing
        # (Pass 3 input, rule_based_step_count, validation token-recall).
        # We'll strip the internal markers from pass2_step_text before
        # writing to params, so the CSV stays clean.
        combined_step_text = pass3_step_text

        # Pass 3 (with rule-based fallback)
        llm_failed = False
        try:
            llm_counts = extract_step_counts(combined_step_text, context, drug, indication)
        except DailyQuotaExceeded:
            raise
        except Exception as e:
            log_debug({
                "event": "pass3_failed_using_rule_fallback",
                "error": str(e),
                "filename": filename,
                "drug": drug,
            })
            llm_counts = {
                "steps_brands": "NA", "steps_generic": "NA",
                "step_phototherapy": "NA",
            }
            llm_failed = True

        rule_counts = rule_based_step_count(combined_step_text)
        final_counts, count_flags = reconcile_step_counts(
            llm_counts, rule_counts, llm_failed=llm_failed,
        )

        # Merge — use the CLEAN pass2_step_text for the CSV column, NOT
        # the augmented combined_step_text (which may carry internal
        # provenance markers like "[FROM STEP THERAPY ANCHOR …]" that
        # are useful for Pass 3 reasoning but must not leak to partner-
        # facing output).
        params = {**simple, **final_counts}
        params["combined_step_text"] = pass2_step_text
        params.setdefault("_warnings", []).extend(count_flags)

        # Validate
        params, critical_failures = validate_all(
            params, source_text=full_text, target_drug=drug,
        )

        if critical_failures and rerun_count < MAX_PIPELINE_RERUNS:
            log_debug({
                "event": "rerun",
                "filename": filename,
                "drug": drug,
                "rerun_count": rerun_count + 1,
                "critical_failures": critical_failures,
            })
            # Invalidate section_cache for this (filename, drug) so we re-section
            section_cache.pop(cache_key, None)
            return process_single_row(
                filename, drug, indication,
                pdf_cache, outline_cache, section_cache,
                rerun_count=rerun_count + 1,
            )

        if critical_failures:
            params.setdefault("_warnings", []).append(
                f"CRITICAL_VALIDATION_FAILED_AFTER_{MAX_PIPELINE_RERUNS}_RERUNS:"
                f"{critical_failures}"
            )

        # Block 7 — Access Score (pure-Python, no LLM)
        try:
            access_result = compute_access_score(params, drug)
            params["access_score"] = access_result["bucket"]
            log_debug({
                "event": "access_score_computed",
                "filename": filename,
                "drug": drug,
                "bucket": access_result["bucket"],
                "reason": access_result.get("reason"),
                "coverage_count": access_result.get("coverage_count"),
                "modifiers": access_result.get("modifiers"),
                "restriction_summary": access_result.get("restriction_summary"),
                "reasoning": access_result.get("reasoning"),
            })
        except Exception as e:
            log_debug({
                "event": "access_score_failed",
                "filename": filename,
                "drug": drug,
                "error": str(e),
            })
            # Per spec: unscorable rows ship Bucket 25 (FDA_BASELINE_MISSING
            # default) with a manual-review warning. Never ship blank.
            params["access_score"] = 25
            params.setdefault("_warnings", []).append(
                f"ACCESS_SCORE_FAILED_DEFAULTED_TO_25: {e}"
            )

        if params.get("_warnings"):
            log_debug({
                "event": "row_warnings",
                "filename": filename,
                "drug": drug,
                "warnings": params["_warnings"],
            })

        return params

    except DailyQuotaExceeded:
        raise
    except Exception as e:
        log_debug({
            "event": "row_failed",
            "filename": filename,
            "drug": drug,
            "error": str(e),
        })
        return _na_row_with_warning(f"EXTRACTION_EXCEPTION: {e}")


def process_all_rows(
    submissions_df: pd.DataFrame,
    pdf_cache: Optional[dict] = None,
    indication_default: str = "Plaque Psoriasis",
) -> list:
    """Loop all rows. Cache PDFs + outlines + assembled contexts.
    Atomic checkpoint every CHECKPOINT_INTERVAL rows. Resume by
    (filename, brand) key — survives spreadsheet edits + reorders.

    On DailyQuotaExceeded: save checkpoint, close PDFs, exit code 2.
    On any other unhandled exception: re-raise (process_single_row catches
    expected ones; anything that leaks is a real bug).
    """
    if pdf_cache is None:
        pdf_cache = {}
    outline_cache: dict = {}
    section_cache: dict = {}

    # Load checkpoint, keyed by (filename, brand)
    completed: dict = {}
    if CHECKPOINT_PATH.exists():
        with open(CHECKPOINT_PATH) as f:
            ckpt = json.load(f)
            for entry in ckpt.get("results", []):
                key = (entry.get("Filename"), entry.get("Brand"))
                completed[key] = entry
        print(f"Resuming with {len(completed)} rows already done.")

    results: list = []
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
                    pdf_cache, outline_cache, section_cache,
                )
            except DailyQuotaExceeded:
                print(
                    "\nDAILY QUOTA EXCEEDED. Saving checkpoint and exiting."
                )
                print("Resume tomorrow with: python pipeline.py")
                _atomic_checkpoint_write(results)
                close_pdf_cache(pdf_cache)
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


def summarize_llm_usage(
    path: Optional[Path] = None,
    since: Optional[float] = None,
) -> None:
    """Per-model LLM usage summary from debug_log.jsonl.

    Reads `llm_call` and `llm_call_failed` events, aggregates by (model, kind),
    prints totals + latency percentiles. Optional `since` (unix epoch seconds)
    restricts to calls started at or after that time — useful for filtering to
    just the current batch when the log spans multiple runs.
    """
    log_path = path or DEBUG_LOG_PATH
    if not log_path.exists():
        print("INFO: no debug log to summarize.")
        return

    from collections import defaultdict
    stats: dict = defaultdict(lambda: {
        "calls": 0, "failed": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "wall_sec": 0.0,
        "durations": [],
        "est_total": 0,
    })

    def _ts_to_epoch(iso: str) -> float:
        try:
            return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    with open(log_path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            event = e.get("event")
            if event not in ("llm_call", "llm_call_failed"):
                continue
            if since is not None:
                ts = _ts_to_epoch(e.get("started_at", ""))
                if ts < since:
                    continue
            # Breakdown now includes key_idx so we see per-key consumption
            # (matters when multiple Groq accounts are in play — each has
            # its own TPD allowance per model).
            key_label = (
                e.get("model", "?"),
                e.get("kind", "?"),
                e.get("key_idx", 0),  # default 0 for older log entries
            )
            s = stats[key_label]
            if event == "llm_call":
                s["calls"] += 1
                s["prompt_tokens"] += int(e.get("prompt_tokens") or 0)
                s["completion_tokens"] += int(e.get("completion_tokens") or 0)
                s["total_tokens"] += int(e.get("total_tokens") or 0)
                s["wall_sec"] += float(e.get("duration_sec") or 0)
                s["est_total"] += int(e.get("est_tokens") or 0)
                d = e.get("duration_sec")
                if d is not None:
                    s["durations"].append(float(d))
            else:
                s["failed"] += 1

    if not stats:
        print("INFO: no llm_call events in log.")
        return

    print("\n===== LLM USAGE (from response.usage, exact) =====")
    print(f"{'model':<45} {'kind':<7} {'key':>3} {'calls':>5} {'fail':>4} "
          f"{'prompt':>10} {'completion':>11} {'total':>10} {'est_sum':>10} "
          f"{'wall_s':>7} {'p50_s':>6} {'p95_s':>6}")
    grand_total_actual = 0
    grand_total_est = 0
    for (model, kind, key_idx), s in sorted(
        stats.items(),
        key=lambda kv: (kv[0][2], -kv[1]["total_tokens"]),
    ):
        durs = sorted(s["durations"])
        p50 = durs[len(durs)//2] if durs else 0
        p95 = durs[int(len(durs)*0.95)] if durs else 0
        print(f"{model:<45} {kind:<7} {key_idx:>3} {s['calls']:>5} {s['failed']:>4} "
              f"{s['prompt_tokens']:>10,} {s['completion_tokens']:>11,} "
              f"{s['total_tokens']:>10,} {s['est_total']:>10,} "
              f"{s['wall_sec']:>7.1f} {p50:>6.2f} {p95:>6.2f}")
        grand_total_actual += s["total_tokens"]
        grand_total_est += s["est_total"]
    print(f"\nGrand totals — actual: {grand_total_actual:,}  |  "
          f"estimated: {grand_total_est:,}  |  "
          f"est/actual ratio: "
          f"{grand_total_est / max(grand_total_actual,1):.2f}")


def print_end_of_batch_summary(results: list) -> None:
    """End-of-batch diagnostics — surface rows needing manual review."""
    flag_counts: dict = {}
    critical_rows: list = []

    for r in results:
        warnings = r.get("_warnings", []) or []
        for w in warnings:
            tag_parts = w.split("_")
            tag = "_".join(tag_parts[:2]) if len(tag_parts) >= 2 else w
            flag_counts[tag] = flag_counts.get(tag, 0) + 1
            if any(
                w.startswith(p)
                for p in (
                    "CRITICAL", "STEP_COUNT_MAJOR", "TB_CONTRADICTION",
                    "REAUTH_CONTRADICTION", "BRAND_MISSED",
                )
            ):
                critical_rows.append(
                    (r.get("Filename"), r.get("Brand"), warnings)
                )

    print("\n===== END-OF-BATCH SUMMARY =====")
    print(f"Total rows: {len(results)}")
    print("\nWarning tag counts:")
    for tag, n in sorted(flag_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {tag}")
    print(f"\nRows needing manual review: {len(critical_rows)}")
    for fn, dg, ws in critical_rows[:20]:
        print(f"  {fn} — {dg}: {ws}")
    if len(critical_rows) > 20:
        print(f"  ... and {len(critical_rows) - 20} more")

    summarize_llm_usage()


def preflight_check(submissions_df: pd.DataFrame, pdf_cache: dict) -> None:
    """Cheap-only pre-flight. NO LLM calls, NO vision.

    1. Cross-reference Submissions filenames against PDF_DIR. Missing → halt.
    2. For each unique filename: cheap text-only ingest + drug-presence scan.
       Zero alias matches → log warning, dump first 2 pages, allow batch.
    3. Populates pdf_cache so process_all_rows doesn't re-ingest.
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

    zero_match: list = []
    for filename, group in submissions_df.groupby("Filename"):
        doc_info = _ingest_pdf_text_only(PDF_DIR / filename)
        pdf_cache[filename] = doc_info
        full_text_lower = doc_info["full_text"].lower()
        for drug in group["Brand"].unique():
            aliases = get_drug_aliases(drug)
            if not any(a in full_text_lower for a in aliases):
                zero_match.append((filename, drug))
                first_pages = "\n".join(doc_info["pages"][:2])[:5000]
                log_debug({
                    "event": "drug_not_found_preflight",
                    "filename": filename,
                    "drug": drug,
                    "first_2_pages_preview": first_pages,
                })

    if zero_match:
        print(f"WARNING: {len(zero_match)} (filename, drug) pairs have no "
              f"alias matches in the PDF. These will produce "
              f"CRITICAL_DRUG_NOT_FOUND rows. Inspect debug_log.jsonl.")
        for fn, dg in zero_match[:10]:
            print(f"   {fn} — {dg}")


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

# ── BLOCK 9 (Phase 9) — Output formatting + CSV write ────────────

# Param keys → CSV column names. The keys MUST match what process_single_row
# actually sets in `params`. Bug B1 in the readiness review found that earlier
# drafts used `step_therapy_text` here while process_single_row set
# `combined_step_text` — silent empty column. Locked here:
COLUMN_MAP = {
    "Filename":                      "Filename",
    "Brand":                         "Brand",
    "age":                           "Age",
    "combined_step_text":            "Step Therapy Requirements Documented in Policy",
    "steps_brands":                  "Number of Steps through Brands",
    "steps_generic":                 "Number of Steps through Generic",
    "step_phototherapy":             "Step through-Phototherapy",
    "tb_test_required":              "TB Test required",
    "quantity_limits_text":          "Quantity Limits",
    "specialist_types":              "Specialist Types",
    "initial_auth_duration_months":  "Initial Authorization Duration(in-months)",
    "reauth_duration_months":        "Reauthorization Duration(in-months)",
    "reauth_required":               "Reauthorization Required",
    "reauth_requirements_text":      "Reauthorization Requirements Documented in Policy",
    "access_score":                  "Access Score",
}

# Param keys allowed to be empty (Phase 2 deferral).
PHASE2_PARAMS = {"access_score"}


def format_row(params: dict) -> dict:
    """Map internal param keys to CSV column names. Raises on missing keys
    (other than Phase 2 deferrals + Filename/Brand which come from the row).
    This catches the silent-empty-column bug class — any wiring mistake
    surfaces immediately.
    """
    missing = [
        k for k in COLUMN_MAP
        if k not in params
        and k not in PHASE2_PARAMS
        and k not in ("Filename", "Brand")
    ]
    if missing:
        raise KeyError(
            f"format_row: params is missing keys: {missing} "
            f"(params has: {list(params.keys())})"
        )
    return {COLUMN_MAP[k]: params.get(k, "") for k in COLUMN_MAP}


def save_csv(results: list, path: Path = OUTPUT_PATH) -> None:
    formatted = [format_row(r) for r in results]
    df = pd.DataFrame(formatted)
    df = df[list(COLUMN_MAP.values())]
    df.to_csv(path, index=False)
    print(f"Saved {len(results)} rows to {path}")


# ── BLOCK 9 (Phase 9) — Reference tab (transposed layout) ────────

def load_reference_tab_transposed(xlsx_path: Path) -> dict:
    """The Reference tab is TRANSPOSED — columns are `Sno., Params, Values`,
    each row is one parameter. This loader reads it row-by-row and maps the
    informal Params labels to canonical CSV column names.

    Returns a dict {csv_column_name: value}, suitable for use as a single
    ground-truth row by the smoke test (if Filename + Brand are present).
    """
    try:
        df = pd.read_excel(xlsx_path, sheet_name="Reference")
    except Exception as e:
        raise RuntimeError(f"Cannot load Reference tab: {e}") from e

    cols_lc = {c.lower().strip(): c for c in df.columns}
    params_col = cols_lc.get("params") or cols_lc.get("parameter")
    values_col = cols_lc.get("values") or cols_lc.get("value")
    if not params_col or not values_col:
        raise RuntimeError(
            f"Reference tab columns not as expected. Got: {list(df.columns)}\n"
            f"Expected a 'Params' column and a 'Values' column."
        )

    label_to_csv = {
        "filename":                          "Filename",
        "file name":                         "Filename",
        "brand":                             "Brand",
        "drug":                              "Brand",
        "age":                               "Age",
        "step therapy":                      "Step Therapy Requirements Documented in Policy",
        "step therapy requirements":         "Step Therapy Requirements Documented in Policy",
        "steps - branded":                   "Number of Steps through Brands",
        "number of steps through brands":    "Number of Steps through Brands",
        "steps - generic":                   "Number of Steps through Generic",
        "number of steps through generic":   "Number of Steps through Generic",
        "step through phototherapy":         "Step through-Phototherapy",
        "step through-phototherapy":         "Step through-Phototherapy",
        "phototherapy":                      "Step through-Phototherapy",
        "tb test":                           "TB Test required",
        "tb test required":                  "TB Test required",
        "quantity limits":                   "Quantity Limits",
        "specialist":                        "Specialist Types",
        "specialist types":                  "Specialist Types",
        "initial authorization duration":    "Initial Authorization Duration(in-months)",
        "initial auth duration":             "Initial Authorization Duration(in-months)",
        "initial auth duration (in months)": "Initial Authorization Duration(in-months)",
        "reauth duration":                   "Reauthorization Duration(in-months)",
        "reauthorization duration":          "Reauthorization Duration(in-months)",
        "reauth required":                   "Reauthorization Required",
        "reauthorization required":          "Reauthorization Required",
        "reauth requirements":               "Reauthorization Requirements Documented in Policy",
        "reauthorization requirements":      "Reauthorization Requirements Documented in Policy",
    }

    row: dict = {}
    unrecognised: list = []
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
        print(f"INFO: Reference tab has {len(unrecognised)} unmapped param labels: "
              f"{unrecognised[:5]}{'...' if len(unrecognised) > 5 else ''}")

    print(f"INFO: Reference tab parsed — {len(row)} fields "
          f"(filename={row.get('Filename')!r}, brand={row.get('Brand')!r})")
    return row


def inspect_additional_data_tab(
    xlsx_path: Path,
    submissions_df: pd.DataFrame,
) -> Optional[pd.DataFrame]:
    """Load the Additional Extracted Data tab and compute overlap with
    Submissions. Returns the overlap DataFrame if usable, else None."""
    try:
        df = pd.read_excel(xlsx_path, sheet_name="Additional Extracted Data")
    except Exception as e:
        print(f"INFO: Additional Extracted Data tab not loadable: {e}. "
              f"Dropping from validation plan.")
        return None

    print(f"\n=== Additional Extracted Data tab inspection ===")
    print(f"  Rows: {len(df)}")
    print(f"  Columns ({len(df.columns)}): {list(df.columns)[:6]}...")

    filename_col = next(
        (c for c in df.columns
         if c.lower().strip() in ("filename", "file_name", "file")),
        None,
    )
    if not filename_col:
        print(f"  No Filename column → not usable for validation overlap.")
        return None

    print(f"  Unique filenames: {df[filename_col].nunique()}")
    overlap = (
        set(df[filename_col].astype(str))
        & set(submissions_df["Filename"].astype(str))
    )
    print(f"  Overlap with Submissions: {len(overlap)} filenames")

    if not overlap:
        print(f"  Zero overlap → not usable. Dropping from validation plan.")
        return None

    overlap_df = df[df[filename_col].astype(str).isin(overlap)].copy()
    print(f"  Using {len(overlap_df)} overlapping rows as silver-standard.\n")
    return overlap_df


# ── BLOCK 9 (Phase 9) — Smoke test ───────────────────────────────

def load_validation_set(
    xlsx_path: Path,
    manual_labels_path: Path = Path("manual_labels.csv"),
) -> pd.DataFrame:
    """Combine Reference tab (1 transposed row) + manual_labels.csv into a
    DataFrame keyed by CSV column names. Throws if both are unavailable.
    """
    parts: list = []

    try:
        ref_row = load_reference_tab_transposed(xlsx_path)
        if ref_row.get("Filename") and ref_row.get("Brand"):
            ref_df = pd.DataFrame([ref_row])
            ref_df["_source"] = "reference"
            parts.append(ref_df)
        else:
            print(f"INFO: Reference tab parsed but has no Filename/Brand anchor. "
                  f"Skipping from validation set (use as comparison only).")
    except Exception as e:
        print(f"WARN: Reference tab not parseable: {e}")

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
            "make Reference tab provide a Filename+Brand row. Use "
            "--skip-smoke-gate to bypass."
        )

    combined = pd.concat(parts, ignore_index=True)

    if "Indication" in combined.columns:
        canonical_pso = {
            "pso", "psoriasis", "plaque psoriasis",
            "moderate-to-severe plaque psoriasis",
            "moderate to severe plaque psoriasis",
        }
        combined["Indication"] = combined["Indication"].astype(str).apply(
            lambda v: "Plaque Psoriasis"
            if v.strip().lower() in canonical_pso else v
        )

    print(f"INFO: validation set — {len(combined)} rows from "
          f"{combined['_source'].value_counts().to_dict()}.")
    return combined


def per_param_match(pred, truth, param_name: str) -> bool:
    """Per-param tolerance.
      - Text fields: token-recall >= 0.70
      - Specialist types: set equality after lowercase + strip
      - Everything else: exact (lowercase) equality
    """
    if pred is None:
        pred = ""
    if truth is None:
        truth = ""
    pred, truth = str(pred).strip(), str(truth).strip()

    text_fields = {
        "combined_step_text",
        "reauth_requirements_text",
        "quantity_limits_text",
    }
    if param_name in text_fields:
        return token_recall(pred, truth) >= 0.70
    if param_name == "specialist_types":
        return (
            {s.strip().lower() for s in pred.split(",") if s.strip()}
            == {s.strip().lower() for s in truth.split(",") if s.strip()}
        )
    return pred.lower() == truth.lower()


def run_smoke_test(xlsx_path: Path) -> bool:
    """Run pipeline on the validation set, score per-param, return True iff
    overall agreement ≥ 0.85. Detailed per-param breakdown printed.
    """
    validation_df = load_validation_set(xlsx_path)
    print(f"\n=== Smoke test on {len(validation_df)} validation rows ===")

    correct = 0
    total = 0
    by_param: dict = {}

    pdf_cache: dict = {}
    outline_cache: dict = {}
    section_cache: dict = {}

    try:
        for _, row in validation_df.iterrows():
            filename = row.get("Filename") or row.get("File")
            drug = row.get("Brand") or row.get("Drug")
            if not filename or not drug:
                continue
            indication = row.get("Indication", "Plaque Psoriasis")
            pred = process_single_row(
                filename, drug, indication,
                pdf_cache, outline_cache, section_cache,
            )
            for param_key, csv_col in COLUMN_MAP.items():
                if param_key in ("Filename", "Brand", "access_score"):
                    continue
                truth = row.get(csv_col)
                if pd.isna(truth) or truth == "" or truth is None:
                    continue
                total += 1
                ok = per_param_match(pred.get(param_key), truth, param_key)
                if ok:
                    correct += 1
                stats = by_param.setdefault(param_key, [0, 0])
                stats[1] += 1
                if ok:
                    stats[0] += 1
    finally:
        close_pdf_cache(pdf_cache)

    rate = correct / total if total else 0
    print(f"\nOverall: {correct}/{total} = {rate:.1%}")
    print(f"Per-param:")
    for param, (c, t) in sorted(by_param.items()):
        print(f"  {param:>40}: {c}/{t}")

    threshold = 0.85
    passed = rate >= threshold
    print(f"\nSmoke test: {'PASS' if passed else 'FAIL'} (threshold {threshold:.0%})")
    return passed


# ── MAIN ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PA Policy Extraction Pipeline (PsO Hackathon)",
    )
    parser.add_argument("--smoke-test", action="store_true",
                        help="Run smoke test only, exit. Halts on failure.")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Skip pre-flight check (NOT recommended).")
    parser.add_argument("--skip-smoke-gate", action="store_true",
                        help="Skip the pre-batch smoke gate.")
    parser.add_argument("--skip-determinism", action="store_true",
                        help="Skip determinism test (saves 3 LLM calls).")
    parser.add_argument("--force-rerun", nargs="*", default=[],
                        help="Force rerun for specific 'filename:brand' keys.")
    parser.add_argument("--pdf-dir", type=Path, default=None,
                        help="Override PDF_DIR (env: PIPELINE_PDF_DIR).")
    parser.add_argument("--xlsx-path", type=Path, default=None,
                        help="Override XLSX_PATH (env: PIPELINE_XLSX_PATH).")
    parser.add_argument("--output-path", type=Path, default=None,
                        help="Override OUTPUT_PATH (env: PIPELINE_OUTPUT_PATH).")
    return parser.parse_args()


def main() -> None:
    global BRANDED_DRUGS, GENERIC_DRUGS, PDF_DIR, XLSX_PATH, OUTPUT_PATH
    args = _parse_args()

    # CLI overrides
    if args.pdf_dir:
        PDF_DIR = args.pdf_dir
    if args.xlsx_path:
        XLSX_PATH = args.xlsx_path
    if args.output_path:
        OUTPUT_PATH = args.output_path

    # Hard requirements
    if not GROQ_API_KEY:
        print("ERROR: GROQ_API_KEY required (set env var or add to .env)")
        sys.exit(1)
    if not PDF_DIR.exists():
        print(f"ERROR: PDF_DIR does not exist: {PDF_DIR}")
        sys.exit(1)
    if not XLSX_PATH.exists():
        print(f"ERROR: Submissions file does not exist: {XLSX_PATH}")
        sys.exit(1)

    # Submission environment may ship only the Submissions file (CSV or
    # XLSX) — no Reference / PsO Brands / Additional Extracted Data sheets.
    # load_drug_classifications() defaults to PSO_MARKET_BASKET in that case.
    is_xlsx = XLSX_PATH.suffix.lower() in (".xlsx", ".xls")

    print("Loading drug classifications...")
    BRANDED_DRUGS, GENERIC_DRUGS = load_drug_classifications(
        XLSX_PATH if is_xlsx else None
    )
    print(f"  Branded: {len(BRANDED_DRUGS)}  |  Generic: {len(GENERIC_DRUGS)}")

    print("Loading submissions...")
    submissions_df = load_submissions(XLSX_PATH)
    print(f"  {len(submissions_df)} rows to process")

    # Block 7 — Load FDA baselines for every unique brand in the batch.
    # Disk-cached to fda_baselines_cache.json — second run skips all
    # openFDA API calls. SILIQ-style "not found" brands are simply absent
    # from the dict; compute_access_score() defaults them to Bucket 25
    # with FDA_BASELINE_MISSING reason.
    global FDA_BASELINE
    print("Loading FDA baselines from openFDA (cached after first run)...")
    unique_brands = sorted({str(b).strip() for b in submissions_df["Brand"] if b})
    FDA_BASELINE = load_all_fda_baselines(unique_brands)
    print(f"  FDA baselines loaded: {len(FDA_BASELINE)}/{len(unique_brands)} brands")
    missing = sorted(set(b.upper() for b in unique_brands) - set(FDA_BASELINE.keys()))
    if missing:
        print(f"  WARN: no FDA baseline for {missing} — those rows default to Bucket 25")

    # Optional Reference-tab inspection (only if input is XLSX with that
    # sheet — submission CSVs don't carry it). Never halts; logs warning.
    if is_xlsx:
        try:
            ref_row = load_reference_tab_transposed(XLSX_PATH)
            log_debug({
                "event": "reference_tab_parsed",
                "n_fields": len(ref_row),
                "has_filename": bool(ref_row.get("Filename")),
                "has_brand": bool(ref_row.get("Brand")),
            })
        except Exception as e:
            print(f"INFO: Reference tab not present (CSV input or missing sheet): {e}")
    else:
        print("INFO: Submissions input is CSV — skipping Reference / Additional Data probes.")

    if not args.skip_determinism:
        determinism_test()

    if is_xlsx:
        inspect_additional_data_tab(XLSX_PATH, submissions_df)

    # Smoke-test-only mode
    if args.smoke_test:
        passed = run_smoke_test(XLSX_PATH)
        sys.exit(0 if passed else 1)

    # Preflight populates pdf_cache, no LLM calls
    pdf_cache: dict = {}
    if not args.skip_preflight:
        preflight_check(submissions_df, pdf_cache)

    # Force-rerun handling: drop matching rows from checkpoint
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

    # Smoke gate depends on Reference sheet + manual_labels.csv — both are
    # dev-only artifacts. Auto-skip when running against a submission CSV.
    if not args.skip_smoke_gate and is_xlsx:
        print("\n=== Pre-batch smoke gate ===")
        if not run_smoke_test(XLSX_PATH):
            print("HALT: smoke test failed. Fix before running batch.")
            sys.exit(1)
    elif not is_xlsx:
        print("INFO: Submissions input is CSV — skipping smoke gate "
              "(no Reference/manual_labels expected).")

    # Full batch
    results = process_all_rows(submissions_df, pdf_cache=pdf_cache)
    save_csv(results)
    print_end_of_batch_summary(results)


if __name__ == "__main__":
    main()
