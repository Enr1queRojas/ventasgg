"""Aplicación FastAPI: arranque, rutas y endpoint de salud."""

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.admin import router as admin_router
from app.api.webhook import router as webhook_router
from app.config import Settings, get_settings
from app.db.database import check_db, init_db, open_db
from app.integrations.whatsapp import WhatsAppClient, WhatsAppConfigError, WhatsAppError
from app.workers.message_worker import MessageWorker

logger = logging.getLogger("sales_bot")

_VERIFY_TOKEN_IN_QUERY = re.compile(r"(hub[._]verify_token=)[^&\s]*")


class RedactVerifyTokenFilter(logging.Filter):
    """Evita que el log de acceso de uvicorn escriba el verify token de Meta (va en la URL)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _VERIFY_TOKEN_IN_QUERY.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args
            )
        return True


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactVerifyTokenFilter) for f in access_logger.filters):
        access_logger.addFilter(RedactVerifyTokenFilter())


def check_whatsapp(client: WhatsAppClient) -> bool:
    """Comprueba token y PHONE_NUMBER_ID al arrancar; un error se registra pero no detiene el servicio."""
    try:
        info = client.get_phone_number_info()
    except WhatsAppError as exc:
        logger.error("No se pudo validar la conexión con WhatsApp (revisa token y PHONE_NUMBER_ID): %s", exc)
        return False
    logger.info(
        "WhatsApp conectado: %s (%s), calidad %s",
        info.get("display_phone_number", "?"), info.get("verified_name", "?"), info.get("quality_rating", "?"),
    )
    return True


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)
    with open_db(settings.database_path) as conn:
        init_db(conn)
    logger.info("Servicio iniciado (env=%s, db=%s)", settings.app_env, settings.database_path)

    whatsapp: WhatsAppClient | None = None
    worker_task: asyncio.Task | None = None
    stop = asyncio.Event()
    if settings.worker_enabled:
        try:
            whatsapp = WhatsAppClient.from_settings(settings)
        except WhatsAppConfigError as exc:
            logger.warning("%s", exc)
        else:
            await asyncio.to_thread(check_whatsapp, whatsapp)
        worker_task = asyncio.create_task(MessageWorker(settings, whatsapp).run(stop), name="message-worker")

    yield

    stop.set()
    if worker_task is not None:
        try:
            await asyncio.wait_for(worker_task, timeout=20)
        except TimeoutError:
            logger.warning("El worker no terminó a tiempo; se cancela")
            worker_task.cancel()
    if whatsapp is not None:
        whatsapp.close()
    logger.info("Servicio detenido")


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="WhatsApp Sales Bot", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings or get_settings()
    app.include_router(webhook_router)
    app.include_router(admin_router)

    @app.get("/health")
    def health(request: Request) -> JSONResponse:
        try:
            with open_db(request.app.state.settings.database_path) as conn:
                check_db(conn)
        except Exception:
            logger.exception("Health check: la base de datos no responde")
            return JSONResponse({"status": "error", "database": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok", "database": "ok"})

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    _settings = get_settings()
    # 0.0.0.0 para que Docker pueda publicar el puerto; en la VM se publica solo en 127.0.0.1.
    uvicorn.run("app.main:app", host="0.0.0.0", port=_settings.app_port)
