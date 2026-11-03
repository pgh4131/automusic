import logging
import time
from datetime import timedelta

from sqlalchemy import select, update

from .config import Settings
from .db import make_database
from .models import AnalysisRun, CorpusTrack, Production, utcnow
from .similarity import ALGORITHM_VERSION, AnalysisError, SearchIndex, extract, file_hash
from .melody_search import MELODY_ALGORITHM, MelodySearchIndex

logger = logging.getLogger("automusic.analysis")


def recover_expired(sessions):
    with sessions.begin() as session:
        session.execute(update(AnalysisRun).where(AnalysisRun.status == "running", AnalysisRun.lease_until < utcnow()).values(
            status="failed", error_message="검사 워커가 중단됐습니다. 다시 검사할 수 있습니다.", lease_until=None, completed_at=utcnow()))


def process_one(settings, sessions):
    with sessions() as session:
        job = session.scalar(select(AnalysisRun).where(AnalysisRun.status == "queued").order_by(AnalysisRun.created_at).limit(1))
        if job is None:
            return False
        job_id = job.id
        lease = utcnow() + timedelta(minutes=10)
        claimed = session.execute(update(AnalysisRun).where(AnalysisRun.id == job_id, AnalysisRun.status == "queued").values(
            status="running", lease_until=lease))
        session.commit()
        if claimed.rowcount != 1:
            return True
    started = time.perf_counter()
    try:
        with sessions() as session:
            job = session.get(AnalysisRun, job_id)
            if job.algorithm_version not in {ALGORITHM_VERSION, MELODY_ALGORITHM}:
                raise AnalysisError("검사 알고리즘이 변경됐습니다. 현재 버전으로 다시 검사를 요청해 주세요.")
            production = session.get(Production, job.production_id)
            path = settings.data_dir / "audio" / production.audio_filename
            if not path.is_file() or file_hash(path) != job.input_hash:
                raise AnalysisError("검사할 음원이 없거나 접수한 버전의 파일과 다릅니다.")
            chroma, seconds = extract(path)
            members = [member["id"] for member in job.corpus_manifest["members"]]
            tracks = list(session.scalars(select(CorpusTrack).where(CorpusTrack.id.in_(members))))
            if job.algorithm_version == MELODY_ALGORITHM:
                result = MelodySearchIndex(settings, tracks, job.corpus_manifest).search(path, job.input_hash)
            else:
                index = SearchIndex(settings, tracks, job.corpus_manifest)
                result = index.search(chroma, seconds, job.corpus_manifest)
            elapsed = (time.perf_counter() - started) * 1000
            session.execute(update(AnalysisRun).where(AnalysisRun.id == job_id,
                AnalysisRun.status == "running", AnalysisRun.lease_until == lease,
                AnalysisRun.lease_until > utcnow()).values(status="ready", result=result,
                elapsed_ms=elapsed, lease_until=None, completed_at=utcnow()))
            session.commit()
            logger.info("analysis_ready id=%s tracks=%d elapsed_ms=%.1f", job_id, len(tracks), elapsed)
    except Exception as exc:
        message = str(exc) if isinstance(exc, AnalysisError) else "분석 처리에 실패했습니다. 입력 음원과 비교 DB를 확인해 주세요."
        with sessions.begin() as session:
            session.execute(update(AnalysisRun).where(AnalysisRun.id == job_id,
                AnalysisRun.status == "running", AnalysisRun.lease_until == lease).values(status="failed",
                error_message=message, elapsed_ms=(time.perf_counter() - started) * 1000,
                lease_until=None, completed_at=utcnow()))
        logger.warning("analysis_failed id=%s type=%s", job_id, type(exc).__name__)
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.load()
    engine, sessions = make_database(settings)
    logger.info("analysis_worker_started")
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
