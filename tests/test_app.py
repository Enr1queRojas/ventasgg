"""Etapa 1: configuración, teléfonos y arranque de FastAPI."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.phone import InvalidPhoneError, format_phone_display, normalize_phone


@pytest.mark.parametrize(
    "raw",
    ["+52 55 1234 5678", "525512345678", "5215512345678", "+52 1 55 1234 5678", "0052 (55) 1234-5678"],
)
def test_normalize_mexican_phone_variants(raw):
    assert normalize_phone(raw) == "525512345678"


@pytest.mark.parametrize("raw", ["", "   ", "5512345678", "abc", "+1234567890123456"])
def test_normalize_phone_rejects_invalid(raw):
    with pytest.raises(InvalidPhoneError):
        normalize_phone(raw)


def test_format_phone_display():
    assert format_phone_display("525512345678") == "+52 55 1234 5678"
    assert format_phone_display("15551234567") == "+15551234567"


def test_empty_env_values_become_none(db_path):
    s = Settings(_env_file=None, database_path=db_path, openai_api_key="", sales_phone="  ")
    assert s.openai_api_key is None
    assert s.sales_phone is None


def test_sales_phone_is_normalized(db_path):
    s = Settings(_env_file=None, database_path=db_path, sales_phone="+52 1 55 9876 5432")
    assert s.sales_phone == "525598765432"


def test_production_requires_secrets(db_path):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, app_env="production", database_path=db_path)
    assert "OPENAI_API_KEY" in str(exc.value)


def test_secrets_are_masked(db_path):
    s = Settings(_env_file=None, database_path=db_path, whatsapp_access_token="super-secret")
    assert "super-secret" not in repr(s)


def test_invalid_timezone_rejected(db_path):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_path=db_path, tz="Mars/Olympus")


def test_health_initializes_db_and_responds(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
    assert settings.database_path.exists()
