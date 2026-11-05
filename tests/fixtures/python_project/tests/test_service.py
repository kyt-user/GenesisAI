from taskboard.service import normalize_tags

def test_normalizes_and_deduplicates():
    assert normalize_tags([" Python ", "PYTHON", "", " tools "]) == ["python", "tools"]

def test_retains_input_order():
    assert normalize_tags(["z", "a", "Z"]) == ["z", "a"]
