"""Tests for the conservative PHI scanner."""

from __future__ import annotations

import pytest

from cpg_contracts.automation.validators.phi import scan_phi


def test_supplied_patient_values_are_errors_without_echoing_the_value() -> None:
    findings = scan_phi("<documentation>Plan includes Jane Q Patient</documentation>", ["Jane Q Patient"])

    finding = next(item for item in findings if item.code == "known-value")
    assert finding.severity == "ERROR"
    assert "Jane Q Patient" not in finding.message


def test_mrn_like_identifier_is_an_error() -> None:
    findings = scan_phi("<bpmn2:documentation>Record AB1234567</bpmn2:documentation>")

    assert any(item.code == "mrn-like" and item.severity == "ERROR" for item in findings)


def test_explicit_date_of_birth_is_an_error() -> None:
    findings = scan_phi('<documentation>DOB: 1980-01-02</documentation>')

    assert any(item.code == "date-of-birth" and item.severity == "ERROR" for item in findings)


def test_date_marked_as_birth_date_is_an_error() -> None:
    findings = scan_phi("born on January 2, 1980")

    assert any(item.code == "date-of-birth" and item.severity == "ERROR" for item in findings)


def test_name_like_text_in_xml_labels_is_a_warning() -> None:
    findings = scan_phi('<bpmn2:task name="Jane Smith"/>')

    assert any(item.code == "name-like" and item.severity == "WARNING" for item in findings)


def test_dmn_variable_name_is_not_treated_as_a_person_name() -> None:
    findings = scan_phi('<bpmn2:dataInput id="lab_order" name="Lab Order"/>')

    assert findings == []


def test_name_like_text_in_literal_assignments_is_a_warning() -> None:
    text = "<assignment><from>John Smith</from><to>recipient</to></assignment>"
    findings = scan_phi(text)

    assert any(item.code == "name-like" and item.severity == "WARNING" for item in findings)


def test_generic_clinical_text_has_no_phi_findings() -> None:
    findings = scan_phi('<bpmn2:task name="Review home blood pressure readings"/>')

    assert findings == []


def test_phi_scanner_is_traced_when_mlflow_is_installed() -> None:
    pytest.importorskip("mlflow")

    assert getattr(scan_phi, "__mlflow_traced__", False)
