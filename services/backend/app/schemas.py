from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProductionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(default="Untitled session", min_length=1, max_length=100)
    prompt: str = Field(min_length=10, max_length=2000)
    provider: Literal["elevenlabs", "demo"] = "elevenlabs"
    genre: str = Field(default="Indie pop", min_length=1, max_length=80)
    mood: str = Field(default="Dreamy", min_length=1, max_length=80)
    vocal: str = Field(default="Soft female vocals", min_length=1, max_length=80)
    language: Literal["Korean", "English", "Japanese"] = "Korean"
    duration_seconds: Literal[30, 60] = 30
    lyrics_mode: Literal["auto", "custom"] = "auto"
    lyrics: str = Field(default="", max_length=3000)
    song_id: Optional[UUID] = None
    parent_production_id: Optional[UUID] = None

    @model_validator(mode="after")
    def validate_lyrics(self):
        if (self.song_id is None) != (self.parent_production_id is None):
            raise ValueError("수정본은 곡과 기준 버전을 함께 지정해야 합니다.")
        if self.lyrics_mode == "custom" and len(self.lyrics) < 10:
            raise ValueError("직접 입력한 가사는 10자 이상이어야 합니다.")
        if self.lyrics_mode == "auto":
            self.lyrics = ""
        return self
