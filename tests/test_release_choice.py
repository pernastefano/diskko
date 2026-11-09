"""A download that matches no MusicBrainz release on its own waits for the user: files named
after a vinyl side are recognised, and the user can pick the release, import with the tags the
files have, or discard it."""

import asyncio
import json

import respx
from mutagen.flac import FLAC

from app.integrations.musicbrainz_client import ReleaseData, TrackData
from app.jobs.worker import JobWorker
from app.models import REQ_COMPLETED, REQ_FAILED, DownloadRequest, LibraryAlbum
from app.services import downloads as svc
from app.services.importer.matching import pair_tracks
from tests import test_postprocess
from tests.conftest import post
from tests.test_importer_units import downloaded
from tests.test_jobs_worker import enqueue, fetch, logs
from tests.test_postprocess import (  # noqa: F401  (env is a fixture)
    RELEASE_MBID,
    TITLES,
    album_folder,
    good_files,
    mock_music,
    put_download,
    quarantined,
    release_json,
    request_of,
    run_job,
    seed,
)

env = test_postprocess.env  # the fixture of the import tests

SIDE_TITLES = [
    "I Might Have Been Queen",
    "What's Love Got to Do with It",
    "Show Some Respect",
    "I Can't Stand the Rain",
    "Private Dancer",
    "Better Be Good to Me",
    "Steel Claw",
    "Help!",
    "1984",
]
SIDE_NUMBERS = ["A1", "A2", "A3", "A4", "A5", "B2", "B3", "B4", "B5"]


def vinyl_release():
    tracks = [
        TrackData(1, i, number, name, 180000, None)
        for i, (number, name) in enumerate(zip(SIDE_NUMBERS, SIDE_TITLES, strict=True), start=1)
    ]
    return ReleaseData(
        "r" * 8 + "-0000-4000-8000-000000000000",
        "1984",
        "GB",
        "Official",
        tracks,
        title="Private Dancer",
    )


# ----------------------------------------------------------- vinyl side numbering


def test_files_named_after_the_vinyl_side_are_recognised(tmp_path):
    spec = [(f"{n} - {t}.flac", {}) for n, t in zip(SIDE_NUMBERS, SIDE_TITLES, strict=True)]
    facts = downloaded(tmp_path, spec)
    assert [f.side for f in facts] and {f.side for f in facts} == set(SIDE_NUMBERS)
    assert sorted(f.title for f in facts) == sorted(SIDE_TITLES)  # the "A1 - " is not the title
    pairs = pair_tracks(facts, vinyl_release())
    assert pairs is not None and len(pairs) == 9


def test_the_side_number_confirms_a_title_that_is_only_roughly_alike(tmp_path):
    spec = [
        ("A1 - I Might Have Bin Queen.flac", {}),
        ("A2 - Whats Love Got To Do With It.flac", {}),
    ]
    release = vinyl_release()
    release.tracks = release.tracks[:2]
    pairs = pair_tracks(downloaded(tmp_path, spec), release)
    assert pairs is not None and len(pairs) == 2


def test_a_title_that_starts_like_a_side_number_is_left_alone(tmp_path):
    facts = downloaded(tmp_path, [("B52s Rock Lobster.flac", {}), ("Abba Waterloo.flac", {})])
    assert [f.side for f in facts] == [None, None]
    assert sorted(f.title for f in facts) == ["Abba Waterloo", "B52s Rock Lobster"]


# ------------------------------------------------- waiting, choosing, importing


async def park(env):
    settings, factory, music, downloads = env
    seed(factory)
    put_download(downloads, good_files())
    with respx.mock(assert_all_called=False) as mock:
        mock_music(mock, release_json([*TITLES, "Bonus Track"]))
        await run_job(settings, factory)
    assert request_of(factory).status == "awaiting_choice"


async def run_choice(env, choice, release=None):
    settings, factory, music, downloads = env
    worker = JobWorker(settings.model_copy(update={"job_poll_interval": 0.02}), factory)
    await worker.start()
    with factory() as db:  # what choosing on the page does before it queues the import
        svc.choose_release_option(db, db.get(DownloadRequest, 1), choice)
        db.commit()
    job_id = enqueue(factory, "postprocess", {"request_id": 1, "release_choice": choice})
    worker.notify()
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock_music(mock, release or release_json([*TITLES, "Bonus Track"]))
            for _ in range(500):
                job = await asyncio.to_thread(fetch, factory, job_id)
                if job.status in ("succeeded", "failed"):
                    return job_id, job
                await asyncio.sleep(0.02)
    finally:
        await worker.stop()
    raise AssertionError("the job did not finish")


async def test_choosing_the_release_imports_the_download_with_a_missing_track(env):
    settings, factory, music, downloads = env
    await park(env)
    job_id, job = await run_choice(env, RELEASE_MBID)
    assert job.status == "succeeded", logs(factory, job_id)
    row = request_of(factory)
    assert row.status == REQ_COMPLETED and row.release_options_json is None
    names = sorted(p.name for p in album_folder(music).glob("*.flac"))
    assert names == ["01 - Opening.flac", "02 - Middle Song.flac", "03 - The Closer.flac"]
    audio = FLAC(album_folder(music) / names[0])
    assert audio["musicbrainz_albumid"] == [RELEASE_MBID] and audio["tracktotal"] == ["4"]
    assert any("as you chose: 3 of 3" in m for m in logs(factory, job_id))
    with factory() as db:
        assert db.query(LibraryAlbum).count() == 1


async def test_importing_with_the_tags_of_the_files_needs_no_release(env):
    settings, factory, music, downloads = env
    await park(env)
    job_id, job = await run_choice(env, "none")
    assert job.status == "succeeded", logs(factory, job_id)
    assert request_of(factory).status == REQ_COMPLETED
    audio = FLAC(next(album_folder(music).glob("01*.flac")))
    assert audio["title"] == ["Opening"] and "musicbrainz_albumid" not in audio


async def test_discarding_it_moves_the_files_to_the_quarantine(env):
    settings, factory, music, downloads = env
    await park(env)
    job_id, job = await run_choice(env, "reject")
    assert job.status == "failed"
    row = request_of(factory)
    assert row.status == REQ_FAILED and "you chose not to import" in row.error
    assert len(list(quarantined(downloads).glob("*.flac"))) == 3
    assert not any(music.rglob("*.flac"))


# ------------------------------------------------------------- the web pages


def parked(env, admin_client):
    settings, factory, music, downloads = env
    return asyncio.run(park(env))


async def test_the_request_page_asks_which_release_and_the_phone_too(env, app, admin_client):
    settings, factory, music, downloads = env
    await park(env)
    page = admin_client.get("/requests/1").text
    assert "data-release-choice" in page and f'data-release="{RELEASE_MBID}"' in page
    assert "3 of 3 files match a track" in page and "Bonus Track" in page
    assert (
        "data-use-release" in page and "data-release-none" in page and "data-release-reject" in page
    )
    assert "data-choose" not in page  # no download candidates to pick any more
    mobile = admin_client.get("/m/requests/1").text
    assert "data-release-choice" in mobile and 'action="/m/requests/1/release"' in mobile
    assert "Waiting for your choice" in admin_client.get("/m/requests").text


async def test_choosing_on_the_page_queues_the_import_with_the_answer(env, app, admin_client):
    settings, factory, music, downloads = env
    await park(env)
    r = post(admin_client, "/requests/1/release", {"choice": RELEASE_MBID}, page="/requests/1")
    assert r.status_code == 303 and r.headers["location"] == "/requests/1"
    row = request_of(factory)
    assert row.status == "downloaded" and row.release_options_json is None
    with factory() as db:
        from app.models import Job

        job = db.query(Job).filter_by(type="postprocess").order_by(Job.id.desc()).first()
        assert json.loads(job.payload_json) == {"request_id": 1, "release_choice": RELEASE_MBID}
    again = post(admin_client, "/requests/1/release", {"choice": RELEASE_MBID}, page="/requests/1")
    assert again.headers["location"].endswith("error=request_not_choosing")


async def test_an_answer_that_was_not_offered_is_refused(env, app, admin_client):
    await park(env)
    r = post(admin_client, "/requests/1/release", {"choice": "made-up"}, page="/requests/1")
    assert r.headers["location"].endswith("error=request_bad_release")
    assert request_of(env[1]).status == "awaiting_choice"
    assert "not one of those offered" in admin_client.get(r.headers["location"]).text


async def test_the_phone_answer_goes_back_to_the_phone_page(env, app, admin_client):
    await park(env)
    r = post(admin_client, "/m/requests/1/release", {"choice": "none"}, page="/m/requests/1")
    assert r.status_code == 303 and r.headers["location"] == "/m/requests/1"
    assert request_of(env[1]).status == "downloaded"


async def test_the_choice_of_a_download_candidate_is_refused_while_a_release_is_asked(
    env, app, admin_client
):
    await park(env)
    r = post(admin_client, "/requests/1/choose", {"index": "0"}, page="/requests/1")
    assert r.headers["location"].endswith("error=request_not_choosing")
