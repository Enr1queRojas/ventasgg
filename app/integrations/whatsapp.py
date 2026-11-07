"""Cliente HTTP para enviar mensajes con Meta WhatsApp Cloud API."""

import logging
from typing import Any

import httpx

from app.config import Settings
from app.phone import InvalidPhoneError, normalize_phone

logger = logging.getLogger("sales_bot.whatsapp")

GRAPH_URL = "https://graph.facebook.com"

# Códigos de error de Meta que conviene reintentar (límites de tasa, fallas temporales).
TRANSIENT_ERROR_CODES = {1, 2, 4, 17, 341, 80007, 130429, 131000, 131016, 131048, 131056, 133004}
RECIPIENT_NOT_ALLOWED = 131030  # número no está en la lista de destinatarios de prueba


class WhatsAppConfigError(RuntimeError):
    """Faltan variables de entorno para usar la API."""


class WhatsAppError(Exception):
    def __init__(self, message: str, *, status: int | None = None, code: int | None = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.transient = transient


def recipient_candidates(wa_id: str) -> list[str]:
    """Números a intentar al enviar.

    Para móviles de México Meta reporta ``521`` + 10 dígitos, pero enviar a esa forma
    suele fallar con 131030 en números de prueba; primero se intenta ``52`` + 10 dígitos
    y, si Meta lo rechaza por no estar autorizado, la forma original.
    """
    digits = "".join(c for c in wa_id if c.isdigit())
    try:
        canonical = normalize_phone(digits)
    except InvalidPhoneError:
        return [digits]
    return list(dict.fromkeys([canonical, digits]))


class WhatsAppClient:
    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        api_version: str,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.phone_number_id = phone_number_id
        self._http = httpx.Client(
            base_url=f"{GRAPH_URL}/{api_version}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> "WhatsAppClient":
        missing = [
            name
            for name, value in (
                ("WHATSAPP_ACCESS_TOKEN", settings.whatsapp_access_token),
                ("WHATSAPP_PHONE_NUMBER_ID", settings.whatsapp_phone_number_id),
                ("WHATSAPP_API_VERSION", settings.whatsapp_api_version),
            )
            if value is None
        ]
        if missing:
            raise WhatsAppConfigError(f"Faltan variables para WhatsApp: {', '.join(missing)}")
        return cls(
            access_token=settings.whatsapp_access_token.get_secret_value(),
            phone_number_id=settings.whatsapp_phone_number_id,
            api_version=settings.whatsapp_api_version,
            **kwargs,
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> dict:
        try:
            response = self._http.request(method, path, json=json, params=params)
        except httpx.TimeoutException as exc:
            # En un envío, el mensaje pudo haberse aceptado: reintentar puede duplicarlo (al-menos-una-vez).
            raise WhatsAppError(f"Timeout al llamar a Meta: {exc}", transient=True) from exc
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"Error de red al llamar a Meta: {exc}", transient=True) from exc

        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.is_success:
            return data

        error = data.get("error") or {}
        code = error.get("code")
        details = (error.get("error_data") or {}).get("details")
        message = f"Meta HTTP {response.status_code}, code={code}: {error.get('message') or response.text[:200]}"
        if details:
            message += f" ({details})"
        transient = response.status_code == 429 or response.status_code >= 500 or code in TRANSIENT_ERROR_CODES
        raise WhatsAppError(message, status=response.status_code, code=code, transient=transient)

    def _send(self, to: str, payload: dict) -> str:
        candidates = recipient_candidates(to)
        for i, recipient in enumerate(candidates):
            try:
                data = self._request(
                    "POST",
                    f"/{self.phone_number_id}/messages",
                    json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": recipient, **payload},
                )
            except WhatsAppError as exc:
                if exc.code == RECIPIENT_NOT_ALLOWED and i < len(candidates) - 1:
                    logger.info("Meta rechazó el destinatario con 131030; se reintenta con la forma alternativa")
                    continue
                raise
            try:
                return data["messages"][0]["id"]
            except (KeyError, IndexError, TypeError) as exc:
                raise WhatsAppError(f"Respuesta inesperada de Meta: {data}") from exc
        raise WhatsAppError("Sin destinatario válido")  # inalcanzable: candidates nunca está vacío

    def send_text(self, to: str, body: str) -> str:
        """Envía texto libre (solo válido dentro de la ventana de 24 h del cliente). Devuelve el wamid."""
        return self._send(to, {"type": "text", "text": {"preview_url": False, "body": body}})

    def send_template(self, to: str, name: str, language_code: str = "es_MX", components: list | None = None) -> str:
        """Envía una plantilla aprobada (necesaria para iniciar conversación fuera de la ventana de 24 h)."""
        template: dict[str, Any] = {"name": name, "language": {"code": language_code}}
        if components:
            template["components"] = components
        return self._send(to, {"type": "template", "template": template})

    def get_phone_number_info(self) -> dict:
        """Datos del número emisor; sirve para comprobar token y PHONE_NUMBER_ID."""
        return self._request(
            "GET",
            f"/{self.phone_number_id}",
            params={"fields": "display_phone_number,verified_name,quality_rating,code_verification_status"},
        )
