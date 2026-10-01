from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from amo_leads.tokens import StaticToken, TokenManager, TokenSource


class Settings(BaseSettings):
    """Настройки из окружения и .env с префиксом AMO_ (пример — .env.example)."""

    model_config = SettingsConfigDict(env_prefix="AMO_", env_file=".env", extra="ignore")

    base_url: str = "https://example.amocrm.ru"  # адрес аккаунта amoCRM
    long_lived_token: str = ""  # долгосрочный токен частной интеграции — проще всего
    client_id: str = ""  # для OAuth: из настроек интеграции
    client_secret: str = ""
    redirect_uri: str = ""
    tokens_file: Path = Path("data/amo-tokens.json")
    pipeline_id: int | None = None  # воронка для заявок; пусто — основная
    database: Path = Path("data/submissions.sqlite3")
    retry_every: float = 60  # секунд между попытками дослать заявки из очереди
    timezone: str = "Europe/Moscow"

    @field_validator("pipeline_id", mode="before")
    @classmethod
    def _empty_is_none(cls, value: Any) -> Any:
        return None if value == "" else value

    def token_source(self) -> TokenSource:
        if self.long_lived_token:
            return StaticToken(self.long_lived_token)
        return TokenManager(
            self.tokens_file,
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.redirect_uri,
        )
