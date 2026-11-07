"""The candidate selection engine: pure functions, so the tests build the answers by hand."""

import pytest

from app.quality import DEFAULT_PROFILES as P
from app.services.candidate_selection import (
    MIN_SCORE,
    Candidate,
    RemoteFile,
    RemoteResponse,
    Wanted,
    album_folder,
    group_by_subdir,
    group_files,
    query_variants,
    select_candidates,
)

ROOT = "@@abcd\\Music\\Aretha Franklin\\"


def flac(path, n=1, size=30_000_000, depth=16, rate=44100):
    name = f"{path}\\{n:02d} Track {n}.flac"
    return RemoteFile(name, size, "flac", None, depth, rate, 200)


def mp3(path, n=1, bit_rate=320, vbr=False):
    return RemoteFile(
        f"{path}\\{n:02d} Track {n}.mp3", 8_000_000, "mp3", bit_rate, None, None, 200, vbr
    )


def album(path, count, make=flac, **kw):
    return tuple(make(path, i + 1, **kw) for i in range(count))


def resp(user, files, free=True, queue=0, speed=2_000_000):
    return RemoteResponse(user, tuple(files), free, queue, speed)


WANTED = Wanted("Aretha Franklin", "Lady Soul", 1968, 10, "flac_standard")
GOOD = ROOT + "Lady Soul (1968) [FLAC]"


def pick(responses, wanted=WANTED, **kw):
    return select_candidates(responses, wanted, P, **kw)


# ------------------------------------------------------------------------ grouping


def test_files_are_grouped_by_user_and_folder():
    a = resp("alice", album(GOOD, 10) + album(ROOT + "Other Album", 3))
    b = resp("bob", album(GOOD, 10))
    groups = group_files([a, b])
    assert sorted((g.username, len(g.audio)) for g in groups) == [
        ("alice", 3),
        ("alice", 10),
        ("bob", 10),
    ]


def test_album_folder_splits_the_file_and_folds_disc_folders():
    assert album_folder("@@x\\Music\\Band\\Album\\01 Song.flac") == ("@@x\\Music\\Band\\Album", "")
    assert album_folder("Band/Album/CD1/01 Song.flac") == ("Band\\Album", "CD1")
    assert album_folder("Band\\Album\\Disc 2\\01 Song.flac") == ("Band\\Album", "Disc 2")
    assert album_folder("01 Song.flac") == ("", "")
    assert album_folder("Band\\Album\\Artwork\\cover.jpg") == ("Band\\Album\\Artwork", "")


def test_a_multi_disc_album_is_one_candidate_with_its_disc_folders():
    files = album(GOOD + "\\CD1", 5) + tuple(flac(GOOD + "\\CD2", i + 1) for i in range(5))
    [cand] = pick([resp("alice", files)])
    assert cand.track_count == 10 and cand.directory.endswith("Lady Soul (1968) [FLAC]")
    by_disc = group_by_subdir(cand)
    assert sorted(by_disc) == ["CD1", "CD2"] and all(len(v) == 5 for v in by_disc.values())


def test_non_audio_and_locked_files_are_ignored():
    extra = (
        RemoteFile(GOOD + "\\cover.jpg", 1000, "jpg"),
        RemoteFile(GOOD + "\\album.m3u", 100, "m3u"),
        RemoteFile(GOOD + "\\11 Locked.flac", 1, "flac", None, 16, 44100, 100, False, True),
    )
    [cand] = pick([resp("alice", album(GOOD, 10) + extra)])
    assert cand.track_count == 10 and all(f["filename"].endswith(".flac") for f in cand.files)
    assert "playlist" in cand.notes


# -------------------------------------------------------------------------- filters


def test_folders_that_are_not_the_album_are_dropped():
    other = resp("bob", album(ROOT + "Spirit in the Dark", 10))
    assert pick([other]) == []


def test_folders_with_too_few_or_too_many_tracks_are_dropped():
    assert pick([resp("a", album(GOOD, 4))]) == []  # a partial upload
    assert pick([resp("a", album(GOOD, 8))]) == []
    nine, ten = resp("a", album(GOOD, 9)), resp("b", album(GOOD, 10))
    assert [c.username for c in pick([nine, ten])] == [
        "b",
        "a",
    ]  # one short is allowed, ranks lower
    assert pick([resp("a", album(GOOD, 40))]) == []  # a whole discography


def test_one_track_of_slack_for_longer_albums():
    long_wanted = Wanted("Aretha Franklin", "Lady Soul", 1968, 12, "flac_standard")
    assert len(pick([resp("a", album(GOOD, 11))], long_wanted)) == 1
    short_wanted = Wanted("Aretha Franklin", "Lady Soul", 1968, 5, "flac_standard")
    assert pick([resp("a", album(GOOD, 4))], short_wanted) == []


def test_quality_below_the_minimum_is_dropped():
    mp3s = resp("a", album(GOOD.replace("FLAC", "MP3"), 10, make=mp3))
    assert pick([mp3s]) == []  # a FLAC minimum
    lenient = Wanted("Aretha Franklin", "Lady Soul", 1968, 10, "mp3_320")
    assert len(pick([mp3s], lenient)) == 1
    mp3_256 = resp("a", album(GOOD, 10, make=mp3, bit_rate=256))
    assert pick([mp3_256], lenient) == []
    assert len(pick([mp3_256], Wanted("Aretha Franklin", "Lady Soul", 1968, 10, "mp3_256"))) == 1


def test_the_weakest_track_sets_the_quality_of_the_folder():
    files = album(GOOD, 9) + (mp3(GOOD, 10, 128),)
    assert pick([resp("a", files)]) == []


def test_v0_variable_bit_rate_counts_as_the_top_lossy_profile():
    v0 = resp("a", album(GOOD, 10, make=mp3, bit_rate=245, vbr=True))
    wanted = Wanted("Aretha Franklin", "Lady Soul", 1968, 10, "mp3_320")
    [cand] = pick([v0], wanted)
    assert cand.quality == "mp3_320"
    cbr_245 = resp("a", album(GOOD, 10, make=mp3, bit_rate=245))
    assert pick([cbr_245], wanted) == []  # the same rate, but not VBR


def test_hi_res_needs_depth_and_rate():
    hires_wanted = Wanted("Aretha Franklin", "Lady Soul", 1968, 10, "flac_hires")
    cd = resp("a", album(GOOD, 10))
    hires = resp("b", album(GOOD, 10, depth=24, rate=96000))
    assert pick([cd], hires_wanted) == []
    [cand] = pick([cd, hires], hires_wanted)
    assert cand.username == "b" and cand.quality == "flac_hires"


def test_unknown_resolution_of_a_flac_counts_as_cd_quality():
    bare = tuple(RemoteFile(f"{GOOD}\\{i:02d} T.flac", 3, "flac") for i in range(1, 11))
    [cand] = pick([resp("a", bare)])
    assert cand.quality == "flac_standard"


# ----------------------------------------------------------------------- scoring


def test_better_quality_wins_when_everything_else_is_equal():
    cd = resp("cd", album(GOOD, 10), speed=1_000_000)
    hires = resp("hi", album(GOOD, 10, depth=24, rate=96000), speed=1_000_000)
    ranked = pick([cd, hires])
    assert [c.username for c in ranked] == ["hi", "cd"]


def test_a_free_slot_and_a_short_queue_win():
    slow = resp("busy", album(GOOD, 10), free=False, queue=200, speed=50_000)
    fast = resp("free", album(GOOD, 10), free=True, queue=0, speed=3_000_000)
    assert [c.username for c in pick([slow, fast])] == ["free", "busy"]


def test_lossless_beats_lossy_when_both_pass():
    wanted = Wanted("Aretha Franklin", "Lady Soul", 1968, 10, "mp3_320")
    a = resp("lossy", album(GOOD, 10, make=mp3))
    b = resp("lossless", album(GOOD, 10))
    assert pick([a, b], wanted)[0].username == "lossless"


def test_the_exact_name_beats_a_loose_one():
    exact = resp("exact", album(ROOT + "Lady Soul", 10))
    loose = resp(
        "loose", album(ROOT + "Aretha Franklin - Lady Soul (Remastered) [FLAC] (1968)", 10)
    )
    ranked = pick([exact, loose])
    assert ranked[0].username == "exact" and len(ranked) == 2


def test_penalties_for_mixed_formats_various_artists_and_other_versions():
    clean = resp("clean", album(GOOD, 10), queue=5)
    mixed = resp("mixed", album(GOOD, 9) + (mp3(GOOD, 10),), queue=5)
    live = resp("live", album(ROOT + "Lady Soul (Live in Paris)", 10), queue=5)
    various = resp("va", album("Various Artists\\Lady Soul", 10), queue=5)
    ranked = pick(
        [clean, mixed, live, various], Wanted("Aretha Franklin", "Lady Soul", 1968, 10, "mp3_320")
    )
    assert ranked[0].username == "clean"
    notes = {c.username: c.notes for c in ranked}
    assert "mixed_formats" in notes.get("mixed", ["mixed_formats"])
    for c in ranked:
        if c.username == "live":
            assert "other_version" in c.notes
        if c.username == "va":
            assert "various" in c.notes


def test_a_compilation_is_not_penalised_for_various_artists():
    wanted = Wanted("Various Artists", "Now Hits", 2000, 10, "flac_standard", various=True)
    [cand] = select_candidates(
        [resp("a", album("Various Artists\\Now Hits (2000)", 10))], wanted, P
    )
    assert "various" not in cand.notes


def test_the_score_is_explained_and_between_zero_and_one():
    [cand] = pick([resp("a", album(GOOD, 10))])
    assert 0 <= cand.score <= 1 and cand.score >= MIN_SCORE
    assert set(cand.breakdown) == {"name", "count", "quality", "availability", "extras", "penalty"}


def test_the_threshold_keeps_poor_candidates_out():
    poor = resp(
        "poor", album(ROOT + "Lady Soul bootleg collection", 10), free=False, queue=900, speed=0
    )
    assert pick([poor], min_score=0.95) == []
    assert pick([resp("a", album(GOOD, 10))], min_score=0.99) == []


# ------------------------------------------------------------------- ranking, limit


def test_the_limit_and_the_order():
    responses = [resp(f"user{i}", album(GOOD, 10), queue=i * 3) for i in range(8)]
    ranked = pick(responses, limit=3)
    assert len(ranked) == 3
    assert [c.score for c in ranked] == sorted((c.score for c in ranked), reverse=True)
    assert [c.username for c in ranked] == ["user0", "user1", "user2"]


def test_one_candidate_per_user_the_better_folder():
    two = resp("alice", album(GOOD, 10) + album(ROOT + "Lady Soul (low quality rip)", 10, make=mp3))
    ranked = pick([two])
    assert [c.username for c in ranked] == ["alice"]
    assert ranked[0].quality == "flac_standard"


def test_the_result_is_deterministic():
    responses = [resp(f"u{i}", album(GOOD, 10), queue=5) for i in range(5)]
    assert [c.username for c in pick(responses)] == [c.username for c in pick(responses[::-1])]


def test_no_responses_no_candidates():
    assert pick([]) == []
    assert pick([resp("a", ())]) == []


def test_without_a_known_track_count_the_other_candidates_decide():
    wanted = Wanted("Aretha Franklin", "Lady Soul", 1968, None, "flac_standard")
    responses = [resp(f"u{i}", album(GOOD, 10)) for i in range(4)] + [resp("short", album(GOOD, 3))]
    ranked = pick(responses, wanted)
    assert "short" not in [c.username for c in ranked]
    assert all(c.track_count == 10 for c in ranked)


# --------------------------------------------------------------------- the rest


def test_candidates_survive_being_stored_as_json():
    import json

    [cand] = pick([resp("a", album(GOOD, 10))])
    back = Candidate.from_dict(json.loads(json.dumps(cand.to_dict())))
    assert back == cand


def test_query_variants():
    # the second variant differs only in case here, so it is not repeated
    assert query_variants("Aretha Franklin", "Lady Soul") == [
        "Aretha Franklin Lady Soul",
        "lady soul",
    ]
    v = query_variants("Beyoncé", "Lemonade (Deluxe Edition)")
    assert v[0] == "Beyoncé Lemonade (Deluxe Edition)"
    assert v[1] == "beyonce lemonade" and v[2] == "lemonade"
    assert query_variants("X", "X") == ["X X", "x"]
    assert len(query_variants("A", "B")) == len({q.lower() for q in query_variants("A", "B")})


@pytest.mark.parametrize("path", ["a\\b\\c.flac", "a/b/c.flac", "c.flac"])
def test_remote_file_properties(path):
    f = RemoteFile(path, 1, "")
    assert f.ext == "flac" and f.is_audio and f.is_lossless
