"""Known DMN validation/compile error patterns and their targeted fixes.

When the syntax validator or the decision engine rejects a generated model, the
raw error is often terse. Matching it against a known pattern lets the creator
receive a concrete "here is the cause and the fix" hint on retry instead of just
the error text — which measurably shortens the repair loop.

This file is LIVING. Whenever a validator or compile error appears that no entry
here matches, add a new ``ErrorPattern`` for it:

    ErrorPattern(
        match="substring or regex found in the error text",
        cause="why the engine/validator rejected it",
        fix="the concrete change the creator should make",
        regex=True,  # omit for a plain case-insensitive substring match
    )

Keep ``match`` specific enough not to collide with unrelated errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorPattern:
    match: str
    cause: str
    fix: str
    regex: bool = False


# Seeded from the known failure classes: unescaped operators, FEEL name
# mismatches, missing structure, and duplicate identifiers.
ERROR_PATTERNS: list[ErrorPattern] = [
    ErrorPattern(
        match="Extraction annotation",
        cause="The generated DMN contains an invalid extraction annotation.",
        fix="Do not emit acp:extraction; remove it from the DMN XML. The system adds the manifest's mechanical annotation.",
    ),
    ErrorPattern(
        match="forbidden control character",
        cause="The DMN contains a non-printable control character copied from source text.",
        fix="Remove non-printable control characters (U+0000–U+001F except tab/newline) from the document; they usually come from copied text — retype the affected FEEL entry.",
    ),
    ErrorPattern(
        match="XML parse error",
        cause="A raw <, > or & in a FEEL expression breaks XML well-formedness.",
        fix="Wrap the FEEL text in <![CDATA[ ... ]]>; never use XML entities inside CDATA.",
    ),
    ErrorPattern(
        match="Wrong namespace",
        cause="The definitions element uses the wrong DMN MODEL namespace URI.",
        fix="Use the exact DMN MODEL namespace URI required by this project on "
            "the root <definitions> element.",
    ),
    ErrorPattern(
        match="No matching global declaration available for the validation root",
        cause="The XML root is not in the DMN 1.4 MODEL namespace.",
        fix="Use https://www.omg.org/spec/DMN/20211108/MODEL/ as the root xmlns.",
    ),
    ErrorPattern(
        match="This element is not expected",
        cause="DMN children are in an order not accepted by the schema.",
        fix="Order children as description, extensionElements, variable, informationRequirement, then the expression; put input/output before rule.",
    ),
    ErrorPattern(
        match="is not a valid value of the atomic type 'xs:ID'",
        cause="An XML id is not unique or is not a valid NCName.",
        fix="Use unique XML ids with no spaces and no leading digit.",
    ),
    ErrorPattern(
        match="unsupported FEEL unary test",
        cause="An input entry uses a FEEL form outside the supported unary-test subset.",
        fix="Use comparisons, ranges, quoted strings, booleans, null, contains(?, \"text\"), or valid and/or/not combinations.",
    ),
    ErrorPattern(
        match="string literal must be quoted",
        cause="A string output entry is a bare FEEL word or phrase.",
        fix="Wrap the literal in double quotes.",
    ),
    ErrorPattern(
        match="CDATA contains XML entities",
        cause="FEEL text contains entity references inside CDATA.",
        fix="Write the literal FEEL operator directly inside CDATA; do not use &lt;, &gt;, or &amp; there.",
    ),
    ErrorPattern(
        match="does not resolve to an existing id",
        cause="An information requirement points to an absent element.",
        fix="Set the href to the #id of an existing inputData or decision.",
    ),
    ErrorPattern(
        match="PRIORITY requires outputValues",
        cause="A PRIORITY table is missing its output precedence values.",
        fix="Add outputValues containing an ordered value list to every output column, or use FIRST/UNIQUE.",
    ),
    ErrorPattern(
        match="name contains FEEL token 'in'",
        cause="A FEEL-scoped name contains the reserved token in.",
        fix="Rename the decision, inputData, or scoped variable without FEEL keywords.",
    ),
    ErrorPattern(
        match="starts with FEEL keyword",
        cause="A FEEL-scoped name starts with a reserved keyword.",
        fix="Rename the decision, inputData, or scoped variable so it does not start with a FEEL keyword.",
    ),
    ErrorPattern(
        match="definitions namespace must not equal the DMN language namespace",
        cause="The definitions target namespace was copied from the DMN language namespace.",
        fix="Set definitions/@namespace to a unique per-model URI beginning with "
            "https://redhat.com/cpg-to-acp/dmn/.",
    ),
    ErrorPattern(
        match="missing hitPolicy",
        cause="A decisionTable has no hitPolicy attribute.",
        fix="Add a hitPolicy attribute (UNIQUE, FIRST, PRIORITY, or COLLECT) to "
            "every decisionTable.",
    ),
    ErrorPattern(
        match="missing typeRef",
        cause="An inputExpression or variable has no typeRef.",
        fix="Give every inputExpression and variable a typeRef "
            "(number, string, or boolean).",
    ),
    ErrorPattern(
        match=r"(inputEntries|outputEntries), expected",
        regex=True,
        cause="A rule has a different number of entries than the table has columns.",
        fix="Emit exactly one inputEntry per <input> column and one outputEntry "
            "per <output> column in every rule. Use '-' for any-value cells.",
    ),
    ErrorPattern(
        match="empty text",
        cause="A rule entry has empty <text>.",
        fix="Every inputEntry/outputEntry needs non-empty text; use '-' for an "
            "any-value input cell.",
    ),
    ErrorPattern(
        match="no informationRequirement",
        cause="A decision does not link to the inputData it consumes.",
        fix="Add an <informationRequirement> with <requiredInput href=\"#...\"/> "
            "to the decision for each inputData it uses.",
    ),
    ErrorPattern(
        match="missing variable element",
        cause="An inputData has no <variable> child.",
        fix="Give every inputData a <variable> child whose name matches the "
            "inputData name and carries a typeRef.",
    ),
    # Decision-engine (Drools/Kogito) compile errors — surfaced via the compile
    # check, not the local syntax validator.
    ErrorPattern(
        match=r"unable to (resolve|find)|not (found|resolved)|no such|unknown (variable|input)",
        regex=True,
        cause="A FEEL inputExpression references a name the engine cannot bind.",
        fix="Make each <input><inputExpression><text> match a declared "
            "inputData/variable @name (or an upstream decision variable) exactly, "
            "character for character.",
    ),
    ErrorPattern(
        match="does not match declared inputData or upstream decision variable names",
        cause="A FEEL inputExpression references a variable outside the decision's declared inputs.",
        fix="Use only variables declared by this decision's informationRequirements. A valid FEEL inputExpression may combine those exact declared names with arithmetic or other FEEL operators; do not invent variable names.",
    ),
    ErrorPattern(
        match="Duplicate id",
        regex=True,
        cause="Two elements share the same id (ids must be unique per document).",
        fix="Give every element a unique id; the definitions @id must differ from "
            "all others.",
    ),
]


def match_error_patterns(errors: list[str]) -> list[ErrorPattern]:
    """Return the known patterns whose match appears in any of ``errors``."""
    hits: list[ErrorPattern] = []
    for pattern in ERROR_PATTERNS:
        for err in errors:
            text = err or ""
            if pattern.regex:
                found = re.search(pattern.match, text, re.IGNORECASE)
            else:
                found = pattern.match.lower() in text.lower()
            if found:
                hits.append(pattern)
                break
    return hits


def format_error_pattern_hints(errors: list[str]) -> str:
    """Render matched patterns as feedback lines, or '' if none match."""
    hits = match_error_patterns(errors)
    if not hits:
        return ""
    lines = [f"- Known error pattern: {p.cause} Fix: {p.fix}" for p in hits]
    return "\n".join(lines)
