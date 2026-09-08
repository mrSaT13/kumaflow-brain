from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


MediaServerType = Literal["navidrome", "jellyfin", "emby", "lyrion", "none"]
AiProvider = Literal["NONE", "OLLAMA", "OPENAI", "GEMINI", "MISTRAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "KumaFlow Brain"
    app_version: str = "0.1.0"
    env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "kumaflow"
    postgres_password: str = "kumaflow"
    postgres_db: str = "kumaflow"
    db_url_override: str = ""  # e.g. sqlite:///./kumaflow.db for local dev without docker

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    media_server_type: MediaServerType = "navidrome"
    navidrome_url: str = ""
    navidrome_user: str = ""
    navidrome_password: str = ""

    jellyfin_url: str = ""
    jellyfin_user_id: str = ""
    jellyfin_token: str = ""

    emby_url: str = ""
    emby_user_id: str = ""
    emby_token: str = ""

    lyrion_url: str = ""

    ai_provider: AiProvider = "NONE"
    openai_api_key: str = ""
    openai_server_url: str = "https://api.openai.com/v1/chat/completions"
    openai_model_name: str = "gpt-4o-mini"
    ollama_server_url: str = "http://localhost:11434"
    ollama_model_name: str = "llama3.1"
    # Ollama Cloud (https://ollama.com) — ключ из https://ollama.com/settings
    ollama_cloud_api_key: str = ""
    ollama_cloud_model: str = "llama3.1"
    gemini_api_key: str = ""
    mistral_api_key: str = ""

    clap_enabled: bool = True
    use_gpu_clustering: bool = False

    yandex_music_token: str = ""
    yandex_music_enabled: bool = False

    # Мост метаданных (MusicBrainz / Last.fm). Адрес задаётся из веб-UI
    # (Настройки → Мост) и хранится в БД; env — только дефолт.
    bridge_url: str = ""
    bridge_enabled: bool = False

    # Настоящий аудио-анализ (worker): где лежат файлы и сколько брать за прогон.
    # MUSIC_DIR — та же папка музыки, что у Navidrome (только чтение).
    # Если не задан — worker тянет аудио стримом из Navidrome через Subsonic API.
    music_dir: str = ""
    analysis_sample_seconds: int = 90
    analysis_max_tracks_per_run: int = 200

    lyrics_providers: list[str] = Field(default_factory=lambda: ["lyrics.ovh", "musixmatch"])
    lyrics_user_agent: str = "Mozilla/5.0 (compatible; KumaFlowBrain/0.1)"

    @property
    def database_url(self) -> str:
        if self.db_url_override:
            return self.db_url_override
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
