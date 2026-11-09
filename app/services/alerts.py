"""Alertas operativas: envíos fallidos, problemas con el token de WhatsApp, etc.

Se mandan por HTTP a ``ALERT_WEBHOOK_URL``:

- **ntfy** (``https://ntfy.sh/<tema>`` o un servidor propio): notificación push al celular
  con la app ntfy. Se detecta por el host y se envía como texto con título.
- **Slack / Discord / otros**: JSON con ``text`` (Slack) y ``content`` (Discord).

Sin URL configurada solo se registran en el log. Cada tipo de alerta tiene un tiempo de
espera (``cooldown``) para no inundar el canal cuando un problema se repite.
"""

import logging
import threading
import time
from collections.abc import Callable
from urllib.parse import urlparse

import httpx

from app.config import Settings

logger = logging.getLogger("sales_bot.alerts")

DEFAULT_COOLDOWN = 30 * 60


class Alerter:
    def __init__(
        self,
        url: str | None,
        *,
        source: str = "Ventas GG",
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.url = url
        self.source = source
        self.clock = clock
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=10, transport=transport) if url else None
        self._ntfy = bool(url) and "ntfy" in (urlparse(url).hostname or "")

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs) -> "Alerter":
        url = settings.alert_webhook_url.get_secret_value() if settings.alert_webhook_url else None
        return cls(url, **kwargs)

    def close(self) -> None:
        if self._http is not None:
            self._http.close()

    def _should_send(self, key: str, cooldown: float) -> bool:
        now = self.clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < cooldown:
                return False
            self._last[key] = now
            return True

    def send(self, key: str, title: str, message: str, *, cooldown: float = DEFAULT_COOLDOWN, urgent: bool = False) -> bool:
        """Envía la alerta si no se mandó una con la misma ``key`` dentro de ``cooldown``. Nunca lanza."""
        if not self._should_send(key, cooldown):
            return False
        logger.warning("ALERTA %s: %s", title, message)
        if self._http is None:
            return False
        try:
            if self._ntfy:  # publicación JSON de ntfy: acepta acentos en el título (los encabezados no)
                parsed = urlparse(self.url)
                response = self._http.post(
                    f"{parsed.scheme}://{parsed.netloc}",
                    json={
                        "topic": parsed.path.strip("/"), "title": f"{self.source}: {title}", "message": message,
                        "priority": 5 if urgent else 4, "tags": ["rotating_light" if urgent else "warning"],
                    },
                )
            else:
                text = f"*{self.source}: {title}*\n{message}"
                response = self._http.post(self.url, json={"text": text, "content": text[:2000]})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("No se pudo enviar la alerta «%s»: %s", title, type(exc).__name__)
            return False
        return True


_alerters: dict[str | None, Alerter] = {}


def get_alerter(settings: Settings) -> Alerter:
    """Una instancia por URL (comparte el control de repetición entre el webhook y el worker)."""
    url = settings.alert_webhook_url.get_secret_value() if settings.alert_webhook_url else None
    if url not in _alerters:
        _alerters[url] = Alerter(url)
    return _alerters[url]
