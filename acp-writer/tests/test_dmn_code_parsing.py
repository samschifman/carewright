"""Tests for parsing DecisionVariable.codes from DMN XML."""

from acp_writer.api import _parse_dmn_metadata


DMN_WITHOUT_CODES = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
             name="Test Model" namespace="test">
  <inputData id="input_sbp" name="Systolic BP">
    <variable id="var_sbp" name="Systolic BP" typeRef="number"/>
  </inputData>
  <inputData id="input_diabetes" name="Has Diabetes">
    <variable id="var_diabetes" name="Has Diabetes" typeRef="boolean"/>
  </inputData>
  <decision id="d1" name="Test Decision">
    <variable id="var_d1" name="Test Decision" typeRef="string"/>
    <decisionTable id="dt1" hitPolicy="UNIQUE">
      <input id="i1"><inputExpression typeRef="number"><text>Systolic BP</text></inputExpression></input>
      <output id="o1" name="Result" typeRef="string"/>
      <rule id="r1"><inputEntry><text>&gt;= 140</text></inputEntry><outputEntry><text>"High"</text></outputEntry></rule>
    </decisionTable>
  </decision>
</definitions>"""


DMN_WITH_EXTENSION_CODES = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
             name="Test Model With Codes" namespace="test">
  <inputData id="input_sbp" name="Systolic BP">
    <variable id="var_sbp" name="Systolic BP" typeRef="number"/>
    <extensionElements>
      <clinicalCode system="http://loinc.org" code="8480-6"/>
    </extensionElements>
  </inputData>
  <inputData id="input_diabetes" name="Has Diabetes">
    <variable id="var_diabetes" name="Has Diabetes" typeRef="boolean"/>
    <extensionElements>
      <clinicalCode system="http://snomed.info/sct" code="44054006"/>
    </extensionElements>
  </inputData>
  <decision id="d1" name="Test Decision">
    <variable id="var_d1" name="Test Decision" typeRef="string"/>
    <decisionTable id="dt1" hitPolicy="UNIQUE">
      <input id="i1"><inputExpression typeRef="number"><text>Systolic BP</text></inputExpression></input>
      <output id="o1" name="Result" typeRef="string"/>
      <rule id="r1"><inputEntry><text>&gt;= 140</text></inputEntry><outputEntry><text>"High"</text></outputEntry></rule>
    </decisionTable>
  </decision>
</definitions>"""


DMN_WITH_DESCRIPTION_CODES = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
             name="Test Model Desc Codes" namespace="test">
  <inputData id="input_sbp" name="Systolic BP">
    <variable id="var_sbp" name="Systolic BP" typeRef="number"/>
    <description>Office systolic blood pressure in mmHg. Code: http://loinc.org|8480-6</description>
  </inputData>
  <inputData id="input_egfr" name="eGFR">
    <variable id="var_egfr" name="eGFR" typeRef="number"/>
    <description>Estimated glomerular filtration rate. Code: http://loinc.org|33914-3</description>
  </inputData>
  <decision id="d1" name="Test Decision">
    <variable id="var_d1" name="Test Decision" typeRef="string"/>
    <decisionTable id="dt1" hitPolicy="UNIQUE">
      <input id="i1"><inputExpression typeRef="number"><text>Systolic BP</text></inputExpression></input>
      <output id="o1" name="Result" typeRef="string"/>
      <rule id="r1"><inputEntry><text>&gt;= 140</text></inputEntry><outputEntry><text>"High"</text></outputEntry></rule>
    </decisionTable>
  </decision>
</definitions>"""

DMN_WITH_EXTRACTION = DMN_WITHOUT_CODES.replace(
    '<inputData id="input_sbp" name="Systolic BP">',
    '<inputData id="input_sbp" name="Systolic BP">\n'
    '    <extensionElements><acp:extraction xmlns:acp="https://redhat.com/cpg-to-acp/dmn">'
    '<![CDATA[{"function":"observation_count","params":{"code":"http://loinc.org|8480-6","duration":"P3M"}}]]>'
    '</acp:extraction></extensionElements>',
)


class TestCodesAbsent:
    def test_no_codes_returns_none(self):
        summary = _parse_dmn_metadata(DMN_WITHOUT_CODES)
        for inp in summary.inputs:
            assert inp.codes is None

    def test_inputs_still_parsed(self):
        summary = _parse_dmn_metadata(DMN_WITHOUT_CODES)
        assert len(summary.inputs) == 2
        assert summary.inputs[0].name == "Systolic BP"
        assert summary.inputs[1].name == "Has Diabetes"


class TestCodesFromExtensionElements:
    def test_codes_extracted(self):
        summary = _parse_dmn_metadata(DMN_WITH_EXTENSION_CODES)
        sbp = summary.inputs[0]
        assert sbp.name == "Systolic BP"
        assert sbp.codes == ["http://loinc.org|8480-6"]

    def test_condition_code_extracted(self):
        summary = _parse_dmn_metadata(DMN_WITH_EXTENSION_CODES)
        diabetes = summary.inputs[1]
        assert diabetes.name == "Has Diabetes"
        assert diabetes.codes == ["http://snomed.info/sct|44054006"]


class TestCodesFromDescription:
    def test_codes_from_description_text(self):
        summary = _parse_dmn_metadata(DMN_WITH_DESCRIPTION_CODES)
        sbp = summary.inputs[0]
        assert sbp.codes == ["http://loinc.org|8480-6"]
        assert sbp.description is not None
        assert "systolic" in sbp.description.lower()

    def test_egfr_code_from_description(self):
        summary = _parse_dmn_metadata(DMN_WITH_DESCRIPTION_CODES)
        egfr = summary.inputs[1]
        assert egfr.codes == ["http://loinc.org|33914-3"]


def test_temporal_extraction_is_parsed_into_contract():
    summary = _parse_dmn_metadata(DMN_WITH_EXTRACTION)
    assert summary.inputs[0].extraction.function == "observation_count"
    assert summary.inputs[0].extraction.params["duration"] == "P3M"


def test_temporal_golden_carries_observation_count_metadata():
    from pathlib import Path

    golden = (Path(__file__).parent / "fixtures" / "dmn" / "glycemic-escalation-monitoring.dmn").read_text()
    summary = _parse_dmn_metadata(golden)
    extraction = summary.inputs[0].extraction
    assert extraction.function == "observation_count"
    assert extraction.params == {
        "code": "http://loinc.org|4548-4",
        "duration": "P6M",
        "threshold": 9,
        "comparator": "ge",
    }


class TestNamespaceTolerance:
    def test_definitions_namespace_is_recorded_for_bpmn_compilation(self):
        summary = _parse_dmn_metadata(DMN_WITHOUT_CODES)
        assert summary.namespace == "test"

    def test_legacy_1_3_namespace_still_parses(self):
        """Metadata parsing is namespace-tolerant: a 1.3-namespace document still
        parses so mixed-vintage models keep working after the 1.4 migration."""
        legacy = DMN_WITHOUT_CODES.replace(
            "https://www.omg.org/spec/DMN/20211108/MODEL/",
            "https://www.omg.org/spec/DMN/20191111/MODEL/",
        )
        summary = _parse_dmn_metadata(legacy)
        assert len(summary.inputs) == 2
        assert summary.inputs[0].name == "Systolic BP"


class TestExistingBehaviorPreserved:
    def test_golden_dmn_still_parses(self):
        """Verify the existing golden DMN files still parse correctly."""
        from pathlib import Path

        golden_dir = Path(__file__).parent / "fixtures" / "dmn"
        for dmn_file in golden_dir.glob("*.dmn"):
            dmn_xml = dmn_file.read_text()
            summary = _parse_dmn_metadata(dmn_xml)
            assert summary.name
            assert len(summary.inputs) > 0
            for inp in summary.inputs:
                assert inp.codes is None
