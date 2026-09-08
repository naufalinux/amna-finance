import pytest

from app.parsing.categories import CATEGORIES, UNCATEGORIZED, infer, normalize


def test_no_keyword_belongs_to_two_categories():
    """A duplicate makes inference depend on dict order, which is arbitrary."""
    seen: dict[str, str] = {}
    clashes = []
    for category, keywords in CATEGORIES.items():
        for keyword in keywords:
            if keyword in seen:
                clashes.append((keyword, seen[keyword], category))
            seen[keyword] = category
    assert clashes == []


@pytest.mark.parametrize(
    "note,expected",
    [
        ("grab", "Transport"),
        ("kopi hitam", "Food & Drink"),
        ("belanja di superindo", "Groceries"),
        ("token listrik", "Bills & Utilities"),
        ("obat batuk", "Health"),
        ("tiket bioskop", "Entertainment"),
        ("kursus bahasa", "Education"),
        ("gopay", "Fees & Transfers"),
        ("isi bensin", "Transport"),
        ("", UNCATEGORIZED),
        ("qwerty asdf", UNCATEGORIZED),
    ],
)
def test_infer(note, expected):
    assert infer(note) == expected


def test_infer_matches_whole_words_only():
    # "es" is a Food keyword; "test" must not match it.
    assert infer("test") == UNCATEGORIZED


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Food & Drink", "Food & Drink"),
        ("food", "Food & Drink"),
        ("FOOD AND DRINK", "Food & Drink"),
        ("transport", "Transport"),
        ("Bills", "Bills & Utilities"),
        (None, UNCATEGORIZED),
        ("", UNCATEGORIZED),
    ],
)
def test_normalize_snaps_llm_output_onto_the_taxonomy(raw, expected):
    assert normalize(raw) == expected
