"""Controlled counterexamples for the chroma baseline, not legal plagiarism labels."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from .similarity import FEATURE_VERSION, SearchIndex, extract, file_hash, manifest_for, save_features


def render(melody, roots, seconds=30):
    rate = 22050
    t = np.arange(seconds * rate) / rate
    signal = np.zeros(len(t), dtype=np.float32)
    for index in range(seconds // 2):
        start, end = index * 2 * rate, (index + 1) * 2 * rate
        local = np.arange(end - start) / rate
        envelope = np.minimum(local * 30, 1) * np.minimum((2 - local) * 30, 1)
        for interval in (0, 4, 7):
            frequency = 440 * 2 ** ((roots[index % len(roots)] + interval - 69) / 12)
            signal[start:end] += .12 * np.sin(2 * np.pi * frequency * local) * envelope
    for index in range(seconds * 2):
        start, end = index * rate // 2, (index + 1) * rate // 2
        local = np.arange(end - start) / rate
        envelope = np.minimum(local * 50, 1) * np.minimum((.5 - local) * 50, 1)
        frequency = 440 * 2 ** ((melody[index % len(melody)] - 69) / 12)
        signal[start:end] += .04 * np.sin(2 * np.pi * frequency * local) * envelope
    return signal, rate


def main():
    melody_a = [60, 62, 64, 67, 69, 67, 64, 62]
    melody_b = [76, 73, 70, 78, 75, 71, 79, 74]
    chords_a = [48, 45, 53, 55]
    chords_b = [49, 54, 51, 56]
    with tempfile.TemporaryDirectory(prefix="automusic-melody-probe-") as directory:
        root = Path(directory)
        audio = root / "corpus" / "audio" / "reference.wav"
        feature = root / "corpus" / "features" / "reference.npz"
        audio.parent.mkdir(parents=True)
        samples, rate = render(melody_a, chords_a)
        sf.write(audio, samples, rate)
        chroma, seconds = extract(audio)
        save_features(feature, chroma, seconds)
        track = SimpleNamespace(id="probe-reference", title="Controlled reference", artist="AutoMusic diagnostic",
            genre="Synthetic diagnostic", source_url="", license_url="", audio_filename=audio.name,
            feature_filename=feature.name, audio_hash=file_hash(audio), feature_hash=file_hash(feature),
            feature_version=FEATURE_VERSION, duration=seconds)
        manifest = manifest_for([track])
        index = SearchIndex(SimpleNamespace(data_dir=root), [track], manifest)
        cases = []
        for name, melody, chords in [
            ("same_melody_same_accompaniment", melody_a, chords_a),
            ("different_melody_same_accompaniment", melody_b, chords_a),
            ("same_melody_different_accompaniment", melody_a, chords_b),
        ]:
            samples, rate = render(melody, chords)
            query = root / "query.wav"
            sf.write(query, samples[7 * rate:17 * rate], rate)
            features, seconds = extract(query)
            result = index.search(features, seconds, manifest)
            cases.append({"case": name, "candidate_found": bool(result["matches"]),
                "distance": result["matches"][0]["distance"] if result["matches"] else None})
        print(json.dumps({"purpose": "Melody sensitivity diagnostic; no legal labels or population accuracy",
            "lead_amplitude": .04, "accompaniment_amplitude_per_note": .12,
            "cases": cases}, indent=2))


if __name__ == "__main__":
    main()
