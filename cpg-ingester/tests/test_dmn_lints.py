"""Focused tests for deterministic DMN structural and FEEL lints."""

from pathlib import Path

from lxml import etree

from cpg_ingester.nodes.dmn_syntax_validator import dmn_syntax_validator
from cpg_ingester.validators.dmn_syntax import (
    check_feel_entries,
    check_feel_names,
    check_extraction_annotations,
    check_hit_policies,
    check_definitions_namespace,
    check_ids_and_references,
    check_input_expressions,
    check_raw_xml,
    check_type_refs,
    check_variable_names,
    validate_dmn,
)

GOLDEN_DIR = Path(__file__).parent.parent / "data" / "golden"

VALID_DMN = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
             id="definitions_example" name="Example"
             namespace="https://redhat.com/cpg-to-acp/dmn/example">
  <inputData id="input_age" name="Age">
    <variable id="variable_age" name="Age" typeRef="number"/>
  </inputData>
  <decision id="decision_recommendation" name="Recommendation">
    <variable id="variable_recommendation" name="Recommendation" typeRef="string"/>
    <informationRequirement id="requirement_age">
      <requiredInput href="#input_age"/>
    </informationRequirement>
    <decisionTable id="table_recommendation" hitPolicy="UNIQUE">
      <input id="input_column_age">
        <inputExpression id="expression_age" typeRef="number">
          <text><![CDATA[Age]]></text>
        </inputExpression>
      </input>
      <output id="output_recommendation" name="Recommendation" typeRef="string"/>
      <rule id="rule_adult">
        <inputEntry id="entry_age"><text><![CDATA[>= 18]]></text></inputEntry>
        <outputEntry id="entry_recommendation"><text><![CDATA["Adult"]]></text></outputEntry>
      </rule>
    </decisionTable>
  </decision>
</definitions>
"""


def _root(xml: str = VALID_DMN) -> etree._Element:
    return etree.fromstring(xml.encode())


def test_ids_and_references_accept_valid_and_reject_broken_links_and_duplicates():
    assert check_ids_and_references(_root()) == ([], [])
    broken = VALID_DMN.replace('href="#input_age"', 'href="#missing"').replace(
        'id="output_recommendation"', 'id="input_age"'
    )
    errors, _ = check_ids_and_references(_root(broken))
    assert any("Duplicate id 'input_age'" in error for error in errors)
    assert any("does not resolve" in error for error in errors)


def test_variable_names_accept_match_and_reject_mismatch():
    assert check_variable_names(_root()) == ([], [])
    mismatch = VALID_DMN.replace('id="variable_age" name="Age"',
                                 'id="variable_age" name="Patient Age"')
    errors, _ = check_variable_names(_root(mismatch))
    assert any("does not match parent name" in error for error in errors)


def test_definitions_namespace_requires_per_model_target_namespace():
    assert check_definitions_namespace(_root()) == ([], [])

    language_namespace = VALID_DMN.replace(
        "namespace=\"https://redhat.com/cpg-to-acp/dmn/example\"",
        "namespace=\"https://www.omg.org/spec/DMN/20211108/MODEL/\"",
    )
    errors, warnings = check_definitions_namespace(_root(language_namespace))
    assert any("must not equal the DMN language namespace" in error for error in errors)
    assert warnings == []

    missing_namespace = VALID_DMN.replace(
        ' namespace="https://redhat.com/cpg-to-acp/dmn/example"', ""
    )
    errors, warnings = check_definitions_namespace(_root(missing_namespace))
    assert errors == []
    assert any("missing or should start" in warning for warning in warnings)


def test_input_expressions_accept_declared_name_and_reject_unknown_name():
    assert check_input_expressions(_root()) == ([], [])
    unknown = VALID_DMN.replace("<![CDATA[Age]]>", "<![CDATA[Age in Years]]>")
    errors, _ = check_input_expressions(_root(unknown))
    assert any("does not match" in error for error in errors)


def test_input_expressions_accept_arithmetic_over_declared_escaped_names():
    additional_inputs = """
  <inputData id="input_weekly_home_bp" name="Weekly Home BP Readings">
    <variable id="variable_weekly_home_bp" name="Weekly Home BP Readings" typeRef="number"/>
  </inputData>
  <inputData id="input_lower_threshold" name="Clinician-Set Lower Threshold">
    <variable id="variable_lower_threshold" name="Clinician-Set Lower Threshold" typeRef="number"/>
  </inputData>
"""
    xml = VALID_DMN.replace(
        '<decision id="decision_recommendation"',
        additional_inputs + '  <decision id="decision_recommendation"',
    ).replace(
        '<decisionTable id="table_recommendation"',
        '<informationRequirement id="requirement_weekly_home_bp">'
        '<requiredInput href="#input_weekly_home_bp"/></informationRequirement>'
        '<informationRequirement id="requirement_lower_threshold">'
        '<requiredInput href="#input_lower_threshold"/></informationRequirement>'
        '<decisionTable id="table_recommendation"',
    ).replace(
        "<![CDATA[Age]]>",
        "<![CDATA[`Weekly Home BP Readings` - `Clinician-Set Lower Threshold`]]>",
    )

    assert check_input_expressions(_root(xml)) == ([], [])


def test_extraction_annotations_accept_consecutive_above_without_comparator():
    xml = VALID_DMN.replace(
        "</inputData>",
        "<extensionElements><extraction><![CDATA[{\"function\":\"consecutive_above\","
        "\"params\":{\"code\":\"http://loinc.org|8480-6\",\"threshold\":140}}]]>"
        "</extraction></extensionElements></inputData>",
        1,
    )
    assert check_extraction_annotations(_root(xml)) == ([], [])


def test_feel_entries_cover_lists_negation_combinations_and_functions():
    expressions = (
        '">= 18, [65..120]"',
        '"not(\"A\", \"B\")"',
        '">= 18 and < 65"',
        '"contains(?, \"statin\") and (contains(?, \"bile acid\") '
        'or contains(?, \"nicotinic acid\"))"',
    )
    for expression in expressions:
        xml = VALID_DMN.replace(">= 18", expression[1:-1])
        assert check_feel_entries(_root(xml)) == ([], [])

    invalid = VALID_DMN.replace(">= 18", "Age >= 18").replace('"Adult"', "Adult")
    errors, _ = check_feel_entries(_root(invalid))
    assert any("unsupported FEEL unary test" in error for error in errors)
    assert any("string literal must be quoted" in error for error in errors)


def test_type_refs_accept_builtins_and_declared_item_definitions():
    assert check_type_refs(_root()) == ([], [])
    declared = VALID_DMN.replace(
        "  <inputData",
        "  <itemDefinition id=\"type_recommendation\" name=\"tRecommendation\">"
        "<typeRef>string</typeRef></itemDefinition>\n  <inputData",
    ).replace('name="Recommendation" typeRef="string"/>',
              'name="Recommendation" typeRef="tRecommendation"/>')
    assert check_type_refs(_root(declared)) == ([], [])

    invalid = VALID_DMN.replace('typeRef="number"', 'typeRef="bad type"', 1)
    errors, _ = check_type_refs(_root(invalid))
    assert any("contains spaces" in error for error in errors)


def test_hit_policy_priority_requires_values_and_first_is_only_a_warning():
    assert check_hit_policies(_root()) == ([], [])
    first = VALID_DMN.replace('hitPolicy="UNIQUE"', 'hitPolicy="FIRST"')
    errors, warnings = check_hit_policies(_root(first))
    assert errors == []
    assert any("order-dependent" in warning for warning in warnings)

    priority = VALID_DMN.replace('hitPolicy="UNIQUE"', 'hitPolicy="PRIORITY"')
    errors, _ = check_hit_policies(_root(priority))
    assert any("requires outputValues" in error for error in errors)

    priority_with_values = priority.replace(
        '<output id="output_recommendation" name="Recommendation" typeRef="string"/>',
        '<output id="output_recommendation" name="Recommendation" typeRef="string">'
        '<outputValues><text><![CDATA["Adult"]]></text></outputValues></output>',
    )
    assert check_hit_policies(_root(priority_with_values)) == ([], [])


def test_feel_names_accept_safe_names_and_reject_keywords():
    assert check_feel_names(_root()) == ([], [])
    invalid = VALID_DMN.replace('name="Age"', 'name="Age in Years"')
    errors, _ = check_feel_names(_root(invalid))
    assert any("contains FEEL token 'in'" in error for error in errors)

    leading = VALID_DMN.replace('name="Age"', 'name="If Eligible"')
    errors, _ = check_feel_names(_root(leading))
    assert any("starts with FEEL keyword 'if'" in error for error in errors)


def test_extraction_annotations_validate_function_and_parameters():
    valid = VALID_DMN.replace(
        '<variable id="variable_age" name="Age" typeRef="number"/>',
        '<extensionElements><acp:extraction xmlns:acp="https://redhat.com/cpg-to-acp/dmn">'
        '<![CDATA[{"function":"observation_count","params":{"code":"http://loinc.org|8480-6","duration":"P3M","threshold":140,"comparator":"ge"}}]]>'
        '</acp:extraction></extensionElements>\n'
        '<variable id="variable_age" name="Age" typeRef="number"/>',
    )
    assert check_extraction_annotations(_root(valid)) == ([], [])
    invalid = valid.replace('"observation_count"', '"not_a_function"')
    errors, _ = check_extraction_annotations(_root(invalid))
    assert any("Extraction annotation" in error for error in errors)


def test_raw_xml_accepts_clean_cdata_and_rejects_entities_and_controls():
    assert check_raw_xml(VALID_DMN) == ([], [])
    entities = VALID_DMN.replace(">= 18", "&gt;= 18")
    errors, _ = check_raw_xml(entities)
    assert any("FEEL will read literally" in error for error in errors)

    errors, _ = check_raw_xml(VALID_DMN.replace("Adult", "Adult\x01"))
    assert any("U+0001" in error for error in errors)


def test_goldens_have_no_errors_and_first_policy_warning_is_non_blocking():
    first_warning_count = 0
    for golden in GOLDEN_DIR.glob("*.dmn"):
        errors, warnings = validate_dmn(golden.read_text())
        assert errors == [], f"{golden.name}: {errors}"
        assert all("FIRST is order-dependent" in warning for warning in warnings)
        first_warning_count += sum("FIRST is order-dependent" in warning for warning in warnings)
    assert first_warning_count > 0


def test_feel_lint_rejects_unknown_types_boolean_case_and_smart_quotes():
    unknown = VALID_DMN.replace('typeRef="number"', 'typeRef="decimal"', 1)
    errors, _ = check_type_refs(_root(unknown))
    assert any("unknown typeRef" in error for error in errors)

    for value in ("True", "FALSE", '“Adult”'):
        xml = VALID_DMN.replace('"Adult"', value)
        errors, _ = check_feel_entries(_root(xml))
        assert errors, value


def test_input_expression_must_be_declared_by_that_decision():
    extra = VALID_DMN.replace(
        "</definitions>",
        '<inputData id="input_other" name="Other"><variable name="Other" typeRef="number"/></inputData>'
        "</definitions>",
    ).replace("<![CDATA[Age]]>", "<![CDATA[Other]]>")
    errors, _ = check_input_expressions(_root(extra))
    assert any("does not match" in error for error in errors)


def test_validator_node_returns_warnings_separately_from_retry_errors():
    first = VALID_DMN.replace('hitPolicy="UNIQUE"', 'hitPolicy="FIRST"')
    result = dmn_syntax_validator({"dmn_xml": first, "item": {"name": "Example"}})
    assert result["syntax_errors"] == []
    assert len(result["syntax_warnings"]) == 1
