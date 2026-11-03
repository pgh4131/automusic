import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    elevenlabs_api_key: str = ""
    model_id: str = "music_v2_5"
    allow_demo: bool = True

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env")
        data_dir = Path(os.getenv("AUTOMUSIC_DATA_DIR", str(ROOT / "data")))
        if not data_dir.is_absolute():
            data_dir = ROOT / data_dir
        data_dir = data_dir.resolve()
        return cls(
            data_dir=data_dir,
            database_url=os.getenv("DATABASE_URL", f"sqlite:///{data_dir / 'automusic.db'}"),
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", ""),
            model_id=os.getenv("ELEVENLABS_MODEL_ID", "music_v2_5"),
            allow_demo=os.getenv("ALLOW_DEMO", "true").lower() == "true",
        )
