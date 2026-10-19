from benchmarks.mutable_default_argument import append_item


def test_each_call_starts_with_a_fresh_collection():
    append_item("first")

    assert append_item("second") == ["second"]
