import httpx
import pytest
import respx
from sqlalchemy import select

from app.db import make_engine, run_migrations
from app.integrations.previews import DeezerClient
from app.models import (
    MB_PROVIDER,
    MB_RESOLVED,
    MB_REVIEW,
    MB_UNMATCHED,
    Artist,
    ArtistReview,
    Job,
    LibraryAlbum,
    ReleaseGroup,
    utcnow,
)
from app.services.musicbrainz import sync
from app.services.musicbrainz.missing import missing_release_groups
from tests.conftest import MB_BASE, post
from tests.dbhelpers import make_album, make_artist, make_rg
from tests.mbdata import ARETHA, aretha_discography, load, nirvana_discographies
from tests.test_jobs_worker import enqueue, logs, wait_for_status
from tests.test_mb_sync import ARETHA_ALBUMS, mount, run_job, seed, state

DEEZER = "https://api.deezer.com"
NO_ARTIST = {"created": "2026-01-01T00:00:00Z", "count": 0, "offset": 0, "artists": []}


def album(id_, title, kind="album", date="2001-05-01"):
    return {"id": id_, "title": title, "record_type": kind, "release_date": date}


def mock_deezer(mock, artists, discographies):
    """artists: [(id, name, fans)]; discographies: {artist id: [albums]}."""
    mock.get(f"{DEEZER}/search/artist").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": i, "name": n, "nb_fan": f} for i, n, f in artists]}
        )
    )
    for artist_id, albums in discographies.items():
        mock.get(f"{DEEZER}/artist/{artist_id}/albums").mock(
            return_value=httpx.Response(200, json={"data": albums, "total": len(albums)})
        )


def fallback_settings(mbsettings, **update):
    return mbsettings.model_copy(update={"providers_metadata": "musicbrainz,deezer", **update})


OBSCURE = (("First Record", 2001), ("Second Record", 2003))
OBSCURE_DISCOGRAPHY = [
    album(11, "First Record"),
    album(12, "Second Record", date="2003-01-01"),
    album(13, "Third Record", date="2005-01-01"),
    album(14, "First Record (Deluxe Edition)", date="2010-01-01"),  # same album, later edition
    album(15, "A Single", kind="single", date="2006-01-01"),
]


def groups_of(factory, artist_id):
    with factory() as db:
        return {
            g.title: g
            for g in db.scalars(select(ReleaseGroup).where(ReleaseGroup.artist_id == artist_id))
        }


def review_of(factory, artist_id):
    with factory() as db:
        return db.scalars(select(ArtistReview).where(ArtistReview.artist_id == artist_id)).all()


# -------------------------------------------------- an artist MusicBrainz does not have


async def test_an_artist_missing_from_musicbrainz_is_filled_in_from_deezer(
    mbsettings, session_factory
):
    settings = fallback_settings(mbsettings)
    artist_id = seed(session_factory, "Obscure Band", albums=OBSCURE)
    with respx.mock(base_url=MB_BASE, assert_all_called=False) as mock:
        mount(mock, NO_ARTIST)
        mock_deezer(mock, [(5, "Obscure Band", 100)], {5: OBSCURE_DISCOGRAPHY})
        worker, job_id = await run_job(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    artist = state(session_factory, artist_id)
    assert artist.mb_status == MB_PROVIDER and artist.mbid is None
    assert (artist.provider, artist.provider_artist_id) == ("deezer", "5")
    assert artist.metadata_synced_at is not None
    groups = groups_of(session_factory, artist_id)
    # the deluxe edition is the same album; the single is kept as a single
    assert sorted(groups) == ["A Single", "First Record", "Second Record", "Third Record"]
    assert {g.source for g in groups.values()} == {"deezer"}
    assert groups["Third Record"].mbid == "deezer:13" and groups["Third Record"].year == 2005
    assert groups["A Single"].category == "single"
    with session_factory() as db:
        owned = {a.title: a.match_status for a in db.scalars(select(LibraryAlbum))}
        missing = [g.title for g in missing_release_groups(db, artist_id, ["album", "ep"])]
        jobs = [j.type for j in db.scalars(select(Job))]
    assert set(owned.values()) == {"matched"}
    assert missing == ["Third Record"]  # the one not owned shows as missing, like any other
    assert "fetch_covers" in jobs
    (review,) = review_of(session_factory, artist_id)
    assert review.reason == "provider" and review.status == "pending"
    assert any("using Deezer" in m for m in logs(session_factory, job_id))


async def test_without_a_sure_match_the_artist_goes_to_review_as_before(
    mbsettings, session_factory
):
    settings = fallback_settings(mbsettings)
    artist_id = seed(session_factory, "Obscure Band", albums=OBSCURE)
    with respx.mock(base_url=MB_BASE, assert_all_called=False) as mock:
        mount(mock, NO_ARTIST)
        # same name, but none of the library albums: a namesake or a tribute band
        mock_deezer(mock, [(6, "Obscure Band", 5000)], {6: [album(21, "Something Else")]})
        worker, job_id = await run_job(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert state(session_factory, artist_id).mb_status == MB_REVIEW
    assert groups_of(session_factory, artist_id) == {}
    assert review_of(session_factory, artist_id)[0].reason == "no_match"


async def test_namesakes_that_cannot_be_told_apart_are_not_guessed(mbsettings, session_factory):
    settings = fallback_settings(mbsettings)
    artist_id = seed(session_factory, "Obscure Band", albums=OBSCURE)
    with respx.mock(base_url=MB_BASE, assert_all_called=False) as mock:
        mount(mock, NO_ARTIST)
        mock_deezer(
            mock,
            [(5, "Obscure Band", 10), (6, "Obscure Band", 20)],
            {5: OBSCURE_DISCOGRAPHY, 6: OBSCURE_DISCOGRAPHY},  # both match equally
        )
        worker, job_id = await run_job(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert state(session_factory, artist_id).mb_status == MB_REVIEW


async def test_the_fallback_can_be_turned_off(mbsettings, session_factory):
    settings = fallback_settings(mbsettings, providers_metadata="musicbrainz")
    artist_id = seed(session_factory, "Obscure Band", albums=OBSCURE)
    with respx.mock(base_url=MB_BASE, assert_all_called=False) as mock:
        mount(mock, NO_ARTIST)
        mock.get(f"{DEEZER}/search/artist").respond(200, json={"data": []})
        worker, job_id = await run_job(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert state(session_factory, artist_id).mb_status == MB_REVIEW
    # Not deezer.call_count: the picture job that follows the sync also searches Deezer, and may
    # or may not have run by now. What counts is that the sync itself never went to Deezer.
    assert not any("deezer" in m.lower() for m in logs(session_factory, job_id))
    assert state(session_factory, artist_id).provider != "deezer"


async def test_ambiguous_artists_are_not_a_case_for_the_fallback(mbsettings, session_factory):
    """Only "MusicBrainz has nobody by that name" asks Deezer, not "several candidates"."""
    settings = fallback_settings(mbsettings)
    artist_id = seed(session_factory, "Nirvana", albums=(("Some Obscure Demo", 1990),))
    with respx.mock(base_url=MB_BASE, assert_all_called=False) as mock:
        mount(mock, load("artist_search_nirvana.json"), nirvana_discographies())
        mock.get(f"{DEEZER}/search/artist").respond(200, json={"data": []})
        worker, job_id = await run_job(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert state(session_factory, artist_id).mb_status == MB_REVIEW
    # Not deezer.call_count: the picture job that follows the sync also searches Deezer, and may
    # or may not have run by now. What counts is that the sync itself never went to Deezer.
    assert not any("deezer" in m.lower() for m in logs(session_factory, job_id))
    assert state(session_factory, artist_id).provider != "deezer"


# ------------------------------------------------- later on: MusicBrainz wins


def provider_artist(factory, name="Aretha Franklin"):
    with factory() as db:
        artist = make_artist(db, name, status=MB_PROVIDER)
        artist.provider, artist.provider_artist_id = "deezer", "77"
        artist.metadata_synced_at = utcnow()
        for title, year in ARETHA_ALBUMS:
            make_album(db, artist, title, year=year)
        make_rg(db, artist, "Lady Soul", 1968, mbid="deezer:1").source = "deezer"
        make_rg(db, artist, "Only At Deezer", 1990, mbid="deezer:2").source = "deezer"
        sync.open_review  # noqa: B018
        db.add(ArtistReview(artist_id=artist.id, reason="provider", candidates_json="[]"))
        db.commit()
        return artist.id


async def test_when_musicbrainz_gets_the_artist_its_data_replaces_deezers(
    mbsettings, session_factory
):
    settings = fallback_settings(mbsettings)
    artist_id = provider_artist(session_factory)
    with respx.mock(base_url=MB_BASE, assert_all_called=False) as mock:
        mount(mock, load("artist_search_aretha.json"), {ARETHA: aretha_discography()})
        mock_deezer(mock, [], {})  # Deezer knows nobody: nothing to add for the unmatched album
        worker, job_id = await run_job(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    artist = state(session_factory, artist_id)
    assert artist.mb_status == MB_RESOLVED and artist.mbid == ARETHA
    assert (artist.provider, artist.provider_artist_id) == (None, None)
    groups = groups_of(session_factory, artist_id)
    assert groups and {g.source for g in groups.values()} == {"musicbrainz"}
    assert "Only At Deezer" not in groups
    assert [r.status for r in review_of(session_factory, artist_id)] == ["resolved"]
    with session_factory() as db:
        assert {a.match_status for a in db.scalars(select(LibraryAlbum))} <= {
            "matched",
            "tentative",
            "unmatched",
        }


async def test_while_musicbrainz_still_lacks_it_the_deezer_data_is_refreshed(
    mbsettings, session_factory
):
    settings = fallback_settings(mbsettings)
    artist_id = provider_artist(session_factory)
    with respx.mock(base_url=MB_BASE, assert_all_called=False) as mock:
        mount(mock, NO_ARTIST)
        mock_deezer(
            mock,
            [],
            {
                77: [
                    album(1, "Lady Soul", date="1968-01-01"),
                    album(3, "Brand New", date="2026-01-01"),
                ]
            },
        )
        worker, job_id = await run_job(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert state(session_factory, artist_id).mb_status == MB_PROVIDER
    assert sorted(groups_of(session_factory, artist_id)) == ["Brand New", "Lady Soul"]
    assert [r.status for r in review_of(session_factory, artist_id)] == ["pending"]  # not doubled


async def test_a_provider_artist_that_musicbrainz_still_lacks_is_kept_when_the_fallback_is_off(
    mbsettings, session_factory
):
    settings = fallback_settings(mbsettings, providers_metadata="musicbrainz")
    artist_id = provider_artist(session_factory)
    with respx.mock(base_url=MB_BASE, assert_all_called=False) as mock:
        mount(mock, NO_ARTIST)
        worker, job_id = await run_job(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert state(session_factory, artist_id).mb_status == MB_PROVIDER  # not sent to review
    assert len(groups_of(session_factory, artist_id)) == 2


def test_provider_artists_are_refreshed_like_the_others(session_factory):
    artist_id = provider_artist(session_factory)
    with session_factory() as db:
        assert sync.artists_needing_sync(db, 30) == []
        db.get(Artist, artist_id).metadata_synced_at = None
        db.commit()
        assert sync.artists_needing_sync(db, 30) == [artist_id]


# --------------------------------------------------------- an album MusicBrainz lacks


async def test_an_album_missing_from_musicbrainz_is_found_at_deezer(mbsettings, session_factory):
    settings = fallback_settings(mbsettings)
    artist_id = seed(session_factory, albums=ARETHA_ALBUMS)
    with respx.mock(base_url=MB_BASE, assert_all_called=False) as mock:
        mount(mock, load("artist_search_aretha.json"), {ARETHA: aretha_discography()})
        mock_deezer(
            mock,
            [(9, "Aretha Franklin", 900)],
            {
                9: [
                    album(1, "Lady Soul", date="1968-01-22"),
                    album(2, "A Record Nobody Catalogued", date="1999-01-01"),
                    album(3, "Unrelated Album", date="2001-01-01"),
                ]
            },
        )
        worker, job_id = await run_job(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        again = enqueue(
            session_factory, "sync_artist_metadata", {"artist_id": artist_id, "force": True}
        )
        worker.notify()
        await wait_for_status(session_factory, again, "succeeded")
        await worker.stop()
    artist = state(session_factory, artist_id)
    assert artist.mb_status == MB_RESOLVED and artist.provider is None  # still a MusicBrainz artist
    groups = groups_of(session_factory, artist_id)
    assert groups["A Record Nobody Catalogued"].source == "deezer"
    assert groups["A Record Nobody Catalogued"].mbid == "deezer:2"
    assert "Unrelated Album" not in groups  # only what the library actually has
    assert groups["Lady Soul"].source == "musicbrainz"  # MusicBrainz keeps what it has
    with session_factory() as db:
        album_row = db.scalar(
            select(LibraryAlbum).where(LibraryAlbum.title == "A Record Nobody Catalogued")
        )
        assert album_row.match_status == "matched"
        assert album_row.release_group_id == groups["A Record Nobody Catalogued"].id
    # the second sync did not prune it
    assert any("found at Deezer" in m for m in logs(session_factory, job_id))


async def test_a_deezer_failure_never_fails_the_musicbrainz_sync(mbsettings, session_factory):
    settings = fallback_settings(mbsettings)
    artist_id = seed(session_factory, albums=ARETHA_ALBUMS)
    with respx.mock(base_url=MB_BASE, assert_all_called=False) as mock:
        mount(mock, load("artist_search_aretha.json"), {ARETHA: aretha_discography()})
        mock.get(f"{DEEZER}/search/artist").respond(500)
        worker, job_id = await run_job(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded", timeout=30)
        await worker.stop()
    assert state(session_factory, artist_id).mb_status == MB_RESOLVED
    messages = logs(session_factory, job_id)
    assert any("Deezer not available" in m for m in messages), messages


# ----------------------------------------------------------------------- the client


async def test_the_discography_is_paged_typed_and_deduplicated(settings, session_factory):
    page1 = [album(i, f"Album {i}") for i in range(1, 4)]
    page2 = [
        album(4, "Album 1 (Remastered 2009)", date="2009-01-01"),
        album(5, "Hits", kind="compile", date="0000-00-00"),
        {"id": "x", "title": "Broken"},
    ]
    with respx.mock(assert_all_called=False) as mock:
        calls = []

        def pages(request):
            index = int(request.url.params["index"])
            calls.append(index)
            rows = page1 if index == 0 else page2 if index == 3 else []
            return httpx.Response(200, json={"data": rows, "total": 6})

        mock.get(f"{DEEZER}/artist/5/albums").mock(side_effect=pages)
        async with DeezerClient(settings, session_factory) as client:
            groups = await client.artist_albums("5")
            with pytest.raises(ValueError):
                await client.artist_albums("../x")
    assert calls[:2] == [0, 3]
    by_title = {g.title: g for g in groups}
    assert sorted(by_title) == ["Album 1", "Album 2", "Album 3", "Hits"]  # remaster dropped
    assert by_title["Hits"].category == "compilation" and by_title["Hits"].year is None
    assert by_title["Album 1"].mbid == "deezer:1" and by_title["Album 1"].year == 2001


# ------------------------------------------------------------- admin and pages


def make_provider_case(app):
    with app.state.session_factory() as db:
        artist = make_artist(db, "Obscure Band", status=MB_PROVIDER)
        artist.provider, artist.provider_artist_id = "deezer", "5"
        album_row = make_album(db, artist, "First Record", year=2001)
        rg = make_rg(db, artist, "First Record", 2001, mbid="deezer:11")
        rg.source = "deezer"
        make_rg(db, artist, "Third Record", 2005, mbid="deezer:13").source = "deezer"
        album_row.release_group_id, album_row.match_status = rg.id, "matched"
        db.add(
            ArtistReview(
                artist_id=artist.id,
                reason="provider",
                candidates_json='[{"provider": "deezer", "provider_id": "5", "name": "Obscure Band",'
                ' "fans": 12, "overlap": 2, "url": "https://www.deezer.com/artist/5"}]',
            )
        )
        db.commit()
        return artist.id, rg.id


def test_the_review_page_shows_the_deezer_data_to_check(app, admin_client):
    artist_id, _ = make_provider_case(app)
    text = admin_client.get("/admin/artists/review").text
    assert f'data-provider-review="{artist_id}"' in text
    assert "https://www.deezer.com/artist/5" in text and "Data from Deezer, to check" in text
    assert f'action="/admin/artists/{artist_id}/confirm"' in text


def test_confirming_keeps_the_data_and_closes_the_review(app, admin_client):
    artist_id, _ = make_provider_case(app)
    r = post(
        admin_client,
        f"/admin/artists/{artist_id}/confirm",
        {"next": "review"},
        page="/admin/artists/review",
    )
    assert r.status_code == 303 and "provider_confirmed" in r.headers["location"]
    with app.state.session_factory() as db:
        artist = db.get(Artist, artist_id)
        assert (artist.mb_status, artist.mb_source, artist.provider) == (
            MB_PROVIDER,
            "manual",
            "deezer",
        )
        assert db.scalar(select(ArtistReview.status)) == "resolved"
        assert db.query(ReleaseGroup).count() == 2


def test_dismissing_drops_the_deezer_data(app, admin_client):
    artist_id, _ = make_provider_case(app)
    post(
        admin_client,
        f"/admin/artists/{artist_id}/dismiss",
        {"next": "review"},
        page="/admin/artists/review",
    )
    with app.state.session_factory() as db:
        artist = db.get(Artist, artist_id)
        assert artist.mb_status == MB_UNMATCHED and artist.provider is None
        assert db.query(ReleaseGroup).count() == 0
        row = db.scalar(select(LibraryAlbum))
        assert row.release_group_id is None and row.match_status == "unmatched"


def test_picking_a_musicbrainz_artist_replaces_the_deezer_data(app, admin_client):
    artist_id, _ = make_provider_case(app)
    mbid = "a" * 8 + "-0000-4000-8000-000000000001"
    post(
        admin_client,
        f"/admin/artists/{artist_id}/resolve",
        {"mbid": mbid, "next": "review"},
        page="/admin/artists/review",
    )
    with app.state.session_factory() as db:
        artist = db.get(Artist, artist_id)
        assert artist.mbid == mbid and artist.provider is None
        assert db.query(ReleaseGroup).count() == 0


def test_the_pages_say_where_the_data_comes_from(app, admin_client):
    artist_id, rg_id = make_provider_case(app)
    page = admin_client.get(f"/artist/{artist_id}").text
    assert 'data-notice="provider"' in page and "https://www.deezer.com/artist/5" in page
    assert "musicbrainz.org/artist" not in page
    with app.state.session_factory() as db:
        missing = db.scalar(select(ReleaseGroup).where(ReleaseGroup.mbid == "deezer:13"))
        missing_id = missing.id
    album_page = admin_client.get(f"/album/{missing_id}")
    assert album_page.status_code == 200
    assert 'data-source="deezer"' in album_page.text
    assert "https://www.deezer.com/album/13" in album_page.text
    assert "musicbrainz.org/release-group" not in album_page.text
    with app.state.session_factory() as db:  # no MusicBrainz tracklist job for it
        assert "fetch_tracklist" not in [j.type for j in db.scalars(select(Job))]


# -------------------------------------------------------------------------- covers


async def test_covers_of_deezer_albums_are_saved_and_served(mbsettings, session_factory, app=None):
    from tests.test_mb_covers import CAA, JPEG, covers_of, run_covers

    settings = fallback_settings(mbsettings, coverart_url=CAA, coverart_min_interval=0.0)
    with session_factory() as db:
        artist = make_artist(db, "Obscure Band", status=MB_PROVIDER)
        artist.provider, artist.provider_artist_id = "deezer", "5"
        make_rg(db, artist, "Third Record", 2005, mbid="deezer:13").source = "deezer"
        make_album(db, artist, "Third Record", year=2005)
        db.commit()
        artist_id = artist.id
    img = "https://cdn-images.dzcdn.net/images/cover/abc/250x250-000000-80-0-0.jpg"
    with respx.mock(assert_all_called=False) as mock:
        caa = mock.get(url__regex=rf"{CAA}/.*").respond(404)
        mock.get(f"{DEEZER}/search/album").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 13,
                            "title": "Third Record",
                            "nb_tracks": 10,
                            "artist": {"name": "Obscure Band"},
                            "cover_medium": img,
                        }
                    ]
                },
            )
        )
        mock.get(img).respond(200, content=JPEG, headers={"content-type": "image/jpeg"})
        worker, job_id = await run_covers(settings, session_factory, artist_id)
        await wait_for_status(session_factory, job_id, "succeeded")
        await worker.stop()
    assert caa.call_count == 0  # not an MBID: the Cover Art Archive is not even asked
    assert covers_of(session_factory, artist_id)["Third Record"] == ("ok", "deezer-13.jpg")
    assert (settings.data_dir / "covers" / "deezer-13.jpg").read_bytes() == JPEG


def test_the_cover_route_serves_deezer_names_only_in_the_expected_shape(app, admin_client):
    covers = app.state.settings.data_dir / "covers"
    covers.mkdir(parents=True, exist_ok=True)
    (covers / "deezer-13.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 16)
    assert admin_client.get("/covers/deezer-13.jpg").status_code == 200
    for bad in ("deezer-.jpg", "deezer-1a.jpg", "deezer:13.jpg", "deezer-13.gif"):
        assert admin_client.get(f"/covers/{bad}").status_code == 404, bad


# ------------------------------------------------------------ settings, migration


def test_metadata_always_keeps_musicbrainz_first(app, admin_client):
    page = "/settings/services"
    text = admin_client.get(page).text
    slot1 = text[text.index('data-slot="providers_metadata-1"') :].split("</select>")[0]
    assert 'value="musicbrainz"' in slot1 and 'value="deezer"' not in slot1
    base = {
        "providers_covers_1": "coverartarchive",
        "providers_previews_1": "itunes",
        "providers_similar_1": "lastfm",
    }
    bad = post(
        admin_client,
        "/settings/services",
        {**base, "providers_metadata_1": "deezer", "providers_metadata_2": "musicbrainz"},
        page=page,
    )
    assert bad.status_code == 400 and 'data-error="providers_metadata"' in bad.text
    off = post(
        admin_client,
        "/settings/services",
        {**base, "providers_metadata_1": "musicbrainz", "providers_metadata_2": ""},
        page=page,
    )
    assert off.status_code == 303
    assert app.state.settings.providers_metadata == "musicbrainz"
    on = post(
        admin_client,
        "/settings/services",
        {**base, "providers_metadata_1": "musicbrainz", "providers_metadata_2": "deezer"},
        page=page,
    )
    assert on.status_code == 303
    assert app.state.settings.providers_metadata == "musicbrainz,deezer"


def test_the_default_turns_the_fallback_on():
    from app.config import Settings
    from app.integrations.providers import metadata_fallback_enabled

    assert metadata_fallback_enabled(Settings(_env_file=None))
    assert not metadata_fallback_enabled(Settings(_env_file=None, providers_metadata="musicbrainz"))
    assert metadata_fallback_enabled(
        Settings(_env_file=None, providers_metadata="deezer")
    )  # first is forced


def test_migration_0013_adds_the_columns_and_keeps_the_data(settings):
    from sqlalchemy import inspect, text

    url = settings.database_url
    run_migrations(url, "0012")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO artists (id, name, sort_name, name_key, mb_status, in_library, created_at, updated_at)"
                " VALUES (1, 'A', 'A', 'a', 'pending', 1, '2026-01-01', '2026-01-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO release_groups (id, mbid, artist_id, title, secondary_types_json, disambiguation,"
                " category, cover_status, synced_at) VALUES (7, 'm', 1, 'T', '[]', '', 'album', 'unknown', '2026-01-01')"
            )
        )
    run_migrations(url)
    run_migrations(url)  # idempotent
    assert {"provider", "provider_artist_id"} <= {
        c["name"] for c in inspect(engine).get_columns("artists")
    }
    with engine.connect() as conn:
        assert conn.execute(text("SELECT source FROM release_groups")).scalar_one() == "musicbrainz"
    engine.dispose()
