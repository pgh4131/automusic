"""Generate original controlled music pairs and calibrate only on composition-disjoint development data."""
import argparse
import hashlib
import json
import platform
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from .config import Settings
from . import melody as melody_module
from .melody import METHOD_VERSION, MelodyUnavailable, compare_notes, extract_notes
from .similarity import FEATURE_VERSION, SearchIndex, extract, file_hash, manifest_for, save_features

GENERATOR_VERSION = "original-multitrack-pairs-v2"
SPLITS = {"development": list(range(10, 18)), "validation": list(range(100, 104)),
          "prior_test_regression": list(range(1000, 1008)), "test": list(range(2000, 2008)),
          "low_register_stress": list(range(3000, 3004))}


def render(pitches, durations, roots, lead=.04, accompaniment=.12, timbre=0):
    rate = 22050
    seconds = float(sum(durations))
    samples = np.zeros(int(np.ceil(seconds * rate)), dtype=np.float32)
    for chord_index, start in enumerate(np.arange(0, seconds, 2)):
        first, last = int(start * rate), min(len(samples), int((start + 2) * rate))
        t = np.arange(last - first) / rate
        envelope = np.minimum(t * 30, 1) * np.minimum((2 - t) * 30, 1)
        for interval in (0, 4, 7):
            frequency = 440 * 2 ** ((roots[chord_index % len(roots)] + interval - 69) / 12)
            samples[first:last] += accompaniment * np.sin(2 * np.pi * frequency * t) * envelope
    start = 0.
    for pitch, duration in zip(pitches, durations):
        first, last = int(start * rate), min(len(samples), int((start + duration) * rate))
        t = np.arange(last - first) / rate
        envelope = np.minimum(t * 45, 1) * np.minimum((duration - t) * 45, 1)
        frequency = 440 * 2 ** ((pitch - 69) / 12)
        waveform = np.sin(2 * np.pi * frequency * t)
        if timbre:
            waveform += .25 * np.sin(4 * np.pi * frequency * t) + .12 * np.sin(6 * np.pi * frequency * t)
        samples[first:last] += lead * waveform * envelope
        start += duration
    return np.clip(samples, -.95, .95), rate


def compositions():
    for split, seeds in SPLITS.items():
        for seed in seeds:
            rng = np.random.default_rng(seed)
            pitches = rng.integers(60, 80, 24)
            if split == "low_register_stress":
                pitches = pitches - 12
            # Avoid adjacent repeated pitches: this baseline does not yet resolve same-pitch rearticulation.
            for index in range(1, len(pitches)):
                if pitches[index] == pitches[index - 1]:
                    pitches[index] += 1
            durations = rng.choice([.3, .45, .6, .75], len(pitches))
            roots = rng.integers(43, 53, 6)
            alternate_roots = rng.integers(44, 55, 6)
            alternate_melody = rng.integers(60, 80, len(pitches))
            if split == "low_register_stress":
                alternate_melody = alternate_melody - 12
            reordered_melody = pitches[rng.permutation(len(pitches))]
            alternate_rhythm = rng.choice([.3, .45, .6, .75], len(pitches))
            rhythm_change = np.where(np.arange(len(pitches)) % 2 == 0, .2, .85)
            lead, accompaniment = [(.04, .12), (.08, .08), (.14, .04)][seed % 3]
            variants = [
                ("same_notes_new_timbre", pitches, durations, roots, 1, True, True),
                ("same_melody_new_accompaniment", pitches, durations, alternate_roots, 0, True, True),
                ("transposed", pitches + 3, durations, roots + 3, 1, True, True),
                ("uniform_tempo_change", pitches, durations / 1.2, roots, 0, True, True),
                ("same_accompaniment_different_melody", alternate_melody, durations, roots, 0, False, None),
                ("same_notes_reordered", reordered_melody, durations, roots, 1, False, None),
                ("unrelated_same_instruments", alternate_melody, alternate_rhythm, alternate_roots, 0, False, None),
                ("same_melody_changed_rhythm", pitches, rhythm_change, roots, 0, True, False),
            ]
            yield split, seed, pitches, durations, roots, lead, accompaniment, variants


def threshold_on_development(rows, field, label):
    accepted = [row for row in rows if row["split"] == "development" and row.get(field) is not None and row[label] is not None]
    values = sorted({row[field] for row in accepted})
    thresholds = [0.] + [(a + b) / 2 for a, b in zip(values, values[1:])] + [1.]
    best = None
    for threshold in thresholds:
        tp = sum(row[label] and row[field] <= threshold for row in accepted)
        fp = sum(not row[label] and row[field] <= threshold for row in accepted)
        fn = sum(row[label] and row[field] > threshold for row in accepted)
        f1 = 2 * tp / max(1, 2 * tp + fp + fn)
        precision = tp / max(1, tp + fp)
        candidate = (f1, precision, -threshold)
        if best is None or candidate > best[0]:
            best = (candidate, threshold)
    return float(best[1]) if best else 0.


def metrics(rows, prediction, label):
    rows = [row for row in rows if row[label] is not None]
    tp = sum(row[label] and row.get(prediction) is True for row in rows)
    fp = sum(not row[label] and row.get(prediction) is True for row in rows)
    fn = sum(row[label] and row.get(prediction) is not True for row in rows)
    tn = sum(not row[label] and row.get(prediction) is False for row in rows)
    unsupported = sum(row.get(prediction) is None for row in rows)
    return {"pairs": len(rows), "tp": tp, "fp": fp, "fn_including_unsupported": fn,
        "tn": tn, "unsupported": unsupported, "precision": round(tp / max(1, tp + fp), 4),
        "recall": round(tp / max(1, tp + fn), 4), "f1": round(2 * tp / max(1, 2 * tp + fp + fn), 4)}


def run(root):
    root.mkdir(parents=True, exist_ok=True)
    rows, manifests = [], []
    for split, seed, pitches, durations, roots, lead, accompaniment, variants in compositions():
        folder = root / split / str(seed)
        audio_dir, feature_dir = folder / "corpus" / "audio", folder / "corpus" / "features"
        audio_dir.mkdir(parents=True, exist_ok=True)
        feature_dir.mkdir()
        reference_path = audio_dir / "reference.wav"
        samples, rate = render(pitches, durations, roots, lead, accompaniment)
        sf.write(reference_path, samples, rate)
        chroma, seconds = extract(reference_path)
        feature = feature_dir / "reference.npz"
        save_features(feature, chroma, seconds)
        track = SimpleNamespace(id=f"original-{seed}", title="Original controlled composition", artist="AutoMusic evaluation generator",
            genre="Synthetic evaluation", source_url="", license_url="", audio_filename=reference_path.name,
            feature_filename=feature.name, audio_hash=file_hash(reference_path), feature_hash=file_hash(feature),
            feature_version=FEATURE_VERSION, duration=seconds)
        manifest = manifest_for([track])
        baseline = SearchIndex(SimpleNamespace(data_dir=folder), [track], manifest)
        try:
            reference_notes, reference_error = extract_notes(reference_path), None
        except MelodyUnavailable as exc:
            reference_notes, reference_error = None, str(exc)
        manifests.append({"split": split, "composition_id": seed, "pitches": pitches.tolist(),
            "durations": durations.tolist(), "roots": roots.tolist(), "lead": lead, "accompaniment": accompaniment,
            "reference_sha256": file_hash(reference_path)})
        for kind, melody, rhythm, chords, timbre, melodic_label, rhythmic_label in variants:
            path = folder / f"{kind}.wav"
            samples, rate = render(melody, rhythm, chords, lead, accompaniment, timbre)
            sf.write(path, samples, rate)
            started = time.perf_counter()
            query_features, query_seconds = extract(path)
            old_result = baseline.search(query_features, query_seconds, manifest)
            row = {"split": split, "composition_id": seed, "kind": kind,
                "melody_label": melodic_label, "rhythm_label": rhythmic_label,
                "baseline_candidate": bool(old_result["matches"]), "query_sha256": file_hash(path)}
            try:
                if reference_notes is None:
                    raise MelodyUnavailable(reference_error)
                query_notes = extract_notes(path)
                result = compare_notes(query_notes, reference_notes)
                row.update({key: result[key] for key in ("melody_distance", "rhythm_distance", "query_notes", "reference_notes")})
            except MelodyUnavailable as exc:
                row.update(melody_distance=None, rhythm_distance=None, error=str(exc))
            row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            rows.append(row)
        print(f"Evaluated {split} composition {seed}", flush=True)
    melody_threshold = threshold_on_development(rows, "melody_distance", "melody_label")
    rhythm_threshold = threshold_on_development(rows, "rhythm_distance", "rhythm_label")
    for row in rows:
        row["melody_prediction"] = row["melody_distance"] <= melody_threshold if row["melody_distance"] is not None else None
        row["rhythm_prediction"] = row["rhythm_distance"] <= rhythm_threshold if row["rhythm_distance"] is not None else None
    report = {"generator_version": GENERATOR_VERSION, "method_version": METHOD_VERSION,
        "split_policy": SPLITS, "threshold_selection": "Maximum development F1, then precision, then smaller threshold; validation/test never used",
        "melody_threshold": melody_threshold, "rhythm_threshold": rhythm_threshold,
        "platform": platform.platform(), "numpy": np.__version__,
        "method_sha256": hashlib.sha256(Path(melody_module.__file__).read_bytes()).hexdigest(),
        "holdout_note": "v1 test seeds 1000–1007 were inspected before the v2 change; v2 reports them only as regression. Seeds 2000–2007 are new held-out compositions.",
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "metrics": {split: {"baseline_as_melody_detector": metrics([row for row in rows if row["split"] == split], "baseline_candidate", "melody_label"),
            "experimental_melody": metrics([row for row in rows if row["split"] == split], "melody_prediction", "melody_label"),
            "experimental_rhythm": metrics([row for row in rows if row["split"] == split], "rhythm_prediction", "rhythm_label")} for split in SPLITS},
        "cases": rows, "compositions": manifests,
        "limitations": ["Synthetic music with known note changes, not legal plagiarism labels or real-vocal accuracy.",
            "All positive variants of a composition stay in one split. Only development data calibrates thresholds.",
            "Foreground pitch estimation may choose bass/accompaniment; low-pitch and same-pitch rearticulation coverage is limited."]}
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = args.output_dir or Settings.load().data_dir / "melody-evaluation-v2"
    # Keep complete previous experiment folders as reproducible evidence.
    if root.exists():
        raise SystemExit("출력 폴더가 이미 있습니다. 새 --output-dir을 지정해 주세요.")
    report = run(root)
    print(json.dumps({key: value for key, value in report.items() if key not in {"cases", "compositions"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
