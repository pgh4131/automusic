import numpy as np
import pytest
import soundfile as sf

from app.melody import MelodyUnavailable, Notes, compare_notes, extract_notes
from app.probe_melody_sensitivity import render


def notes(pitches, durations=None):
    durations = np.asarray(durations if durations is not None else [.5] * len(pitches), dtype=float)
    return Notes(np.asarray(pitches, dtype=float), np.r_[0., np.cumsum(durations)[:-1]], durations, float(durations.sum()))


def test_note_intervals_preserve_transposition_and_uniform_tempo():
    pitches = np.array([60, 62, 65, 61, 68, 64, 63, 70])
    durations = np.array([.3, .6, .45, .75, .6, .3, .45, .6])
    result = compare_notes(notes(pitches + 5, durations / 1.2), notes(pitches, durations))
    assert result["melody_distance"] == 0
    assert result["rhythm_distance"] == 0
    assert result["melody_match"] and result["rhythm_match"]
    assert result["legal_verdict"] is None


def test_note_duration_pattern_is_distinct_from_pitch_intervals():
    pitches = [60, 62, 65, 61, 68, 64, 63, 70]
    result = compare_notes(notes(pitches, [.2, .9] * 4), notes(pitches))
    assert result["melody_distance"] == 0
    assert result["rhythm_distance"] > .25
    assert result["melody_match"] and not result["rhythm_match"]


def test_subsequence_returns_both_note_time_spans():
    phrase = [60, 62, 65, 61, 68, 64, 63, 70]
    query = notes(phrase)
    reference = notes([51, 48, 56, 52] + phrase + [58, 53, 57, 50])
    result = compare_notes(query, reference)
    assert result["melody_distance"] == 0
    assert result["query_start"] == 0 and result["query_end"] == 4
    assert result["reference_start"] == 2 and result["reference_end"] == 6


def test_audio_counterexamples_are_discriminated(tmp_path):
    melody_a = [60, 62, 64, 67, 69, 67, 64, 62]
    melody_b = [76, 73, 70, 78, 75, 71, 79, 74]
    chords_a, chords_b = [48, 45, 53, 55], [49, 54, 51, 56]
    path = tmp_path / "reference.wav"
    samples, rate = render(melody_a, chords_a)
    sf.write(path, samples, rate)
    reference = extract_notes(path)
    results = {}
    for name, melody, chords in [("different_melody", melody_b, chords_a), ("different_accompaniment", melody_a, chords_b)]:
        samples, rate = render(melody, chords)
        path = tmp_path / f"{name}.wav"
        sf.write(path, samples[7 * rate:17 * rate], rate)
        results[name] = compare_notes(extract_notes(path), reference)
    assert results["different_melody"]["melody_match"] is False
    assert results["different_accompaniment"]["melody_match"] is True


def test_insufficient_pitch_information_is_not_a_negative_match(tmp_path):
    path = tmp_path / "silence.wav"
    sf.write(path, np.zeros(22050 * 8), 22050)
    with pytest.raises(MelodyUnavailable):
        extract_notes(path)
    with pytest.raises(MelodyUnavailable):
        compare_notes(notes([60] * 3), notes([60] * 8))
