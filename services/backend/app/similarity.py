"""Local chroma retrieval + subsequence DTW baseline, not a plagiarism classifier."""
import hashlib
import json
import time
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

FEATURE_VERSION = "contrast-chroma-22050-2048-4fps-v2"
ALGORITHM_VERSION = "pooled-contrast-dtw-v3"
FPS = 4
WINDOW = 10 * FPS
STRIDE = 5 * FPS
MAX_QUERY_SECONDS = 180
MAX_DISTANCE = 0.18  # Uncalibrated, conservative baseline; not a legal/confidence threshold.


class AnalysisError(Exception):
    pass


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract(path):
    # Inspect before decoding and read a bounded number of samples. No audioread/ffmpeg fallback.
    try:
        with sf.SoundFile(path) as audio:
            seconds = audio.frames / audio.samplerate
            if not 6 <= seconds <= MAX_QUERY_SECONDS or audio.channels > 8 or audio.samplerate > 192000 or audio.frames * audio.channels * 4 > 128 * 1024 * 1024:
                raise AnalysisError("검사 가능한 음원은 6~180초, 최대 8채널·192kHz입니다.")
            samples = audio.read(int(MAX_QUERY_SECONDS * audio.samplerate) + 1, dtype="float32", always_2d=True).mean(axis=1)
            rate = audio.samplerate
    except (RuntimeError, sf.LibsndfileError) as exc:
        raise AnalysisError("음원을 디코딩할 수 없습니다.") from exc
    if not np.isfinite(samples).all() or np.sqrt(np.mean(samples ** 2)) < 1e-5:
        raise AnalysisError("무음이거나 유효하지 않은 음원은 검사할 수 없습니다.")
    samples = librosa.resample(samples, orig_sr=rate, target_sr=22050)
    spectrum = np.abs(librosa.stft(samples, n_fft=4096, hop_length=2048)) ** 2
    if float(np.median(librosa.feature.spectral_flatness(S=np.sqrt(spectrum)))) > 0.35:
        raise AnalysisError("잡음 성분이 많아 음높이 기반 비교에 필요한 특징을 얻기 어렵습니다. 이 음원은 검사하지 못했습니다.")
    # Compress peaks to reduce timbre dominance; retain original time origin (no silence trimming).
    chroma = librosa.feature.chroma_stft(S=np.sqrt(spectrum), sr=22050, n_fft=4096,
                                         hop_length=2048, tuning=0, norm=None)
    count = max(1, int(np.ceil(seconds * FPS)))
    frame_times = np.arange(chroma.shape[1]) * 2048 / 22050
    pooled = np.zeros((12, count), dtype=np.float32)
    for index in range(count):
        mask = (frame_times >= index / FPS) & (frame_times < (index + 1) / FPS)
        if mask.any():
            pooled[:, index] = chroma[:, mask].mean(axis=1)
    # Suppress the common pitch-class floor: otherwise broadband noise can look similar to every song.
    pooled = np.maximum(pooled - pooled.mean(axis=0, keepdims=True), 0)
    norms = np.linalg.norm(pooled, axis=0)
    if np.count_nonzero(norms > 1e-8) < 6 * FPS:
        raise AnalysisError("분석 가능한 소리 구간이 너무 짧습니다.")
    return (pooled / np.maximum(norms, 1e-8)).astype(np.float32), seconds


def windows(chroma):
    length = min(WINDOW, chroma.shape[1])
    starts = list(range(0, chroma.shape[1] - length + 1, STRIDE))
    if starts[-1] != chroma.shape[1] - length:
        starts.append(chroma.shape[1] - length)
    return [(start, chroma[:, start:start + length]) for start in starts]


def descriptor(chroma):
    value = np.stack([block.mean(axis=1) for block in np.array_split(chroma, 4, axis=1)], axis=1)
    value = value.reshape(-1)
    return value / max(float(np.linalg.norm(value)), 1e-8)


def save_features(path, chroma, seconds):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    with partial.open("wb") as stream:
        np.savez_compressed(stream, chroma=chroma, seconds=np.array(seconds), version=np.array(FEATURE_VERSION))
    partial.replace(path)


def load_features(path):
    with np.load(path, allow_pickle=False) as data:
        chroma = data["chroma"]
        if str(data["version"]) != FEATURE_VERSION or chroma.ndim != 2 or chroma.shape[0] != 12 or not np.isfinite(chroma).all():
            raise AnalysisError("검색 특징 버전 또는 파일이 올바르지 않습니다.")
        return chroma.copy(), float(data["seconds"])


def manifest_for(tracks):
    members = [{"id": t.id, "feature_hash": t.feature_hash, "audio_hash": t.audio_hash} for t in sorted(tracks, key=lambda t: t.id)]
    digest = hashlib.sha256(json.dumps(members, sort_keys=True).encode()).hexdigest()
    return {"id": digest, "feature_version": FEATURE_VERSION, "members": members}


class SearchIndex:
    def __init__(self, settings, tracks, manifest):
        self.tracks = {track.id: track for track in tracks}
        self.features = {}
        self.descriptors = []
        self.locations = []
        for member in manifest["members"]:
            track = self.tracks.get(member["id"])
            if track is None or track.feature_hash != member["feature_hash"] or track.audio_hash != member["audio_hash"]:
                raise AnalysisError("접수한 비교 DB의 구성과 현재 데이터가 다릅니다.")
            feature = settings.data_dir / "corpus" / "features" / track.feature_filename
            audio = settings.data_dir / "corpus" / "audio" / track.audio_filename
            if not feature.is_file() or not audio.is_file() or file_hash(feature) != member["feature_hash"] or file_hash(audio) != member["audio_hash"]:
                raise AnalysisError("비교 DB 파일이 없거나 변경됐습니다. 코퍼스를 다시 준비해 주세요.")
            chroma, _ = load_features(feature)
            self.features[track.id] = chroma
            for start, section in windows(chroma):
                self.descriptors.append(descriptor(section))
                self.locations.append((track.id, start))
        if not self.descriptors:
            raise AnalysisError("비교 음원 DB가 비어 있습니다.")
        self.matrix = np.stack(self.descriptors)

    def search(self, chroma, seconds, manifest):
        started = time.perf_counter()
        query_windows = windows(chroma)
        # Exact retrieval baseline; no ANN recall loss. Search all 12 pitch-class rotations.
        candidates = {}
        retrieval = []
        for query_start, query in query_windows:
            similarities = np.stack([np.einsum("ij,j->i", self.matrix, descriptor(np.roll(query, shift, axis=0))) for shift in range(12)])
            order = np.argsort(similarities.max(axis=0))[::-1]
            chosen = {}
            for position in order:
                track_id, ref_start = self.locations[position]
                if track_id not in chosen:
                    shift = int(np.argmax(similarities[:, position]))
                    chosen[track_id] = (shift, ref_start)
                if len(chosen) >= min(20, len(self.tracks)):
                    break
            retrieval.append((query_start, query, chosen))
        retrieval_ms = (time.perf_counter() - started) * 1000
        for query_start, query, chosen in retrieval:
            for track_id, (preferred_shift, _) in chosen.items():
                reference = self.features[track_id]
                # Retrieval pooling may favour another rotation; verify all rotations during alignment.
                for shift in range(12):
                    shifted = np.roll(query, shift, axis=0)
                    cost = np.clip(1 - np.einsum("ik,ij->kj", shifted, reference), 0, 2)
                    # Bounded slope (0.5x–2x); no unconstrained horizontal/vertical stretches.
                    _, path = librosa.sequence.dtw(C=cost, subseq=True,
                        step_sizes_sigma=np.array([[1, 1], [1, 2], [2, 1]]),
                        weights_mul=np.array([1., 1., 1.]))
                    distance = float(np.mean(cost[path[:, 0], path[:, 1]]))
                    start, end = int(path[-1, 1]), int(path[0, 1]) + 1
                    if not 0.5 <= (end - start) / query.shape[1] <= 2:
                        continue
                    if distance > MAX_DISTANCE:
                        continue
                    match = {"track_id": track_id, "distance": round(distance, 6),
                        "query_start": round(query_start / FPS, 3),
                        "query_end": round(min(seconds, (query_start + query.shape[1]) / FPS), 3),
                        "reference_start": round(start / FPS, 3),
                        "reference_end": round(min(self.tracks[track_id].duration, end / FPS), 3),
                        "pitch_shift": shift if shift <= 6 else shift - 12}
                    if track_id not in candidates or distance < candidates[track_id]["distance"]:
                        candidates[track_id] = match
        items = sorted(candidates.values(), key=lambda match: match["distance"])[:5]
        for match in items:
            track = self.tracks[match["track_id"]]
            match.update(title=track.title, artist=track.artist, genre=track.genre,
                source_url=track.source_url, license_url=track.license_url,
                audio_url=f"/api/v1/corpus/tracks/{track.id}/audio",
                time_basis="FMA 30초 데이터셋 클립 기준")
        return {"matches": items, "corpus_id": manifest["id"], "corpus_tracks": len(self.tracks),
            "algorithm_version": ALGORITHM_VERSION, "feature_version": FEATURE_VERSION,
            "query_duration": seconds, "query_windows": len(query_windows),
            "retrieval_ms": round(retrieval_ms, 2), "search_ms": round((time.perf_counter() - started) * 1000, 2),
            "threshold": MAX_DISTANCE, "coverage": "전체 입력을 겹치는 최대 10초 창으로 비교",
            "notice": "등록된 비교 DB 안에서 찾은 chroma 기반 후보입니다. 거리값은 표절 확률이 아니며, 후보가 없어도 비유사성을 보장하지 않습니다. 임계값은 아직 품질 평가 전입니다."}
