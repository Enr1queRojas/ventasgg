"""Etapa 5: cliente de envío de WhatsApp (Meta simulado con httpx.MockTransport)."""

import json

import httpx
import pytest

from app.integrations.whatsapp import (
    WhatsAppClient,
    WhatsAppError,
    recipient_candidates,
)


def make_client(handler) -> tuple[WhatsAppClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = WhatsAppClient(
        access_token="TOKEN", phone_number_id="PNID", api_version="v25.0", transport=httpx.MockTransport(record)
    )
    return client, calls


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})


def meta_error(status: int, code: int, message: str = "error"):
    return lambda request: httpx.Response(status, json={"error": {"code": code, "message": message}})


def test_recipient_candidates_mexico():
    assert recipient_candidates("5215512345678") == ["525512345678", "5215512345678"]
    assert recipient_candidates("525512345678") == ["525512345678"]
    assert recipient_candidates("15551234567") == ["15551234567"]


def test_send_text_payload_and_auth():
    client, calls = make_client(ok)
    assert client.send_text("5215512345678", "Hola") == "wamid.OUT"
    [req] = calls
    assert req.url.path == "/v25.0/PNID/messages"
    assert req.headers["Authorization"] == "Bearer TOKEN"
    body = json.loads(req.content)
    assert body["to"] == "525512345678"
    assert body["type"] == "text" and body["text"]["body"] == "Hola"


def test_falls_back_to_original_wa_id_on_131030():
    def handler(request):
        to = json.loads(request.content)["to"]
        return ok(request) if to == "5215512345678" else meta_error(400, 131030)(request)

    client, calls = make_client(handler)
    assert client.send_text("5215512345678", "Hola") == "wamid.OUT"
    assert [json.loads(c.content)["to"] for c in calls] == ["525512345678", "5215512345678"]


@pytest.mark.parametrize(
    ("status", "code", "transient"),
    [(500, 1, True), (429, 130429, True), (400, 131047, False), (401, 190, False)],
)
def test_error_classification(status, code, transient):
    client, _ = make_client(meta_error(status, code))
    with pytest.raises(WhatsAppError) as exc:
        client.send_text("525512345678", "Hola")
    assert exc.value.transient is transient
    assert exc.value.code == code


def test_network_error_is_transient():
    def boom(request):
        raise httpx.ConnectError("sin red")

    client, _ = make_client(boom)
    with pytest.raises(WhatsAppError) as exc:
        client.send_text("525512345678", "Hola")
    assert exc.value.transient


def test_send_template():
    client, calls = make_client(ok)
    client.send_template("525512345678", "hello_world", "en_US")
    body = json.loads(calls[0].content)
    assert body["type"] == "template"
    assert body["template"] == {"name": "hello_world", "language": {"code": "en_US"}}


def test_startup_check_logs_number_or_error(caplog):
    from app.main import check_whatsapp

    info = {"display_phone_number": "+52 1 871 233 3190", "verified_name": "Graneros Guerra", "quality_rating": "GREEN"}
    client, _ = make_client(lambda request: httpx.Response(200, json=info))
    with caplog.at_level("INFO", logger="sales_bot"):
        assert check_whatsapp(client) is True
    assert "Graneros Guerra" in caplog.text

    client, _ = make_client(meta_error(401, 190, "Error validating access token"))
    with caplog.at_level("ERROR", logger="sales_bot"):
        assert check_whatsapp(client) is False
    assert "code=190" in caplog.text and "TOKEN" not in caplog.text
