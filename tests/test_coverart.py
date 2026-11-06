import httpx
import pytest
import respx

from app.integrations.coverart import MAX_IMAGE_BYTES, CoverArtClient, CoverArtError
from app.integrations.ratelimit import RateLimiter
from tests.mbdata import LADY_SOUL_RG, FakeTime

CAA = "https://coverartarchive.test"
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 100


@pytest.fixture
def cover(settings):
    ft = FakeTime()
    s = settings.model_copy(update={"coverart_url": CAA, "mb_user_agent": "Diskko/0.1 ( test )"})
    return CoverArtClient(s, limiter=RateLimiter(0.5, clock=ft.clock, sleep=ft.sleep)), ft


def mock_front(mock, rg=LADY_SOUL_RG, size=250, **kw):
    return mock.get(f"{CAA}/release-group/{rg}/front-{size}", **kw)


async def test_front_cover_follows_the_redirect_to_archive_org(cover):
    client, _ = cover
    with respx.mock(assert_all_called=True) as mock:
        route = mock_front(mock).respond(
            307, headers={"location": "https://archive.test/download/x_thumb250.jpg"}
        )
        mock.get("https://archive.test/download/x_thumb250.jpg").respond(
            200, content=JPEG, headers={"content-type": "image/jpeg"}
        )
        async with client:
            result = await client.fetch_front(LADY_SOUL_RG)
    assert result == (JPEG, ".jpg")
    assert route.calls.last.request.headers["User-Agent"].startswith("Diskko")


async def test_png_is_accepted(cover):
    client, _ = cover
    with respx.mock() as mock:
        mock_front(mock, size=500).respond(
            200, content=b"\x89PNG" + b"\0" * 20, headers={"content-type": "image/png"}
        )
        async with client:
            _, ext = await client.fetch_front(LADY_SOUL_RG, 500)
    assert ext == ".png"


async def test_release_group_without_cover_is_none(cover):
    client, _ = cover
    with respx.mock() as mock:
        mock_front(mock).respond(404, text="Not found")
        async with client:
            assert await client.fetch_front(LADY_SOUL_RG) is None


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(503), "HTTP 503"),
        (
            httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
            "content type",
        ),
        (
            httpx.Response(
                200, content=b"x" * (MAX_IMAGE_BYTES + 1), headers={"content-type": "image/jpeg"}
            ),
            "too large",
        ),
    ],
)
async def test_bad_answers_are_errors(cover, response, message):
    client, _ = cover
    with respx.mock() as mock:
        mock_front(mock).mock(return_value=response)
        async with client:
            with pytest.raises(CoverArtError, match=message):
                await client.fetch_front(LADY_SOUL_RG)


async def test_connection_errors_do_not_leak_the_url(cover):
    client, _ = cover
    with respx.mock() as mock:
        mock_front(mock).mock(side_effect=httpx.ConnectError("refused coverartarchive.test"))
        async with client:
            with pytest.raises(CoverArtError) as exc:
                await client.fetch_front(LADY_SOUL_RG)
    assert "coverartarchive.test" not in str(exc.value)


@pytest.mark.parametrize("mbid", ["../../etc/passwd", "not-a-uuid", "", LADY_SOUL_RG + "/x"])
async def test_invalid_mbid_never_reaches_the_network(cover, mbid):
    client, _ = cover
    with respx.mock(assert_all_called=False) as mock:
        async with client:
            with pytest.raises(ValueError):
                await client.fetch_front(mbid)
    assert mock.calls.call_count == 0


async def test_invalid_size_is_rejected(cover):
    client, _ = cover
    async with client:
        with pytest.raises(ValueError):
            await client.fetch_front(LADY_SOUL_RG, 300)


async def test_requests_are_spaced(cover):
    client, ft = cover
    with respx.mock() as mock:
        mock.get(url__regex=rf"{CAA}/release-group/.*").respond(404)
        async with client:
            for _ in range(3):
                await client.fetch_front(LADY_SOUL_RG)
    assert len(ft.sleeps) == 2 and all(s >= 0.5 - 1e-9 for s in ft.sleeps)
