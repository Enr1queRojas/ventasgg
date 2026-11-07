"""Etapa 4: verificación del webhook, firma de Meta, filtrado y deduplicación."""

import hashlib
import hmac
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.database import open_db
from app.main import RedactVerifyTokenFilter, create_app

VERIFY_TOKEN = "verify-123"
APP_SECRET = "app-secret-xyz"
PHONE_NUMBER_ID = "111222333"
URL = "/webhook/whatsapp"


@pytest.fixture
def wa_settings(db_path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_path=db_path,
        whatsapp_verify_token=VERIFY_TOKEN,
        whatsapp_app_secret=APP_SECRET,
        whatsapp_phone_number_id=PHONE_NUMBER_ID,
        worker_enabled=False,
    )


@pytest.fixture
def client(wa_settings):
    with TestClient(create_app(wa_settings)) as c:
        yield c


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(client, payload, *, secret: str = APP_SECRET, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "X-Hub-Signature-256": signature or sign(body, secret)}
    return client.post(URL, content=body, headers=headers)


def event(*messages, statuses=None, phone_number_id=PHONE_NUMBER_ID) -> dict:
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": phone_number_id}}
    if messages:
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}]}


def text_msg(mid="wamid.1", body="Hola, necesito 30 bultos de maíz blanco", sender="5215512345678"):
    return {"from": sender, "id": mid, "timestamp": "1762480000", "type": "text", "text": {"body": body}}


def stored(settings):
    with open_db(settings.database_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id")]


# ------------------------------------------------------------------ GET (verificación)


def test_verify_returns_challenge(client):
    r = client.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "249582424"})
    assert r.status_code == 200
    assert r.text == "249582424"


@pytest.mark.parametrize(
    "params",
    [
        {"hub.mode": "subscribe", "hub.verify_token": "incorrecto", "hub.challenge": "1"},
        {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1"},
        {"hub.mode": "subscribe", "hub.challenge": "1"},
        {},
    ],
)
def test_verify_rejects(client, params):
    assert client.get(URL, params=params).status_code == 403


def test_verify_rejects_when_token_not_configured(settings):
    with TestClient(create_app(settings)) as c:
        r = c.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"})
    assert r.status_code == 403


# ------------------------------------------------------------------ POST (eventos)


def test_text_message_is_stored_pending(client, wa_settings):
    assert post(client, event(text_msg())).json() == {"status": "ok"}
    [m] = stored(wa_settings)
    assert m["whatsapp_message_id"] == "wamid.1"
    assert m["phone"] == "5215512345678"  # wa_id original, necesario para responder
    assert m["text"] == "Hola, necesito 30 bultos de maíz blanco"
    assert (m["direction"], m["message_type"], m["status"]) == ("inbound", "text", "pending")


def test_duplicate_delivery_is_stored_once(client, wa_settings):
    assert post(client, event(text_msg())).status_code == 200
    assert post(client, event(text_msg())).status_code == 200
    assert len(stored(wa_settings)) == 1


def test_multiple_messages_in_one_event(client, wa_settings):
    post(client, event(text_msg("wamid.a", "hola"), text_msg("wamid.b", "30 bultos")))
    assert [m["text"] for m in stored(wa_settings)] == ["hola", "30 bultos"]


def test_invalid_signature_rejected(client, wa_settings):
    assert post(client, event(text_msg()), secret="otro-secreto").status_code == 401
    assert post(client, event(text_msg()), signature="sha256=abc").status_code == 401
    r = client.post(URL, content=json.dumps(event(text_msg())), headers={"Content-Type": "application/json"})
    assert r.status_code == 401
    assert stored(wa_settings) == []


def test_post_rejected_without_app_secret(settings):
    with TestClient(create_app(settings)) as c:
        assert post(c, event(text_msg())).status_code == 503


def test_status_updates_are_ignored(client, wa_settings):
    statuses = [{"id": "wamid.out", "status": "delivered", "recipient_id": "5215512345678"}]
    assert post(client, event(statuses=statuses)).json() == {"status": "ignored"}
    assert stored(wa_settings) == []


def test_other_phone_number_id_ignored(client, wa_settings):
    assert post(client, event(text_msg(), phone_number_id="999")).json() == {"status": "ignored"}


def test_non_whatsapp_object_ignored(client):
    assert post(client, {"object": "page", "entry": []}).json() == {"status": "ignored"}


def test_unsupported_media_stored_for_reply(client, wa_settings):
    audio = {"from": "5215512345678", "id": "wamid.audio", "type": "audio", "audio": {"id": "media1"}}
    post(client, event(audio))
    [m] = stored(wa_settings)
    assert (m["message_type"], m["text"], m["status"]) == ("unsupported", None, "pending")


def test_reactions_ignored(client, wa_settings):
    reaction = {"from": "5215512345678", "id": "wamid.r", "type": "reaction", "reaction": {"emoji": "👍"}}
    assert post(client, event(reaction)).json() == {"status": "ignored"}


def test_button_reply_treated_as_text(client, wa_settings):
    msg = {"from": "5215512345678", "id": "wamid.btn", "type": "interactive",
           "interactive": {"type": "button_reply", "button_reply": {"id": "yes", "title": "Sí, registrar"}}}
    post(client, event(msg))
    assert stored(wa_settings)[0]["text"] == "Sí, registrar"


def test_malformed_json_rejected(client):
    body = b"{no es json"
    r = client.post(URL, content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert r.status_code == 400


# ------------------------------------------------------------------ logs


def test_access_log_redacts_verify_token():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", "/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=SECRETO&hub_verify_token=SECRETO", "1.1", 200),
        None,
    )
    RedactVerifyTokenFilter().filter(record)
    assert "SECRETO" not in record.getMessage()
