from benchmarks.off_by_one_pagination import paginate


def test_pagination_returns_every_item_on_the_requested_page():
    items = ["a", "b", "c", "d"]

    assert paginate(items, page=1, page_size=2) == ["c", "d"]
