"""Experimental foreground pitch-contour and note-duration comparison.

This is a signal-processing baseline, not a verified vocal separator or a legal classifier.
"""
from dataclasses import dataclass

import librosa
import numpy as np
import soundfile as sf
from scipy.ndimage import median_filter

METHOD_VERSION = "foreground-pitch-notes-v2"
SR = 22050
HOP = 256


class MelodyUnavailable(ValueError):
    pass


@dataclass
class Notes:
    pitches: np.ndarray
    starts: np.ndarray
    durations: np.ndarray
    seconds: float
    coverage: float = 1.0


def extract_notes(path):
    with sf.SoundFile(path) as audio:
        seconds = audio.frames / audio.samplerate
        if not 6 <= seconds <= 180 or audio.frames * audio.channels * 4 > 128 * 1024 * 1024:
            raise MelodyUnavailable("음원 길이 또는 디코딩 크기가 분석 범위를 벗어납니다.")
        samples = audio.read(dtype="float32", always_2d=True).mean(axis=1)
        rate = audio.samplerate
    if not np.isfinite(samples).all() or float(np.sqrt(np.mean(samples ** 2))) < 1e-5:
        raise MelodyUnavailable("분석할 음높이 정보가 부족합니다.")
    samples = librosa.resample(samples, orig_sr=rate, target_sr=SR)
    spectrum = np.abs(librosa.stft(samples, n_fft=4096, hop_length=HOP))
    # Limit the experimental contour search to 110–1397 Hz; lower bass is not treated as melody.
    midi = np.arange(45, 90)
    frequencies = librosa.midi_to_hz(midi)
    salience = np.zeros((len(midi), spectrum.shape[1]), dtype=np.float64)
    for harmonic, weight in ((1, 1.), (2, .35), (3, .15)):
        bins = np.rint(frequencies * harmonic * 4096 / SR).astype(int)
        for offset in (-1, 0, 1):
            salience += weight / 3 * spectrum[np.clip(bins + offset, 0, spectrum.shape[0] - 1)]
    # Discount sustained background energy without assuming the foreground is the loudest source.
    kernel = int(round(3.0 * SR / HOP)) | 1
    background = median_filter(salience, size=(1, kernel), mode="nearest")
    foreground = np.maximum(salience - background, 0) + .08 * salience
    probability = np.maximum(foreground, 1e-8)
    probability /= probability.sum(axis=0, keepdims=True)
    changes = np.abs(midi[:, None] - midi[None, :])
    transition = np.exp(-.6 * np.minimum(changes, 12))
    transition /= transition.sum(axis=1, keepdims=True)
    states = librosa.sequence.viterbi(probability, transition)
    contour = median_filter(midi[states], size=5, mode="nearest")
    energy = np.sqrt(np.mean(spectrum ** 2, axis=0))
    voiced = energy > max(float(energy.max()) * .015, 1e-5)
    boundaries = np.flatnonzero(np.r_[True, (np.diff(contour) != 0) | (np.diff(voiced.astype(int)) != 0), True])
    pitches, starts, durations = [], [], []
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        duration = min(seconds, end * HOP / SR) - start * HOP / SR
        if voiced[start] and duration >= .08:
            pitches.append(float(np.median(contour[start:end])))
            starts.append(start * HOP / SR)
            durations.append(duration)
    notes = Notes(np.asarray(pitches), np.asarray(starts), np.asarray(durations), seconds,
                  sum(durations) / seconds)
    if len(notes.pitches) < 6 or len(np.unique(notes.pitches)) < 3 or notes.coverage < .4:
        raise MelodyUnavailable("비교할 선율 후보가 충분하지 않습니다.")
    return notes


def compare_notes(query, reference, melody_threshold=.2, rhythm_threshold=.25):
    if min(len(query.pitches), len(reference.pitches)) < 6:
        raise MelodyUnavailable("최소 6개의 추출 음표가 필요합니다. 이는 실험 조건이며 법적 기준이 아닙니다.")
    if len(query.pitches) > len(reference.pitches):
        # Pair evaluation can compare either order. Production subsequence queries keep explicit direction.
        result = compare_notes(reference, query, melody_threshold, rhythm_threshold)
        result["query_start"], result["reference_start"] = result["reference_start"], result["query_start"]
        result["query_end"], result["reference_end"] = result["reference_end"], result["query_end"]
        return result
    q_intervals, r_intervals = np.diff(query.pitches), np.diff(reference.pitches)
    melody = np.minimum(np.abs(q_intervals[:, None] - r_intervals[None, :]) / 12, 1)
    q_duration = np.maximum(query.durations[:-1], .01)
    r_duration = np.maximum(reference.durations[:-1], .01)
    q_relative, r_relative = q_duration / np.median(q_duration), r_duration / np.median(r_duration)
    rhythm = np.minimum(np.abs(np.log2(q_relative[:, None] / r_relative[None, :])), 1)
    # Preserve melodic alignment; duration is only a tie-breaker, then reported separately.
    cost = melody + 1e-6 * rhythm
    _, path = librosa.sequence.dtw(C=cost, subseq=True,
        step_sizes_sigma=np.array([[1, 1], [1, 2], [2, 1]]), weights_mul=np.ones(3))
    melodic_distance = float(melody[path[:, 0], path[:, 1]].mean())
    rhythmic_distance = float(rhythm[path[:, 0], path[:, 1]].mean())
    q_start, q_end = int(path[-1, 0]), int(path[0, 0]) + 1
    r_start, r_end = int(path[-1, 1]), int(path[0, 1]) + 1
    return {"melody_distance": round(melodic_distance, 6), "rhythm_distance": round(rhythmic_distance, 6),
        "melody_match": melodic_distance <= melody_threshold,
        "rhythm_match": rhythmic_distance <= rhythm_threshold,
        "query_start": round(float(query.starts[q_start]), 3),
        "query_end": round(float(query.starts[q_end] + query.durations[q_end]), 3),
        "reference_start": round(float(reference.starts[r_start]), 3),
        "reference_end": round(float(reference.starts[r_end] + reference.durations[r_end]), 3),
        "aligned_notes": len(path) + 1, "query_notes": len(query.pitches), "reference_notes": len(reference.pitches),
        "aligned_query_fraction": round(len(np.unique(path[:, 0])) / len(q_intervals), 4),
        "method": METHOD_VERSION, "legal_verdict": None}
