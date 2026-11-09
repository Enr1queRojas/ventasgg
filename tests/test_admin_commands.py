"""Comandos de WhatsApp del número de ventas para administrar clientes."""

import pytest

from app.config import Settings
from app.db import repository
from app.models import MessageType
from app.services.admin_commands import Command, parse_admin_phone, parse_command
from app.workers.message_worker import MessageWorker

SALES_PHONE = "+52 33 1111 2222"
SALES_WA_ID = "5213311112222"  # forma en que Meta reporta al vendedor


class Sender:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to, body):
        self.sent.append((to, body))
        return f"wamid.{len(self.sent)}"

    def send_template(self, to, name, language_code="es_MX", components=None):
        raise AssertionError("los comandos responden con texto")


@pytest.fixture
def admin(conn, db_path):
    settings = Settings(_env_file=None, app_env="test", database_path=db_path, sales_phone=SALES_PHONE, worker_enabled=False)
    sender = Sender()
    worker = MessageWorker(settings, sender)
    counter = iter(range(1, 1000))

    def say(text: str, phone: str = SALES_WA_ID) -> str:
        repository.insert_inbound_message(
            conn, whatsapp_message_id=f"w{next(counter)}", phone=phone, text=text, message_type=MessageType.TEXT
        )
        before = len(sender.sent)
        worker.run_once()
        [(to, body)] = sender.sent[before:]
        assert to == phone
        return body

    return say


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("alta Juan 8711234567", Command("alta", "Juan 8711234567")),
        ("Alta: Juan", Command("alta", "Juan")),
        ("BAJA 871", Command("baja", "871")),
        ("clientes", Command("clientes")),
        ("Búscar juan", Command("buscar", "juan")),
        ("hola", Command("ayuda")),
        ("", Command("ayuda")),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize(
    ("raw", "phone"),
    [("871 123 4567", "528711234567"), ("+52 1 871 123 4567", "528711234567"), ("(871) 123-45-67", "528711234567")],
)
def test_parse_admin_phone_assumes_mexico_for_10_digits(raw, phone):
    assert parse_admin_phone(raw) == phone


def test_alta_registers_customer_who_can_then_order(conn, admin):
    reply = admin("alta Juan Pérez 871 123 4567")
    assert "Cliente registrado: Juan Pérez — +52 871 123 4567" in reply
    customer = repository.get_customer_by_phone(conn, "528711234567")
    assert customer is not None and customer.name == "Juan Pérez" and customer.active
    # el nuevo cliente ya no recibe el mensaje de «no registrado»
    assert "no está registrado" not in admin("Hola", phone="5218711234567")


def test_alta_accepts_phone_first(conn, admin):
    assert "Cliente registrado" in admin("alta +52 871 123 4567 Ferretería López")
    assert repository.get_customer_by_phone(conn, "528711234567").name == "Ferretería López"


def test_alta_existing_updates_name_and_reactivates(conn, admin):
    admin("alta Juan 8711234567")
    assert "Cliente actualizado: Juan Pérez" in admin("alta Juan Pérez 8711234567")
    admin("baja 8711234567")
    assert "Cliente reactivado" in admin("alta Juan Pérez 8711234567")
    assert repository.get_customer_by_phone(conn, "528711234567").active
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1


def test_baja_deactivates(conn, admin):
    admin("alta Juan 8711234567")
    assert "Cliente dado de baja: Juan" in admin("baja 871 123 4567")
    assert repository.get_customer_by_phone(conn, "528711234567") is None
    assert "no está registrado" in admin("Hola", phone="5218711234567")


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("alta Juan", "Para dar de alta"),
        ("alta 8711234567", "Para dar de alta"),
        ("alta Juan 123", "Para dar de alta"),
        ("baja", "Para dar de baja"),
        ("baja 123", "no es válido"),
        ("baja 8719999999", "No hay ningún cliente"),
        ("alta Ventas 33 1111 2222", "número de ventas"),
    ],
)
def test_invalid_commands_explain_usage(conn, admin, text, fragment):
    assert fragment in admin(text)
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0


def test_clientes_and_buscar(conn, admin):
    admin("alta Juan Pérez 8711234567")
    admin("alta Ana López 8717654321")
    admin("baja 8717654321")
    listing = admin("clientes")
    assert "Clientes activos: 1 (de 2 registrados)" in listing
    assert "Juan Pérez" in listing and "Ana López" not in listing
    found = admin("buscar lópez")
    assert "Ana López" in found and "(inactivo)" in found
    assert "Juan Pérez" in admin("buscar 1234567")
    assert "No encontré" in admin("buscar pedro")


def test_customer_cannot_run_commands(conn, customer, admin):
    """Un cliente que escribe «alta ...» no administra nada (va al flujo de pedidos)."""
    admin("alta Intruso 8711234567", phone="5215512345678")
    assert repository.get_customer_by_phone(conn, "528711234567", active_only=False) is None


# ------------------------------------------------------------------ frases naturales


@pytest.mark.parametrize(
    ("text", "action", "argument"),
    [
        ("¿Qué pedidos hay pendientes?", "pedidos", ""),
        ("lista de pedidos", "pedidos", ""),
        ("ver pedidos", "pedidos", ""),
        ("Pedidos de hoy", "pedidos", ""),
        ("¿qué entregas tengo programadas?", "pedidos", ""),
        ("ver el pedido 12", "pedido", "12"),
        ("detalle del pedido #3", "pedido", "3"),
        ("PED-0012", "pedido", "12"),
        ("¿cómo va el folio 7?", "pedido", "7"),
        ("lista de clientes", "clientes", ""),
        ("¿cuántos clientes tengo?", "clientes", ""),
        ("busca a Juan", "buscar", "Juan"),
        ("buscar cliente Ana López", "buscar", "Ana López"),
        ("Da de alta a Juan Pérez con el teléfono 871 123 4567", "alta", "Juan Pérez con el teléfono 871 123 4567"),
        ("dar de baja al 871 123 4567", "baja", "871 123 4567"),
        ("programar 12 viernes", "solo_panel", ""),
        ("cancelar pedido 3", "solo_panel", ""),
        ("Hola", "ayuda", ""),
        ("gracias", "cortesia", ""),
        ("¿me mandas el reporte de ventas?", "no_entendido", "¿me mandas el reporte de ventas?"),
    ],
)
def test_parse_natural_phrases(text, action, argument):
    assert parse_command(text) == Command(action, argument)


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Da de alta a Juan Pérez con el teléfono 871 123 4567", "Juan Pérez"),
        ("Nuevo cliente: Ferretería López, 871-765-4321", "Ferretería López"),
        ("agrega al cliente Ana Ruiz cel 8711234567", "Ana Ruiz"),
        ("registra a Pedro Gómez número +52 871 123 4567", "Pedro Gómez"),
        ("alta Juan Pérez 871 123 4567", "Juan Pérez"),
    ],
)
def test_alta_from_natural_phrase(conn, admin, text, name):
    assert "Cliente registrado" in admin(text)
    customer = repository.list_customers(conn)[0]
    assert customer.name == name and customer.phone in {"528711234567", "528717654321"}


def test_unknown_message_says_so(admin):
    reply = admin("¿me mandas el reporte?")
    assert reply.startswith("No entendí «¿me mandas el reporte?»") and "*pedidos*" in reply
    assert admin("gracias").startswith("¡Con gusto!")


def test_lista_de_pedidos_no_longer_lists_customers(conn, admin):
    admin("alta Juan Pérez 8711234567")
    reply = admin("lista de pedidos")
    assert "No hay pedidos pendientes" in reply and "Juan Pérez" not in reply
