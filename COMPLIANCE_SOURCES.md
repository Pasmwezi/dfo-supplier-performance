# Supplier Performance Management — compliance source register

## Scope, applicability and provenance

This register documents the Canadian federal contractual clauses and historical forms supplied by the user as the design basis for the application’s scoring profiles. It is not a determination that those instruments are current, incorporated into a particular contract, or applicable to every organization. Verify the executed contract, current authoritative instruments, applicable law and sector policy before selecting a regime or taking adverse action. Private and other-sector users must establish their own contractual and policy basis; Canadian federal guidance is not automatically binding on them.

Historical source filenames and agency/form names below are retained verbatim for factual provenance and traceability, **not application branding**. The application is organization-neutral Supplier Performance Management. The original source labels do not confer endorsement, compliance certification, an Authority to Operate, delegated authority, or authorization to suspend or exclude a supplier.

Security, privacy, accessibility, records retention, language obligations, notice/representation procedures and legal/delegated-authority review remain organization- and sector-dependent production requirements. Canadian federal requirements (including Protected B, ATIP, Library and Archives Canada, official languages and GC identity/hosting controls) must be assessed where applicable; other sectors must validate their corresponding obligations.

## Construction
The following summarizes the supplied instruments, not universal application policy.
- **GI16**: Requires Canada to evaluate contractor performance during and on completion based on quality of workmanship, timeliness, project management, contract management, and health and safety; PWGSC-TPSGC 2913 records performance.
- **GC1.22**: Five criteria, each 0–20. Bands: unacceptable 0–5; not satisfactory 6–10; satisfactory 11–16; superior 17–20. Source outcomes: ≥85 congratulations; 51–84 meets expectations; 30–50 warning; <30 suspension. The source also calls for suspension at 30–50 with any criterion ≤5. In the application, these suspension thresholds produce recommendations only; an authorized, separately recorded decision and applicable due process are required. Warning text references a second score ≤50 within two years.
- **Form applicability**: Form 2913 permits **Project Management** and **Contract Management** to be marked N/A. Quality of Workmanship, Time, and Health and Safety remain required. The source PDF JavaScript removes an N/A criterion from the total and reduces the displayed maximum by 20 points.

Source: `CONSTRUCTION_TEMPLATE_ITT_Above 500K_EN.docx` and `select-contractor-performance-evaluation-2913.pdf` provided by the user. The application records contract value, whether performance evaluation is required, and the selected contractual performance regime. The source file's “Above $500K” folder is provenance, not by itself authority to apply the clause to another instrument; administrators must select the regime supported by the executed contract and current policy.

## Architectural and Engineering services
The following summarizes the supplied instruments, not universal application policy.
- **GI23**: Evaluation includes all or some of Design, Quality of Results, Management, Time and Cost; unsatisfactory performance may result in ineligibility; DFO-TPSGC 2913-1 records performance.
- **GC26**: Five criteria, 20 points each, and the same outcome thresholds as GC1.22. Warning text references a second score ≤50 within two years.
- **PWGSC-TPSGC 2913-1**: Design, Quality of Results, Management, Time, Cost; each 0–20; total /100.
- **Form applicability**: Form 2913-1 permits **Design**, **Management**, and **Cost** to be marked N/A. Quality of Results and Time remain required. The PDF removes the N/A score fields, although its printed denominator remains `/100`.

## CPERF project and contract information
Forms 2913 and 2913-1 identify the evaluated project using the contract number, project number, client reference number, description of work, firm/contractor name and address, project-manager contact information, contract award amount/date, final amount, completion date, and the number of contract changes. Construction form 2913 additionally records the contractor’s superintendent, uses **change orders** terminology, and records the final certificate date; consultant form 2913-1 uses **amendments** terminology. The application stores these values in a one-to-one evaluation project record rather than relying only on mutable supplier/contract master data. They are included in complete evaluation version snapshots and field-level audit history. Core form inputs—description of work, project manager name/email, and award amount/date—are browser-required; close-out fields remain available when known or applicable.

## N/A scoring implementation
For consistent outcome thresholds, the application records both raw applicable points and the applicable maximum, then normalizes the result to a percentage: `raw applicable points ÷ applicable maximum × 100`. N/A criteria are excluded rather than scored as zero. Every N/A selection requires a rationale in Comments and remains visible as **Not Applicable** in the evaluation record. This is an explicit application normalization choice addressing the denominator inconsistency between forms 2913 and 2913-1 while preserving a transparent audit trail. It does not amend either source instrument: the deploying organization must approve the scoring interpretation against its executed contract and applicable policy before authoritative use.

Source: `DFO_RFSO_AE_Template_EN.docx` and `select-consultant-performance-evaluation-2913-1.pdf` provided by the user.

## Configurable eight-criterion A&E model
The requested eight-criterion model is implemented as an organization-neutral extended administrative profile. `AE_EXTENDED` is the canonical contractual regime identifier; `DFO_AE_EXTENDED` remains supported as a legacy input and stored value so existing evaluations and contracts retain compatibility without a cosmetic data migration. The eight-criterion `AE` evaluation profile is distinct from the five-criterion `AE_CPERF` profile.

Because the supplied GC26 and form 2913-1 prescribe five 20-point categories, the system retains traceability/mapping and also supports the legacy five-criterion CPERF profile. Legal/policy validation and any required contractual amendment are necessary before the extended model becomes an authoritative contractual evaluation instrument. Neither renaming the regime nor selecting a profile makes it compliant with GC26/2913-1 or authorizes suspension. For private and other-sector contracts, validate the model, thresholds, correspondence and adverse-action procedures against the organization’s own terms and policy.

## Operational naming compatibility

New installations use organization-neutral names and canonical `SPM_*` environment variables. Legacy `DFO_SPM_*` environment equivalents remain compatible. Existing directory, Compose project, database, volumes, attachments and backups are intentionally preserved to avoid disconnecting or losing operational data; retained historical identifiers are not branding or a reason to recreate storage. See [`README.md`](README.md) for deployment and upgrade safeguards.
