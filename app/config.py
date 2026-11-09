"""Configuración de la aplicación leída desde variables de entorno y ``.env``."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.phone import normalize_phone

BASE_DIR = Path(__file__).resolve().parent.parent

# Campos opcionales en desarrollo: un valor vacío en .env ("OPENAI_API_KEY=") equivale a no definido.
_OPTIONAL_FIELDS = (
    "openai_api_key",
    "openai_model",
    "whatsapp_access_token",
    "whatsapp_phone_number_id",
    "whatsapp_verify_token",
    "whatsapp_app_secret",
    "whatsapp_api_version",
    "sales_phone",
    "sales_notification_template",
)

# Obligatorios cuando APP_ENV=production.
_REQUIRED_IN_PRODUCTION = (
    "openai_api_key",
    "openai_model",
    "whatsapp_access_token",
    "whatsapp_phone_number_id",
    "whatsapp_verify_token",
    "whatsapp_app_secret",
    "whatsapp_api_version",
    "sales_phone",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        str_strip_whitespace=True,
    )

    # Application
    app_env: Literal["development", "test", "production"] = "development"
    app_port: int = Field(default=8003, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    tz: str = "America/Mexico_City"
    database_path: Path = BASE_DIR / "data" / "sales_bot.db"

    # OpenAI
    openai_api_key: SecretStr | None = None
    openai_model: str | None = None

    # Meta WhatsApp
    whatsapp_access_token: SecretStr | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_verify_token: SecretStr | None = None
    whatsapp_app_secret: SecretStr | None = None
    whatsapp_api_version: str | None = None

    # Sales
    sales_phone: str | None = None
    sales_notification_template: str | None = None  # nombre de la plantilla aprobada en Meta
    sales_notification_template_language: str = "es_MX"

    # Worker
    worker_enabled: bool = True
    worker_poll_interval: float = Field(default=1.0, gt=0)
    worker_max_attempts: int = Field(default=5, ge=1)

    @field_validator(*_OPTIONAL_FIELDS, mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("tz")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Zona horaria desconocida: {value}") from exc
        return value

    @field_validator("database_path")
    @classmethod
    def _absolute_db_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else (BASE_DIR / value).resolve()

    @field_validator("sales_phone")
    @classmethod
    def _normalize_sales_phone(cls, value: str | None) -> str | None:
        return normalize_phone(value) if value is not None else None

    @model_validator(mode="after")
    def _require_production_secrets(self) -> "Settings":
        if self.app_env == "production":
            missing = [name.upper() for name in _REQUIRED_IN_PRODUCTION if getattr(self, name) is None]
            if missing:
                raise ValueError(f"Faltan variables obligatorias en producción: {', '.join(missing)}")
        return self

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


@lru_cache
def get_settings() -> Settings:
    return Settings()
