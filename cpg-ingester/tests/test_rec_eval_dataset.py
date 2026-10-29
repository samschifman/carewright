"""Dataset mistakes must fail before any model is constructed."""

import copy

import pytest
from rec_eval_fixtures import example_case, write_case


def test_load_valid_case(tmp_path):
    from benchmarks.recommendations.dataset import load_case

    _, ref = write_case(tmp_path)
    assert load_case(ref, tmp_path).golden.recommendations[0].id == "walk"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(schema_version="2.0.0"),
        lambda d: d.update(source_text=" "),
        lambda d: d.update(annotations=[]),
        lambda d: d["golden"].update(contract_version="99"),
        lambda d: d["golden"]["recommendations"][0].update(source_cpg="wrong"),
        lambda d: d["golden"]["recommendations"].append(
            copy.deepcopy(d["golden"]["recommendations"][0])
        ),
        lambda d: d["items"].append(copy.deepcopy(d["items"][0])),
        lambda d: d["annotations"][0].update(source_quote="not in source"),
        lambda d: d["annotations"][0].update(required_atoms=["invented"]),
        lambda d: d["items"][0].update(section="../../escape"),
    ],
)
def test_reject_invalid_case(tmp_path, mutation):
    from benchmarks.recommendations.dataset import load_case

    data = example_case()
    mutation(data)
    _, ref = write_case(tmp_path, data)
    with pytest.raises(ValueError):
        load_case(ref, tmp_path)


@pytest.mark.parametrize("key", ["source", "golden"])
def test_digest_mismatch(tmp_path, key):
    from benchmarks.recommendations.dataset import load_case

    _, ref = write_case(tmp_path)
    bad = ref.model_copy(
        update={"source_digests": {**ref.source_digests, key: "0" * 64}}
    )
    with pytest.raises(ValueError, match=f"{key}.*digest"):
        load_case(bad, tmp_path)


def test_ref_metadata_and_symlink_escape(tmp_path):
    from benchmarks.recommendations.dataset import load_case

    _, ref = write_case(tmp_path / "outside")
    root = tmp_path / "cases"
    root.mkdir()
    (root / "walking.json").symlink_to(tmp_path / "outside" / "walking.json")
    with pytest.raises(ValueError, match="root"):
        load_case(ref, root)
    with pytest.raises(ValueError, match="identity"):
        load_case(ref.model_copy(update={"corpus": "wrong"}), tmp_path / "outside")


def test_draft_is_development_only(tmp_path):
    from benchmarks.recommendations.dataset import load_case

    _, ref = write_case(tmp_path)
    with pytest.raises(ValueError, match="reviewed"):
        load_case(ref, tmp_path, require_reviewed=True)


def test_annotations_and_case_metadata_are_pinned(tmp_path):
    import json

    from benchmarks.recommendations.dataset import load_case

    data, ref = write_case(tmp_path)
    data["annotations"][0]["action_aliases"] = ["swim"]
    (tmp_path / "walking.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="case.*digest"):
        load_case(ref, tmp_path)
