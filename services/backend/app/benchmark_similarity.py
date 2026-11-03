"""Reproducible local retrieval checks on FMA excerpts, not plagiarism accuracy."""
import argparse
import json
import platform
import tempfile
import time
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
from sqlalchemy import select

from .config import Settings
from .db import make_database
from .models import CorpusTrack
from .similarity import ALGORITHM_VERSION, FEATURE_VERSION, AnalysisError, SearchIndex, extract, manifest_for


def run(settings, positive_count=10):
    engine, sessions = make_database(settings)
    try:
        with sessions() as session:
            tracks = list(session.scalars(select(CorpusTrack).where(CorpusTrack.feature_version == FEATURE_VERSION).order_by(CorpusTrack.id)))
        if len(tracks) < positive_count + 5:
            raise ValueError("비교 음원을 먼저 충분히 준비해 주세요.")
        # Hold out whole recordings, not another crop of an indexed recording.
        indexed, held_out = tracks[:-5], tracks[-5:]
        manifest = manifest_for(indexed)
        build_start = time.perf_counter()
        index = SearchIndex(settings, indexed, manifest)
        cases = []
        with tempfile.TemporaryDirectory(prefix="automusic-benchmark-") as directory:
            query_path = Path(directory) / "query.wav"

            def evaluate(samples, rate, kind, track_id=None, offset=None):
                sf.write(query_path, samples, rate)
                started = time.perf_counter()
                try:
                    chroma, seconds = extract(query_path)
                except AnalysisError as exc:
                    cases.append({"kind": kind, "expected_track": track_id, "top1": None,
                        "top1_correct": False if track_id else None, "found_top5": False if track_id else None,
                        "rejected": True, "error": str(exc), "candidate_count": 0,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
                    return
                result = index.search(chroma, seconds, manifest)
                matches = result["matches"]
                expected = next((match for match in matches if match["track_id"] == track_id), None)
                cases.append({"kind": kind, "expected_track": track_id,
                    "top1": matches[0]["track_id"] if matches else None,
                    "found_top5": expected is not None if track_id else None,
                    "top1_correct": bool(matches and matches[0]["track_id"] == track_id) if track_id else None,
                    "reference_start_error_seconds": round(abs(expected["reference_start"] - offset), 3) if expected and offset is not None else None,
                    "candidate_count": len(matches), "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    "top1_distance": matches[0]["distance"] if matches else None,
                    "retrieval_ms": result["retrieval_ms"], "search_ms": result["search_ms"]})

            for track in indexed[:positive_count]:
                samples, rate = sf.read(settings.data_dir / "corpus" / "audio" / track.audio_filename, dtype="float32", always_2d=True)
                mono = samples.mean(axis=1)
                offset = 7.25
                excerpt = mono[int(offset * rate):int((offset + 10) * rate)]
                evaluate(excerpt, rate, "original_excerpt", track.id, offset)
                if len(cases) <= 15:
                    evaluate(librosa.effects.pitch_shift(excerpt, sr=rate, n_steps=2), rate, "pitch_up_2", track.id, offset)
                    evaluate(librosa.effects.time_stretch(excerpt, rate=1.15), rate, "tempo_1.15", track.id, offset)
            for track in held_out:
                samples, rate = sf.read(settings.data_dir / "corpus" / "audio" / track.audio_filename, dtype="float32", always_2d=True)
                evaluate(samples[int(7.25 * rate):int(17.25 * rate)].mean(axis=1), rate, "held_out_recording")
            evaluate(np.random.default_rng(42).normal(0, .05, 22050 * 10), 22050, "white_noise")

        kinds = {}
        for kind in sorted({case["kind"] for case in cases}):
            rows = [case for case in cases if case["kind"] == kind]
            timings = np.array([case["elapsed_ms"] for case in rows])
            kinds[kind] = {"queries": len(rows), "top1_correct": sum(case["top1_correct"] is True for case in rows),
                "found_top5": sum(case["found_top5"] is True for case in rows),
                "queries_with_candidates": sum(case["candidate_count"] > 0 for case in rows),
                "rejected": sum(case.get("rejected", False) for case in rows),
                "median_ms": round(float(np.median(timings)), 2), "max_ms": round(float(timings.max()), 2)}
        report = {"algorithm_version": ALGORITHM_VERSION, "feature_version": FEATURE_VERSION,
            "indexed_tracks": len(indexed), "held_out_tracks": len(held_out), "corpus_id": manifest["id"],
            "platform": platform.platform(), "python": platform.python_version(), "librosa": librosa.__version__,
            "index_and_experiments_ms": round((time.perf_counter() - build_start) * 1000, 2),
            "results": kinds, "cases": cases,
            "limitations": ["동일 녹음의 발췌·통제된 변형을 이용한 검색 실험이며 작곡 유사성·표절 판정 정확도가 아닙니다.",
                "소수 사례의 로컬 측정이며 운영 환경의 p95나 일반화 성능을 주장하지 않습니다.",
                "Held-out recording candidates are not automatically false positives for musical similarity."]}
        return report
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = Settings.load()
    report = run(settings)
    output = args.output or settings.data_dir / "corpus" / "benchmark.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
