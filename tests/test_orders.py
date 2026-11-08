"""Etapas 6-7: motor de pedidos con el agente simulado (sin llamadas a OpenAI)."""

from datetime import date
from functools import partial

import pytest

from app.config import Settings
from app.db import repository
from app.integrations.openai_agent import AgentContext
from app.models import AgentIntent, AgentItem, AgentTurn, MessageType, OrderStatus
from app.services.message_processor import process_message
from app.workers.message_worker import MessageWorker

WA_ID = "5215512345678"
SALES = "+52 33 1111 2222"


def turn(intent=AgentIntent.ORDER_INFO, items=(), reply="¡Hola! ¿Qué necesitas?", **kw) -> AgentTurn:
    data = dict(
        intent=intent, items=list(items), replace_all_items=False, requested_date=None, date_is_ambiguous=False,
        urgency=None, urgency_note=None, notes=None, reply=reply,
    )
    data.update(kw)
    return AgentTurn(**data)


def item(text, qty=None, unit=None, sku=None, action="set") -> AgentItem:
    return AgentItem(action=action, product_text=text, sku=sku, quantity=qty, unit=unit)


class ScriptedAgent:
    """Devuelve los turnos en orden y guarda el contexto recibido."""

    def __init__(self):
        self.queue: list[AgentTurn] = []
        self.contexts: list[AgentContext] = []

    def interpret(self, context: AgentContext) -> AgentTurn:
        self.contexts.append(context)
        return self.queue.pop(0)


class Sender:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to, body):
        self.sent.append((to, body))
        return f"wamid.{len(self.sent)}"


class Chat:
    """Simula la conversación de punta a punta: webhook (insert) -> worker -> envío."""

    def __init__(self, conn, settings):
        self.conn = conn
        self.agent = ScriptedAgent()
        self.sender = Sender()
        self.worker = MessageWorker(settings, self.sender, processor=partial(process_message, agent=self.agent))
        self.n = 0

    def say(self, text: str, agent_turn: AgentTurn | None) -> list[str]:
        if agent_turn is not None:
            self.agent.queue.append(agent_turn)
        self.n += 1
        before = len(self.sender.sent)
        repository.insert_inbound_message(
            self.conn, whatsapp_message_id=f"w{self.n}", phone=WA_ID, text=text, message_type=MessageType.TEXT
        )
        self.worker.run_once()
        return [body for _, body in self.sender.sent[before:]]

    def order(self):
        return self.conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 1").fetchone()

    def items(self, order_id):
        return [
            (r["sku"], r["quantity"], r["unit"])
            for r in self.conn.execute(
                "SELECT p.sku, i.quantity, i.unit FROM order_items i JOIN products p ON p.id = i.product_id "
                "WHERE order_id = ? ORDER BY p.sku", (order_id,)
            )
        ]


@pytest.fixture
def chat(conn, customer, products, db_path):
    settings = Settings(_env_file=None, app_env="test", database_path=db_path, sales_phone=SALES, worker_enabled=False)
    return Chat(conn, settings)


def future(days=4) -> str:
    return date.fromordinal(date.today().toordinal() + days).isoformat()


# ------------------------------------------------------------------ flujos completos


def test_full_order_in_one_message_then_confirm(chat):
    [summary] = chat.say(
        "Necesito 30 bultos de maíz blanco para el martes, me urge",
        turn(items=[item("maíz blanco", 30, "bultos", "MAIZ-BLANCO")], requested_date=future(), urgency="high",
             urgency_note="ya no tengo producto"),
    )
    assert "Maíz blanco: 30 bultos" in summary
    assert "Alta (ya no tengo producto)" in summary
    assert "¿Lo registro así?" in summary
    assert chat.order()["status"] == "awaiting_confirmation"

    replies = chat.say("Sí, correcto", turn(AgentIntent.CONFIRM))
    order = chat.order()
    assert order["status"] == "pending_sales"
    assert order["folio"] == f"PED-{order['id']:04d}"
    assert order["confirmed_at"] is not None
    assert len(replies) == 2
    assert order["folio"] in replies[0]
    assert "El área de ventas te asignará y confirmará la fecha de entrega" in replies[0]
    seller_to, seller_text = chat.sender.sent[-1]
    assert seller_to == "523311112222"
    assert "NUEVO PEDIDO" in seller_text and "Maíz blanco: 30 bultos" in seller_text
    assert "La fecha de entrega aún no está confirmada" in seller_text


def test_incomplete_order_asks_only_missing(chat):
    [r1] = chat.say("Necesito maíz blanco", turn(items=[item("maíz blanco", sku="MAIZ-BLANCO")]))
    assert "¿Cuántos bultos o kilos de Maíz blanco" in r1
    assert chat.items(chat.order()["id"]) == []

    [r2] = chat.say("200 kilos", turn(items=[item("maíz blanco", 200, "kg", "MAIZ-BLANCO")]))
    assert "Anotado" in r2 and "200 kg" in r2
    assert "¿Para qué día lo necesitas?" in r2

    [r3] = chat.say("Mañana, me urge", turn(requested_date=future(1), urgency="high"))
    assert "Este es tu pedido" in r3
    assert chat.order()["status"] == "awaiting_confirmation"


def test_history_is_sent_to_agent(chat):
    chat.say("Necesito maíz blanco", turn(items=[item("maíz blanco", sku="MAIZ-BLANCO")]))
    chat.say("200 kilos", turn(items=[item("maíz blanco", 200, "kg", "MAIZ-BLANCO")]))
    ctx = chat.agent.contexts[-1]
    assert ctx.message == "200 kilos"
    assert ctx.history[0] == ("user", "Necesito maíz blanco")
    assert ctx.history[1][0] == "assistant"
    assert ctx.order["status"] == "draft"


def test_multiple_items_and_units(chat):
    [summary] = chat.say(
        "10 bultos de maíz blanco y 200 kg de amarillo para el viernes",
        turn(items=[item("maíz blanco", 10, "bultos", "MAIZ-BLANCO"), item("amarillo", 200, "kg", "MAIZ-AMARILLO")],
             requested_date=future()),
    )
    assert "Maíz blanco: 10 bultos" in summary and "Maíz amarillo: 200 kg" in summary
    assert chat.items(chat.order()["id"]) == [("MAIZ-AMARILLO", 200, "kg"), ("MAIZ-BLANCO", 10, "bultos")]


def test_change_after_summary_requires_new_confirmation(chat):
    chat.say("30 bultos de maíz blanco para el martes",
             turn(items=[item("maíz blanco", 30, "bultos", "MAIZ-BLANCO")], requested_date=future()))
    [summary] = chat.say("Mejor 20", turn(items=[item("maíz blanco", 20, "bultos", "MAIZ-BLANCO")]))
    assert "Maíz blanco: 20 bultos" in summary
    assert chat.items(chat.order()["id"]) == [("MAIZ-BLANCO", 20, "bultos")]
    assert chat.order()["status"] == "awaiting_confirmation"


def test_confirm_with_changes_is_not_registration(chat):
    chat.say("30 bultos de maíz blanco para el martes",
             turn(items=[item("maíz blanco", 30, "bultos", "MAIZ-BLANCO")], requested_date=future()))
    [reply] = chat.say("Sí, pero mejor 25", turn(AgentIntent.CONFIRM, items=[item("maíz blanco", 25, "bultos", "MAIZ-BLANCO")]))
    assert "25 bultos" in reply
    assert chat.order()["status"] == "awaiting_confirmation"


def test_confirm_without_summary_does_not_register(chat):
    chat.say("Necesito 30 bultos de maíz blanco", turn(items=[item("maíz blanco", 30, "bultos", "MAIZ-BLANCO")]))
    [reply] = chat.say("Sí", turn(AgentIntent.CONFIRM))
    assert "¿Para qué día" in reply
    assert chat.order()["status"] == "draft"


def test_duplicate_confirmation_registers_once(chat):
    chat.say("30 bultos de maíz blanco para el martes",
             turn(items=[item("maíz blanco", 30, "bultos", "MAIZ-BLANCO")], requested_date=future()))
    chat.say("Sí", turn(AgentIntent.CONFIRM))
    chat.say("Sí", turn(AgentIntent.CONFIRM, reply="¡Gracias!"))
    assert chat.conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending_sales'").fetchone()[0] == 1
    assert sum("NUEVO PEDIDO" in body for _, body in chat.sender.sent) == 1


def test_cancel_draft(chat):
    chat.say("30 bultos de maíz blanco", turn(items=[item("maíz blanco", 30, "bultos", "MAIZ-BLANCO")]))
    [reply] = chat.say("Mejor cancélalo", turn(AgentIntent.CANCEL))
    assert "cancelé" in reply
    assert chat.order()["status"] == "cancelled"
    assert repository.get_active_order(chat.conn, 1) is None


def test_cancel_without_order(chat):
    [reply] = chat.say("cancela", turn(AgentIntent.CANCEL))
    assert "No tienes ningún pedido" in reply


# ------------------------------------------------------------------ validaciones


def test_ambiguous_product_asks_clarification(chat):
    [reply] = chat.say("30 bultos de maíz", turn(items=[item("maíz", 30, "bultos")]))
    assert "Maíz amarillo o Maíz blanco" in reply
    assert chat.items(chat.order()["id"]) == []


def test_unknown_product(chat):
    [reply] = chat.say("10 kg de frijol", turn(items=[item("frijol", 10, "kg")]))
    assert "No encuentro «frijol»" in reply


def test_invented_sku_falls_back_to_text_resolution(chat):
    chat.say("10 kg de blanco", turn(items=[item("blanco", 10, "kg", sku="NO-EXISTE")]))
    assert chat.items(chat.order()["id"]) == [("MAIZ-BLANCO", 10, "kg")]


def test_unit_not_allowed(chat):
    [reply] = chat.say("50 kg de salvado", turn(items=[item("salvado", 50, "kg", "SALVADO-TRIGO")]))
    assert "se vende en bultos" in reply


def test_single_unit_product_assumes_its_unit(chat):
    chat.say("10 de salvado", turn(items=[item("salvado", 10, None, "SALVADO-TRIGO")]))
    assert chat.items(chat.order()["id"]) == [("SALVADO-TRIGO", 10, "bultos")]


def test_missing_unit_with_two_options(chat):
    [reply] = chat.say("30 de maíz blanco", turn(items=[item("maíz blanco", 30, None, "MAIZ-BLANCO")]))
    assert "¿son bultos o kilos?" in reply


def test_past_date_rejected(chat):
    [reply] = chat.say("30 bultos de blanco para ayer",
                       turn(items=[item("blanco", 30, "bultos", "MAIZ-BLANCO")], requested_date="2020-01-01"))
    assert "ya pasó" in reply
    assert chat.order()["requested_date"] is None


def test_ambiguous_date_asks(chat):
    [reply] = chat.say("30 bultos de blanco para el martes",
                       turn(items=[item("blanco", 30, "bultos", "MAIZ-BLANCO")], date_is_ambiguous=True))
    assert "fecha exacta" in reply


def test_remove_item(chat):
    chat.say("10 bultos de blanco y 5 de salvado",
             turn(items=[item("blanco", 10, "bultos", "MAIZ-BLANCO"), item("salvado", 5, "bultos", "SALVADO-TRIGO")]))
    chat.say("quita el salvado", turn(items=[item("salvado", action="remove", sku="SALVADO-TRIGO")]))
    assert chat.items(chat.order()["id"]) == [("MAIZ-BLANCO", 10, "bultos")]


# ------------------------------------------------------------------ respuestas del modelo


def test_greeting_uses_model_reply(chat):
    [reply] = chat.say("Hola", turn(AgentIntent.GREETING, reply="¡Hola, Cliente! ¿Qué necesitas hoy?"))
    assert reply == "¡Hola, Cliente! ¿Qué necesitas hoy?"
    assert chat.order() is None  # un saludo no crea borrador


def test_unsafe_model_reply_is_replaced(chat):
    [reply] = chat.say("¿Cuándo llega?", turn(AgentIntent.QUESTION, reply="Tu pedido PED-0009 quedó registrado y te lo entregamos el lunes"))
    assert "PED-" not in reply and "entregamos" not in reply


def test_agent_failure_retries_then_apologizes(conn, customer, products, db_path):
    settings = Settings(_env_file=None, app_env="test", database_path=db_path, worker_enabled=False, worker_max_attempts=1)

    class Broken:
        def interpret(self, context):
            raise RuntimeError("OpenAI no disponible")

    sender = Sender()
    worker = MessageWorker(settings, sender, processor=partial(process_message, agent=Broken()))
    repository.insert_inbound_message(conn, whatsapp_message_id="w1", phone=WA_ID, text="hola", message_type=MessageType.TEXT)
    worker.run_once()
    assert "problema técnico" in sender.sent[0][1]
    assert conn.execute("SELECT status FROM messages WHERE direction = 'inbound'").fetchone()[0] == "failed"


def test_order_status_values_constant():
    assert OrderStatus.PENDING_SALES.value == "pending_sales"


def test_paper_order_in_piezas(chat):
    repository.upsert_product(chat.conn, sku="CAJA-PAPEL", name="Caja de papel", allowed_units=["piezas"])
    [summary] = chat.say(
        "20 cajas de papel para el martes",
        turn(items=[item("cajas de papel", 20, None, "CAJA-PAPEL")], requested_date=future()),
    )
    assert "Caja de papel: 20 piezas" in summary
    assert chat.items(chat.order()["id"]) == [("CAJA-PAPEL", 20, "piezas")]
