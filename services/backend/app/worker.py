import hashlib
import logging
import time
from datetime import timedelta
from pathlib import Path

from mutagen import File as AudioFile
from sqlalchemy import select, update

from .config import Settings
from .db import make_database
from .models import Production, utcnow
from .providers import DemoProvider, ElevenLabsProvider, ProviderError, plan_lyrics

logger = logging.getLogger("automusic.worker")


def recover_expired(sessions):
    with sessions.begin() as session:
        # An interrupted paid call is never blindly retried.
        session.execute(update(Production).where(
            Production.status.in_(["planning", "generating", "saving"]),
            Production.lease_until < utcnow(),
        ).values(status="needs_review", error_code="worker_interrupted",
            error_message="작업이 중단됐습니다. 생성 제공자의 처리·과금 여부를 확인해 주세요.", updated_at=utcnow()))


def process_one(settings, sessions, provider_factory=None):
    with sessions() as session:
        job = session.scalar(select(Production).where(Production.status == "queued").order_by(Production.created_at).limit(1))
        if job is None:
            return False
        job_id = job.id
        claimed = session.execute(update(Production).where(Production.id == job_id, Production.status == "queued").values(
            status="planning", lease_until=utcnow() + timedelta(minutes=15), updated_at=utcnow()))
        session.commit()
        if claimed.rowcount != 1:
            return True

    partial = settings.data_dir / "audio" / f"{job_id}.part"
    partial.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sessions() as session:
            production = session.get(Production, job_id)
            if production.provider == "elevenlabs" and not settings.elevenlabs_api_key:
                raise ProviderError("missing_api_key", "ELEVENLABS_API_KEY를 설정해 주세요.")
            if production.provider == "demo" and not settings.allow_demo:
                raise ProviderError("demo_disabled", "데모 모드가 비활성화되어 있습니다.")
            provider = provider_factory(production.provider) if provider_factory else (
                DemoProvider() if production.provider == "demo" else ElevenLabsProvider(settings))
            plan = provider.build_plan(production)
            advanced = session.execute(update(Production).execution_options(synchronize_session=False).where(
                Production.id == job_id, Production.status == "planning", Production.lease_until > utcnow(),
            ).values(composition_plan=plan,
                lyrics=plan_lyrics(plan) if production.provider != "demo" else production.lyrics,
                status="generating", updated_at=utcnow()))
            session.commit()
            if advanced.rowcount != 1:
                return True
            song_id = provider.generate(production, plan, partial)
            advanced = session.execute(update(Production).execution_options(synchronize_session=False).where(
                Production.id == job_id, Production.status == "generating", Production.lease_until > utcnow(),
            ).values(status="saving", updated_at=utcnow()))
            session.commit()
            if advanced.rowcount != 1:
                return True
            metadata = AudioFile(partial)
            if metadata is None or metadata.info.length <= 0 or metadata.info.length > 180:
                raise ProviderError("invalid_audio", "재생 가능한 길이의 음원 파일을 받지 못했습니다.")
            extension = "wav" if production.provider == "demo" else "mp3"
            filename = f"{job_id}.{extension}"
            destination = partial.with_name(filename)
            digest = hashlib.sha256(partial.read_bytes()).hexdigest()
            partial.replace(destination)
            # Conditional commit prevents a stale worker overwriting a reviewed job.
            changed = session.execute(update(Production).execution_options(synchronize_session=False).where(
                Production.id == job_id, Production.status == "saving", Production.lease_until > utcnow(),
            ).values(
                status="ready", audio_filename=filename, audio_duration=metadata.info.length,
                audio_bytes=destination.stat().st_size, content_hash=digest, provider_song_id=song_id,
                lease_until=None, updated_at=utcnow()))
            session.commit()
            if changed.rowcount == 1:
                logger.info("production_ready id=%s provider=%s", job_id, production.provider)
    except ProviderError as exc:
        with sessions.begin() as session:
            session.execute(update(Production).where(Production.id == job_id, Production.status.in_(["planning", "generating", "saving"])).values(
                status="needs_review" if exc.uncertain else "failed", error_code=exc.code,
                error_message=exc.message, lease_until=None, updated_at=utcnow()))
        logger.warning("production_failed id=%s code=%s", job_id, exc.code)
    except Exception:
        # No exception bodies or prompts: SDK errors can contain sensitive request data.
        with sessions.begin() as session:
            session.execute(update(Production).where(Production.id == job_id, Production.status.in_(["planning", "generating", "saving"])).values(
                status="needs_review", error_code="processing_interrupted",
                error_message="음원 처리에 실패했습니다. 생성 결과 확인 전 자동 재시도하지 않습니다.", lease_until=None, updated_at=utcnow()))
        logger.error("production_processing_interrupted id=%s", job_id)
    finally:
        partial.unlink(missing_ok=True)
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.load()
    engine, sessions = make_database(settings)
    logger.info("worker_started")
    try:
        while True:
            recover_expired(sessions)
            if not process_one(settings, sessions):
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
