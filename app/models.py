"""Modelos de dominio compartidos por la base de datos y los servicios.

Incluye el esquema de salida estructurada del agente de OpenAI (``AgentTurn``).
"""

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Unit(StrEnum):
    KG = "kg"
    BULTOS = "bultos"
    PIEZAS = "piezas"  # cajas, pacas u otras unidades que se cuentan por pieza


class Urgency(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class OrderStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PENDING_SALES = "pending_sales"
    CANCELLED = "cancelled"
    CLOSED = "closed"


# Un cliente solo puede tener un pedido en alguno de estos estados a la vez.
ACTIVE_ORDER_STATUSES: tuple[OrderStatus, ...] = (OrderStatus.DRAFT, OrderStatus.AWAITING_CONFIRMATION)


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageType(StrEnum):
    TEXT = "text"
    TEMPLATE = "template"
    SYSTEM = "system"
    UNSUPPORTED = "unsupported"


class MessageStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"  # entrante ya atendido
    SENT = "sent"  # saliente aceptado por Meta
    FAILED = "failed"


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True)


class Customer(_Record):
    id: int
    name: str
    phone: str
    active: bool
    created_at: str


class Product(_Record):
    id: int
    sku: str
    name: str
    aliases: tuple[str, ...] = ()
    allowed_units: tuple[Unit, ...] = Field(min_length=1)
    kg_per_bag: float | None = None
    active: bool = True


class Order(_Record):
    id: int
    folio: str | None
    customer_id: int
    requested_date: date | None
    urgency: Urgency | None
    urgency_note: str | None
    notes: str | None
    status: OrderStatus
    created_at: str
    updated_at: str
    confirmed_at: str | None


class Message(_Record):
    id: int
    whatsapp_message_id: str | None
    direction: MessageDirection
    phone: str
    text: str | None
    message_type: MessageType
    status: MessageStatus
    attempts: int
    next_attempt_at: str | None
    last_error: str | None
    order_id: int | None
    created_at: str
    updated_at: str


class OrderItem(_Record):
    id: int
    order_id: int
    product_id: int
    quantity: float
    unit: Unit


# --------------------------------------------------------------------------- salida del LLM
# Esquema de Structured Outputs: todos los campos son obligatorios; null = "no se mencionó".


class AgentIntent(StrEnum):
    ORDER_INFO = "order_info"  # aporta o corrige datos del pedido
    CONFIRM = "confirm"  # acepta el resumen presentado
    CANCEL = "cancel"  # descarta el pedido en curso
    GREETING = "greeting"
    QUESTION = "question"  # precios, existencias, horarios, etc.
    OTHER = "other"


class AgentItem(BaseModel):
    action: Literal["set", "remove"] = Field(
        description="set: agregar la partida o reemplazar su cantidad/unidad; remove: quitar el producto del pedido."
    )
    product_text: str = Field(description="Producto tal como lo nombró el cliente.")
    sku: str | None = Field(description="SKU del catálogo si la correspondencia es inequívoca; null si hay duda.")
    quantity: float | None = Field(description="Cantidad numérica; null si no se ha indicado.")
    unit: Literal["kg", "bultos", "piezas"] | None = Field(
        description="Unidad; null si no se ha indicado. Cajas, pacas o piezas -> piezas."
    )


class AgentTurn(BaseModel):
    intent: AgentIntent
    items: list[AgentItem] = Field(
        description="Solo partidas nuevas, corregidas o eliminadas en este mensaje. Vacío si no cambian."
    )
    replace_all_items: bool = Field(
        description="true si el cliente quiere sustituir todas las partidas anteriores por las de este mensaje."
    )
    requested_date: str | None = Field(description="Fecha solicitada en YYYY-MM-DD si se menciona y es inequívoca.")
    date_is_ambiguous: bool = Field(description="true si mencionó una fecha que no se puede resolver con certeza.")
    urgency: Literal["normal", "high", "critical"] | None = Field(description="Solo si el cliente la expresa.")
    urgency_note: str | None = Field(description="Motivo de la urgencia en palabras del cliente.")
    notes: str | None = Field(description="Observaciones nuevas para ventas (dirección, horario, etc.).")
    reply: str = Field(description="Respuesta breve y natural en español para el cliente.")
