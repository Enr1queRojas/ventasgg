"""Etapa 8 (parcial): worker, respuestas controladas, reintentos y recuperación."""

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.db import repository
from app.integrations.whatsapp import WhatsAppError
from app.models import MessageType
from app.workers.message_worker import MessageWorker, backoff_seconds

CUSTOMER_WA_ID = "5215512345678"  # forma en que Meta reporta al cliente registrado (+52 55 1234 5678)
UNKNOWN_WA_ID = "5213399998888"
SALES_PHONE = "+52 33 1111 2222"


class FakeSender:
    def __init__(self, errors: list[Exception] | None = None):
        self.sent: list[tuple[str, str]] = []
        self.errors = list(errors or [])

    def send_text(self, to: str, body: str) -> str:
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((to, body))
        return f"wamid.out{len(self.sent)}"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def worker_settings(db_path) -> Settings:
    return Settings(_env_file=None, app_env="test", database_path=db_path, sales_phone=SALES_PHONE, worker_enabled=False)


@pytest.fixture
def clock():
    return Clock()


def inbound(conn, wamid, phone, text="Hola", message_type=MessageType.TEXT):
    return repository.insert_inbound_message(conn, whatsapp_message_id=wamid, phone=phone, text=text, message_type=message_type)


def rows(conn, direction=None):
    sql = "SELECT * FROM messages" + (" WHERE direction = ?" if direction else "") + " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, (direction,) if direction else ())]


def test_registered_customer_gets_reply(conn, customer, worker_settings, clock):
    inbound(conn, "w1", CUSTOMER_WA_ID)
    sender = FakeSender()
    assert MessageWorker(worker_settings, sender, clock=clock).run_once()

    assert sender.sent[0][0] == CUSTOMER_WA_ID
    assert "Cliente de prueba" in sender.sent[0][1]
    [msg_in] = rows(conn, "inbound")
    [msg_out] = rows(conn, "outbound")
    assert msg_in["status"] == "processed"
    assert (msg_out["status"], msg_out["whatsapp_message_id"], msg_out["attempts"]) == ("sent", "wamid.out1", 1)


def test_unknown_number_gets_sales_contact(conn, customer, worker_settings, clock):
    inbound(conn, "w1", UNKNOWN_WA_ID)
    sender = FakeSender()
    MessageWorker(worker_settings, sender, clock=clock).run_once()
    [(to, body)] = sender.sent
    assert to == UNKNOWN_WA_ID
    assert "no está registrado" in body
    assert "+52 33 1111 2222" in body


def test_sales_phone_is_outside_order_flow(conn, worker_settings, clock):
    inbound(conn, "w1", "5213311112222")
    sender = FakeSender()
    MessageWorker(worker_settings, sender, clock=clock).run_once()
    assert "número de ventas" in sender.sent[0][1]


def test_unsupported_media_asks_for_text(conn, customer, worker_settings, clock):
    inbound(conn, "w1", CUSTOMER_WA_ID, text=None, message_type=MessageType.UNSUPPORTED)
    sender = FakeSender()
    MessageWorker(worker_settings, sender, clock=clock).run_once()
    assert "solo puedo leer mensajes de texto" in sender.sent[0][1]


def test_transient_send_error_is_retried_with_backoff(conn, customer, worker_settings, clock):
    inbound(conn, "w1", CUSTOMER_WA_ID)
    sender = FakeSender(errors=[WhatsAppError("timeout", transient=True)])
    worker = MessageWorker(worker_settings, sender, clock=clock)

    worker.run_once()
    [out] = rows(conn, "outbound")
    assert (out["status"], out["attempts"]) == ("pending", 1)
    assert sender.sent == []

    worker.run_once()  # aún no vence el backoff
    assert sender.sent == []

    clock.advance(backoff_seconds(1))
    worker.run_once()
    assert len(sender.sent) == 1
    assert rows(conn, "outbound")[0]["status"] == "sent"


def test_permanent_send_error_fails_immediately(conn, customer, worker_settings, clock):
    inbound(conn, "w1", CUSTOMER_WA_ID)
    sender = FakeSender(errors=[WhatsAppError("fuera de ventana", code=131047, transient=False)])
    MessageWorker(worker_settings, sender, clock=clock).run_once()
    [out] = rows(conn, "outbound")
    assert out["status"] == "failed"
    assert "fuera de ventana" in out["last_error"]


def test_gives_up_after_max_attempts(conn, customer, db_path, clock):
    settings = Settings(_env_file=None, app_env="test", database_path=db_path, worker_enabled=False, worker_max_attempts=2)
    inbound(conn, "w1", CUSTOMER_WA_ID)
    sender = FakeSender(errors=[WhatsAppError("x", transient=True)] * 5)
    worker = MessageWorker(settings, sender, clock=clock)
    worker.run_once()
    clock.advance(300)
    worker.run_once()
    out = rows(conn, "outbound")[0]
    assert (out["status"], out["attempts"]) == ("failed", 2)


def test_processor_error_keeps_inbound_for_retry(conn, customer, worker_settings, clock):
    inbound(conn, "w1", CUSTOMER_WA_ID)

    def broken(*args):
        raise RuntimeError("OpenAI caído")

    MessageWorker(worker_settings, FakeSender(), processor=broken, clock=clock).run_once()
    [msg] = rows(conn, "inbound")
    assert (msg["status"], msg["attempts"]) == ("pending", 1)
    assert "OpenAI caído" in msg["last_error"]
    assert rows(conn, "outbound") == []


def test_messages_from_same_phone_stay_in_order(conn, customer, worker_settings, clock):
    inbound(conn, "w1", CUSTOMER_WA_ID, text="primero")
    inbound(conn, "w2", CUSTOMER_WA_ID, text="segundo")
    seen: list[str] = []
    calls = {"n": 0}

    def flaky(conn_, settings_, message):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("falla temporal")
        seen.append(message.text)
        return []

    worker = MessageWorker(worker_settings, FakeSender(), processor=flaky, clock=clock)
    worker.run_once()
    assert seen == []  # "segundo" espera a que se procese "primero"
    clock.advance(backoff_seconds(1))
    worker.run_once()
    assert seen == ["primero", "segundo"]


def test_recover_resets_processing(conn, customer, worker_settings, clock):
    inbound(conn, "w1", CUSTOMER_WA_ID)
    conn.execute("UPDATE messages SET status = 'processing'")
    worker = MessageWorker(worker_settings, FakeSender(), clock=clock)
    assert worker.recover() == 1
    worker.run_once()
    assert rows(conn, "inbound")[0]["status"] == "processed"


def test_without_sender_replies_stay_queued(conn, customer, worker_settings, clock):
    inbound(conn, "w1", CUSTOMER_WA_ID)
    MessageWorker(worker_settings, None, clock=clock).run_once()
    assert rows(conn, "outbound")[0]["status"] == "pending"
