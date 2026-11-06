import pytest

from app.services.musicbrainz.categories import (
    CATEGORIES,
    DEFAULT_MISSING_TYPES,
    category_for,
    year_of,
)


@pytest.mark.parametrize(
    ("primary", "secondary", "expected"),
    [
        ("Album", [], "album"),
        ("EP", [], "ep"),
        ("Single", [], "single"),
        ("Broadcast", [], "broadcast"),
        ("Other", [], "other"),
        ("Album", ["Live"], "live"),
        ("Album", ["Compilation"], "compilation"),
        ("Album", ["Soundtrack"], "soundtrack"),
        ("Single", ["Remix"], "remix"),
        ("Album", ["Compilation", "Live"], "compilation"),  # priority order
        ("Album", ["Soundtrack", "Compilation"], "soundtrack"),
        ("Album", ["DJ-mix"], "dj-mix"),
        ("Album", ["Mixtape/Street"], "mixtape"),
        ("Album", ["Audio drama"], "audio-drama"),
        ("Album", ["Field recording"], "field-recording"),
        ("Album", ["live"], "live"),  # case-insensitive
        (None, [], "other"),  # a missing primary type happens in the real data
        (None, ["Live"], "live"),
        ("Album", ["Some Future Type"], "album"),  # unknown secondary types are ignored
        ("Weird", [], "other"),
    ],
)
def test_category(primary, secondary, expected):
    assert category_for(primary, secondary) == expected
    assert expected in CATEGORIES


def test_default_missing_types_are_studio_releases():
    assert DEFAULT_MISSING_TYPES == ("album", "ep")
    assert set(DEFAULT_MISSING_TYPES) <= set(CATEGORIES)


@pytest.mark.parametrize(
    ("date", "year"),
    [
        ("1968", 1968),
        ("1968-03", 1968),
        ("1968-03-22", 1968),
        ("", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_year_of(date, year):
    assert year_of(date) == year
