from benchmarks.unsafe_nested_dictionary_access import display_name


def test_missing_nested_profile_uses_anonymous_fallback():
    assert display_name({"user": {}}) == "Anonymous"
