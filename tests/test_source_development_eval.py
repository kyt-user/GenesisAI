from pathlib import Path

from genesisai.evals.source_development import load_dataset, run_all


DATASET = Path(__file__).parent / "source_development_dataset" / "manifest.yaml"


def test_source_development_dataset_is_strict():
    data = load_dataset(DATASET)
    assert [case["id"] for case in data["cases"]] == [
        "html_continuation",
        "python_creation",
        "java_limited_verification",
        "unsupported_binary",
    ]


def test_source_development_simulation_passes(tmp_path):
    report = run_all(DATASET, tmp_path / "simulation")

    assert report["status"] == "passed"
    assert all(item["status"] == "passed" for item in report["results"])
    assert (tmp_path / "simulation" / "report-source-development.json").is_file()
