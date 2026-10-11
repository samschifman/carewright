"""FHIR Syntax Validation — structural checks for FHIR Bundles.

Validates required fields, reference resolution, coded field
completeness, and AI Transparency IG compliance.
"""

from typing import Any


def validate_fhir_bundle(bundle: dict) -> list[str]:
    """Run all structural checks on a FHIR Bundle. Returns list of error strings."""
    errors: list[str] = []
    errors.extend(_check_bundle_fields(bundle))
    errors.extend(_check_resources(bundle))
    errors.extend(_check_references(bundle))
    errors.extend(_check_automation_references(bundle))
    errors.extend(_check_ai_transparency(bundle))
    return errors


def _check_bundle_fields(bundle: dict) -> list[str]:
    errors = []
    if bundle.get("resourceType") != "Bundle":
        errors.append("Missing or wrong resourceType (expected 'Bundle')")
    if not bundle.get("type"):
        errors.append("Bundle missing 'type' field")
    if not bundle.get("entry"):
        errors.append("Bundle has no entries")
    return errors


def _check_resources(bundle: dict) -> list[str]:
    errors = []
    for i, entry in enumerate(bundle.get("entry", [])):
        resource = entry.get("resource")
        if not resource:
            errors.append(f"Entry {i} has no resource")
            continue

        rt = resource.get("resourceType")
        if not rt:
            errors.append(f"Entry {i} missing resourceType")
            continue

        if not resource.get("id"):
            errors.append(f"{rt} at entry {i} missing id")

        if bundle.get("type") == "transaction":
            if not entry.get("request"):
                errors.append(f"{rt} at entry {i} missing request (transaction bundle)")

        _check_required_fields(resource, rt, i, errors)
        _check_coded_fields(resource, rt, i, errors)

    return errors


def _check_required_fields(resource: dict, rt: str, idx: int, errors: list[str]) -> None:
    if rt == "CarePlan":
        for field in ["status", "intent", "subject"]:
            if not resource.get(field):
                errors.append(f"CarePlan at entry {idx} missing '{field}'")
    elif rt == "Goal":
        for field in ["lifecycleStatus", "description", "subject"]:
            if not resource.get(field):
                errors.append(f"Goal at entry {idx} missing '{field}'")
    elif rt == "MedicationRequest":
        for field in ["status", "intent", "subject", "medicationCodeableConcept"]:
            if not resource.get(field):
                errors.append(f"MedicationRequest at entry {idx} missing '{field}'")
    elif rt == "ServiceRequest":
        for field in ["status", "intent", "subject", "code"]:
            if not resource.get(field):
                errors.append(f"ServiceRequest at entry {idx} missing '{field}'")
    elif rt == "Task":
        for field in ["status", "intent", "for", "code"]:
            if not resource.get(field):
                errors.append(f"Task at entry {idx} missing '{field}'")
        if not resource.get("input"):
            errors.append(f"Task at entry {idx} missing 'input'")
    elif rt == "DocumentReference":
        for field in ["status", "content"]:
            if not resource.get(field):
                errors.append(f"DocumentReference at entry {idx} missing '{field}'")
        for content_index, content in enumerate(resource.get("content", [])):
            attachment = content.get("attachment") or {}
            if not attachment.get("contentType"):
                errors.append(
                    f"DocumentReference at entry {idx} content[{content_index}] missing attachment.contentType"
                )
            if not (attachment.get("data") or attachment.get("url")):
                errors.append(
                    f"DocumentReference at entry {idx} content[{content_index}] attachment needs data or url"
                )


def _check_coded_fields(resource: dict, rt: str, idx: int, errors: list[str]) -> None:
    codings = _find_codings(resource)
    for path, coding in codings:
        if not coding.get("system"):
            errors.append(f"{rt} at entry {idx}: coding at {path} missing 'system'")
        if not coding.get("code"):
            errors.append(f"{rt} at entry {idx}: coding at {path} missing 'code'")


def _find_codings(obj: Any, path: str = "") -> list[tuple[str, dict]]:
    results: list[tuple[str, dict]] = []
    if isinstance(obj, dict):
        if "system" in obj and "code" in obj and "resourceType" not in obj:
            results.append((path, obj))
        for key, val in obj.items():
            if key in ("meta", "request"):
                continue
            results.extend(_find_codings(val, f"{path}.{key}" if path else key))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            results.extend(_find_codings(item, f"{path}[{i}]"))
    return results


def _check_references(bundle: dict) -> list[str]:
    errors = []
    known_urls: set[str] = set()
    for entry in bundle.get("entry", []):
        url = entry.get("fullUrl", "")
        if url:
            known_urls.add(url)
        resource = entry.get("resource", {})
        rid = resource.get("id", "")
        rt = resource.get("resourceType", "")
        if rid and rt:
            known_urls.add(f"{rt}/{rid}")
            known_urls.add(f"urn:uuid:{rid}")

    refs = _find_references(bundle)
    for path, ref_str in refs:
        if ref_str.startswith("Patient/"):
            continue
        if ref_str not in known_urls:
            errors.append(f"Unresolved reference at {path}: {ref_str}")

    return errors


def _check_automation_references(bundle: dict) -> list[str]:
    """Resolve references nested under the project automation extensions."""
    errors: list[str] = []
    known: set[str] = set()
    for entry in bundle.get("entry", []):
        url = entry.get("fullUrl")
        resource = entry.get("resource") or {}
        rid, rt = resource.get("id"), resource.get("resourceType")
        if url:
            known.add(url)
        if rid and rt:
            known.update({f"{rt}/{rid}", f"urn:uuid:{rid}"})

    def require_reference(value: Any, path: str) -> None:
        reference = value.get("reference") if isinstance(value, dict) else None
        if not isinstance(reference, str):
            errors.append(f"Missing automation reference at {path}")
        elif reference not in known:
            errors.append(f"Unresolved automation reference at {path}: {reference}")

    automation_ext_url = (
        "https://github.com/samschifman/cpg-to-acp/fhir/StructureDefinition/"
        "activity-automation"
    )
    for entry_index, entry in enumerate(bundle.get("entry", [])):
        resource = entry.get("resource") or {}
        if resource.get("resourceType") != "CarePlan":
            continue
        for activity_index, activity in enumerate(resource.get("activity", [])):
            for extension_index, extension in enumerate(activity.get("extension", [])):
                if extension.get("url") != automation_ext_url:
                    continue
                components: dict[str, list[dict]] = {}
                for item in extension.get("extension", []):
                    components.setdefault(item.get("url", ""), []).append(item)
                for name in ("definition", "instance"):
                    values = components.get(name, [])
                    if len(values) != 1:
                        errors.append(
                            f"CarePlan at entry {entry_index} activity[{activity_index}] automation extension must have one {name} reference"
                        )
                    else:
                        require_reference(
                            values[0].get("valueReference"),
                            f"CarePlan.activity[{activity_index}].extension[{extension_index}].{name}",
                        )
                if len(components.get("status", [])) != 1 or not components["status"][0].get("valueCode"):
                    errors.append(
                        f"CarePlan at entry {entry_index} activity[{activity_index}] automation extension needs one status code"
                    )

    # The Task.process-definition input is a nested valueReference and is part
    # of the project contract; the generic FHIR walker already checks it, but
    # this verifies exactly one such input exists.
    for entry_index, entry in enumerate(bundle.get("entry", [])):
        resource = entry.get("resource") or {}
        if resource.get("resourceType") != "Task":
            continue
        definitions = [
            item for item in resource.get("input", [])
            if any(
                coding.get("code") == "process-definition"
                for coding in ((item.get("type") or {}).get("coding") or [])
            )
        ]
        if len(definitions) != 1:
            errors.append(f"Task at entry {entry_index} must have one process-definition input")
        else:
            require_reference(
                definitions[0].get("valueReference"),
                f"Task.input process-definition at entry {entry_index}",
            )
    return errors


def _find_references(obj: Any, path: str = "") -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        if "reference" in obj and isinstance(obj["reference"], str):
            results.append((path, obj["reference"]))
        for key, val in obj.items():
            if key in ("meta", "request", "extension"):
                continue
            results.extend(_find_references(val, f"{path}.{key}" if path else key))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            results.extend(_find_references(item, f"{path}[{i}]"))
    return results


def _check_ai_transparency(bundle: dict) -> list[str]:
    errors = []
    has_device = False
    has_ai_provenance = False

    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        rt = resource.get("resourceType", "")

        security = resource.get("meta", {}).get("security", [])
        aiast = [s for s in security if s.get("code") == "AIAST"]
        if not aiast:
            errors.append(f"{rt}/{resource.get('id', '?')} missing AIAST meta.security")

        if rt == "Device":
            type_codings = resource.get("type", {}).get("coding", [])
            if any(c.get("code") == "Artificial-Intelligence" for c in type_codings):
                has_device = True

        if rt == "Provenance":
            profiles = resource.get("meta", {}).get("profile", [])
            if any("AI-Provenance" in p for p in profiles):
                has_ai_provenance = True

    if not has_device:
        errors.append("Missing AI-Device resource")
    if not has_ai_provenance:
        errors.append("Missing AI-Provenance resource")

    return errors
