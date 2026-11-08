"""Fill an empty DISKKO database with a small, fictional demo library (for screenshots and demos).

Fictional artists and albums only. Cover images are drawn here (plain gradients), no network is
used. Usage:
    DATA_DIR=./demo-data python scripts/seed_demo.py
    DATA_DIR=./demo-data uvicorn app.main:app --port 8100     # sign in: admin / demo-password-1
Users: admin (dark theme) and marco (light theme), both with password demo-password-1.
"""

import colorsys
import struct
import uuid
import zlib
from pathlib import Path

from app.auth.users import create_user
from app.config import get_settings
from app.db import make_engine, make_session_factory, run_migrations
from app.integrations.discovery_types import BioData, TopTrackData
from app.jobs.handlers.fetch_covers import covers_dir
from app.models import (
    Artist,
    DownloadRequest,
    LibraryAlbum,
    ReleaseGroup,
    User,
    utcnow,
)
from app.services.artist_profile import store_bio, store_top_tracks
from app.services.library.grouping import name_key
from app.services.musicbrainz.categories import category_for, year_of
from app.services.musicbrainz.matching import match_artist_albums

PASSWORD = "demo-password-1"
# artist -> [(title, year, primary type, secondary types, owned)]
LIBRARY = {
    "Aurora Vale": [
        ("Northern Lights", 2009, "Album", (), True),
        ("Glass Harbour", 2012, "Album", (), True),
        ("Winter Letters", 2015, "EP", (), True),
        ("Slow Tide", 2018, "Album", (), False),
        ("Live at the Observatory", 2019, "Album", ("Live",), False),
    ],
    "The Paper Kites Orchestra": [
        ("Origami Sky", 2004, "Album", (), True),
        ("Folded Maps", 2007, "Album", (), True),
        ("Postcards", 2010, "Album", (), True),
        ("Small Hours", 2014, "Album", (), True),
    ],
    "Marlowe & the Static": [
        ("Static Bloom", 1998, "Album", (), True),
        ("Radio Silence", 2001, "Album", (), False),
        ("Night Shift", 2005, "Album", (), False),
        ("Wire", 2006, "Single", (), False),
    ],
    "Nova Quartet": [
        ("Blue Corridor", 1971, "Album", (), True),
        ("Late Train", 1973, "Album", (), False),
        ("Quarter Past Seven", 1976, "Album", (), True),
    ],
    "Sunday Parade": [
        ("Brass Garden", 2016, "Album", (), True),
        ("Sidewalk Songs", 2020, "Album", (), True),
        ("Open Windows", 2022, "EP", (), False),
    ],
    "Lena Okafor": [
        ("Salt", 2013, "Album", (), True),
        ("Harmattan", 2017, "Album", (), False),
        ("Inland", 2021, "Album", (), False),
    ],
    "Echo Foundry": [
        ("Cast Iron", 2011, "Album", (), True),
        ("Slag", 2014, "Album", (), True),
        ("Remelt", 2019, "Album", ("Compilation",), True),
    ],
    "Violet Hour": [
        ("First Light", 2008, "Album", (), False),
        ("Afterglow", 2010, "Album", (), False),
    ],
}
# (artist, album, status, progress)
REQUESTS = [
    ("Aurora Vale", "Slow Tide", "downloading", 0.62),
    ("Marlowe & the Static", "Radio Silence", "completed", 1.0),
    ("Lena Okafor", "Harmattan", "queued", 0.0),
    ("Violet Hour", "First Light", "failed", 0.0),
    ("Nova Quartet", "Late Train", "completed", 1.0),
]


def png(path: Path, size: int, hue: float) -> None:
    """A square gradient, written with the standard library."""
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            t = (x + y) / (2 * size)
            r, g, b = colorsys.hls_to_rgb((hue + 0.12 * t) % 1, 0.28 + 0.34 * (1 - t), 0.55)
            row += bytes((int(r * 255), int(g * 255), int(b * 255)))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")
    )


def main() -> None:
    settings = get_settings()
    run_migrations(settings.database_url)
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    covers = covers_dir(settings.data_dir)
    covers.mkdir(parents=True, exist_ok=True)
    with factory() as db:
        if db.query(User).count():
            raise SystemExit("The database already has users: use an empty DATA_DIR.")
        admin = create_user(db, "admin", PASSWORD, "admin", settings, language="en")
        marco = create_user(db, "marco", PASSWORD, "user", settings, language="en")
        admin.theme, marco.theme = "dark", "light"
        groups: dict[tuple[str, str], ReleaseGroup] = {}
        for n, (name, albums) in enumerate(LIBRARY.items()):
            artist = Artist(
                name=name,
                sort_name=name,
                name_key=name_key(name),
                mbid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"demo-artist-{name}")),
                mb_status="resolved",
                in_library=True,
                metadata_synced_at=utcnow(),
            )
            db.add(artist)
            db.flush()
            for i, (title, year, primary, secondary, owned) in enumerate(albums):
                mbid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"demo-rg-{name}-{title}"))
                rg = ReleaseGroup(
                    mbid=mbid,
                    artist_id=artist.id,
                    title=title,
                    primary_type=primary,
                    secondary_types_json=str(list(secondary)).replace("'", '"'),
                    first_release_date=str(year),
                    year=year_of(str(year)),
                    category=category_for(primary, list(secondary)),
                    cover_status="ok",
                    cover_path=f"{mbid}.png",
                )
                db.add(rg)
                png(covers / f"{mbid}.png", 300, ((n * 7 + i * 3) % 20) / 20)
                groups[(name, title)] = rg
                if owned:
                    db.add(
                        LibraryAlbum(
                            artist_id=artist.id,
                            source="filesystem",
                            path=f"{name}/{title}",
                            title=title,
                            year=year,
                            is_compilation="Compilation" in secondary,
                            track_count=10,
                        )
                    )
            db.flush()
            match_artist_albums(db, artist.id)
            # a profile and a top list, so the artist page needs no lookups (jobs may be off)
            store_bio(
                db,
                artist.id,
                "en",
                BioData(f"{name} is a fictional act made up for the DISKKO demo."),
                "demo",
            )
            store_bio(db, artist.id, "it", None, None)
            store_top_tracks(
                db,
                artist.id,
                "demo",
                [
                    (
                        TopTrackData(
                            title=f"{a[0]} (single mix)", album=a[0], duration_s=190 + 7 * k
                        ),
                        None,
                    )
                    for k, a in enumerate(albums[:5])
                ],
            )
            artist.profile_checked_at = artist.top_checked_at = utcnow()
        db.flush()
        for artist_name, title, status, progress in REQUESTS:
            db.add(
                DownloadRequest(
                    user_id=marco.id if progress < 1 else admin.id,
                    release_group_id=groups[(artist_name, title)].id,
                    status=status,
                    quality_key="flac_standard",
                    progress=progress,
                    error="No source found with the wanted quality" if status == "failed" else None,
                )
            )
        db.commit()
    engine.dispose()
    print(f"Demo library ready in {settings.data_dir}. Sign in as admin or marco / {PASSWORD}")


if __name__ == "__main__":
    main()
