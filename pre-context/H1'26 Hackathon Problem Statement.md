**Payer Policy Intelligence: Extracting Access Quality Indicators & Defining Quality of Access from Prior Authorization Policy Documents**

# **Problem Overview**

Pharmaceutical manufacturers face a complex challenge to get access from insurance providers: navigating hundreds of Prior Authorization (PA) policies issued by health insurance payers across the United States. These policies govern when and how a drug will be covered for patients subscribing to the policies, and they vary significantly by payer (private commercial, government, etc.), state, drug, and indication. Understanding the restrictiveness of each payer policy and quantifying the access quality a manufacturer has secured for the brand is critical for market access strategy, field force effectiveness, and commercial planning.

Today, pharma teams must rely on expensive data sources or manually read and interpret these dense, unstructured policy documents to assess access quality \- a labor-intensive, expensive, and error-prone process. Your problem statement is to automate this pipeline: digitize policies, extract structured, standardized access parameters and define access score from raw payer PA policy PDFs using AI and NLP and produce outputs that can be evaluated against a ground truth benchmark.

# **Background & Context**

## **What is Prior Authorization (PA)?**

Prior Authorization is a cost-control mechanism used by health insurance payers. Before covering a prescription drug, the payer requires the prescriber to demonstrate that the patient meets specific clinical criteria. These criteria vary widely across payers and can include:

* Age thresholds and eligible patient populations

* Step therapy requirements (patients must first try and fail cheaper alternatives)

* Duration of prior treatment failure

* Specialist prescriber types (e.g., only dermatologists may prescribe)

* TB testing prerequisites

* Quantity limits and dosing restrictions

* Authorization durations and reauthorization requirements

## **Why Access Quality Matters**

When a pharma company negotiates coverage for a drug, the resulting policy reflects the 'quality of access' achieved. A policy with fewer step therapy requirements, broader age eligibility, and longer authorization periods represents better access than a highly restrictive policy. Measuring these differences systematically \- across dozens of payers and multiple brands \- is the core business problem this hackathon addresses.

The illustration below captures an example of payer restrictiveness:

| Criterion | FDA Minimum | Payer A (Less Restrictive) | Payer B (More Restrictive) |
| ----- | ----- | ----- | ----- |
| **Age Eligibility** | 18+ years | 18+ years | 30+ years |
| **Step Therapy (Brands)** | None required | 1 branded step | 3 branded steps |
| **Auth Duration** | Not specified | 12 months | 6 months |
| **TB Test Required** | No | No | Yes |

# **Hackathon Objective**

Your challenge is to build a GenAI pipeline that reads payer PA policy PDF documents and extracts structured values for 12 predefined business parameters (see *Business Rules Reference section* below). The extracted values will be compared against a ground truth dataset to evaluate accuracy. A successful solution will demonstrate:

* Accurate extraction of structured clinical and administrative criteria from unstructured PDF text

* Sound interpretation of complex payer policy language (OR/AND conditions, age-group-specific clauses, etc.)

* Access Score Accuracy

* Intelligent handling of edge cases such as missing values, implied defaults, and multi-brand policies within the same document

* Scalability \- a pipeline that can process many PDFs efficiently


As you set the pipeline to extract these parameter values, you will also need to create a “Access Quality” parameter, that is a score of 0 – 100 (0 indicate No access, 25 is restricted access against FDA guidelines, 50 is Parity with FDA label, 75 is preferred than FDA label, 100 is the best possible access against all competitors / no restrictions applied). You can create this framework based on the parameter values you have extracted and come up with a logic / GenAI process for the same.

# **Data Provided**

## **Please find [here](https://zsassociates-my.sharepoint.com/:f:/g/personal/avijeet_pandey_zs_com/IgChcHllkQ1BT4JITkpLkTFRAXBwQeecCir5FRBGhaQI8sc?e=Q7YMWb) the folder with below details:**

## **1\. Policy PDF Documents (Input)**

You will be provided with a ZIP archive containing a set of payer PA policy PDF files. Each file may cover one or more drug brands and indications. File names follow the pattern: {file\_id}.pdf (e.g., 330109-4880941.pdf). These are real, publicly available payer policy documents. The documents are primarily focused on Psoriasis (PsO) indication for two biologic brands: TREMFYA (guselkumab) and STELARA (ustekinumab).

## **2.1 Business Rules Reference (Parameter Definitions)**

A reference document (PA\_Business\_Rules.xlsx \- Business Rules tab) defines the 12 parameters you must extract, including their definitions, edge case handling instructions, and example outputs. Review this carefully before building your extraction logic. 

## **2.2. Ground Truth Dataset (Evaluation Standard)**

The PA\_Business\_Rules.xlsx file (*Submissions* tab) contains the expected submission format. The structure is:

| Column | Example Value | Description |
| ----- | ----- | ----- |
| **Filename** | 330109-4880941.pdf | Source PDF filename |
| **Brand** | TREMFYA | Drug brand name (TREMFYA or STELARA) |
| **Age** | \>=18 | Extracted value for the Age parameter |
| **Step Therapy Requirements...** | (full text) | Extracted step therapy text from the policy |
| **\[...14 more parameters\]** | ... | One column per parameter as defined in the Business Rules tab |

Each row in the submission file should correspond to a unique (Filename, Brand) combination. A single PDF may contain policies for multiple brands (e.g., both TREMFYA and STELARA), requiring separate rows for each.

# **Evaluation Criteria**

Submissions will be scored across two key dimensions:

| Criteria | What We Are Looking For |
| ----- | ----- |
| **Extraction Accuracy** | How closely do your extracted values match the ground truth? Scoring is per-parameter, per-row |
| **Access Score Accuracy** | How closely does your access score (that you derived using the extracted parameters, ranging 0-100) match the gold standard |

# **Deliverables**

Your submission must be a single ZIP archive containing:

1. **result.csv** \- Your extraction output file matching the ground truth format (one row per Filename \+ Brand \+ Indication combination). All 13 parameters must be populated for each row (at a filename-brand level)

2. **Notebook(s) / codebase** \- Full pipeline from PDF ingestion to structured output. All intermediate and final outputs must be visible. Code should be clean, commented, and **reproducible**.

3. **(Optional) Dashboard or visual assets** \- Screenshots or a hosted link to any interactive visualization of the extracted data.

# 

# 

# **Technical Guidance** 

## **Tools & Resources**

* Candidates must use open-source LLMs only for the final submission. The required setup is the Gemini 2.5 model via free API access (up to 1,500 API calls per day), running in a Kaggle or Google Colab Notebook (free tier). No paid APIs, no local GPU setups, and no proprietary model calls will be accepted; solutions will be tested for reproducibility.


* Reference Link –

  * https://ai.google.dev/gemini-api/docs/pricing 

  * [10 Best Free LLM APIs for Developers (2025) — GPT, Claude, Llama & More](https://publicapis.io/blog/free-llm-apis)

# **Timeline**

| Milestone | Date |
| ----- | ----- |
| **Problem Statement & Data Release** | 22nd May’26 |
| **Submission Deadline** | 1st June’26 9AM IST |

Note: No extensions will be granted. Submissions received after the deadline will not be evaluated.

**Good luck \- we look forward to seeing your solutions\!**

