from src.pagination import paginate


def test_first_page():
    page = paginate(list(range(25)), page=1, per_page=10)
    assert page.items == list(range(10))
    assert page.page == 1
    assert page.total == 25


def test_second_page():
    page = paginate(list(range(25)), page=2, per_page=10)
    assert page.items == list(range(10, 20))


def test_total_pages_exact_multiple():
    page = paginate(list(range(20)), page=1, per_page=10)
    assert page.total_pages == 2


def test_total_pages_with_remainder():
    """25 items at 10 per page needs 3 pages, not 2."""
    page = paginate(list(range(25)), page=1, per_page=10)
    assert page.total_pages == 3


def test_last_partial_page_is_reachable():
    page = paginate(list(range(25)), page=3, per_page=10)
    assert page.items == [20, 21, 22, 23, 24]
    assert page.has_next is False


def test_has_next_on_a_partial_final_page():
    page = paginate(list(range(25)), page=2, per_page=10)
    assert page.has_next is True, "page 2 of 3 must report a next page"


def test_has_previous():
    assert paginate(list(range(25)), page=1).has_previous is False
    assert paginate(list(range(25)), page=2).has_previous is True


def test_single_item():
    page = paginate([1], page=1, per_page=10)
    assert page.total_pages == 1
    assert page.has_next is False


def test_empty_list():
    page = paginate([], page=1, per_page=10)
    assert page.items == []
    assert page.total_pages == 0
    assert page.has_next is False


def test_page_past_the_end_is_empty():
    page = paginate(list(range(5)), page=99, per_page=10)
    assert page.items == []


def test_to_dict():
    data = paginate(list(range(25)), page=1, per_page=10).to_dict()
    assert data["total_pages"] == 3
    assert data["has_next"] is True
