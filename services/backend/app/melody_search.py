"""Opt-in experimental note-contour search over the same immutable reference snapshot."""
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

import numpy as np

from .compare_melody import calibration_from
from .melody import MelodyUnavailable, Notes, compare_notes, extract_notes
from .similarity import AnalysisError, SearchIndex

CALIBRATION_PATH = Path(__file__).with_name("melody_calibration.json")
MELODY_ALGORITHM = "experimental-notes-v2-" + hashlib.sha256(CALIBRATION_PATH.read_bytes()).hexdigest()[:12]


def cached_notes(path, source_hash, cache_dir, method_hash):
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{source_hash}-{method_hash[:16]}.json"
    if cache.is_file():
        try:
            value = json.loads(cache.read_text())
            if value["source_hash"] != source_hash or value["method_hash"] != method_hash:
                raise ValueError("cache revision mismatch")
            if value.get("status") == "insufficient":
                raise MelodyUnavailable(value["reason"])
            notes = Notes(np.asarray(value["pitches"]), np.asarray(value["starts"]),
                np.asarray(value["durations"]), float(value["seconds"]), float(value["coverage"]))
            if not (len(notes.pitches) == len(notes.starts) == len(notes.durations) >= 6
                    and np.isfinite(np.r_[notes.pitches, notes.starts, notes.durations, notes.seconds, notes.coverage]).all()
                    and (notes.durations > 0).all() and (np.diff(notes.starts) >= 0).all()
                    and 0 <= notes.starts[0] and np.max(notes.starts + notes.durations) <= notes.seconds + .02):
                raise ValueError("invalid cached notes")
            return notes
        except (KeyError, ValueError, TypeError) as exc:
            if isinstance(exc, MelodyUnavailable):
                raise
            # Derived caches can be rebuilt from the separately validated immutable source.
    value = {"source_hash": source_hash, "method_hash": method_hash}
    error = None
    try:
        notes = extract_notes(path)
        value.update(status="ready", pitches=notes.pitches.tolist(), starts=notes.starts.tolist(),
            durations=notes.durations.tolist(), seconds=notes.seconds, coverage=notes.coverage)
    except MelodyUnavailable as exc:
        error = exc
        value.update(status="insufficient", reason=str(exc))
    partial = cache.with_name(f"{cache.name}.{uuid4().hex}.part")
    partial.write_text(json.dumps(value, sort_keys=True))
    partial.replace(cache)
    if error:
        raise error
    return notes


def note_windows(notes):
    size = min(10., notes.seconds)
    starts = list(np.arange(0, max(0, notes.seconds - size) + .001, 5.))
    last = max(0, notes.seconds - size)
    if not starts or abs(starts[-1] - last) > .01:
        starts.append(last)
    for start in starts:
        end = start + size
        mask = (notes.starts < end) & (notes.starts + notes.durations > start)
        clipped_start = np.maximum(notes.starts[mask], start)
        clipped_end = np.minimum(notes.starts[mask] + notes.durations[mask], end)
        valid = clipped_end - clipped_start >= .08
        yield Notes(notes.pitches[mask][valid], clipped_start[valid],
                    (clipped_end - clipped_start)[valid], notes.seconds, notes.coverage)


class MelodySearchIndex:
    def __init__(self, settings, tracks, manifest):
        self.settings, self.tracks, self.manifest = settings, tracks, manifest
        # Reuse source/feature hash checks; this mode then scans every usable reference rather than applying a chroma gate.
        SearchIndex(settings, tracks, manifest)
        self.calibration, self.thresholds = calibration_from(CALIBRATION_PATH)

    def search(self, path, input_hash):
        started = time.perf_counter()
        cache = self.settings.data_dir / "melody-features"
        method_hash = self.calibration["method_sha256"]
        try:
            query = cached_notes(path, input_hash, cache, method_hash)
        except MelodyUnavailable as exc:
            raise AnalysisError(f"선율·음길이 실험 분석 불가: {exc} 유사하지 않다는 결과가 아닙니다.") from exc
        windows = list(note_windows(query))
        usable = [window for window in windows if len(window.pitches) >= 6]
        if not usable:
            raise AnalysisError("실험 조건에 맞는 선율 구간을 추출하지 못했습니다. 유사하지 않다는 결과가 아닙니다.")
        matches, unavailable, reviewed = [], [], 0
        for track in self.tracks:
            audio = self.settings.data_dir / "corpus" / "audio" / track.audio_filename
            try:
                reference = cached_notes(audio, track.audio_hash, cache, method_hash)
            except MelodyUnavailable as exc:
                unavailable.append({"track_id": track.id, "reason": str(exc)})
                continue
            reviewed += 1
            candidates = [compare_notes(window, reference, *self.thresholds) for window in usable]
            candidates = [candidate for candidate in candidates if candidate["melody_match"]]
            if not candidates:
                continue
            result = min(candidates, key=lambda candidate: (candidate["melody_distance"], candidate["rhythm_distance"]))
            result.update(track_id=track.id, title=track.title, artist=track.artist, genre=track.genre,
                source_url=track.source_url, license_url=track.license_url,
                distance=result["melody_distance"], evidence_type="melody_and_duration" if result["rhythm_match"] else "melody_only",
                audio_url=f"/api/v1/corpus/tracks/{track.id}/audio", time_basis="FMA 30초 데이터셋 클립 기준")
            matches.append(result)
        if not reviewed:
            raise AnalysisError("비교곡에서 선율 후보를 충분히 추출하지 못했습니다. 검사가 완료되지 않았습니다.")
        matches.sort(key=lambda match: (match["melody_distance"], match["rhythm_distance"], match["track_id"]))
        return {"matches": matches[:5], "analysis_mode": "melody_rhythm", "corpus_id": self.manifest["id"],
            "corpus_tracks": len(self.tracks), "reviewed_tracks": reviewed, "unavailable_references": unavailable,
            "query_windows": len(windows), "reviewed_windows": len(usable),
            "coverage": "선율 후보를 추출한 최대 10초 구간만 실험 비교",
            "coverage_status": "partial" if unavailable or len(usable) != len(windows) else "available_features",
            "algorithm_version": MELODY_ALGORITHM, "query_duration": query.seconds,
            "melody_threshold": self.thresholds[0], "rhythm_threshold": self.thresholds[1],
            "search_ms": round((time.perf_counter() - started) * 1000, 2),
            "notice": "선율 후보·음 길이를 별도로 비교한 실험 결과입니다. 실제 보컬과 주선율 추출 정확도는 미검증이며, 코드 진행·창작성·의거관계·법적 표절 여부를 판정하지 않습니다."}
