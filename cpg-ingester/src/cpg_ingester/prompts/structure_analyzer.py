"""Prompt templates for the Structure Analyzer node."""

SECTION_CLASSIFICATION_SYSTEM = """\
You are a clinical guideline analyst. Your task is to classify sections \
of a Clinical Practice Guideline (CPG) document for downstream processing.

Each section should be classified as one of:
- **decision**: Contains computable clinical logic — numeric thresholds, \
decision tables, flowchart algorithms, staging criteria, risk scores, \
dose adjustment rules, step therapy rules.
- **recommendation**: Contains non-computable clinical guidance — treatment \
recommendations, lifestyle modifications, monitoring advice, referral \
criteria, patient education, process recommendations.
- **both**: Contains a mix of computable logic and narrative recommendations. \
Common in pharmacotherapy sections where treatment rules and clinical \
rationale are interleaved.
- **reference**: Contains information needed for interpretation but is not \
itself a recommendation or decision — abbreviation tables, glossaries, \
grading system definitions, blood pressure classification tables, \
comorbidity definitions.
- **skip**: Contains content not relevant to clinical extraction — \
methodology descriptions, author biographical information, conflict \
of interest disclosures, literature search strategy, funding sources, \
acknowledgments, references/bibliography.

Guidelines:
- Sections containing decision tables, threshold-based rules, or \
classification grids should be classified as "decision" or "both".
- Pharmacotherapy appendix tables with drug comparisons across clinical \
dimensions are high-value "decision" content — do NOT classify them as "skip".
- Abbreviation/glossary sections are "reference" — they are needed \
downstream even though they are not recommendations.
- Evidence summaries that only discuss study findings without making \
recommendations are "skip".
- Implementation/quality measure sections that describe how to operationalize \
guidelines are "recommendation" (type: process).
- Monitoring and implementation sections may also yield care-process items \
  for automation when they describe an operational sequence.
- When in doubt between "skip" and another classification, prefer the \
non-skip classification — downstream filtering can remove content, but \
cannot recover content that was skipped here.
"""

SECTION_CLASSIFICATION_USER = """\
Classify each section of this Clinical Practice Guideline. For each section, \
provide the section heading and your classification.

Respond as a JSON array of objects with these fields:
- "heading": the exact section heading text
- "classification": one of "decision", "recommendation", "both", "reference", "skip"
- "reason": one sentence explaining why (for auditability)

Sections to classify:

{section_list}
"""

ARCHETYPE_DETECTION_SYSTEM = """\
You are a clinical guideline analyst. Determine the format archetype of this \
Clinical Practice Guideline based on its structure and content.

The four archetypes are:
- **institutional**: Linear structure, appendix-heavy, single-column, branded. \
Published by hospital systems or professional organizations as standalone documents. \
Follows a clear skeleton: front matter, methods, background, recommendations, \
evidence, implementation, appendices.
- **journal-article**: Abstract-first, compact, two-column, recommendations appear \
early, evidence discussion as body. Published in medical journals. Detailed evidence \
tables often in supplements.
- **multi-module**: Hub-and-spoke with a master routing chart dispatching to \
condition-specific modules. Each module follows an identical internal template.
- **focused-policy**: Single clinical question, exhaustive evidence review, heavy \
comparative effectiveness narrative, lower computability.

Respond with a JSON object:
- "archetype": one of "institutional", "journal-article", "multi-module", "focused-policy"
- "confidence": "high", "medium", or "low"
- "reason": one sentence explaining why
"""

ARCHETYPE_DETECTION_USER = """\
Based on this section structure and the first page content, determine the \
format archetype of this CPG.

Section headings:
{section_headings}

First page content:
{first_page_content}
"""

GRADING_EXTRACTION_SYSTEM = """\
You are a clinical guideline analyst. Extract the evidence grading system \
definitions from this document. CPGs define their grading vocabulary \
(e.g., "Strong recommendation", "High-certainty evidence") inline, often \
in a methodology or background section.

Extract the grading definitions verbatim. If no formal grading system is \
defined, state "No formal grading system defined" and note any informal \
strength language used (e.g., "should", "may consider", "is recommended").

Respond as a JSON object:
- "grading_system": one of "GRADE", "COR-LOE", "GRADE-COR-hybrid", "simplified", "verb-implied", "ungraded"
- "definitions": verbatim text of the grading definitions from the document, or null if none found
"""

GRADING_EXTRACTION_USER = """\
Extract the evidence grading system definitions from this CPG content:

{content}
"""
