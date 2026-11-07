from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class QualityProfile(Base):
    """One rung of the quality ladder (see app/quality.py). Editable in the settings."""

    __tablename__ = "quality_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(48), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16))  # lossless | lossy | any
    rank: Mapped[int] = mapped_column(Integer)  # higher is better
    min_bit_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Hz
    min_bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)  # kbps
    accept_v0: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    color: Mapped[str] = mapped_column(String(16), default="slate")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
