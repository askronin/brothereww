# PA Policy Extraction Pipeline

Reads payer Prior-Authorization PDFs and extracts 12 standardized parameters + an Access Score for each (PDF, brand) pair in the submissions sheet. Built for Plaque Psoriasis policies.

---

## ▶ Quick Start

```bash
# 1. Install dependencies (Python 3.9+)
pip install pandas openpyxl pymupdf groq

# 2. Add your Groq API key(s) to .env
groq_api_key_1=gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 3. Run
python3 pipeline.py
```

Reads `submissions.xlsx` + `PDFs/` folder, writes `result.csv`. Resumable — re-running picks up from the last checkpoint.

Get a free Groq key: https://console.groq.com/keys · More keys = faster batch (1 key ≈ 2-3h for 79 rows, 3 keys ≈ 1h).

---

## 📁 Folder Layout

```
project/
├── pipeline.py          ← single-file pipeline (run this)
├── README.md
├── .env                 ← API keys
├── submissions.xlsx     ← Submissions sheet with Filename + Brand columns
├── PDFs/                ← one policy PDF per filename in submissions
└── result.csv           ← output (created/overwritten)
```

No other Excel sheets required. Submission file can be CSV instead of XLSX.

---

## 🧪 Testing With Your Own Data

**Same submissions, your keys:** edit `.env`, `python3 pipeline.py`.

**Your own submissions + PDFs:**
1. Replace `submissions.xlsx` (Submissions sheet, columns: `Filename`, `Brand`)
2. Replace contents of `PDFs/` (filenames must match the `Filename` column)
3. Delete `result.csv` + `checkpoint.json` for clean run
4. `python3 pipeline.py`

Custom paths via CLI:
```bash
python3 pipeline.py \
    --submissions-path my.xlsx \
    --pdf-dir my_pdfs/ \
    --output-path my_result.csv
```

Re-run specific rows: `python3 pipeline.py --force-rerun "100123-4567890.pdf:TREMFYA"`

---

## 📊 Output — `result.csv`

15 columns per row:

| Field | Format |
|---|---|
| Filename, Brand | from input |
| Age | `>=N` or `NA` |
| Step Therapy Requirements Documented in Policy | verbatim text or `NA` |
| Number of Steps through Brands / Generic | integer or `NA` |
| Step through-Phototherapy | `Yes` / `No` / `NA` |
| TB Test required | `Yes` / `No` |
| Quantity Limits | verbatim text or `NA` |
| Specialist Types | named specialty or `NA` |
| Initial / Reauthorization Duration (in-months) | integer / `Unspecified` / `NA` |
| Reauthorization Required | `Yes` / `No` |
| Reauthorization Requirements Documented in Policy | verbatim text or `NA` |
| Access Score | bucket: `0` / `15` / `25` / `50` / `75` / `100` |

---

## 🤖 Groq Models Used

| Model | Used for | Why |
|---|---|---|
| **Llama 4 Scout 17B** | Pass 1 + Pass 2 (extraction), Vision OCR | Largest TPM budget on free tier (30K/min, 500K/day) — primary workhorse. Vision-capable for image-OCR fallback. |
| **Llama 3.3 70B Versatile** | Pass 3 (step counting), outline-to-section mapping, recursive zoom | Strongest reasoning model for chain-of-thought AND/OR resolution + structured-JSON section anchors. |
| **Qwen 32B, GPT-OSS 120B** | Fallback chain | Used when primary models hit per-minute rate limits or daily quotas. |

**Smart routing:** every LLM call walks a model fallback chain. When one hits TPM/TPD limits, the call routes to the next available model. Vision-only calls always use Scout.

**Multi-key TPD pooling:** add up to 9 keys in `.env` (`groq_api_key_1` ... `_9`). Per-(key, model) daily token budget is tracked separately, so 3 keys = 3× daily throughput. Organization-restricted keys are auto-skipped after first detection.

---

## 🧠 How It Works — Extraction Pipeline

```
STARTUP                                                       once per batch
├─ Load .env + Groq keys
├─ Load submissions sheet
├─ Fetch FDA baselines (openFDA → DailyMed fallback, disk-cached)
└─ Pre-flight: verify PDFs exist + cheap drug-name scan
                                                            
PER ROW (×N submissions)
  ┌─────────────────────────────────────────────────┐
  │  1. PDF INGESTION                               │
  │     PyMuPDF text extraction                     │
  │     + Vision OCR fallback (Scout) for sparse    │
  │       pages (<100 chars)                        │
  └─────────────────────────────────────────────────┘
                       ▼
  ┌─────────────────────────────────────────────────┐
  │  2. SECTIONING (outline-driven)                 │
  │     • Small docs → use full text                │
  │     • Large docs → extract outline → LLM finds  │
  │       4 section anchors → deterministic slice   │
  │     • Recursive zoom on oversized sections      │
  │     • Assemble: UNIVERSAL + TABLES + DRUG +     │
  │       REAUTH + step-therapy keyword anchor      │
  └─────────────────────────────────────────────────┘
                       ▼
  ┌─────────────────────────────────────────────────┐
  │  3. 3-PASS LLM EXTRACTION                       │
  │     Pass 1: 7 simple fields (age, TB, durations,│
  │             specialist, quantity limits, reauth)│
  │     Pass 2: step therapy verbatim text          │
  │     Pass 3: branded/generic/photo step counts   │
  │             with chain-of-thought AND/OR        │
  │             resolution                          │
  └─────────────────────────────────────────────────┘
                       ▼
  ┌─────────────────────────────────────────────────┐
  │  4. VALIDATION & RULES                          │
  │     • Format rules (sentinels, age, durations)  │
  │     • Reauth Required derived from duration     │
  │     • Verbatim presence check (anti-halluc.)    │
  │     • Specialist sanity (override LLM if not    │
  │       literally in source)                      │
  │     • Rule-based step counter (fallback)        │
  │     • Critical failure → auto-rerun (max 2)     │
  └─────────────────────────────────────────────────┘
                       ▼
  ┌─────────────────────────────────────────────────┐
  │  5. ACCESS SCORE (pure Python, zero LLM)        │
  └─────────────────────────────────────────────────┘
                       ▼
              Write row to result.csv
              Save checkpoint every 10 rows
```

---

## 📈 How It Works — Access Score

Pure Python. Zero LLM calls. Deterministic. Compares extracted policy values against the FDA baseline for that drug.

```
Input: 12 extracted params + drug name
                       ▼
1. FDA BASELINE
   openFDA API → fallback to DailyMed → disk-cached
   Parses Plaque-Psoriasis-specific section only
   (NOT mixed with PsA / UC / Crohn's age cutoffs)
                       ▼
2. LAYER A — Pre-checks (override gates)
     • Reauthorization inference
       (duration implies required)
     • All 6 FDA fields null  → Bucket 0  (PRODUCT_NOT_FOUND)
     • Only ≤1 field known    → Bucket 25 (INSUFFICIENT_DATA)
                       ▼
3. LAYER B — 6 FDA Comparisons
   Age · Brand Steps · Generic Steps · Phototherapy · TB Test · Specialist
   Each → MORE_RESTRICTIVE (severity Minor/Moderate/Major/Severe)
        / EQUIVALENT / LESS_RESTRICTIVE / UNKNOWN
                       ▼
4. LAYER C — 3 Access Modifier Features
   A. Initial Auth Duration   (≥12 mo = Improvement, <6 mo = Minor restriction)
   B. Reauthorization Required (No = Improvement, Yes = evaluate C)
   C. Reauth Duration          (same scale as A)
   + Consistency check: reauth < initial/2 → +1 minor restriction
                       ▼
5. LAYER D — Bucket Assignment (first match wins)
   Severe≥1 AND Major≥1      → Bucket 0   Near-Impossible
   Major≥2 OR Severe≥1        → Bucket 15  Very Restrictive
   Major==1 OR Mod≥2 OR Min≥3 → Bucket 25  Restrictive
   Improvement≥2 (else clean) → Bucket 100 Best Access
   Improvement≥1 (else clean) → Bucket 75  Better Than FDA
   Otherwise clean            → Bucket 50  FDA Parity
                       ▼
Output: bucket ∈ {0, 15, 25, 50, 75, 100}
```

---

## 🛠 Common Issues

| Problem | Fix |
|---|---|
| `ERROR: GROQ_API_KEY required` | Add a key to `.env` |
| `Daily quota exceeded` | Add more keys to `.env` or wait for UTC midnight reset |
| `Combo exhausted` (informational) | Auto-handled — pipeline rotates to next key/model |
| Row looks wrong | `python3 pipeline.py --force-rerun "filename.pdf:BRAND"` |
| Want a clean run | Delete `checkpoint.json` + `debug_log.jsonl` |

`debug_log.jsonl` contains a per-event JSONL audit trail. Each row gets a `row_warnings` event listing any tagged issues for manual review.

---

## 📂 Files Created During Run

| File | Purpose |
|---|---|
| `result.csv` | Final output |
| `checkpoint.json` | Resume state (delete for fresh run) |
| `debug_log.jsonl` | Per-event audit trail |
| `fda_baselines_cache.json` | Cached FDA labels (regenerated on delete) |
