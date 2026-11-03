import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models import Production, utcnow
from app.providers import ElevenLabsProvider, ProviderError
from app.worker import process_one, recover_expired


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def payload(**overrides):
    return {"prompt": "An original dreamy vocal song about a rainy evening", "provider": "demo", **overrides}


def submit(client, key="request-12345", **overrides):
    return client.post("/api/v1/productions", json=payload(**overrides), headers={"Idempotency-Key": key})


def test_real_provider_requires_key_and_exposes_no_secret(client):
    response = submit(client, provider="elevenlabs")
    assert response.status_code == 503
    assert client.get("/api/v1/productions").json()["items"] == []
    config = client.get("/api/v1/config").json()
    assert config["providers"][0]["available"] is False
    assert "xi-api-key" not in str(config)


def test_validation_rejects_short_prompt_custom_lyrics_and_unknown_fields(client):
    assert submit(client, prompt="short").status_code == 422
    assert submit(client, lyrics_mode="custom", lyrics="hi").status_code == 422
    assert submit(client, unexpected="ignored?").status_code == 422


def test_repeated_request_is_idempotent_and_conflicts_are_rejected(client):
    first = submit(client)
    second = submit(client)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/api/v1/productions").json()["items"]) == 1
    assert submit(client, title="Changed payload").status_code == 409


def test_demo_entire_flow_persists_audio_download_and_range(client, app, settings):
    record = submit(client).json()
    assert record["status"] == "queued" and record["audio_url"] is None
    assert process_one(settings, app.state.sessions)
    record = client.get(f"/api/v1/productions/{record['id']}").json()
    assert record["status"] == "ready"
    assert record["provider"] == "demo" and record["model_id"] == "local-synth"
    assert record["audio_duration"] == pytest.approx(30, abs=0.1)
    assert record["analysis_status"] == "on_demand"
    audio = client.get(record["audio_url"])
    assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
    assert audio.headers["content-type"] == "audio/wav"
    partial = client.get(record["audio_url"], headers={"Range": "bytes=0-43"})
    assert partial.status_code == 206 and len(partial.content) == 44
    download = client.get(record["download_url"])
    assert "attachment" in download.headers["content-disposition"]
    assert not process_one(settings, app.state.sessions)
    with TestClient(create_app(settings)) as second_client:
        assert second_client.get(f"/api/v1/productions/{record['id']}").json()["status"] == "ready"


def test_api_does_not_execute_generation_in_request(client, settings):
    record = submit(client).json()
    assert client.get(f"/api/v1/productions/{record['id']}").json()["status"] == "queued"
    assert not (settings.data_dir / "audio").exists()


def test_missing_and_unready_audio(client):
    record = submit(client).json()
    assert client.get(f"/api/v1/productions/{record['id']}/audio").status_code == 409
    assert client.get("/api/v1/productions/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/api/v1/productions/not-a-uuid").status_code == 422


def test_uncertain_paid_request_is_not_retried(client, app, settings):
    class UncertainProvider:
        calls = 0
        def build_plan(self, production):
            return {"chunks": [{"text": "Original lyrics", "duration_ms": 30000}]}
        def generate(self, *args):
            self.calls += 1
            raise ProviderError("generation_uncertain", "Check provider before retry.", uncertain=True)
    provider = UncertainProvider()
    record = submit(client).json()
    process_one(settings, app.state.sessions, lambda _: provider)
    assert client.get(f"/api/v1/productions/{record['id']}").json()["status"] == "needs_review"
    assert not process_one(settings, app.state.sessions, lambda _: provider)
    assert provider.calls == 1


def test_expired_worker_marks_review_instead_of_resubmitting(client, app):
    record = submit(client).json()
    with app.state.sessions.begin() as session:
        row = session.get(Production, record["id"])
        row.status = "generating"
        row.lease_until = utcnow() - timedelta(seconds=1)
    recover_expired(app.state.sessions)
    assert client.get(f"/api/v1/productions/{record['id']}").json()["status"] == "needs_review"


def test_stale_worker_cannot_overwrite_review_status(client, app, settings):
    class InterruptedProvider:
        def build_plan(self, production):
            with app.state.sessions.begin() as session:
                session.get(Production, production.id).status = "needs_review"
            return {"chunks": [{"text": "Original lyrics", "duration_ms": 30000}]}
        def generate(self, *args):
            pytest.fail("An interrupted worker must not start a paid generation")
    record = submit(client).json()
    process_one(settings, app.state.sessions, lambda _: InterruptedProvider())
    assert client.get(f"/api/v1/productions/{record['id']}").json()["status"] == "needs_review"


def test_elevenlabs_contract_plans_then_streams_audio(settings, tmp_path):
    calls = []
    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        assert request.headers["xi-api-key"] == "test-only-key"
        if request.url.path == "/v1/music/plan":
            return httpx.Response(200, json={"chunks": [{"text": "[Verse]\nOriginal words", "duration_ms": 30000, "positive_styles": ["indie pop"]}]})
        assert request.url.path == "/v1/music"
        assert "prompt" not in json.loads(request.content)
        assert request.url.params["output_format"] == "mp3_48000_192"
        return httpx.Response(200, content=b"mock-audio-bytes", headers={"song-id": "external-song-123"})
    provider = ElevenLabsProvider(replace(settings, elevenlabs_api_key="test-only-key"), transport=httpx.MockTransport(handler))
    job = SimpleNamespace(**payload(lyrics_mode="auto", duration_seconds=30, genre="Indie pop", mood="Dreamy", vocal="Female vocals", language="Korean"))
    plan = provider.build_plan(job)
    output = tmp_path / "contract.part"
    assert provider.generate(job, plan, output) == "external-song-123"
    assert output.read_bytes() == b"mock-audio-bytes"
    assert len(calls) == 2 and calls[1][1]["composition_plan"] == plan


def test_custom_lyrics_preserved_and_prompt_influences_plan(settings):
    def handler(request):
        assert "rainy evening" in json.loads(request.content)["prompt"]
        return httpx.Response(200, json={"chunks": [{"text": "provider changed these words", "duration_ms": 30000, "positive_styles": ["dreamy acoustic guitar"]}]})
    provider = ElevenLabsProvider(settings, transport=httpx.MockTransport(handler))
    job = SimpleNamespace(**payload(lyrics_mode="custom", lyrics="[Verse]\nMy exact original lyrics", duration_seconds=30, genre="Indie pop", mood="Dreamy", vocal="Female vocals", language="English"))
    plan = provider.build_plan(job)
    assert plan["chunks"][0]["text"] == job.lyrics
    assert "dreamy acoustic guitar" in plan["chunks"][0]["positive_styles"]


def test_invalid_plan_and_provider_rejection(settings):
    with pytest.raises(ProviderError, match="곡 구성"):
        ElevenLabsProvider.validate_plan({"chunks": []}, 30)
    with pytest.raises(ProviderError) as error:
        ElevenLabsProvider.check_response(httpx.Response(503), generation=True)
    assert error.value.uncertain


def ready_first_version(client, app, settings):
    record = submit(client, title="Rainy evening").json()
    process_one(settings, app.state.sessions)
    return client.get(f"/api/v1/productions/{record['id']}").json()


def revision(client, source, key="revision-12345", **overrides):
    return submit(client, key=key, **{"song_id": source["song_id"], "parent_production_id": source["id"], **overrides})


def test_root_song_and_version_metadata(client):
    first = submit(client, title="My first song").json()
    assert first["version_no"] == 1 and first["parent_production_id"] is None
    library = client.get("/api/v1/songs").json()["items"]
    assert len(library) == 1 and library[0]["id"] == first["song_id"]
    assert library[0]["title"] == "My first song" and library[0]["version_count"] == 1
    assert client.get(f"/api/v1/songs/{first['song_id']}/versions").json()["items"] == [first]


def test_revision_preserves_original_audio_lyrics_and_settings(client, app, settings):
    first = ready_first_version(client, app, settings)
    original_audio = client.get(first["audio_url"]).content
    second = revision(client, first, title="More hopeful", duration_seconds=60,
        genre="Synth pop", lyrics_mode="custom", lyrics="[Verse]\nKeep these original words").json()
    assert second["song_id"] == first["song_id"] and second["song_title"] == first["title"]
    assert second["version_no"] == 2 and second["parent_production_id"] == first["id"]
    process_one(settings, app.state.sessions)
    second = client.get(f"/api/v1/productions/{second['id']}").json()
    assert second["status"] == "ready" and second["audio_duration"] == pytest.approx(60)
    assert second["lyrics"] == "[Verse]\nKeep these original words"
    assert second["audio_url"] != first["audio_url"]
    assert client.get(f"/api/v1/productions/{first['id']}").json() == first
    assert client.get(first["audio_url"]).content == original_audio
    versions = client.get(f"/api/v1/songs/{first['song_id']}/versions").json()["items"]
    assert [item["version_no"] for item in versions] == [2, 1]


def test_branching_from_v1_gets_next_number_not_overwriting_v2(client, app, settings):
    first = ready_first_version(client, app, settings)
    second = revision(client, first, key="second-version").json()
    third = revision(client, first, key="third-version").json()
    assert [second["version_no"], third["version_no"]] == [2, 3]
    assert third["parent_production_id"] == first["id"]
    assert client.get("/api/v1/songs").json()["items"][0]["version_count"] == 3


def test_revision_idempotency_does_not_allocate_extra_versions(client, app, settings):
    first = ready_first_version(client, app, settings)
    second = revision(client, first).json()
    assert revision(client, first).json()["id"] == second["id"]
    assert revision(client, first, title="Conflicting retry").status_code == 409
    assert client.get("/api/v1/songs").json()["items"][0]["version_count"] == 2


def test_invalid_revision_sources_do_not_create_orphan_songs(client, app, settings):
    first = ready_first_version(client, app, settings)
    another = submit(client, key="another-song").json()
    assert revision(client, first, song_id=another["song_id"]).status_code == 404
    assert submit(client, key="partial-reference", song_id=first["song_id"]).status_code == 422
    assert revision(client, another, key="not-ready-source").status_code == 409
    assert client.get("/api/v1/songs/00000000-0000-0000-0000-000000000000/versions").status_code == 404
    assert len(client.get("/api/v1/songs").json()["items"]) == 2
    assert len(client.get(f"/api/v1/songs/{first['song_id']}/versions").json()["items"]) == 1


def test_failed_revision_remains_in_history(client, app, settings):
    first = ready_first_version(client, app, settings)
    second = revision(client, first).json()
    with app.state.sessions.begin() as session:
        row = session.get(Production, second["id"])
        row.status, row.error_code = "failed", "provider_rejected"
    third = revision(client, first, key="third-after-fail").json()
    versions = client.get(f"/api/v1/songs/{first['song_id']}/versions").json()["items"]
    assert [item["version_no"] for item in versions] == [3, 2, 1]
    assert versions[1]["status"] == "failed" and third["parent_production_id"] == first["id"]


def test_concurrent_revisions_have_unique_ordered_numbers(client, app, settings):
    from concurrent.futures import ThreadPoolExecutor
    first = ready_first_version(client, app, settings)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda i: revision(client, first, key=f"parallel-revision-{i}"), range(4)))
    assert all(item.status_code == 202 for item in responses)
    assert sorted(item.json()["version_no"] for item in responses) == [2, 3, 4, 5]
    assert client.get("/api/v1/songs").json()["items"][0]["version_count"] == 5


def test_concurrent_duplicate_revision_allocates_only_once(client, app, settings):
    from concurrent.futures import ThreadPoolExecutor
    first = ready_first_version(client, app, settings)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: revision(client, first, key="same-concurrent-key"), range(4)))
    assert all(item.status_code == 202 for item in responses)
    assert len({item.json()["id"] for item in responses}) == 1
    assert client.get("/api/v1/songs").json()["items"][0]["version_count"] == 2


def test_legacy_database_backfill_preserves_rows_audio_and_retry_hash(settings):
    import hashlib
    from uuid import uuid4
    from sqlalchemy import create_engine
    from app.schemas import ProductionInput
    from app.models import Song, SongVersion

    # Build exactly the old schema: no song tables, an existing ready production.
    engine = create_engine(settings.database_url)
    Production.__table__.create(engine)
    old_payload = ProductionInput(**payload()).model_dump(exclude={"song_id", "parent_production_id"})
    fingerprint = hashlib.sha256(json.dumps(old_payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    production_id = str(uuid4())
    audio_dir = settings.data_dir / "audio"
    audio_dir.mkdir()
    original_audio = b"preserved-existing-file"
    (audio_dir / "legacy.wav").write_bytes(original_audio)
    with engine.begin() as connection:
        connection.execute(Production.__table__.insert().values(id=production_id,
            idempotency_key="legacy-retry-key", payload_hash=fingerprint, model_id="local-synth",
            status="ready", audio_filename="legacy.wav", **old_payload))
        before = dict(connection.execute(select(Production.__table__)).mappings().one())
    engine.dispose()

    for _ in range(2):
        app = create_app(settings)
        with TestClient(app) as client:
            row = client.get(f"/api/v1/productions/{production_id}").json()
            assert row["version_no"] == 1 and row["song_id"] == production_id
            assert client.get(row["audio_url"]).content == original_audio
            assert submit(client, key="legacy-retry-key").json()["id"] == production_id
            with app.state.sessions() as session:
                assert len(session.scalars(select(Song)).all()) == 1
                assert len(session.scalars(select(SongVersion)).all()) == 1
                after = dict(session.execute(select(Production.__table__)).mappings().one())
                assert after == before
