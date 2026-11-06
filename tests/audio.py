"""Tiny valid audio files generated on the fly (no encoder needed).

FLAC: STREAMINFO is enough for mutagen (rate, bit depth, length). MP3: CBR frames.
OGG Vorbis and M4A: hand-built headers. WAV: stdlib. Tags are written with mutagen.
"""

import struct
import wave
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TCMP, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSO2, TXXX, UFID
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.ogg import OggPage
from mutagen.oggvorbis import OggVorbis

_MP3_BITRATE_CODE = {128: 0x9, 192: 0xB, 256: 0xD, 320: 0xE}


def make_flac(path: Path, sample_rate=44100, bit_depth=16, channels=2, seconds=3) -> Path:
    total = sample_rate * seconds
    packed = (sample_rate << 44) | ((channels - 1) << 41) | ((bit_depth - 1) << 36) | total
    streaminfo = struct.pack(">HH", 4096, 4096) + b"\0" * 6 + packed.to_bytes(8, "big") + b"\0" * 16
    block = bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo
    path.write_bytes(b"fLaC" + block + b"\0" * 2000)
    return path


def make_mp3(path: Path, bitrate=128, frames=40) -> Path:
    header = bytes([0xFF, 0xFB, (_MP3_BITRATE_CODE[bitrate] << 4), 0x00])  # MPEG1 L3 44.1kHz
    frame_len = 144 * bitrate * 1000 // 44100
    path.write_bytes((header + b"\0" * (frame_len - 4)) * frames)
    return path


def make_ogg(path: Path, rate=44100, seconds=3, nominal=160000) -> Path:
    ident = b"\x01vorbis" + struct.pack("<IBIiii", 0, 2, rate, 0, nominal, 0) + bytes([0xB8, 1])
    vendor = b"diskko-tests"
    comment = (
        b"\x03vorbis" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0) + b"\x01"
    )
    pages = []
    for seq, (packet, position) in enumerate(
        [(ident, 0), (comment, 0), (b"\0" * 50, rate * seconds)]
    ):
        page = OggPage()
        page.packets = [packet]
        page.sequence, page.serial, page.position = seq, 1, position
        page.first, page.last = seq == 0, seq == 2
        pages.append(page.write())
    path.write_bytes(b"".join(pages))
    return path


def _box(kind: bytes, *children: bytes) -> bytes:
    body = b"".join(children)
    return struct.pack(">I4s", 8 + len(body), kind) + body


def _full_box(kind: bytes, *children: bytes) -> bytes:
    return _box(kind, struct.pack(">I", 0), *children)


def make_m4a(
    path: Path, codec=b"mp4a", rate=44100, bitrate=256000, bit_depth=16, seconds=3
) -> Path:
    mvhd = _full_box(b"mvhd", struct.pack(">IIII", 0, 0, rate, rate * seconds) + b"\0" * 80)
    mdhd = _full_box(b"mdhd", struct.pack(">IIIIHH", 0, 0, rate, rate * seconds, 0x55C4, 0))
    hdlr = _full_box(b"hdlr", b"\0\0\0\0soun" + b"\0" * 12 + b"\0")
    esds = _full_box(
        b"esds",
        bytes([3, 25, 0, 0, 0, 4, 17, 0x40, 0x15, 0, 0, 0])
        + struct.pack(">II", bitrate, bitrate)
        + bytes([5, 2, 0x12, 0x10, 6, 1, 2]),
    )
    entry = (
        b"\0" * 6
        + struct.pack(">H", 1)
        + b"\0" * 8
        + struct.pack(">HHHH", 2, bit_depth, 0, 0)
        + struct.pack(">I", rate << 16)
    )
    stsd = _full_box(b"stsd", struct.pack(">I", 1), _box(codec, entry, esds))
    moov = _box(
        b"moov", mvhd, _box(b"trak", _box(b"mdia", mdhd, hdlr, _box(b"minf", _box(b"stbl", stsd))))
    )
    ftyp = _box(b"ftyp", b"M4A \0\0\0\0M4A mp42isom")
    path.write_bytes(ftyp + moov + _box(b"mdat", b"\0" * 1000))
    return path


def make_wav(path: Path, rate=44100, bit_depth=16, seconds=1) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(bit_depth // 8)
        w.setframerate(rate)
        w.writeframes(b"\0" * (rate * seconds * 2 * (bit_depth // 8)))
    return path


# ------------------------------------------------------------------------ tagging


def tag_file(path: Path, **t) -> None:
    """Write tags. Keys: title, artist, albumartist, album, track ('1' or '1/10'), disc, date,
    compilation, artist_sort, mbid_release, mbid_release_group, mbid_artist,
    mbid_album_artist, mbid_recording."""
    suffix = path.suffix.lower()
    if suffix in (".flac", ".ogg"):
        audio = FLAC(path) if suffix == ".flac" else OggVorbis(path)
        names = {
            "title": "title",
            "artist": "artist",
            "albumartist": "albumartist",
            "album": "album",
            "track": "tracknumber",
            "disc": "discnumber",
            "date": "date",
            "artist_sort": "albumartistsort",
            "mbid_release": "musicbrainz_albumid",
            "mbid_release_group": "musicbrainz_releasegroupid",
            "mbid_artist": "musicbrainz_artistid",
            "mbid_album_artist": "musicbrainz_albumartistid",
            "mbid_recording": "musicbrainz_trackid",
        }
        for key, value in t.items():
            if key == "compilation":
                audio["compilation"] = "1" if value else "0"
            else:
                audio[names[key]] = str(value)
        audio.save()
    elif suffix == ".mp3":
        tags = ID3()
        simple = {
            "title": TIT2,
            "artist": TPE1,
            "albumartist": TPE2,
            "album": TALB,
            "track": TRCK,
            "disc": TPOS,
            "date": TDRC,
            "artist_sort": TSO2,
        }
        free = {
            "mbid_release": "MusicBrainz Album Id",
            "mbid_release_group": "MusicBrainz Release Group Id",
            "mbid_artist": "MusicBrainz Artist Id",
            "mbid_album_artist": "MusicBrainz Album Artist Id",
        }
        for key, value in t.items():
            if key in simple:
                tags.add(simple[key](encoding=3, text=str(value)))
            elif key == "compilation":
                tags.add(TCMP(encoding=3, text="1" if value else "0"))
            elif key == "mbid_recording":
                tags.add(UFID(owner="http://musicbrainz.org", data=str(value).encode("ascii")))
            else:
                tags.add(TXXX(encoding=3, desc=free[key], text=str(value)))
        tags.save(path)
    elif suffix == ".m4a":
        audio = MP4(path)
        simple = {
            "title": "\xa9nam",
            "artist": "\xa9ART",
            "albumartist": "aART",
            "album": "\xa9alb",
            "date": "\xa9day",
            "artist_sort": "soaa",
        }
        free = {
            "mbid_release": "MusicBrainz Album Id",
            "mbid_release_group": "MusicBrainz Release Group Id",
            "mbid_artist": "MusicBrainz Artist Id",
            "mbid_album_artist": "MusicBrainz Album Artist Id",
            "mbid_recording": "MusicBrainz Track Id",
        }
        for key, value in t.items():
            if key in simple:
                audio[simple[key]] = [str(value)]
            elif key == "track":
                n, _, total = str(value).partition("/")
                audio["trkn"] = [(int(n), int(total or 0))]
            elif key == "disc":
                n, _, total = str(value).partition("/")
                audio["disk"] = [(int(n), int(total or 0))]
            elif key == "compilation":
                audio["cpil"] = bool(value)
            else:
                audio[f"----:com.apple.iTunes:{free[key]}"] = [
                    MP4FreeForm(str(value).encode("utf-8"))
                ]
        audio.save()
    else:
        raise ValueError(f"cannot tag {suffix}")


BUILDERS = {
    ".flac": make_flac,
    ".mp3": make_mp3,
    ".ogg": make_ogg,
    ".m4a": make_m4a,
    ".wav": make_wav,
}


def make_track(path: Path, **kwargs) -> Path:
    """Create `path` (format by extension) with optional tags: make_track(p, artist='A', ...)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    build = {k: kwargs.pop(k) for k in list(kwargs) if k.startswith("_")}
    BUILDERS[path.suffix.lower()](path, **{k[1:]: v for k, v in build.items()})
    if kwargs:
        tag_file(path, **kwargs)
    return path
