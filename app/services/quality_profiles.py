"""Quality profiles: storage, validation, editing and re-classification of the library."""

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import LibraryAlbum, LibraryTrack, QualityProfile
from app.quality import (
    BIT_DEPTHS,
    BITRATES_KBPS,
    COLORS,
    DEFAULT_QUALITY,
    KIND_ANY,
    KIND_LOSSLESS,
    KIND_LOSSY,
    KINDS,
    SAMPLE_RATES_HZ,
    SEED_PROFILES,
    Profile,
    ProfileSet,
    slugify,
)
from app.services.app_settings import get_setting, set_setting

MAX_NAME = 80
DEFAULT_KEY_SETTING = "config.default_quality"


class ProfileError(ValueError):
    """A rejected change; `code` is the suffix of an `error.quality_*` i18n key."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class ProfileInput:
    name: str
    kind: str
    color: str
    min_bit_depth: int | None = None
    min_sample_rate: int | None = None  # Hz
    min_bitrate: int | None = None
    accept_v0: bool = False


# --------------------------------------------------------------------- loading


def to_profile(row: QualityProfile) -> Profile:
    return Profile(
        key=row.key,
        name=row.name,
        kind=row.kind,
        rank=row.rank,
        min_bit_depth=row.min_bit_depth,
        min_sample_rate=row.min_sample_rate,
        min_bitrate=row.min_bitrate,
        accept_v0=bool(row.accept_v0),
        color=row.color,
    )


def load_profiles(db: Session) -> ProfileSet:
    rows = db.scalars(select(QualityProfile)).all()
    if not rows:  # never happens after the migration; keeps a broken database usable
        return ProfileSet(SEED_PROFILES)
    return ProfileSet(to_profile(r) for r in rows)


def default_key(db: Session) -> str:
    """The profile new requests aim for (a runtime setting, falling back to FLAC Standard)."""
    return get_setting(db, DEFAULT_KEY_SETTING, DEFAULT_QUALITY)


def _rows(db: Session) -> list[QualityProfile]:
    return list(db.scalars(select(QualityProfile).order_by(QualityProfile.rank)))


# ------------------------------------------------------------------ validation


def _standard(raw: str, allowed: tuple[int, ...], *, scale: float = 1) -> int:
    """The value, if it is one of the standards offered in the form."""
    try:
        value = round(float(raw.strip().replace(",", ".")) * scale)
    except (ValueError, OverflowError):
        raise ProfileError("quality_number") from None
    if value not in allowed:
        raise ProfileError("quality_number")
    return value


def parse_form(form: dict[str, str]) -> ProfileInput:
    name = " ".join((form.get("name") or "").split())
    if not name:
        raise ProfileError("quality_name_required")
    if len(name) > MAX_NAME or any(ord(c) < 32 for c in name):
        raise ProfileError("quality_name_invalid")
    kind = form.get("kind", "")
    if kind not in KINDS:
        raise ProfileError("quality_kind")
    color = form.get("color", "slate")
    if color not in COLORS:
        raise ProfileError("quality_color")
    data = ProfileInput(name=name, kind=kind, color=color)
    if kind == KIND_LOSSLESS:
        data.min_bit_depth = _standard(form.get("min_bit_depth", ""), BIT_DEPTHS)
        data.min_sample_rate = _standard(
            form.get("min_sample_rate", ""), SAMPLE_RATES_HZ, scale=1000
        )
    elif kind == KIND_LOSSY:
        data.min_bitrate = _standard(form.get("min_bitrate", ""), BITRATES_KBPS)
        data.accept_v0 = form.get("accept_v0", "") in ("1", "on", "true")
    return data


def _check_name(db: Session, name: str, ignore_id: int | None) -> None:
    stmt = select(QualityProfile.id).where(func.lower(QualityProfile.name) == name.lower())
    if ignore_id is not None:
        stmt = stmt.where(QualityProfile.id != ignore_id)
    if db.scalar(stmt) is not None:
        raise ProfileError("quality_name_taken")


def _apply(row: QualityProfile, data: ProfileInput) -> None:
    row.name, row.kind, row.color = data.name, data.kind, data.color
    row.min_bit_depth = data.min_bit_depth
    row.min_sample_rate = data.min_sample_rate
    row.min_bitrate = data.min_bitrate
    row.accept_v0 = data.accept_v0


# ------------------------------------------------------------------- operations
# Every function below leaves the commit and the re-classification job to the caller.


def create_profile(db: Session, data: ProfileInput) -> QualityProfile:
    _check_name(db, data.name, None)
    keys = db.scalars(select(QualityProfile.key)).all()
    top = db.scalar(select(func.max(QualityProfile.rank))) or 0
    row = QualityProfile(key=slugify(data.name, keys), rank=top + 1)
    _apply(row, data)
    db.add(row)
    db.flush()
    return row


def update_profile(db: Session, profile_id: int, data: ProfileInput) -> QualityProfile:
    row = db.get(QualityProfile, profile_id)
    if row is None:
        raise ProfileError("quality_not_found")
    _check_name(db, data.name, profile_id)
    _apply(row, data)
    db.flush()
    return row


def move_profile(db: Session, profile_id: int, direction: str) -> None:
    """Swap with the neighbour: 'up' makes the profile better ranked."""
    rows = _rows(db)
    index = next((i for i, r in enumerate(rows) if r.id == profile_id), None)
    if index is None:
        raise ProfileError("quality_not_found")
    other = index + (1 if direction == "up" else -1)
    if not 0 <= other < len(rows):
        return
    rows[index].rank, rows[other].rank = rows[other].rank, rows[index].rank
    db.flush()


def delete_profile(db: Session, profile_id: int) -> str:
    """Remove a profile; returns its key. The default and the last profile stay."""
    row = db.get(QualityProfile, profile_id)
    if row is None:
        raise ProfileError("quality_not_found")
    if row.key == default_key(db):
        raise ProfileError("quality_delete_default")
    if (db.scalar(select(func.count(QualityProfile.id))) or 0) <= 1:
        raise ProfileError("quality_delete_last")
    db.delete(row)
    db.flush()
    return row.key


def set_default(db: Session, key: str) -> None:
    if db.scalar(select(QualityProfile.id).where(QualityProfile.key == key)) is None:
        raise ProfileError("quality_not_found")
    set_setting(db, DEFAULT_KEY_SETTING, key)


def restore_defaults(db: Session) -> None:
    """Replace every profile with the initial ones and make FLAC Standard the default."""
    for row in db.scalars(select(QualityProfile)).all():
        db.delete(row)
    db.flush()
    for profile in SEED_PROFILES:
        db.add(
            QualityProfile(
                key=profile.key,
                name=profile.name,
                kind=profile.kind,
                rank=profile.rank,
                min_bit_depth=profile.min_bit_depth,
                min_sample_rate=profile.min_sample_rate,
                min_bitrate=profile.min_bitrate,
                accept_v0=profile.accept_v0,
                color=profile.color,
            )
        )
    db.flush()
    set_setting(db, DEFAULT_KEY_SETTING, DEFAULT_QUALITY)


# -------------------------------------------------------------- classification


def reclassify(db: Session, profiles: ProfileSet) -> tuple[int, int]:
    """Recompute every track and album tier. Returns (tracks changed, albums changed).

    Tracks are grouped by their quality-relevant fields, so the work is one UPDATE per distinct
    combination (a few hundred at most) instead of one per file.
    """
    t = LibraryTrack
    combos = db.execute(
        select(t.lossless, t.v0, t.bitrate, t.bit_depth, t.sample_rate).distinct()
    ).all()
    tracks_changed = 0
    for lossless, v0, bitrate, bit_depth, sample_rate in combos:
        tier = profiles.classify(
            lossless=bool(lossless),
            bitrate_kbps=bitrate,
            bit_depth=bit_depth,
            sample_rate=sample_rate,
            v0=bool(v0),
        )
        where = [
            t.lossless.is_(bool(lossless)),
            t.v0.is_(bool(v0)),
            t.bitrate.is_(bitrate) if bitrate is None else t.bitrate == bitrate,
            t.bit_depth.is_(bit_depth) if bit_depth is None else t.bit_depth == bit_depth,
            t.sample_rate.is_(sample_rate) if sample_rate is None else t.sample_rate == sample_rate,
            t.quality_tier != tier,
        ]
        tracks_changed += db.execute(update(t).where(*where).values(quality_tier=tier)).rowcount

    weakest: dict[int, str] = {}
    for album_id, tier in db.execute(select(t.album_id, t.quality_tier).distinct()):
        current = weakest.get(album_id)
        weakest[album_id] = tier if current is None else profiles.album_quality([current, tier])
    albums_changed = 0
    by_tier: dict[str, list[int]] = {}
    for album_id, tier in weakest.items():
        by_tier.setdefault(tier, []).append(album_id)
    a = LibraryAlbum
    for tier, ids in by_tier.items():
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            albums_changed += db.execute(
                update(a).where(a.id.in_(chunk), a.quality_tier != tier).values(quality_tier=tier)
            ).rowcount
    return tracks_changed, albums_changed


__all__ = [
    "KIND_ANY",
    "ProfileError",
    "ProfileInput",
    "create_profile",
    "default_key",
    "delete_profile",
    "load_profiles",
    "move_profile",
    "parse_form",
    "reclassify",
    "restore_defaults",
    "set_default",
    "update_profile",
]
