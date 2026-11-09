"""Alertas: envío a ntfy/Slack/Discord, control de repetición y disparadores."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import repository
from app.integrations.whatsapp import WhatsAppError
from app.main import create_app
from app.services.alerts import Alerter
from app.workers.message_worker import MessageWorker


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def recorder():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200)

    return calls, httpx.MockTransport(handler)


def test_ntfy_payload_and_cooldown():
    calls, transport = recorder()
    clock = Clock()
    alerter = Alerter("https://ntfy.sh/ventasgg-alertas-x1", transport=transport, clock=clock)
    assert alerter.send("k", "Envíos fallidos", "detalle", urgent=True)
    assert not alerter.send("k", "Envíos fallidos", "otra vez")  # dentro del cooldown
    assert alerter.send("otra", "Otra alerta", "x")
    clock.now += 31 * 60
    assert alerter.send("k", "Envíos fallidos", "de nuevo")
    assert len(calls) == 3
    body = json.loads(calls[0].content)
    assert str(calls[0].url) == "https://ntfy.sh"
    assert body["topic"] == "ventasgg-alertas-x1" and body["title"] == "Ventas GG: Envíos fallidos"
    assert body["priority"] == 5 and body["message"] == "detalle"


def test_slack_discord_payload():
    calls, transport = recorder()
    Alerter("https://hooks.slack.com/services/T/B/X", transport=transport).send("k", "Título", "mensaje")
    body = json.loads(calls[0].content)
    assert body["text"] == body["content"] == "*Ventas GG: Título*\nmensaje"


def test_without_url_only_logs(caplog):
    with caplog.at_level("WARNING", logger="sales_bot.alerts"):
        assert Alerter(None).send("k", "Título", "mensaje") is False
    assert "ALERTA Título: mensaje" in caplog.text


def test_alert_failure_never_raises():
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    assert Alerter("https://example.com/hook", transport=transport).send("k", "T", "m") is False


class RecordingAlerter(Alerter):
    def __init__(self):
        super().__init__(None)
        self.sent: list[tuple[str, str, str]] = []

    def send(self, key, title, message, **kwargs):
        if super().send(key, title, message, **kwargs):
            pass
        self.sent.append((key, title, message))
        return True


class FailingSender:
    def __init__(self, error):
        self.error = error

    def send_text(self, to, body):
        raise self.error

    def send_template(self, *args, **kwargs):
        raise self.error


@pytest.fixture
def settings(db_path):
    return Settings(_env_file=None, app_env="test", database_path=db_path, worker_enabled=False, worker_max_attempts=1)


def test_permanent_send_failure_alerts(conn, settings):
    alerter = RecordingAlerter()
    repository.enqueue_outbound(conn, phone="525512345678", text="hola")
    error = WhatsAppError("Meta HTTP 400, code=131026: undeliverable", code=131026)
    MessageWorker(settings, FailingSender(error), alerter=alerter).run_once()
    [(key, title, message)] = alerter.sent
    assert key == "send-failed:525512345678" and title == "Mensaje sin entregar" and "131026" in message


def test_token_error_alerts_urgently(conn, settings):
    alerter = RecordingAlerter()
    repository.enqueue_outbound(conn, phone="525512345678", text="hola")
    error = WhatsAppError("Meta HTTP 401, code=190: Error validating access token", code=190)
    MessageWorker(settings, FailingSender(error), alerter=alerter).run_once()
    assert [k for k, _, _ in alerter.sent] == ["whatsapp-token", "send-failed:525512345678"]


class InfoSender:
    def __init__(self):
        self.fail = False
        self.calls = 0

    def send_text(self, to, body):
        return "wamid"

    def get_phone_number_info(self):
        self.calls += 1
        if self.fail:
            raise WhatsAppError("Meta HTTP 401, code=190: token expired", code=190)
        return {"display_phone_number": "+52 1 871 233 3190", "verified_name": "Graneros Guerra", "quality_rating": "GREEN"}


def test_periodic_whatsapp_check_alerts_on_failure_and_recovery(settings, caplog):
    alerter = RecordingAlerter()
    clock = Clock()
    sender = InfoSender()
    worker = MessageWorker(settings, sender, alerter=alerter, monotonic=clock)
    with caplog.at_level("INFO", logger="sales_bot"):
        worker.check_whatsapp_if_due()  # primera vez: siempre
    assert sender.calls == 1 and "WhatsApp conectado: +52 1 871 233 3190 (Graneros Guerra)" in caplog.text
    worker.check_whatsapp_if_due()
    assert sender.calls == 1  # todavía no toca

    sender.fail = True
    clock.now += settings.whatsapp_check_interval
    worker.check_whatsapp_if_due()
    assert [t for _, t, _ in alerter.sent] == ["WhatsApp sin conexión"]

    sender.fail = False
    clock.now += settings.whatsapp_check_interval
    worker.check_whatsapp_if_due()
    assert [t for _, t, _ in alerter.sent] == ["WhatsApp sin conexión", "WhatsApp conectado de nuevo"]


def test_public_health(db_path):
    settings = Settings(_env_file=None, app_env="test", database_path=db_path, worker_enabled=False)
    with TestClient(create_app(settings)) as client:
        response = client.get("/webhook/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}
