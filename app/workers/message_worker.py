"""Worker interno: procesa mensajes entrantes y entrega los salientes, con reintentos.

Corre como tarea de fondo dentro del proceso FastAPI (una sola instancia). Todo el
estado vive en la tabla ``messages``, así que un reinicio no pierde trabajo.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.config import Settings
from app.db import repository
from app.db.database import open_db, transaction
from app.models import Message, MessageDirection, MessageType
from app.services.message_processor import Reply, process_message

logger = logging.getLogger("sales_bot.worker")

MAX_BACKOFF_SECONDS = 300
PROCESSING_FAILED_REPLY = (
    "Disculpa, tuve un problema técnico al procesar tu mensaje. ¿Me lo puedes enviar de nuevo en unos minutos?"
)


class TextSender(Protocol):
    def send_text(self, to: str, body: str) -> str: ...

    def send_template(
        self, to: str, name: str, language_code: str = "es_MX", components: list | None = None
    ) -> str: ...


Processor = Callable[..., "list[Reply] | Callable[..., list[Reply]]"]


def enqueue_replies(conn, replies: "list[Reply]") -> None:
    """Encola respuestas (texto o plantilla) para que el worker las entregue."""
    for reply in replies:
        repository.enqueue_outbound(
            conn,
            phone=reply.phone,
            text=reply.text,
            order_id=reply.order_id,
            message_type=MessageType.TEMPLATE if reply.template else MessageType.TEXT,
            template_payload=reply.template.to_json() if reply.template else None,
        )


def backoff_seconds(attempts: int) -> int:
    """5 s, 10 s, 20 s, 40 s... con tope de 5 min."""
    return min(5 * 2 ** max(attempts - 1, 0), MAX_BACKOFF_SECONDS)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class MessageWorker:
    def __init__(
        self,
        settings: Settings,
        sender: TextSender | None,
        processor: Processor = process_message,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.settings = settings
        self.sender = sender
        self.processor = processor
        self.clock = clock

    # ------------------------------------------------------------------ pasos

    def recover(self) -> int:
        with open_db(self.settings.database_path) as conn:
            count = repository.recover_processing(conn)
        if count:
            logger.warning("Recuperados %d mensaje(s) que quedaron en proceso tras un reinicio", count)
        return count

    def process_next_inbound(self) -> bool:
        with open_db(self.settings.database_path) as conn:
            message = repository.claim_next_message(conn, MessageDirection.INBOUND, _iso(self.clock()))
            if message is None:
                return False
            try:
                result = self.processor(conn, self.settings, message)
                with transaction(conn):
                    # El procesador puede devolver respuestas listas o un paso que escribe en la BD
                    # (pedido) y se ejecuta en la misma transacción que el encolado.
                    replies = result(conn) if callable(result) else result
                    enqueue_replies(conn, replies)
                    repository.mark_processed(conn, message.id)
            except Exception as exc:
                logger.exception("Error procesando el mensaje %s", message.id)
                self._retry_or_fail(conn, message, f"{type(exc).__name__}: {exc}", transient=True)
            return True

    def deliver_next_outbound(self) -> bool:
        if self.sender is None:
            return False
        with open_db(self.settings.database_path) as conn:
            message = repository.claim_next_message(conn, MessageDirection.OUTBOUND, _iso(self.clock()))
            if message is None:
                return False
            try:
                wamid = self._send(message)
            except Exception as exc:
                transient = getattr(exc, "transient", True)
                logger.warning("No se pudo enviar el mensaje %s: %s", message.id, exc)
                self._retry_or_fail(conn, message, str(exc), transient=transient)
            else:
                repository.mark_sent(conn, message.id, wamid)
                logger.info("Mensaje %s enviado", message.id)
            return True

    def _send(self, message: Message) -> str:
        if message.message_type is MessageType.TEMPLATE and message.template_payload:
            template = json.loads(message.template_payload)
            params = template.get("body_params") or []
            components = (
                [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}] if params else None
            )
            return self.sender.send_template(message.phone, template["name"], template["language"], components)
        return self.sender.send_text(message.phone, message.text or "")

    def run_once(self) -> bool:
        """Procesa todo lo listo en este momento. Devuelve True si hubo trabajo."""
        did_work = False
        while self.process_next_inbound():
            did_work = True
        while self.deliver_next_outbound():
            did_work = True
        return did_work

    def _retry_or_fail(self, conn, message: Message, error: str, *, transient: bool) -> None:
        attempts = message.attempts + 1
        if transient and attempts < self.settings.worker_max_attempts:
            next_at = self.clock() + timedelta(seconds=backoff_seconds(attempts))
            repository.schedule_retry(conn, message.id, attempts=attempts, next_attempt_at=_iso(next_at), error=error)
        else:
            repository.mark_failed(conn, message.id, attempts=attempts, error=error)
            logger.error("Mensaje %s marcado como fallido tras %d intento(s): %s", message.id, attempts, error)
            if message.direction is MessageDirection.INBOUND:
                repository.enqueue_outbound(conn, phone=message.phone, text=PROCESSING_FAILED_REPLY)

    # ------------------------------------------------------------------ ciclo

    async def run(self, stop: asyncio.Event) -> None:
        await asyncio.to_thread(self.recover)
        if self.sender is None:
            logger.warning("Worker sin cliente de WhatsApp: las respuestas quedarán en cola sin enviarse")
        logger.info("Worker iniciado (intervalo %.1fs)", self.settings.worker_poll_interval)
        while not stop.is_set():
            try:
                did_work = await asyncio.to_thread(self.run_once)
            except Exception:
                logger.exception("Error inesperado en el worker")
                did_work = False
            if not did_work:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.settings.worker_poll_interval)
                except TimeoutError:
                    pass
        logger.info("Worker detenido")
