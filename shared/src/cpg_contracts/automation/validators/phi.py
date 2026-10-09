"""Conservative PHI heuristics for generated BPMN text.

Known-value and MRN matches are high-confidence errors. Dates are errors only
when explicitly labeled as a date of birth, which avoids treating clinical
cadence dates as patient identifiers. Two title-cased words are warnings only
when they occur in a BPMN name, documentation, or literal assignment; generic
labels can still look like names, so callers should treat those findings as
review prompts rather than proof of PHI.
"""

from __future__ import annotations

from collections.abc import Iterable
import html
import re

from cpg_contracts.automation._tracing import trace
from cpg_contracts.automation.validators.results import Finding


_MRN_RE = re.compile(r"\b[A-Z]{2,3}\d{5,}\b")
_DATE = (
    r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?\s+\d{1,2},?\s+\d{4})"
)
_DOB_RE = re.compile(
    rf"\b(?:DOB|D\.O\.B\.|date\s+of\s+birth|birth\s*date|born(?:\s+on)?)\b"
    rf"\s*(?:(?:is|:|=)\s*)?{_DATE}",
    re.IGNORECASE,
)
_TITLE_CASED_NAME = r"[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?"
_NAME_PAIR_RE = re.compile(
    rf"\b{_TITLE_CASED_NAME}\s+{_TITLE_CASED_NAME}\b"
)
_NAME_ATTRIBUTE_RE = re.compile(r"\bname\s*=\s*(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)
_DATA_VARIABLE_TAG_RE = re.compile(
    r"<(?:[A-Za-z_][\w.-]*:)?(?:dataInput|dataOutput)\b[^>]*>",
    re.IGNORECASE | re.DOTALL,
)
_DOCUMENTATION_RE = re.compile(
    r"<(?:[A-Za-z_][\w.-]*:)?documentation\b[^>]*>(.*?)</(?:[A-Za-z_][\w.-]*:)?documentation\s*>",
    re.IGNORECASE | re.DOTALL,
)
_ASSIGNMENT_RE = re.compile(
    r"<(?:[A-Za-z_][\w.-]*:)?assignment\b[^>]*>(.*?)</(?:[A-Za-z_][\w.-]*:)?assignment\s*>",
    re.IGNORECASE | re.DOTALL,
)
_ASSIGNMENT_FROM_RE = re.compile(
    r"<(?:[A-Za-z_][\w.-]*:)?from\b[^>]*>(.*?)</(?:[A-Za-z_][\w.-]*:)?from\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _known_value_pattern(value: str) -> re.Pattern[str] | None:
    value = value.strip()
    if not value:
        return None
    escaped = re.escape(value)
    escaped = escaped.replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


def _literal_assignment_values(text: str) -> list[str]:
    values = []
    for assignment in _ASSIGNMENT_RE.finditer(text):
        for literal in _ASSIGNMENT_FROM_RE.finditer(assignment.group(1)):
            value = literal.group(1)
            value = value.replace("<![CDATA[", "").replace("]]>", "")
            values.append(html.unescape(_TAG_RE.sub(" ", value)))
    return values


def _name_contexts(text: str) -> list[str]:
    text_without_data_variable_names = _DATA_VARIABLE_TAG_RE.sub("", text)
    contexts = [
        match.group(2)
        for match in _NAME_ATTRIBUTE_RE.finditer(text_without_data_variable_names)
    ]
    contexts.extend(match.group(1) for match in _DOCUMENTATION_RE.finditer(text))
    contexts.extend(_literal_assignment_values(text))
    return contexts


@trace(name="bpmn.phi")
def scan_phi(text: str, known_values: Iterable[str] = ()) -> list[Finding]:
    """Find supplied patient values, MRN-like ids, DOBs, and name-like labels.

    Messages deliberately omit matched text so a PHI finding does not repeat
    the sensitive value in logs or validation records.
    """
    text = html.unescape(text)
    findings = []
    if any(
        (pattern := _known_value_pattern(value)) is not None and pattern.search(text)
        for value in known_values
    ):
        findings.append(
            Finding(
                rung="L3",
                severity="ERROR",
                code="known-value",
                message="BPMN text contains a value supplied from patient demographics",
            )
        )
    if _MRN_RE.search(text):
        findings.append(
            Finding(
                rung="L3",
                severity="ERROR",
                code="mrn-like",
                message="BPMN text contains an identifier matching the MRN-like pattern",
            )
        )
    if _DOB_RE.search(text):
        findings.append(
            Finding(
                rung="L3",
                severity="ERROR",
                code="date-of-birth",
                message="BPMN text contains a date explicitly labeled as a date of birth",
            )
        )
    if any(_NAME_PAIR_RE.search(context) for context in _name_contexts(text)):
        findings.append(
            Finding(
                rung="L3",
                severity="WARNING",
                code="name-like",
                message="BPMN name, documentation, or literal assignment contains a name-like phrase",
            )
        )
    return findings
