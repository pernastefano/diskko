import pytest

from app.services.musicbrainz.matching import title_similarity, year_score
from app.services.musicbrainz.titles import album_key, strip_edition


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Lady Soul (Deluxe Edition)", "Lady Soul"),
        ("Lady Soul [Remastered 2011]", "Lady Soul"),
        ("Nevermind (20th Anniversary Super Deluxe Edition)", "Nevermind"),
        ("Nevermind - Remastered", "Nevermind"),
        ("Nevermind: Deluxe Edition", "Nevermind"),
        ("Rumours (Expanded & Remastered)", "Rumours"),
        ("Abbey Road (2019 Mix)", "Abbey Road"),
        ("Thriller (Bonus Track Version)", "Thriller"),
        ("The Wall (Disc 1)", "The Wall"),
        ("Revolver (Mono)", "Revolver"),
        # meaningful brackets are kept
        ("(What's the Story) Morning Glory?", "(What's the Story) Morning Glory?"),
        ("Songs (For Swingin' Lovers)", "Songs (For Swingin' Lovers)"),
        ("Ten", "Ten"),
        ("Remastered", "Remastered"),  # never reduce a title to nothing
    ],
)
def test_strip_edition(raw, expected):
    assert strip_edition(raw) == expected


def test_album_key_ignores_case_accents_punctuation_and_editions():
    assert album_key("Homogénic (Remastered)") == album_key("HOMOGENIC")
    assert album_key("Rock & Roll") == album_key("Rock and Roll")
    assert album_key("Aretha’s Gold") == album_key("Aretha's Gold")  # typographic apostrophe
    assert album_key("Lady Soul - Deluxe Edition") == album_key("Lady Soul")


@pytest.mark.parametrize(
    ("a", "b", "minimum", "maximum"),
    [
        ("Lady Soul", "Lady Soul (Deluxe)", 100, 100),
        ("In Utero", "In Utero [Remastered 2013]", 100, 100),
        ("Nevermind", "Nervermind", 85, 99),  # typo
        ("Discovery", "Homework", 0, 40),
        ("Greatest Hits Vol. 1", "Greatest Hits Vol. 2", 80, 99),
        ("", "Something", 0, 0),
    ],
)
def test_title_similarity(a, b, minimum, maximum):
    assert minimum <= title_similarity(a, b) <= maximum


@pytest.mark.parametrize(
    ("a", "b", "score"),
    [
        (1968, 1968, 1.0),
        (1968, 1969, 0.8),
        (1968, 1970, 0.5),
        (1968, 1971, 0.3),
        (1968, 1980, 0.0),
        (None, 1968, 0.5),
        (1968, None, 0.5),
    ],
)
def test_year_score(a, b, score):
    assert year_score(a, b) == score
