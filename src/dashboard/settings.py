"""Typed settings loader. Reads .env once at boot; never logs secrets."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

# pyrefly: ignore [untyped-import]
import yaml
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from pyprojroot import here


class Settings(BaseSettings):
    """Process-wide settings. Construct via `get_settings()`."""

    # secrets — SecretStr prevents accidental repr/log leaks
    token: SecretStr = Field(default=SecretStr(""), alias="TOKEN")
    trades_flexid: SecretStr = Field(default=SecretStr(""), alias="TRADES_FLEXID")
    github_pat: SecretStr = Field(default=SecretStr(""), alias="GITHUB_PERSONAL_ACCESS_TOKEN")

    # accounts (treated as sensitive — masked when displayed)
    us_account: SecretStr = Field(default=SecretStr(""), alias="US_ACCOUNT")
    sg_account: SecretStr = Field(default=SecretStr(""), alias="SG_ACCOUNT")

    # runtime knobs
    log_level: str = Field(default="INFO", alias="LOGLEVEL")
    active_status: str = Field(default="", alias="ACTIVESTATUS")

    # IBKR connection — defaults match snp_config.yml live setup
    ib_host: str = Field(default="127.0.0.1")
    ib_port: int = Field(default=1300)  # 1300 live / 1301 paper
    ib_client_id: int = Field(default=13, alias="IB_CLIENT_ID")  # dashboard CID; override via env
    ib_mode: Literal["live", "paper"] = Field(default="live")

    # market config (loaded from YAML, not env)
    min_cushion: float = 0.20
    max_dte: int = 50
    reap_ratio: float = 0.025
    min_reap_dte: int = 1

    # display / market config (from snp_config.yml only)
    currency: str = "USD"      # base currency shown in header (CURRENCY key in YAML)
    protect_me: bool = False
    cover_std_mult: float = 0.65
    covxpmult: float = 1.2

    model_config = SettingsConfigDict(
        env_file=str(here() / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- helpers ----------------------------------------------------------

    @property
    def account(self) -> str:
        """Active account number, plain text, only used at IBKR API boundary."""
        return self.us_account.get_secret_value()

    @property
    def account_masked(self) -> str:
        """e.g. 'U***1234' — safe for UI."""
        a = self.account
        if not a or len(a) < 4:
            return "••••"
        return f"{a[0]}{'•' * (len(a) - 4)}{a[-3:]}"

    def merge_yaml(self, path: Path) -> Settings:
        """Overlay YAML market config (non-secret knobs only)."""
        if not path.exists():
            return self
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        port_live = cfg.get("PORT")
        port_paper = cfg.get("PAPER")
        return self.model_copy(
            update={
                "ib_port": port_live if self.ib_mode == "live" else port_paper or self.ib_port,
                "ib_client_id": int(cfg.get("CID", self.ib_client_id)),
                "min_cushion": float(cfg.get("MINCUSHION", self.min_cushion)),
                "max_dte": int(cfg.get("MAX_DTE", self.max_dte)),
                "reap_ratio": float(cfg.get("REAPRATIO", self.reap_ratio)),
                "min_reap_dte": int(cfg.get("MINREAPDTE", self.min_reap_dte)),
                "currency": str(cfg.get("CURRENCY", self.currency)),
                "protect_me": bool(cfg.get("PROTECT_ME", self.protect_me)),
                "cover_std_mult": float(cfg.get("COVER_STD_MULT", self.cover_std_mult)),
                "covxpmult": float(cfg.get("COVXPMULT", self.covxpmult)),
            }
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so we read .env exactly once per process."""
    base = Settings()
    return base.merge_yaml(here() / "config" / "snp_config.yml")
