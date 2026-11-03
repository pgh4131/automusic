import math
import re
import struct
import wave
from pathlib import Path

import httpx


class ProviderError(Exception):
    def __init__(self, code, message, uncertain=False):
        super().__init__(message)
        self.code, self.message, self.uncertain = code, message, uncertain


def plan_lyrics(plan):
    return "\n\n".join(chunk["text"] for chunk in plan["chunks"] if chunk.get("text"))


class ElevenLabsProvider:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    def client(self):
        return httpx.Client(
            base_url="https://api.elevenlabs.io",
            headers={"xi-api-key": self.settings.elevenlabs_api_key},
            timeout=httpx.Timeout(300, connect=15),
            transport=self.transport,
        )

    def build_plan(self, production):
        styles = [production.genre, production.mood, production.vocal,
                  "expressive sung vocals", "clear lead vocal melody", "full band arrangement"]
        description = (
            f"{production.prompt}\nGenre: {production.genre}. Mood: {production.mood}. "
            f"Vocal: {production.vocal}. Write original {production.language} lyrics. "
            "Include sung vocals, a verse and a memorable chorus. Do not make an instrumental."
        )
        if production.lyrics_mode == "custom":
            description += "\nChoose English style directions to match this original lyric excerpt:\n" + production.lyrics[:1000]
        try:
            with self.client() as client:
                response = client.post("/v1/music/plan", json={
                    "prompt": description, "music_length_ms": production.duration_seconds * 1000,
                    "model_id": self.settings.model_id,
                }, timeout=60)
                self.check_response(response)
                plan = response.json()
        except httpx.HTTPError as exc:
            raise ProviderError("plan_unavailable", "곡 구성 요청에 실패했습니다. API 연결을 확인해 주세요.") from exc
        except ValueError as exc:
            raise ProviderError("invalid_plan", "생성 제공자가 올바른 곡 구성을 반환하지 않았습니다.") from exc
        self.validate_plan(plan, production.duration_seconds)
        if production.lyrics_mode == "custom":
            # Keep supplied words verbatim; use the plan only for musical direction.
            collected_styles = list(dict.fromkeys(
                style for chunk in plan["chunks"] for style in chunk.get("positive_styles", [])
            ))
            plan = {"chunks": [{"text": production.lyrics,
                "duration_ms": production.duration_seconds * 1000,
                "positive_styles": (collected_styles or styles)[:50],
                "negative_styles": ["instrumental only", "spoken word"], "context_adherence": "high"}]}
        return plan

    @staticmethod
    def validate_plan(plan, requested_seconds):
        chunks = plan.get("chunks", []) if isinstance(plan, dict) else []
        valid = bool(chunks) and len(chunks) <= 30 and all(
            isinstance(c, dict) and isinstance(c.get("text"), str)
            and isinstance(c.get("duration_ms"), int)
            and 3000 <= c["duration_ms"] <= 120000 for c in chunks
        )
        if not valid or sum(c["duration_ms"] for c in chunks) != requested_seconds * 1000:
            raise ProviderError("invalid_plan", "요청 길이에 맞는 곡 구성을 받지 못했습니다. 생성은 시작하지 않았습니다.")
        if not re.sub(r"\[[^\]]*\]|\{[^}]*\}|\s", "", plan_lyrics(plan)):
            raise ProviderError("missing_lyrics", "가사가 포함된 곡 구성을 받지 못했습니다. 생성은 시작하지 않았습니다.")

    @staticmethod
    def check_response(response, generation=False):
        if response.is_success:
            return
        code = response.status_code
        if code in (401, 403):
            raise ProviderError("provider_access", "생성 API 키 또는 Music API 이용 권한을 확인해 주세요.")
        if code in (400, 422):
            raise ProviderError("provider_rejected", "생성 제공자가 입력을 거절했습니다. 프롬프트·가사·조건을 확인해 주세요.")
        if code == 429:
            raise ProviderError("provider_limit", "생성 제공자의 사용량 또는 요청 한도에 도달했습니다.")
        raise ProviderError("provider_error", "생성 제공자의 응답을 확인할 수 없습니다. 자동으로 다시 생성하지 않습니다.", uncertain=generation)

    def generate(self, production, plan, destination: Path):
        try:
            with self.client() as client:
                with client.stream("POST", "/v1/music", params={"output_format": "mp3_48000_192"}, json={
                    "composition_plan": plan, "model_id": self.settings.model_id,
                }) as response:
                    self.check_response(response, generation=True)
                    written = 0
                    with destination.open("wb") as file:
                        for chunk in response.iter_bytes():
                            written += len(chunk)
                            if written > 30 * 1024 * 1024:
                                raise ProviderError("output_too_large", "생성 결과가 저장 한도를 초과했습니다. 다시 생성하지 않았습니다.")
                            file.write(chunk)
                    return response.headers.get("song-id")
        except httpx.HTTPError as exc:
            raise ProviderError("generation_uncertain", "생성 응답 수신이 중단됐습니다. 과금 여부 확인 전 자동으로 다시 생성하지 않습니다.", uncertain=True) from exc


class DemoProvider:
    """Explicit local transport demo: synthesized instrumental audio, never AI vocals."""

    def build_plan(self, production):
        return {"chunks": [{"text": "[Local demo — no AI-generated vocals]",
            "duration_ms": production.duration_seconds * 1000}]}

    def generate(self, production, plan, destination: Path):
        sample_rate = 22050
        melody = [261.63, 329.63, 392.00, 440.00, 392.00, 329.63, 293.66, 329.63]
        with wave.open(str(destination), "wb") as audio:
            audio.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
            for second in range(production.duration_seconds):
                samples = bytearray()
                for index in range(sample_rate):
                    t = second + index / sample_rate
                    note_t = t % 0.5
                    envelope = min(1, note_t / 0.025) * math.exp(-note_t * 5)
                    fade = min(1, t / 0.2, max(0, (production.duration_seconds - t) / 0.6))
                    freq = melody[int(t * 2) % len(melody)]
                    value = 0.16 * envelope * math.sin(2 * math.pi * freq * t)
                    value += 0.035 * math.sin(2 * math.pi * 130.81 * t)
                    samples.extend(struct.pack("<h", int(value * fade * 32767)))
                audio.writeframes(samples)
        return None
