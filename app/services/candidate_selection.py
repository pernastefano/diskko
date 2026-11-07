"""Choosing what to download: turn the answers of a Soulseek search into ranked candidates.

Pure functions on plain data, no I/O. A candidate is one folder of one user (a multi-disc
album, whose disc folders sit side by side, is one candidate). The steps are:

1. group the files of every response by (user, folder);
2. drop what cannot be the wanted album: no audio, a name that does not match, too few or too
   many tracks, a quality below the minimum profile;
3. score what is left (name, completeness, quality, availability, minus penalties);
4. keep the best few above a threshold, best first. The caller downloads the first and falls
   back on the next ones if it fails, or shows them for a manual choice.

Soulseek paths use backslashes; file attributes are optional (lossless files carry bit depth
and sample rate, lossy ones a bit rate).
"""

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

from rapidfuzz import fuzz

from app.quality import LOSSLESS_CODECS, ProfileSet
from app.services.library.grouping import name_key

AUDIO_EXTENSIONS = frozenset(
    {"flac", "mp3", "m4a", "aac", "ogg", "opus", "wav", "aiff", "aif", "ape", "wv", "wma", "alac"}
)
# extension -> codec name used by the quality profiles (anything else counts as lossy)
_LOSSLESS_EXT = {"wv": "wavpack", "aif": "aiff", "alac": "alac"} | {c: c for c in LOSSLESS_CODECS}

MIN_SCORE = 0.55  # below this a candidate is not offered at all
NAME_GATE = 0.60  # the folder name must resemble the album at least this much
V0_RANGE = (200, 275)  # kbps of a variable bit rate file that is a LAME V0

WEIGHTS = {"name": 0.40, "count": 0.15, "quality": 0.20, "availability": 0.20, "extras": 0.05}
PENALTY_MIXED = 0.10
PENALTY_VARIOUS = 0.15
PENALTY_VERSION = 0.15  # live / remix / karaoke... that the album title does not ask for
PENALTY_PLAYLIST = 0.05

_SEP = re.compile(r"[\\/]+")
_DISC_DIR = re.compile(r"^(cd|disc|disk|dvd)\s*[-_. ]?\s*\d+\b", re.IGNORECASE)
_NUMBER = re.compile(r"^\W*\d")
_VERSION_WORDS = ("live", "remix", "karaoke", "instrumental", "acoustic", "demo", "tribute")
_NOISE = re.compile(
    r"[\(\[][^\)\]]*\b(flac|mp3|320|v0|web|cd|vinyl|lossless|16|24|44|96|192|kbps|"
    r"deluxe|remaster\w*|edition|expanded|anniversary|bonus)\b[^\)\]]*[\)\]]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RemoteFile:
    filename: str
    size: int = 0
    extension: str = ""
    bit_rate: int | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None
    length: int | None = None
    is_vbr: bool = False
    locked: bool = False

    @property
    def ext(self) -> str:
        return (self.extension or self.filename.rsplit(".", 1)[-1]).lower().lstrip(".")

    @property
    def is_audio(self) -> bool:
        return self.ext in AUDIO_EXTENSIONS and not self.locked

    @property
    def is_lossless(self) -> bool:
        return self.ext in _LOSSLESS_EXT

    @property
    def is_v0(self) -> bool:
        low, high = V0_RANGE
        return bool(self.is_vbr and self.bit_rate and low <= self.bit_rate <= high)


@dataclass(frozen=True)
class RemoteResponse:
    username: str
    files: tuple[RemoteFile, ...] = ()
    has_free_slot: bool = False
    queue_length: int = 0
    upload_speed: int = 0  # bytes per second


@dataclass(frozen=True)
class Wanted:
    artist: str
    album: str
    year: int | None = None
    track_count: int | None = None  # None: not known, the other candidates tell
    min_quality: str = ""  # key of the lowest quality profile accepted
    various: bool = False  # a compilation: "Various Artists" in the path is expected


@dataclass
class Candidate:
    username: str
    directory: str  # the album folder on the user's machine
    files: list[dict[str, Any]]  # {filename, size, subdir}: what to download
    quality: str  # key of the weakest track's profile
    score: float
    breakdown: dict[str, float]
    track_count: int
    total_size: int
    has_free_slot: bool = False
    queue_length: int = 0
    upload_speed: int = 0
    extensions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate":
        return cls(**data)


# --------------------------------------------------------------------------- helpers


def split_path(path: str) -> list[str]:
    return [p for p in _SEP.split(path) if p]


def album_folder(path: str) -> tuple[str, str]:
    """(album folder, disc subfolder) of a file path. CD1/Disc 2 folders fold into their parent."""
    parts = split_path(path)[:-1]  # without the file name
    sub = ""
    if parts and _DISC_DIR.match(parts[-1]):
        sub = parts.pop()
    return "\\".join(parts), sub


def clean_title(text: str) -> str:
    return name_key(_NOISE.sub(" ", text))


def query_variants(artist: str, album: str) -> list[str]:
    """What to search for, most specific first; the caller stops at the first that works.

    The text as written, then without punctuation and edition words ("Deluxe Edition"), then the
    album alone (the name check of the engine keeps that one safe).
    """
    plain_album = " ".join(clean_title(album).split())
    plain_artist = " ".join(name_key(artist).split())
    variants: list[str] = []
    for text in (f"{artist} {album}", f"{plain_artist} {plain_album}", plain_album):
        text = " ".join(text.split())
        if text and text.lower() not in {v.lower() for v in variants}:
            variants.append(text)
    return variants


def _classify(f: RemoteFile, profiles: ProfileSet) -> str:
    return profiles.classify(
        lossless=f.is_lossless,
        bitrate_kbps=f.bit_rate,
        bit_depth=f.bit_depth,
        sample_rate=f.sample_rate,
        v0=f.is_v0,
    )


# ---------------------------------------------------------------------------- engine


@dataclass
class _Group:
    username: str
    directory: str
    audio: list[tuple[RemoteFile, str]]  # (file, disc subfolder)
    other_names: list[str]
    response: RemoteResponse


def group_files(responses: list[RemoteResponse]) -> list[_Group]:
    """Step 1: one group per (user, folder); disc folders are folded into the album folder."""
    groups: dict[tuple[str, str], _Group] = {}
    for resp in responses:
        for f in resp.files:
            folder, sub = album_folder(f.filename)
            key = (resp.username, folder.lower())
            group = groups.setdefault(key, _Group(resp.username, folder, [], [], resp))
            if f.is_audio:
                group.audio.append((f, sub))
            else:
                group.other_names.append(f.filename.rsplit("\\", 1)[-1].lower())
    return list(groups.values())


def _name_score(group: _Group, wanted: Wanted) -> tuple[float, float]:
    """(album similarity, artist similarity) of the folder path against the wanted names."""
    parts = split_path(group.directory)
    folder = clean_title(parts[-1]) if parts else ""
    near = clean_title(" ".join(parts[-2:]))  # artist folders are often the parent
    album = clean_title(wanted.album)
    artist = name_key(wanted.artist)
    sim_album = max(fuzz.ratio(folder, album), fuzz.partial_ratio(album, folder) * 0.95) / 100
    sim_artist = max(fuzz.partial_ratio(artist, near), fuzz.partial_ratio(artist, folder)) / 100
    return sim_album, sim_artist


def _expected_count(wanted: Wanted, counts: list[int]) -> int | None:
    if wanted.track_count:
        return wanted.track_count
    return round(median(counts)) if counts else None


def select_candidates(
    responses: list[RemoteResponse],
    wanted: Wanted,
    profiles: ProfileSet,
    *,
    limit: int = 5,
    min_score: float = MIN_SCORE,
) -> list[Candidate]:
    """The best candidates, best first (at most `limit`, none below `min_score`)."""
    minimum_rank = profiles.rank(wanted.min_quality) if wanted.min_quality else -1
    ranks = [p.rank for p in profiles.profiles]
    low, high = min(ranks), max(ranks)

    # Steps 1 and 2a: groups whose folder looks like the album and holds audio
    named: list[tuple[_Group, float]] = []
    for group in group_files(responses):
        if not group.audio:
            continue
        sim_album, sim_artist = _name_score(group, wanted)
        if sim_album < NAME_GATE:
            continue
        named.append((group, 0.7 * sim_album + 0.3 * sim_artist))

    expected = _expected_count(wanted, [len(g.audio) for g, _ in named])
    candidates: list[Candidate] = []
    for group, name_score in named:
        count = len(group.audio)
        if expected:
            slack = 1 if expected >= 6 else 0
            if count < expected - slack or count > max(expected * 1.6, expected + 6):
                continue

        # Step 2b: quality of the whole folder is that of its weakest track
        keys = [_classify(f, profiles) for f, _ in group.audio]
        quality = profiles.album_quality(keys)
        if profiles.rank(quality) < minimum_rank:
            continue

        # Step 3: scoring
        count_score = 1.0 if not expected else max(0.0, 1 - abs(count - expected) / expected)
        quality_score = (profiles.rank(quality) - low) / max(high - low, 1)
        resp = group.response
        availability = (
            0.5 * bool(resp.has_free_slot)
            + 0.3 / (1 + resp.queue_length / 10)
            + 0.2 * min(resp.upload_speed / 1_000_000, 1.0)
        )
        extras = 1.0
        notes: list[str] = []
        exts = sorted({f.ext for f, _ in group.audio})
        penalty = 0.0
        if len(exts) > 1:
            penalty += PENALTY_MIXED
            notes.append("mixed_formats")
        path_text = group.directory.lower()
        if "various" in path_text and not wanted.various:
            penalty += PENALTY_VARIOUS
            notes.append("various")
        asked = wanted.album.lower()
        if any(w in path_text and w not in asked for w in _VERSION_WORDS):
            penalty += PENALTY_VERSION
            notes.append("other_version")
        if any(n.endswith((".m3u", ".m3u8")) for n in group.other_names):
            penalty += PENALTY_PLAYLIST
            notes.append("playlist")
        if wanted.year and str(wanted.year) in group.directory:
            extras = 1.0
        elif wanted.year:
            extras = 0.85
        breakdown = {
            "name": round(name_score, 3),
            "count": round(count_score, 3),
            "quality": round(quality_score, 3),
            "availability": round(availability, 3),
            "extras": round(extras, 3),
            "penalty": round(penalty, 3),
        }
        score = sum(WEIGHTS[k] * breakdown[k] for k in WEIGHTS) - penalty
        score = max(0.0, min(1.0, score))
        if score < min_score:
            continue
        files = [
            {"filename": f.filename, "size": f.size, "subdir": sub}
            for f, sub in sorted(group.audio, key=lambda fs: fs[0].filename.lower())
        ]
        candidates.append(
            Candidate(
                username=group.username,
                directory=group.directory,
                files=files,
                quality=quality,
                score=round(score, 4),
                breakdown=breakdown,
                track_count=count,
                total_size=sum(f.size for f, _ in group.audio),
                has_free_slot=resp.has_free_slot,
                queue_length=resp.queue_length,
                upload_speed=resp.upload_speed,
                extensions=exts,
                notes=notes,
            )
        )

    # one user may offer the same album twice (two folders): keep the better one
    best_by_user: dict[str, Candidate] = {}
    for cand in sorted(candidates, key=lambda c: -c.score):
        best_by_user.setdefault(cand.username, cand)
    ranked = sorted(
        best_by_user.values(), key=lambda c: (-c.score, c.queue_length, c.username.lower())
    )
    return ranked[:limit]


def group_by_subdir(candidate: Candidate) -> dict[str, list[dict[str, Any]]]:
    """The files of a candidate by disc folder ('' for a single-disc album)."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in candidate.files:
        groups[f["subdir"]].append(f)
    return dict(groups)
