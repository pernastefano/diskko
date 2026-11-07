"""The pieces of the import: names, integrity, matching, tagging, staging and moving."""

import base64
import os

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis

from app.integrations.musicbrainz_client import ReleaseData, TrackData
from app.services.importer import integrity, naming
from app.services.importer import library_import as lib
from app.services.importer.matching import choose_release, facts_of, pair_tracks
from app.services.importer.tagging import Cover, TaggingError, TrackTags, can_tag, write_tags
from app.services.library.tags import read_audio_file
from tests.audio import make_flac, make_track, make_wav

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
RELEASE = "11111111-1111-4111-8111-111111111111"
GROUP = "22222222-2222-4222-8222-222222222222"
ARTIST = "33333333-3333-4333-8333-333333333333"
GUEST = "44444444-4444-4444-8444-444444444444"
RECORDING = "55555555-5555-4555-8555-555555555555"

# ------------------------------------------------------------------------ naming


@pytest.mark.parametrize(
    "template",
    ["{artist}/{album} ({year})", "{album}", "{artist} - {album}/{disc}", "{track:02d} {album}"],
)
def test_good_folder_templates(template):
    assert naming.validate_template(template, required="album") == template


@pytest.mark.parametrize(
    ("template", "code"),
    [
        ("{artist}/{label}", "template_invalid"),  # unknown placeholder
        ("{album.__class__}", "template_invalid"),  # attribute access
        ("{album!r}", "template_invalid"),  # conversion
        ("{album:>20}", "template_invalid"),  # a width on text
        ("{track:0200d}{album}", "template_invalid"),
        ("../{album}", "template_invalid"),
        ("/{album}", "template_invalid"),
        ("{artist}", "template_missing"),  # no {album}
        ("{album", "template_invalid"),  # unbalanced
        ("", "template_invalid"),
        ("x" * 300 + "{album}", "template_invalid"),
    ],
)
def test_bad_folder_templates(template, code):
    with pytest.raises(naming.TemplateError) as exc:
        naming.validate_template(template, required="album")
    assert exc.value.code == code


def test_the_file_template_needs_a_title():
    assert naming.validate_template("{track:02d} - {title}", required="title")
    with pytest.raises(naming.TemplateError):
        naming.validate_template("{track:02d}", required="title")


def test_folders_come_from_the_template_and_values_are_made_safe():
    values = naming.NameValues("AC/DC", 'Back: "Live"?', 1980)
    assert naming.folder_parts(naming.DEFAULT_FOLDER_TEMPLATE, values) == [
        "AC-DC",
        "Back_ _Live__ (1980)",
    ]
    no_year = naming.NameValues("Band", "Album", None)
    assert naming.folder_parts(naming.DEFAULT_FOLDER_TEMPLATE, no_year) == ["Band", "Album"]
    assert naming.folder_parts("{album}", naming.NameValues("A", "Rock [] ()")) == ["Rock"]


def test_a_part_never_holds_a_separator_a_dot_run_or_a_reserved_name():
    assert naming.clean_part("..") == "_" and naming.clean_part("  ") == "_"
    assert naming.clean_part("a/b\\c") == "a-b_c"
    assert naming.clean_part("name. ") == "name"
    assert naming.clean_part("CON") == "_CON" and naming.clean_part("lpt1.txt") == "_lpt1.txt"
    assert naming.clean_part("é") == "é"  # normalised (NFC)
    long = naming.clean_part("é" * 300)
    assert len(long.encode("utf-8")) <= naming.MAX_PART_BYTES and long


def test_file_names_follow_the_template_and_discs_do_not_collide():
    values = naming.NameValues("A", "B", 2000, 3, 2, "Song: One")
    assert naming.file_stem(naming.DEFAULT_FILE_TEMPLATE, values, multi_disc=False) == (
        "03 - Song_ One"
    )
    assert naming.file_stem(naming.DEFAULT_FILE_TEMPLATE, values, multi_disc=True) == (
        "2-03 - Song_ One"
    )
    own = "{disc}.{track} {title}"
    assert naming.file_stem(own, values, multi_disc=True) == "2.3 Song_ One"


# --------------------------------------------------------------------- integrity


def test_files_are_found_and_links_out_of_the_folder_are_refused(tmp_path):
    root = tmp_path / "dl"
    make_track(root / "CD1" / "01.flac")
    (root / "cover.JPG").write_bytes(JPEG)
    (root / ".hidden.flac").write_bytes(b"x")
    (root / "notes.txt").write_text("x")
    audio, images = integrity.find_files(root)
    assert [p.name for p in audio] == ["01.flac"] and [p.name for p in images] == ["cover.JPG"]
    outside = tmp_path / "outside.flac"
    make_flac(outside)
    os.symlink(outside, root / "evil.flac")
    with pytest.raises(integrity.ImportRejected, match="links outside"):
        integrity.find_files(root)


def test_every_damaged_file_is_named(tmp_path):
    root = tmp_path / "dl"
    make_track(root / "01 ok.flac")
    (root / "02 empty.flac").write_bytes(b"")
    (root / "03 garbage.mp3").write_bytes(b"not audio at all" * 50)
    make_flac(root / "04 silent.flac", seconds=0)
    audio, _ = integrity.find_files(root)
    with pytest.raises(integrity.ImportRejected) as exc:
        integrity.check_files(root, audio)
    message = str(exc.value)
    assert "3 file(s) are damaged" in message
    assert all(name in message for name in ("02 empty", "03 garbage", "04 silent"))
    assert "01 ok" not in message


def test_good_files_come_back_with_their_info(tmp_path):
    root = tmp_path / "dl"
    make_track(root / "01.flac", artist="X", _sample_rate=96000, _bit_depth=24)
    audio, _ = integrity.find_files(root)
    (found,) = integrity.check_files(root, audio)
    assert (found.info.sample_rate, found.info.bit_depth, found.info.artist) == (96000, 24, "X")
    assert found.relative == "01.flac" and found.size > 0
    with pytest.raises(integrity.ImportRejected, match="no audio files"):
        integrity.check_files(root, [])


def test_the_cover_of_a_download_is_the_named_biggest_image(tmp_path):
    small, big, other = tmp_path / "cover.jpg", tmp_path / "front.png", tmp_path / "scan.jpg"
    small.write_bytes(JPEG)
    big.write_bytes(PNG + b"\0" * 500)
    other.write_bytes(JPEG + b"\0" * 5000)
    assert integrity.pick_cover_file([small, big, other]) == big  # named ones first
    assert integrity.pick_cover_file([other]) == other
    huge = tmp_path / "huge.jpg"
    huge.write_bytes(b"\0" * (integrity.MAX_IMAGE_BYTES + 1))
    assert integrity.pick_cover_file([huge]) is None and integrity.pick_cover_file([]) is None


# ---------------------------------------------------------------------- matching


def release(titles, *, discs=1, date="2000-03-04", title="Album", mbid=RELEASE):
    tracks = []
    for i, name in enumerate(titles):
        disc = 1 + (i * discs) // len(titles)
        position = i + 1 - ((disc - 1) * len(titles)) // discs
        tracks.append(TrackData(disc, position, str(position), name, 180000, None))
    return ReleaseData(mbid, date, "US", "Official", tracks, title=title)


def downloaded(tmp_path, spec):
    """spec: [(relative path, tags)]: files with those tags (the audio itself is a stub)."""
    root = tmp_path / "dl"
    for relative, tags in spec:
        make_track(root / relative, **tags)
    audio, _ = integrity.find_files(root)
    return facts_of(integrity.check_files(root, audio))


TITLES = ["Opening", "Middle Song", "The Closer"]


def test_files_pair_with_tracks_by_title_even_when_numbers_are_wrong(tmp_path):
    facts = downloaded(
        tmp_path,
        [
            ("a.flac", {"title": "The Closer", "track": 1}),
            ("b.flac", {"title": "opening", "track": 2}),
            ("c.flac", {"title": "Middle Song (remastered)", "track": 3}),
        ],
    )
    pairs = pair_tracks(facts, release(TITLES))
    assert {facts[i].title: t.title for i, t in pairs.items()} == {
        "The Closer": "The Closer",
        "opening": "Opening",
        "Middle Song (remastered)": "Middle Song",
    }


def test_a_file_that_matches_nothing_stops_the_pairing(tmp_path):
    facts = downloaded(
        tmp_path,
        [
            ("a.flac", {"title": "Opening"}),
            ("b.flac", {"title": "Something Else Entirely"}),
            ("c.flac", {"title": "The Closer"}),
        ],
    )
    assert pair_tracks(facts, release(TITLES)) is None


def test_numbers_help_when_titles_are_only_roughly_alike(tmp_path):
    facts = downloaded(
        tmp_path,
        [("01 - Opning.flac", {}), ("02 - Middle Sng.flac", {}), ("03 - The Closr.flac", {})],
    )
    assert [(f.track_no, f.disc_no) for f in facts] == [(1, 1), (2, 1), (3, 1)]  # from the names
    pairs = pair_tracks(facts, release(TITLES))
    assert pairs is not None and [pairs[i].position for i in range(3)] == [1, 2, 3]


def test_the_disc_comes_from_the_tag_or_the_folder_name(tmp_path):
    facts = downloaded(
        tmp_path,
        [
            ("CD1/01 a.flac", {"title": "a"}),
            ("Disc 2/01 b.flac", {"title": "b"}),
            ("x/01 c.flac", {"title": "c", "disc": 3}),
        ],
    )
    assert sorted(f.disc_no for f in facts) == [1, 2, 3]


def test_the_release_with_the_right_number_of_tracks_is_chosen(tmp_path):
    facts = downloaded(
        tmp_path, [(f"{i}.flac", {"title": t, "track": i + 1}) for i, t in enumerate(TITLES)]
    )
    plain = release(TITLES, title="Album", mbid=RELEASE, date="2001-01-01")
    deluxe = release([*TITLES, "Bonus"], title="Album (Deluxe)", mbid=GROUP)
    older = release(TITLES, title="Album", mbid=GUEST, date="1999-05-05")
    chosen, pairs = choose_release([deluxe, plain, older], facts, "Album")
    assert chosen.mbid == GUEST and len(pairs) == 3  # the earliest of the right size
    assert choose_release([deluxe], facts, "Album") is None  # none has three tracks
    other_titles = release(["x", "y", "z"])
    assert choose_release([other_titles], facts, "Album") is None  # same size, other tracks


# ----------------------------------------------------------------------- tagging


def tags(**over):
    base = dict(
        title="Opening",
        artist="The Band feat. Guest",
        album_artist="The Band",
        album="Album",
        track=3,
        disc=2,
        date="2000-03-04",
        track_total=12,
        disc_total=2,
        compilation=True,
        release_types=["album", "compilation"],
        mbid_release=RELEASE,
        mbid_release_group=GROUP,
        mbid_artists=[ARTIST, GUEST],
        mbid_album_artists=[ARTIST],
        mbid_recording=RECORDING,
    )
    base.update(over)
    return TrackTags(**base)


def test_flac_gets_the_standard_tags_and_a_cover_and_loses_the_old_ones(tmp_path):
    path = make_track(tmp_path / "a.flac", title="spam", artist="Spammer")
    old = FLAC(path)
    old["comment"] = "visit http://spam.example"
    old["replaygain_track_gain"] = "-6.5 dB"
    old["encodedby"] = "someone"
    old.save()
    write_tags(path, tags(), Cover(JPEG, "image/jpeg"))
    audio = FLAC(path)
    assert audio["title"] == ["Opening"] and audio["artist"] == ["The Band feat. Guest"]
    assert audio["albumartist"] == ["The Band"] and audio["album"] == ["Album"]
    assert audio["tracknumber"] == ["3"] and audio["tracktotal"] == ["12"]
    assert audio["discnumber"] == ["2"] and audio["disctotal"] == ["2"]
    assert audio["date"] == ["2000-03-04"] and audio["compilation"] == ["1"]
    assert audio["releasetype"] == ["album", "compilation"]
    assert audio["musicbrainz_albumid"] == [RELEASE]
    assert audio["musicbrainz_releasegroupid"] == [GROUP]
    assert audio["musicbrainz_artistid"] == [ARTIST, GUEST]
    assert audio["musicbrainz_albumartistid"] == [ARTIST]
    assert audio["musicbrainz_trackid"] == [RECORDING]
    for stray in ("comment", "encodedby"):
        assert stray not in audio  # the old tags are gone
    assert audio["replaygain_track_gain"] == ["-6.5 dB"]  # ... except ReplayGain
    (picture,) = audio.pictures
    assert (picture.type, picture.mime, picture.data) == (3, "image/jpeg", JPEG)


def test_the_app_reads_back_what_it_wrote(tmp_path):
    path = make_track(tmp_path / "a.flac")
    write_tags(path, tags())
    info = read_audio_file(path)
    assert (info.title, info.album, info.album_artist, info.track_no, info.disc_no) == (
        "Opening",
        "Album",
        "The Band",
        3,
        2,
    )
    assert (info.year, info.compilation) == (2000, True)
    assert (info.mbid_release, info.mbid_release_group) == (RELEASE, GROUP)
    assert info.mbid_recording == RECORDING and info.mbid_album_artist == ARTIST


def test_mp3_gets_id3v24_with_musicbrainz_frames_a_cover_and_no_id3v1(tmp_path):
    path = make_track(tmp_path / "a.mp3", title="spam", artist="Spammer")
    old = ID3(path)
    from mutagen.id3 import COMM, TXXX

    old.add(COMM(encoding=3, lang="eng", desc="", text="http://spam.example"))
    old.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text="-3 dB"))
    old.save(path)
    write_tags(path, tags(), Cover(PNG, "image/png"))
    id3 = ID3(path)
    assert id3.version == (2, 4, 0)
    assert str(id3["TIT2"]) == "Opening" and str(id3["TPE1"]) == "The Band feat. Guest"
    assert str(id3["TPE2"]) == "The Band" and str(id3["TALB"]) == "Album"
    assert str(id3["TRCK"]) == "3/12" and str(id3["TPOS"]) == "2/2"
    assert str(id3["TDRC"]) == "2000-03-04" and str(id3["TCMP"]) == "1"
    assert id3["TXXX:MusicBrainz Album Id"].text == [RELEASE]
    assert id3["TXXX:MusicBrainz Release Group Id"].text == [GROUP]
    assert id3["TXXX:MusicBrainz Artist Id"].text == [ARTIST, GUEST]
    assert id3["TXXX:MusicBrainz Album Type"].text == ["album", "compilation"]
    assert id3["UFID:http://musicbrainz.org"].data == RECORDING.encode()
    assert not id3.getall("COMM") and str(id3["TXXX:REPLAYGAIN_TRACK_GAIN"]) == "-3 dB"
    (apic,) = id3.getall("APIC")
    assert (apic.type, apic.mime, apic.data) == (3, "image/png", PNG)
    assert path.read_bytes()[-128:-125] != b"TAG"  # no ID3v1 footer


def test_m4a_gets_atoms_freeform_ids_and_a_cover(tmp_path):
    path = make_track(tmp_path / "a.m4a", title="spam")
    write_tags(path, tags(), Cover(JPEG, "image/jpeg"))
    audio = MP4(path)
    assert audio["\xa9nam"] == ["Opening"] and audio["aART"] == ["The Band"]
    assert audio["\xa9alb"] == ["Album"] and audio["\xa9day"] == ["2000-03-04"]
    assert audio["trkn"] == [(3, 12)] and audio["disk"] == [(2, 2)] and audio["cpil"] is True
    ids = audio["----:com.apple.iTunes:MusicBrainz Artist Id"]
    assert [bytes(i).decode() for i in ids] == [ARTIST, GUEST]
    assert bytes(audio["----:com.apple.iTunes:MusicBrainz Track Id"][0]).decode() == RECORDING
    assert bytes(audio["covr"][0]) == JPEG


def test_ogg_gets_vorbis_comments_and_an_embedded_picture(tmp_path):
    path = make_track(tmp_path / "a.ogg", title="spam")
    write_tags(path, tags(), Cover(JPEG, "image/jpeg"))
    audio = OggVorbis(path)
    assert audio["title"] == ["Opening"] and audio["musicbrainz_trackid"] == [RECORDING]
    from mutagen.flac import Picture

    picture = Picture(base64.b64decode(audio["metadata_block_picture"][0]))
    assert (picture.type, picture.mime, picture.data) == (3, "image/jpeg", JPEG)


def test_without_a_release_only_the_known_fields_are_written(tmp_path):
    path = make_track(tmp_path / "a.flac")
    bare = TrackTags(title="T", artist="A", album_artist="A", album="Al", track=1)
    write_tags(path, bare)
    audio = FLAC(path)
    assert audio["title"] == ["T"] and audio["tracknumber"] == ["1"]
    assert not [k for k in audio if k.startswith("musicbrainz")] and "date" not in audio
    assert not audio.pictures


def test_formats_that_cannot_be_tagged_are_said_so(tmp_path):
    wav = make_wav(tmp_path / "a.wav")
    assert not can_tag(wav) and can_tag(tmp_path / "a.FLAC") and can_tag(tmp_path / "b.opus")
    with pytest.raises(TaggingError, match="cannot tag"):
        write_tags(wav, tags())
    broken = tmp_path / "broken.flac"
    broken.write_bytes(b"nope")
    with pytest.raises(TaggingError):
        write_tags(broken, tags())


# ---------------------------------------------------------------- staging and moving


def plan_of(tmp_path, specs):
    root = tmp_path / "dl"
    for relative, kwargs in specs:
        make_track(root / relative, **kwargs)
    audio, _ = integrity.find_files(root)
    files = integrity.check_files(root, audio)
    return [
        lib.PlannedFile(
            f,
            tags(title=f"Song {i}", track=i, disc=1, track_total=len(files)),
            naming.NameValues("The Band", "Album", 2000, i, 1, f"Song {i}"),
            can_tag(f.path),
        )
        for i, f in enumerate(files, start=1)
    ]


def test_staging_copies_tags_and_renames_and_leaves_the_download_alone(tmp_path):
    plan = plan_of(tmp_path, [("zz.flac", {}), ("aa.mp3", {}), ("raw.wav", {})])
    before = {p.source.path: p.source.path.read_bytes() for p in plan}
    staging = tmp_path / "music" / ".diskko-import" / "1" / "Album (2000)"
    done = lib.stage(
        plan,
        staging,
        file_template="{track:02d} - {title}",
        multi_disc=False,
        cover=Cover(JPEG, "image/jpeg"),
    )
    assert sorted(p.name for p in staging.iterdir()) == [
        "01 - Song 1.mp3",
        "02 - Song 2.wav",
        "03 - Song 3.flac",
        "cover.jpg",
    ]
    assert (staging / "cover.jpg").read_bytes() == JPEG
    assert FLAC(staging / "03 - Song 3.flac")["title"] == ["Song 3"]
    assert str(ID3(staging / "01 - Song 1.mp3")["TIT2"]) == "Song 1"
    wav = staging / "02 - Song 2.wav"
    assert wav.read_bytes() == plan[1].source.path.read_bytes()  # untagged formats are copied
    assert all(p.source.path.read_bytes() == before[p.source.path] for p in plan)  # originals
    lib.verify_staged(done)


def test_two_tracks_with_the_same_name_do_not_overwrite_each_other(tmp_path):
    plan = plan_of(tmp_path, [("a.flac", {}), ("b.flac", {})])
    for item in plan:
        object.__setattr__(item, "name", naming.NameValues("A", "B", 2000, 1, 1, "Same"))
    staging = tmp_path / "stage"
    lib.stage(plan, staging, file_template="{title}", multi_disc=False, cover=None)
    assert sorted(p.name for p in staging.iterdir()) == ["Same (2).flac", "Same.flac"]


def test_a_staged_copy_that_lost_its_audio_is_caught(tmp_path):
    bad = tmp_path / "x.flac"
    make_flac(bad, seconds=0)
    with pytest.raises(integrity.ImportRejected, match="lost its audio"):
        lib.verify_staged([bad])
    bad.write_bytes(b"junk")
    with pytest.raises(integrity.ImportRejected, match="damaged after copying"):
        lib.verify_staged([bad])


def test_the_move_is_a_single_rename_and_never_overwrites(tmp_path):
    staging = tmp_path / "music" / ".diskko-import" / "1" / "Album"
    staging.mkdir(parents=True)
    (staging / "a.flac").write_bytes(b"1")
    final = tmp_path / "music" / "Band" / "Album (2000)"
    lib.commit(staging, final)
    assert (final / "a.flac").read_bytes() == b"1" and not staging.exists()
    other = tmp_path / "music" / ".diskko-import" / "2" / "Album"
    other.mkdir(parents=True)
    with pytest.raises(integrity.ImportRejected, match="already exists"):
        lib.commit(other, final)
    assert (final / "a.flac").read_bytes() == b"1" and other.exists()  # nothing was touched


def test_an_old_copy_is_set_aside_not_deleted(tmp_path):
    old = tmp_path / "music" / "Band" / "Album (2000)"
    old.mkdir(parents=True)
    (old / "a.mp3").write_bytes(b"old")
    aside = lib.set_aside(old, tmp_path / "music" / lib.REPLACED_DIR, 7, "20260101-000000")
    assert not old.exists() and (aside / "a.mp3").read_bytes() == b"old"
    assert aside.parent.name == "7-20260101-000000"
    again = tmp_path / "music" / "Band" / "Album (2000)"
    again.mkdir(parents=True)
    second = lib.set_aside(again, tmp_path / "music" / lib.REPLACED_DIR, 7, "20260101-000000")
    assert second != aside and second.exists()  # a second copy never replaces the first


def test_quarantine_keeps_everything_with_the_reason(tmp_path):
    source = tmp_path / "dl" / "5"
    make_track(source / "a.flac")
    root = tmp_path / "q"
    moved = lib.quarantine(source, root, 5, "the quality is too low")
    assert not source.exists() and (moved / "a.flac").exists()
    assert "the quality is too low" in (moved / "REASON.txt").read_text()
    source2 = tmp_path / "dl2" / "5"
    make_track(source2 / "b.flac")
    again = lib.quarantine(source2, root, 5, "again")
    assert again != moved and again.name == "5-2" and (moved / "a.flac").exists()


# ------------------------------------------------------------------------ places


def test_the_download_folder_must_be_inside_the_downloads_area(settings, tmp_path):
    s = settings.model_copy(update={"downloads_dir": tmp_path / "dl"})
    good = tmp_path / "dl" / "diskko" / "9"
    good.mkdir(parents=True)
    assert lib.request_dir(s, "diskko/9", 9) == good.resolve()
    assert lib.request_dir(s, None, 9) == good.resolve()
    for bad in ("diskko/../..", "../other", "diskko", "/etc", "diskko/missing"):
        with pytest.raises(integrity.ImportRejected):
            lib.request_dir(s, bad, 9)
    os.symlink(tmp_path, tmp_path / "dl" / "diskko" / "link")
    with pytest.raises(integrity.ImportRejected):
        lib.request_dir(s, "diskko/link", 9)  # a link out of the area
    with pytest.raises(integrity.ImportRejected, match="downloads folder"):
        lib.request_dir(settings.model_copy(update={"downloads_dir": tmp_path / "nope"}), None, 9)


def test_the_album_folder_stays_under_the_music_folder(settings, tmp_path):
    s = settings.model_copy(update={"music_dir": tmp_path / "music"})
    (tmp_path / "music").mkdir()
    values = naming.NameValues("Band", "Album", 2000)
    assert lib.final_folder(s, naming.DEFAULT_FOLDER_TEMPLATE, values) == (
        tmp_path / "music" / "Band" / "Album (2000)"
    )
    # a link inside the library that points outside is not followed
    os.symlink(tmp_path, tmp_path / "music" / "Band")
    with pytest.raises(integrity.ImportRejected, match="outside the music folder"):
        lib.final_folder(s, naming.DEFAULT_FOLDER_TEMPLATE, values)


def test_the_quarantine_folder_defaults_under_the_downloads(settings, tmp_path):
    s = settings.model_copy(update={"downloads_dir": tmp_path / "dl", "quarantine_dir": ""})
    assert lib.quarantine_root(s) == tmp_path / "dl" / "diskko" / "quarantine"
    custom = settings.model_copy(update={"quarantine_dir": str(tmp_path / "q")})
    assert lib.quarantine_root(custom) == tmp_path / "q"
