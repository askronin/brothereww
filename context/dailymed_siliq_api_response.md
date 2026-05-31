# DailyMed API — SILIQ Response Reference

> **Purpose:** Show partner what DailyMed returns for SILIQ (openFDA mein missing) and which fields our pipeline uses for FDA baseline + access score.
>
> **Fetched:** 2026-05-31  
> **Drug:** SILIQ (brodalumab)

---

## Why DailyMed?

| Source | SILIQ result |
|--------|--------------|
| openFDA (`api.fda.gov/drug/label.json`) | `404 NOT_FOUND` — no label in index |
| DailyMed (`dailymed.nlm.nih.gov`) | Label found — SPL version 21, published May 2026 |

DailyMed is the proposed **fallback** when openFDA returns no data. Same label text ? same `parse_fda_label()` logic.

---

## API Call 1 — Search by drug name

**Endpoint**

```
GET https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?drug_name=SILIQ&pagesize=5
```

**Response (pretty)**

```json
{
  "data": [
    {
      "spl_version": 21,
      "published_date": "May 25, 2026",
      "title": "SILIQ (BRODALUMAB) INJECTION [BAUSCH HEALTH US LLC]",
      "setid": "1a550c33-456a-4833-814e-8591aea7c688"
    },
    {
      "spl_version": 3,
      "published_date": "Jan 13, 2025",
      "title": "G-11 (CERCIS SILIQUASTRUM WHOLE) SOLUTION [DNA LABS, INC.]",
      "setid": "f3d60fea-1f71-4fb8-813b-bdffb4b7a38b"
    }
  ],
  "metadata": {
    "db_published_date": "May 29, 2026 07:40:57PM EST",
    "elements_per_page": 5,
    "total_elements": 2,
    "total_pages": 1,
    "current_page": 1
  }
}
```

### Field tags — Search response

| Field | Value (SILIQ row) | Tag | Notes |
|-------|-------------------|-----|-------|
| `setid` | `1a550c33-456a-4833-814e-8591aea7c688` | **`[USE]`** | Used to fetch full label XML in Call 2 |
| `title` | `SILIQ (BRODALUMAB) INJECTION [BAUSCH HEALTH US LLC]` | **`[USE]`** | Match brand + generic name; filter false positives (2nd result is unrelated homonym) |
| `spl_version` | `21` | `[REFERENCE]` | Label revision number — useful for audit |
| `published_date` | `May 25, 2026` | `[REFERENCE]` | Human-readable publish date |
| `metadata.*` | pagination info | `[IGNORE]` | Not needed for baseline extraction |

---

## API Call 2 — Full prescribing label (XML)

**Endpoint**

```
GET https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/1a550c33-456a-4833-814e-8591aea7c688.xml
```

**Human-readable label page**

https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=1a550c33-456a-4833-814e-8591aea7c688

### Label metadata (from XML root)

| Field | Value | Tag | Maps to pipeline field |
|-------|-------|-----|------------------------|
| `effectiveTime/@value` | `20260520` | **`[USE]`** | ? `label_effective_date`: `"2026-05-20"` |
| Document title | SILIQ (brodalumab) injection | `[REFERENCE]` | Confirms drug identity |
| Manufacturer | Bausch Health US LLC | `[IGNORE]` | Not used in access score |

---

## Key label sections (extracted from XML)

### Section 1 — INDICATIONS AND USAGE

**Tag:** **`[USE — SOURCE TEXT]`** — primary input for `min_age`, `indication_severity`

> SILIQ® (brodalumab) is indicated for the treatment of **moderate to severe plaque psoriasis** in **adult patients** who are candidates for systemic therapy or phototherapy and have **failed to respond or have lost response to other systemic therapies**.
>
> SILIQ is a human **interleukin-17 receptor A (IL-17RA) antagonist** indicated for the treatment of moderate to severe plaque psoriasis in adult patients who are candidates for systemic therapy or phototherapy and have failed to respond or have lost response to other systemic therapies. (1)

| Phrase in label | Extracted field | Tag |
|-----------------|-----------------|-----|
| "adult patients" (no pediatric age) | `min_age: 18` | **`[USE — Access Score]`** |
| "moderate to severe plaque psoriasis" | `indication_severity: "moderate-to-severe"` | `[CACHE]` — stored, not in 6-way compare |
| "interleukin-17 receptor A (IL-17RA) antagonist" | `pharm_class` | `[CACHE]` — metadata only |
| "failed to respond … other systemic therapies" | *(not extracted as steps)* | `[NOTE]` — pipeline hardcodes `brand_steps: 0` for all FDA baselines |
| No weight cutoff in PsO section | `min_weight_kg: null` | `[CACHE]` — stored if present on other drugs |

---

### Section 2.1 — Tuberculosis Assessment

**Tag:** **`[USE — SOURCE TEXT]`** — input for TB-related baseline fields

> Evaluate patients for **tuberculosis (TB) infection** prior to initiating treatment with SILIQ [see Warnings and Precautions (5.4)].

| Inference | Pipeline field | Tag |
|-----------|----------------|-----|
| Label recommends TB evaluation before start | `tb_evaluation_recommended: true` | `[CACHE]` |
| Not a prior-auth gate in FDA label | `tb_test_required_as_pa_gate: false` | **`[USE — Access Score]`** — compared vs payer `tb_test_required` |

---

### Section 5.5 — Risk for Latent Tuberculosis Reactivation

**Tag:** `[REFERENCE]` — supports TB baseline; same conclusion as 2.1

> Evaluate patients for tuberculosis (TB) infection prior to initiating treatment with SILIQ. Do not administer SILIQ to patients with active TB infection. Initiate treatment for latent TB prior to administering SILIQ…

---

### Sections we do NOT parse

| Section | Tag | Reason |
|---------|-----|--------|
| 2.2 Dosage (210 mg schedule) | `[IGNORE]` | Dosing — not in access score model |
| 3 Dosage forms | `[IGNORE]` | Form/strength — not used |
| 4 Contraindications (Crohn's, hypersensitivity) | `[IGNORE]` | Not in baseline schema |
| 5.1 Suicidal ideation / REMS | `[IGNORE]` | Safety — not in access score |
| 12 Clinical Pharmacology / Mechanism | `[REFERENCE]` | Could inform `pharm_class` on other sources; we take class from openfda block when available |

---

## Final parsed baseline (pipeline output)

After DailyMed XML ? openFDA-shaped dict ? `parse_fda_label("SILIQ")`:

```json
{
  "brand_name": "SILIQ",
  "min_age": 18,
  "min_weight_kg": null,
  "indication_severity": "moderate-to-severe",
  "brand_steps": 0,
  "generic_steps": 0,
  "phototherapy_required": false,
  "tb_test_required_as_pa_gate": false,
  "tb_evaluation_recommended": true,
  "specialist_restriction": null,
  "label_effective_date": "2026-05-20",
  "inn": "BRODALUMAB",
  "pharm_class": "Interleukin-17 Receptor A Antagonist [EPC]"
}
```

### Field tags — Parsed baseline

| Field | SILIQ value | Tag | Used in access score? |
|-------|-------------|-----|----------------------|
| `brand_name` | `"SILIQ"` | `[CACHE]` | Lookup key in `FDA_BASELINE` dict |
| `min_age` | `18` | **`[USE — Access Score]`** | `compare_age()` — Layer B |
| `min_weight_kg` | `null` | `[CACHE]` | Not in 6-way compare today |
| `indication_severity` | `"moderate-to-severe"` | `[CACHE]` | Stored for reference / future use |
| `brand_steps` | `0` | **`[USE — Access Score]`** | `compare_brand_steps()` — **hardcoded** for all FDA drugs |
| `generic_steps` | `0` | **`[USE — Access Score]`** | `compare_generic_steps()` — **hardcoded** |
| `phototherapy_required` | `false` | **`[USE — Access Score]`** | `compare_phototherapy()` — **hardcoded** |
| `tb_test_required_as_pa_gate` | `false` | **`[USE — Access Score]`** | `compare_tb_test()` — **hardcoded** (FDA recommends eval, not PA gate) |
| `tb_evaluation_recommended` | `true` | `[CACHE]` | Stored; not in 6-way compare |
| `specialist_restriction` | `null` | **`[USE — Access Score]`** | `compare_specialist()` — no FDA specialist-only rule |
| `label_effective_date` | `"2026-05-20"` | `[CACHE]` | Audit / freshness tracking |
| `inn` | `"BRODALUMAB"` | `[CACHE]` | Generic name metadata |
| `pharm_class` | `"Interleukin-17 Receptor A Antagonist [EPC]"` | `[CACHE]` | Drug class metadata |

---

## Tag legend

| Tag | Meaning |
|-----|---------|
| **`[USE — Access Score]`** | Directly drives one of the **6 FDA comparisons** in `compute_access_score()` |
| **`[USE — SOURCE TEXT]`** | Raw label section we parse with regex / section slicing |
| **`[USE]`** | Required for fetching or matching (e.g. `setid`, `title`) |
| **`[CACHE]`** | Stored in `fda_baselines_cache.json` but not in the 6-way compare today |
| **`[HARDCODED]`** | Not read from label — fixed in `parse_fda_label()` by design |
| **`[REFERENCE]`** | Useful for humans / audit; pipeline ignores |
| **`[IGNORE]`** | Present in API response; we don't use |

---

## Access score impact (SILIQ today vs after DailyMed fallback)

| Scenario | Bucket | Reason |
|----------|--------|--------|
| **Today** (no openFDA label) | **25** | `FDA_BASELINE_MISSING` — default bucket |
| **After DailyMed fallback** | Computed normally | All 6 comparisons run vs payer policy (age ?18, steps, TB, etc.) |

---

## Data flow (proposed)

```
openFDA search by brand_name
        ?
        ?? found ??? parse_fda_label() ??? fda_baselines_cache.json
        ?
        ?? 404/missing
                ?
                ?
        DailyMed search (drug_name=SILIQ)
                ?
                ?? pick row where title matches brand + (BRODALUMAB)
                ?
                ?
        Fetch SPL XML by setid
                ?
                ?
        Map to openFDA shape:
          • indications_and_usage ? Section 1 text
          • effective_time        ? effectiveTime value
          • openfda.generic_name  ? from title / label
          • openfda.pharm_class_epc ? from mechanism text (optional)
                ?
                ?
        parse_fda_label()  (same function as openFDA path)
                ?
                ?
        fda_baselines_cache.json  (source: "dailymed")
```

---

## Side-by-side: openFDA vs DailyMed for SILIQ

| | openFDA | DailyMed |
|---|---------|----------|
| SILIQ label available? | No | Yes |
| Response format | JSON | JSON (search) + XML (label) |
| `indications_and_usage` | N/A | Section 1 text |
| `effective_time` | N/A | `20260520` |
| Parsed `min_age` | N/A | `18` |
| API key required? | No | No |
| Rate limits | Generous | Generous (NIH public API) |
