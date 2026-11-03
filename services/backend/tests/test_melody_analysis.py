import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app import melody
from app.analysis_jobs import process_one
from app.compare_melody import calibration_from
from app.melody_search import CALIBRATION_PATH, cached_notes
from app.models import CorpusTrack
from app.similarity import FEATURE_VERSION, extract, file_hash, save_features
from test_productions import app, client, settings
from test_similarity import corpus, ready_query, song


def test_bundled_calibration_rejects_stale_method_and_invalid_threshold(tmp_path):
    value = json.loads(CALIBRATION_PATH.read_text())
    assert calibration_from(CALIBRATION_PATH)[0]["method_sha256"] == hashlib.sha256(Path(melody.__file__).read_bytes()).hexdigest()
    value["method_sha256"] = "old-method"
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="버전"):
        calibration_from(path)
    value["method_sha256"] = hashlib.sha256(Path(melody.__file__).read_bytes()).hexdigest()
    value["melody_threshold"] = float("nan")
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="임계값"):
        calibration_from(path)


def test_analysis_modes_keep_separate_cached_results(client, app, settings, corpus):
    record = ready_query(client, app, settings)
    endpoint = f"/api/v1/productions/{record['id']}/analyses"
    baseline = client.post(endpoint).json()
    experimental = client.post(endpoint, json={"mode": "melody_rhythm"}).json()
    assert baseline["id"] != experimental["id"]
    assert baseline["mode"] == "chroma" and experimental["mode"] == "melody_rhythm"
    assert client.post(endpoint, json={"mode": "melody_rhythm"}).json()["id"] == experimental["id"]
    assert client.post(endpoint, json={"mode": "legal_plagiarism"}).status_code == 422
    assert process_one(settings, app.state.sessions)
    assert process_one(settings, app.state.sessions)
    result = client.get(f"/api/v1/analyses/{experimental['id']}").json()
    assert result["status"] == "ready", result
    assert result["result"]["reviewed_tracks"] == 3
    assert result["result"]["matches"][0]["track_id"] == "test-123"
    match = result["result"]["matches"][0]
    assert "melody_distance" in match and "rhythm_distance" in match and match["legal_verdict"] is None
    assert client.post(endpoint).json()["id"] == baseline["id"]
    assert client.post(endpoint, json={"mode": "melody_rhythm"}).json()["status"] == "ready"


def test_unextractable_reference_is_partial_coverage_not_full_clearance(client, app, settings, corpus):
    root = settings.data_dir / "corpus"
    audio = root / "audio" / "constant.wav"
    sf.write(audio, .1 * np.sin(2 * np.pi * 330 * np.arange(22050 * 10) / 22050), 22050)
    features, seconds = extract(audio)
    feature = root / "features" / "constant.npz"
    save_features(feature, features, seconds)
    with app.state.sessions.begin() as session:
        session.add(CorpusTrack(id="constant-reference", source="test", source_id="constant", title="Constant test tone",
            artist="Test", genre="Test", source_url="https://example.com", license_url="https://example.com",
            audio_filename=audio.name, feature_filename=feature.name, audio_hash=file_hash(audio),
            feature_hash=file_hash(feature), feature_version=FEATURE_VERSION, duration=seconds))
    record = ready_query(client, app, settings)
    job = client.post(f"/api/v1/productions/{record['id']}/analyses", json={"mode": "melody_rhythm"}).json()
    process_one(settings, app.state.sessions)
    result = client.get(f"/api/v1/analyses/{job['id']}").json()
    assert result["status"] == "ready", result
    assert result["result"]["coverage_status"] == "partial"
    assert result["result"]["reviewed_tracks"] == 3 and result["result"]["corpus_tracks"] == 4
    assert result["result"]["unavailable_references"][0]["track_id"] == "constant-reference"


def test_unextractable_query_fails_instead_of_returning_no_match(client, app, settings, corpus, monkeypatch):
    from app.melody import MelodyUnavailable
    record = ready_query(client, app, settings)
    def unsupported(_):
        raise MelodyUnavailable("Test insufficient contour")
    monkeypatch.setattr("app.melody_search.extract_notes", unsupported)
    job = client.post(f"/api/v1/productions/{record['id']}/analyses", json={"mode": "melody_rhythm"}).json()
    process_one(settings, app.state.sessions)
    result = client.get(f"/api/v1/analyses/{job['id']}").json()
    assert result["status"] == "failed" and result["result"] is None
    assert "유사하지 않다는 결과가 아닙니다" in result["error_message"]


def test_derived_note_cache_can_rebuild_from_verified_source(tmp_path):
    path = tmp_path / "input.wav"
    samples, rate = song(123)
    sf.write(path, samples, rate)
    method_hash = calibration_from(CALIBRATION_PATH)[0]["method_sha256"]
    original = cached_notes(path, file_hash(path), tmp_path / "cache", method_hash)
    cache_path = next((tmp_path / "cache").glob("*.json"))
    cache_path.write_text("broken")
    rebuilt = cached_notes(path, file_hash(path), tmp_path / "cache", method_hash)
    assert np.array_equal(original.pitches, rebuilt.pitches)
