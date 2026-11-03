import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timezone
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .db import make_database
from .models import Production, Song, SongVersion, utcnow
from .schemas import ProductionInput
from .analysis_api import create_router as create_analysis_router


def serialize(row, version, song):
    result = {key: getattr(row, key) for key in (
        "id", "title", "prompt", "provider", "model_id", "language", "genre", "mood", "vocal",
        "duration_seconds", "lyrics_mode", "lyrics", "status", "error_code", "error_message",
        "audio_duration", "audio_bytes",
    )}
    result["created_at"] = row.created_at.replace(tzinfo=timezone.utc).isoformat()
    result["audio_url"] = f"/api/v1/productions/{row.id}/audio" if row.status == "ready" else None
    result["download_url"] = f"/api/v1/productions/{row.id}/audio?download=true" if row.status == "ready" else None
    result["analysis_status"] = "on_demand"
    result.update(song_id=version.song_id, song_title=song.title,
                  version_no=version.version_no, parent_production_id=version.parent_production_id)
    return result


def version_query():
    return select(Production, SongVersion, Song).join(
        SongVersion, Production.id == SongVersion.production_id,
    ).join(Song, SongVersion.song_id == Song.id)


def serialize_in_session(session, row):
    version = session.get(SongVersion, row.id)
    song = session.get(Song, version.song_id)
    return serialize(row, version, song)


def create_app(settings=None):
    settings = settings or Settings.load()
    engine, sessions = make_database(settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.dispose()

    app = FastAPI(title="AutoMusic API", version="0.3.0", lifespan=lifespan)
    app.state.sessions = sessions
    app.state.settings = settings
    app.include_router(create_analysis_router(settings, sessions))

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/v1/config")
    def config():
        return {"providers": [
            {"id": "elevenlabs", "name": "ElevenLabs Music", "available": bool(settings.elevenlabs_api_key),
             "description": "보컬·가사 포함 실제 AI 음악 생성"},
            {"id": "demo", "name": "로컬 데모", "available": settings.allow_demo,
             "description": "저장·재생 검증용 합성 연주음 · AI 노래 생성 아님"},
        ], "durations": [30, 60], "similarity_available": True}

    @app.get("/api/v1/productions")
    def list_productions():
        with sessions() as session:
            rows = session.execute(version_query().order_by(Production.created_at.desc()).limit(100)).all()
            return {"items": [serialize(*row) for row in rows]}

    @app.get("/api/v1/songs")
    def list_songs():
        with sessions() as session:
            rows = session.execute(version_query().where(
                SongVersion.version_no == Song.next_version - 1,
            ).order_by(Song.updated_at.desc()).limit(100)).all()
            return {"items": [{"id": song.id, "title": song.title,
                "version_count": song.next_version - 1, "latest_version": serialize(row, version, song)}
                for row, version, song in rows]}

    @app.get("/api/v1/songs/{song_id}/versions")
    def list_versions(song_id: UUID):
        with sessions() as session:
            song = session.get(Song, str(song_id))
            if song is None:
                raise HTTPException(404, "곡을 찾을 수 없습니다.")
            rows = session.execute(version_query().where(Song.id == song.id).order_by(
                SongVersion.version_no.desc(),
            )).all()
            return {"song_id": song.id, "title": song.title, "items": [serialize(*row) for row in rows]}

    @app.post("/api/v1/productions", status_code=202)
    def create_production(payload: ProductionInput, idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128)):
        # Excluding absent version fields preserves pre-upgrade idempotency hashes.
        fingerprint = hashlib.sha256(json.dumps(payload.model_dump(mode="json", exclude_none=True), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with sessions() as session:
            existing = session.scalar(select(Production).where(Production.idempotency_key == idempotency_key))
            if existing:
                if existing.payload_hash != fingerprint:
                    raise HTTPException(409, "같은 요청 키에 다른 입력을 사용할 수 없습니다.")
                return serialize_in_session(session, existing)
            if payload.provider == "elevenlabs" and not settings.elevenlabs_api_key:
                raise HTTPException(503, "ELEVENLABS_API_KEY를 서버 .env에 설정해 주세요. 실제 AI 생성은 아직 연결되지 않았습니다.")
            if payload.provider == "demo" and not settings.allow_demo:
                raise HTTPException(403, "데모 모드가 비활성화되어 있습니다.")
            try:
                if payload.song_id:
                    parent = session.get(Production, str(payload.parent_production_id))
                    parent_version = session.get(SongVersion, str(payload.parent_production_id))
                    if parent is None or parent_version is None or parent_version.song_id != str(payload.song_id):
                        raise HTTPException(404, "해당 곡에 속한 기준 버전을 찾을 수 없습니다.")
                    if parent.status != "ready":
                        raise HTTPException(409, "재생 가능한 버전을 기준으로 수정해 주세요.")
                    song_id = str(payload.song_id)
                    # One atomic UPDATE serializes competing requests for this song.
                    allocated = session.scalar(update(Song).where(Song.id == song_id).values(
                        next_version=Song.next_version + 1, updated_at=utcnow(),
                    ).returning(Song.next_version))
                    version_no = allocated - 1
                else:
                    song_id, version_no = str(uuid4()), 1
                    session.add(Song(id=song_id, title=payload.title, next_version=2))
                row = Production(id=str(uuid4()), idempotency_key=idempotency_key, payload_hash=fingerprint,
                    model_id=settings.model_id if payload.provider == "elevenlabs" else "local-synth",
                    **payload.model_dump(exclude={"song_id", "parent_production_id"}))
                session.add(row)
                # No ORM relationship is required by workers; flush FK targets first.
                session.flush()
                session.add(SongVersion(production_id=row.id, song_id=song_id, version_no=version_no,
                    parent_production_id=str(payload.parent_production_id) if payload.parent_production_id else None))
                session.commit()
            except IntegrityError:
                session.rollback()
                row = session.scalar(select(Production).where(Production.idempotency_key == idempotency_key))
                if row is None or row.payload_hash != fingerprint:
                    raise HTTPException(409, "중복 요청의 입력이 일치하지 않습니다.")
            return serialize_in_session(session, row)

    def get_row(session, production_id):
        row = session.get(Production, str(production_id))
        if row is None:
            raise HTTPException(404, "제작 기록을 찾을 수 없습니다.")
        return row

    @app.get("/api/v1/productions/{production_id}")
    def get_production(production_id: UUID):
        with sessions() as session:
            return serialize_in_session(session, get_row(session, production_id))

    @app.get("/api/v1/productions/{production_id}/audio")
    def audio(production_id: UUID, download: bool = False):
        with sessions() as session:
            row = get_row(session, production_id)
            if row.status != "ready" or not row.audio_filename:
                raise HTTPException(409, "음원이 아직 준비되지 않았습니다.")
            path = settings.data_dir / "audio" / row.audio_filename
            if not path.is_file():
                raise HTTPException(404, "저장된 음원 파일을 찾을 수 없습니다.")
            extension = path.suffix
            mime = "audio/wav" if extension == ".wav" else "audio/mpeg"
            return FileResponse(path, media_type=mime,
                filename=f"automusic-{row.id[:8]}{extension}" if download else None,
                headers={"Cache-Control": "private, max-age=3600"})

    return app
