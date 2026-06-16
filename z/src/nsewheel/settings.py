"""Secrets / connection settings via pydantic-settings.

Credentials are read from the environment / ``.env`` and kept as ``SecretStr`` so they are
never accidentally logged. Strategy parameters live in ``config.py`` (YAML), not here.
"""

from __future__ import annotations

from functools import lru_cache

from dotenv import find_dotenv, load_dotenv
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .config import load_config

load_dotenv(find_dotenv(usecwd=True))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KITE_", extra="ignore")

    api_key: SecretStr = SecretStr("")
    api_secret: SecretStr = SecretStr("")
    access_token: SecretStr = SecretStr("")
    user_id: SecretStr = SecretStr("")

    @property
    def offline(self) -> bool:
        """OFFLINE comes from YAML config (env-overridable); credentials are optional then."""
        return bool(load_config().get("OFFLINE", True))

    def kite_api_key(self) -> str:
        return self.api_key.get_secret_value()

    def kite_access_token(self) -> str:
        return self.access_token.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
