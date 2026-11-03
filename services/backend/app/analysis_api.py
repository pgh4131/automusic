import hashlib
from datetime import timezone
from uuid import UUID, uuid4
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, ConfigDict

from .models import AnalysisRun, CorpusTrack, Production
from .similarity import ALGORITHM_VERSION, FEATURE_VERSION, manifest_for
from .melody_search import MELODY_ALGORITHM


class AnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["chroma", "melody_rhythm"] = "chroma"


def serialize_analysis(row):
    return {"id": row.id, "production_id": row.production_id, "status": row.status,
        "created_at": row.created_at.replace(tzinfo=timezone.utc).isoformat(),
        "completed_at": row.completed_at.replace(tzinfo=timezone.utc).isoformat() if row.completed_at else None,
        "error_message": row.error_message, "elapsed_ms": row.elapsed_ms,
        "corpus_tracks": len(row.corpus_manifest["members"]), "corpus_id": row.corpus_manifest["id"],
        "algorithm_version": row.algorithm_version,
        "mode": "melody_rhythm" if row.algorithm_version.startswith("experimental-notes-") else "chroma",
        "result": row.result}


def create_router(settings, sessions):
    router = APIRouter(prefix="/api/v1")

    @router.get("/corpus")
    def corpus():
        with sessions() as session:
            tracks = list(session.scalars(select(CorpusTrack).where(CorpusTrack.feature_version == FEATURE_VERSION)))
            manifest = manifest_for(tracks)
            return {"tracks": len(tracks), "source": "FMA Small", "snapshot_id": manifest["id"],
                "feature_version": FEATURE_VERSION, "algorithm_version": ALGORITHM_VERSION,
                "algorithms": {"chroma": ALGORITHM_VERSION, "melody_rhythm": MELODY_ALGORITHM},
                "time_basis": "원곡 전체가 아닌 FMA 30초 클립", "available": bool(tracks)}

    @router.post("/productions/{production_id}/analyses", status_code=202)
    def request_analysis(production_id: UUID, payload: Optional[AnalysisInput] = None):
        mode = payload.mode if payload else "chroma"
        algorithm = MELODY_ALGORITHM if mode == "melody_rhythm" else ALGORITHM_VERSION
        with sessions() as session:
            production = session.get(Production, str(production_id))
            if production is None:
                raise HTTPException(404, "검사할 버전을 찾을 수 없습니다.")
            if production.status != "ready" or not production.audio_filename or not production.content_hash:
                raise HTTPException(409, "재생 가능한 음원 버전에서 검사를 요청해 주세요.")
            tracks = list(session.scalars(select(CorpusTrack).where(CorpusTrack.feature_version == FEATURE_VERSION)))
            if not tracks:
                raise HTTPException(409, "비교 음원 DB를 먼저 준비해 주세요.")
            manifest = manifest_for(tracks)
            key = hashlib.sha256(f"{production.id}:{production.content_hash}:{manifest['id']}:{algorithm}".encode()).hexdigest()
            row = session.scalar(select(AnalysisRun).where(AnalysisRun.cache_key == key))
            if row:
                # Repeated active/completed requests share the same result. An explicit click may retry a failed local analysis.
                if row.status == "failed":
                    row.status, row.error_message, row.completed_at = "queued", None, None
                    session.commit()
                return serialize_analysis(row)
            row = AnalysisRun(id=str(uuid4()), production_id=production.id, cache_key=key,
                input_hash=production.content_hash, corpus_manifest=manifest, algorithm_version=algorithm)
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                row = session.scalar(select(AnalysisRun).where(AnalysisRun.cache_key == key))
                if row is None:
                    raise HTTPException(409, "동시 검사 요청을 확인할 수 없습니다. 다시 요청해 주세요.")
            return serialize_analysis(row)

    @router.get("/productions/{production_id}/analyses")
    def list_analyses(production_id: UUID):
        with sessions() as session:
            if session.get(Production, str(production_id)) is None:
                raise HTTPException(404, "검사할 버전을 찾을 수 없습니다.")
            rows = session.scalars(select(AnalysisRun).where(AnalysisRun.production_id == str(production_id)).order_by(AnalysisRun.created_at.desc()).limit(20))
            return {"items": [serialize_analysis(row) for row in rows]}

    @router.get("/analyses/{analysis_id}")
    def get_analysis(analysis_id: UUID):
        with sessions() as session:
            row = session.get(AnalysisRun, str(analysis_id))
            if row is None:
                raise HTTPException(404, "검사 기록을 찾을 수 없습니다.")
            return serialize_analysis(row)

    @router.get("/corpus/tracks/{track_id}/audio")
    def reference_audio(track_id: str):
        with sessions() as session:
            track = session.get(CorpusTrack, track_id)
            if track is None:
                raise HTTPException(404, "비교 음원을 찾을 수 없습니다.")
            path = settings.data_dir / "corpus" / "audio" / track.audio_filename
            if not path.is_file():
                raise HTTPException(404, "비교 음원 파일이 없습니다.")
            return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=3600"})

    return router
