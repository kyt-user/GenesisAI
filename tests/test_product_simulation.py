"""P7 五场景离线总验收。"""

from pathlib import Path

from genesisai.evals.simulation import load_dataset, run_all


DATASET = Path(__file__).parent / "product_acceptance_dataset"


def test_dataset_is_strict_and_has_five_isolated_cases():
    cases = load_dataset(DATASET)
    assert [case[0]["id"] for case in cases] == [
        "case_01_code_fix", "case_02_file_management", "case_03_web_research",
        "case_04_memory_and_skill", "case_05_office_files",
    ]
    assert all("version" not in case for case, _ in cases)


def test_all_five_offline_product_simulations_pass(tmp_path):
    report = run_all(DATASET, tmp_path / "results", mode="offline")
    assert report["status"] == "passed"
    assert len(report["results"]) == 5
    assert all(item["status"] == "passed" and item["fixture_unchanged"] for item in report["results"])
    assert (tmp_path / "results" / "report-offline.json").is_file()
