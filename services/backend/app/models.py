from datetime import datetime, timezone

from sqlalchemy import JSON, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from typing import Optional


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Production(Base):
    __tablename__ = "productions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(100))
    prompt: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(20))
    model_id: Mapped[str] = mapped_column(String(50))
    language: Mapped[str] = mapped_column(String(30))
    genre: Mapped[str] = mapped_column(String(80))
    mood: Mapped[str] = mapped_column(String(80))
    vocal: Mapped[str] = mapped_column(String(80))
    duration_seconds: Mapped[int] = mapped_column(Integer)
    lyrics_mode: Mapped[str] = mapped_column(String(10))
    lyrics: Mapped[str] = mapped_column(Text, default="")
    composition_plan: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider_song_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    audio_filename: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    audio_duration: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    audio_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Song(Base):
    __tablename__ = "songs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    next_version: Mapped[int] = mapped_column(Integer, default=2)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SongVersion(Base):
    """Additive mapping: original production rows and audio URLs remain untouched."""
    __tablename__ = "song_versions"
    __table_args__ = (
        UniqueConstraint("song_id", "version_no", name="uq_song_version_number"),
        CheckConstraint("version_no > 0", name="positive_version_number"),
    )

    production_id: Mapped[str] = mapped_column(ForeignKey("productions.id"), primary_key=True)
    song_id: Mapped[str] = mapped_column(ForeignKey("songs.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    parent_production_id: Mapped[Optional[str]] = mapped_column(ForeignKey("productions.id"), nullable=True)


class CorpusTrack(Base):
    __tablename__ = "corpus_tracks"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    source: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(Text)
    artist: Mapped[str] = mapped_column(Text)
    genre: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    license_url: Mapped[str] = mapped_column(Text)
    audio_filename: Mapped[str] = mapped_column(String(120))
    feature_filename: Mapped[str] = mapped_column(String(120))
    audio_hash: Mapped[str] = mapped_column(String(64))
    feature_hash: Mapped[str] = mapped_column(String(64))
    feature_version: Mapped[str] = mapped_column(String(80))
    duration: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    production_id: Mapped[str] = mapped_column(ForeignKey("productions.id"), index=True)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    corpus_manifest: Mapped[dict] = mapped_column(JSON)
    algorithm_version: Mapped[str] = mapped_column(String(80))
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    elapsed_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
