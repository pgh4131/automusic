import io
import zipfile
from datetime import timedelta

import httpx
import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import func, select

from app.analysis_jobs import process_one as analyze, recover_expired
from app.fma import RangeFile, license_url
from app.models import AnalysisRun, CorpusTrack, Production, utcnow
from app.similarity import AnalysisError, FEATURE_VERSION, SearchIndex, extract, file_hash, manifest_for, save_features
from app.worker import process_one as generate
from test_productions import app, client, settings, submit


def song(seed, seconds=30, pitch=0):
    rng = np.random.default_rng(seed)
    rate = 22050
    notes = rng.choice(np.arange(48, 73), seconds)
    chunks = []
    t = np.arange(rate) / rate
    for note in notes:
        frequency = 440 * 2 ** ((int(note) + pitch - 69) / 12)
        envelope = np.minimum(t * 25, 1) * np.minimum((1 - t) * 25, 1)
        chunks.append(envelope * sum(np.sin(2 * np.pi * frequency * harmonic * t) / harmonic for harmonic in (1, 2, 3)) * .15)
    return np.concatenate(chunks).astype(np.float32), rate


@pytest.fixture
def corpus(app, settings):
    tracks = []
    root = settings.data_dir / "corpus"
    (root / "audio").mkdir(parents=True)
    (root / "features").mkdir()
    for seed in (123, 456, 789):
        audio = root / "audio" / f"{seed}.mp3"
        samples, rate = song(seed)
        sf.write(audio, samples, rate, format="WAV")
        features, seconds = extract(audio)
        feature = root / "features" / f"{seed}.npz"
        save_features(feature, features, seconds)
        track = CorpusTrack(id=f"test-{seed}", source="test", source_id=str(seed), title=f"Original {seed}",
            artist="Test author", genre="Test", source_url="https://example.com/original",
            license_url="https://creativecommons.org/licenses/by/4.0/", audio_filename=audio.name,
            feature_filename=feature.name, audio_hash=file_hash(audio), feature_hash=file_hash(feature),
            feature_version=FEATURE_VERSION, duration=seconds)
        tracks.append(track)
    with app.state.sessions.begin() as session:
        session.add_all(tracks)
    return tracks


def ready_query(client, app, settings):
    record = submit(client).json()
    generate(settings, app.state.sessions)
    with app.state.sessions.begin() as session:
        production = session.get(Production, record["id"])
        samples, rate = song(123)
        path = settings.data_dir / "audio" / production.audio_filename
        sf.write(path, samples[7 * rate:17 * rate], rate)
        production.content_hash = file_hash(path)
        production.audio_duration = 10
    return record


def test_excerpt_and_transposition_locate_original_with_times(corpus, settings):
    manifest = manifest_for(corpus)
    index = SearchIndex(settings, corpus, manifest)
    for pitch in (0, 2):
        samples, rate = song(123, pitch=pitch)
        path = settings.data_dir / f"query-{pitch}.wav"
        sf.write(path, samples[7 * rate:17 * rate], rate)
        features, seconds = extract(path)
        result = index.search(features, seconds, manifest)
        match = result["matches"][0]
        assert match["track_id"] == "test-123"
        assert match["query_start"] == 0 and match["query_end"] == 10
        assert match["reference_start"] == pytest.approx(7, abs=.5)
        assert match["reference_end"] == pytest.approx(17, abs=.5)
        assert match["pitch_shift"] == -pitch
        assert "probability" not in result and "confidence" not in match


def test_analysis_is_manual_cached_and_persists_per_version(client, app, settings, corpus):
    record = ready_query(client, app, settings)
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AnalysisRun)) == 0
    endpoint = f"/api/v1/productions/{record['id']}/analyses"
    first = client.post(endpoint).json()
    assert first["status"] == "queued"
    assert client.post(endpoint).json()["id"] == first["id"]
    assert analyze(settings, app.state.sessions)
    result = client.get(f"/api/v1/analyses/{first['id']}").json()
    assert result["status"] == "ready" and result["result"]["matches"][0]["track_id"] == "test-123"
    assert client.post(endpoint).json()["id"] == first["id"]
    assert not analyze(settings, app.state.sessions)
    # A new immutable input version starts without inherited analyses.
    second = submit(client, key="revision-query-key", song_id=record["song_id"], parent_production_id=record["id"]).json()
    assert client.get(f"/api/v1/productions/{second['id']}/analyses").json()["items"] == []
    assert client.get(endpoint).json()["items"][0]["id"] == first["id"]
    audio = client.get(result["result"]["matches"][0]["audio_url"], headers={"Range": "bytes=0-43"})
    assert audio.status_code == 206


def test_empty_corpus_and_unready_input_are_rejected(client):
    record = submit(client).json()
    assert client.post(f"/api/v1/productions/{record['id']}/analyses").status_code == 409
    assert client.get("/api/v1/corpus").json()["available"] is False


def test_changed_input_and_corrupt_corpus_fail_instead_of_returning_no_candidates(client, app, settings, corpus):
    record = ready_query(client, app, settings)
    row = client.post(f"/api/v1/productions/{record['id']}/analyses").json()
    with app.state.sessions() as session:
        path = settings.data_dir / "audio" / session.get(Production, record["id"]).audio_filename
    original = path.read_bytes()
    path.write_bytes(b"changed")
    analyze(settings, app.state.sessions)
    result = client.get(f"/api/v1/analyses/{row['id']}").json()
    assert result["status"] == "failed" and result["result"] is None
    path.write_bytes(original)
    client.post(f"/api/v1/productions/{record['id']}/analyses")
    (settings.data_dir / "corpus" / "features" / corpus[0].feature_filename).write_bytes(b"corrupt")
    analyze(settings, app.state.sessions)
    assert client.get(f"/api/v1/analyses/{row['id']}").json()["status"] == "failed"


def test_expired_analysis_requires_explicit_retry(client, app, settings, corpus):
    record = ready_query(client, app, settings)
    row = client.post(f"/api/v1/productions/{record['id']}/analyses").json()
    with app.state.sessions.begin() as session:
        run = session.get(AnalysisRun, row["id"])
        run.status, run.lease_until = "running", utcnow() - timedelta(seconds=1)
    recover_expired(app.state.sessions)
    assert not analyze(settings, app.state.sessions)
    assert client.post(f"/api/v1/productions/{record['id']}/analyses").json()["status"] == "queued"


def test_silence_is_not_a_successful_search(settings):
    path = settings.data_dir / "silence.wav"
    sf.write(path, np.zeros(22050 * 10), 22050)
    with pytest.raises(AnalysisError, match="무음"):
        extract(path)


def test_broadband_noise_is_reported_as_unsupported_instead_of_a_match(settings):
    path = settings.data_dir / "noise.wav"
    sf.write(path, np.random.default_rng(42).normal(0, .05, 22050 * 10), 22050)
    with pytest.raises(AnalysisError, match="잡음"):
        extract(path)


def test_simultaneous_analysis_clicks_share_one_job(client, app, settings, corpus):
    from concurrent.futures import ThreadPoolExecutor
    record = ready_query(client, app, settings)
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: client.post(f"/api/v1/productions/{record['id']}/analyses"), range(3)))
    assert all(response.status_code == 202 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AnalysisRun)) == 1


def test_accepted_catalog_is_fixed_and_completed_results_survive_restart(client, app, settings, corpus):
    from fastapi.testclient import TestClient
    from app.main import create_app
    record = ready_query(client, app, settings)
    first = client.post(f"/api/v1/productions/{record['id']}/analyses").json()
    original = corpus[0]
    with app.state.sessions.begin() as session:
        session.add(CorpusTrack(id="test-added", **{column.name: getattr(original, column.name)
            for column in CorpusTrack.__table__.columns if column.name != "id"}))
    assert analyze(settings, app.state.sessions)
    with TestClient(create_app(settings)) as restarted:
        result = restarted.get(f"/api/v1/analyses/{first['id']}").json()
        assert result["status"] == "ready" and result["result"]["corpus_tracks"] == 3
        second = restarted.post(f"/api/v1/productions/{record['id']}/analyses").json()
        assert second["id"] != first["id"] and second["corpus_tracks"] == 4


def test_licenses_are_allowlisted_exactly():
    assert license_url("Attribution 4.0 International").endswith("/by/4.0/")
    assert license_url("CC0 1.0 Universal").endswith("/zero/1.0/")
    for value in ("", "Public Domain", "Attribution-NonCommercial 4.0 International", "Attribution-NoDerivatives 4.0 International"):
        assert license_url(value) is None


def test_remote_zip_download_validates_ranges_and_crc():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sample.mp3", b"test-audio" * 100)
    content = data.getvalue()
    def respond(request):
        start, end = map(int, request.headers["range"][6:].split("-"))
        return httpx.Response(206, headers={"Content-Range": f"bytes {start}-{end}/{len(content)}"}, content=content[start:end + 1])
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with zipfile.ZipFile(RangeFile(client, "https://example.com/data.zip")) as archive:
            assert archive.read("sample.mp3") == b"test-audio" * 100
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"not-ranged"))) as client:
        with pytest.raises(ValueError, match="전체 ZIP"):
            RangeFile(client, "https://example.com/data.zip")
