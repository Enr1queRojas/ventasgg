"""Panel de administración: piezas comunes (autenticación, CSRF, plantilla HTML, formato).

HTML generado en el servidor, sin JavaScript ni dependencias extra. Protegido con HTTP
Basic (``ADMIN_USERNAME`` / ``ADMIN_PASSWORD``); sin contraseña el panel responde 404.
No se publica en el túnel (solo ``/webhook*``): se abre por SSH, ver README.

Los formularios llevan un token derivado de la contraseña para que otra página abierta
en el mismo navegador no pueda enviar cambios aprovechando la sesión Basic (CSRF).
"""

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime
from html import escape
from typing import Annotated
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import Settings
from app.models import Urgency
from app.services.admin_commands import STATUS_LABELS  # noqa: F401  (reexportado para las páginas)

logger = logging.getLogger("sales_bot.admin")

_basic = HTTPBasic(realm="Ventas GG", auto_error=False)

URGENCY_LABELS = {Urgency.NORMAL: "Normal", Urgency.HIGH: "Alta", Urgency.CRITICAL: "Crítica"}
MAX_FORM_BYTES = 10_000

_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def require_admin(
    request: Request, credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)]
) -> Settings:
    settings = _settings(request)
    if settings.admin_password is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    ok = credentials is not None and (
        secrets.compare_digest(credentials.username.encode(), settings.admin_username.encode())
        & secrets.compare_digest(credentials.password.encode(), settings.admin_password.get_secret_value().encode())
    )
    if not ok:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="Credenciales inválidas",
            headers={"WWW-Authenticate": 'Basic realm="Ventas GG"'},
        )
    return settings


Admin = Annotated[Settings, Depends(require_admin)]
router = APIRouter(prefix="/admin", tags=["admin"])


def csrf_token(settings: Settings) -> str:
    key = settings.admin_password.get_secret_value().encode()
    return hmac.new(key, b"ventasgg-admin-csrf", hashlib.sha256).hexdigest()


class FormData(dict[str, str]):
    """Campos del formulario (primer valor de cada uno) y, con ``getlist``, todos los valores."""

    def __init__(self, values: dict[str, list[str]]):
        super().__init__({k: v[0] for k, v in values.items()})
        self._values = values

    def getlist(self, key: str) -> list[str]:
        return list(self._values.get(key, []))


async def read_form(request: Request, settings: Settings) -> FormData:
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    data = FormData(parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True))
    if not hmac.compare_digest(data.get("csrf", ""), csrf_token(settings)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Formulario vencido; recarga la página")
    return data


# --------------------------------------------------------------------------- HTML

_CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--text:#1d2329;--muted:#66707a;--line:#dde1e6;
--accent:#1f7a4d;--danger:#b3261e;--ok-bg:#e6f4ea;--err-bg:#fce8e6}
@media (prefers-color-scheme:dark){:root{--bg:#15181b;--card:#1e2226;--text:#e6e8ea;--muted:#9aa3ab;
--line:#30363c;--accent:#4cc38a;--danger:#f28b82;--ok-bg:#1d3326;--err-bg:#3a1f1d}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
background:var(--bg);color:var(--text)}header{background:var(--card);border-bottom:1px solid var(--line);
padding:12px 16px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}header b{font-size:17px}
nav a{color:var(--muted);text-decoration:none;margin-right:16px;padding:4px 0}
nav a.on{color:var(--text);border-bottom:2px solid var(--accent)}main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0 16px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px;margin-bottom:16px}.table{overflow-x:auto}table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:13px;color:var(--muted);font-weight:600;white-space:nowrap}tr:last-child td{border-bottom:0}
.muted{color:var(--muted)}.nowrap{white-space:nowrap}form.inline{display:inline}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:end}label{font-size:13px;color:var(--muted);display:block}
input,select{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);
color:var(--text);min-width:0}button{font:inherit;padding:7px 12px;border-radius:7px;border:1px solid var(--line);
background:var(--card);color:var(--text);cursor:pointer}button.primary{background:var(--accent);border-color:var(--accent);
color:#fff}button.danger{color:var(--danger)}.badge{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;
background:var(--ok-bg)}.badge.off{background:var(--err-bg)}.flash{padding:10px 14px;border-radius:8px;margin-bottom:16px;
background:var(--ok-bg)}.flash.err{background:var(--err-bg)}
.badge.warn{background:#fff4ce;color:#5c4400}@media (prefers-color-scheme:dark){.badge.warn{background:#3d3317;color:#f5d77a}}
a{color:var(--accent)}a.button{display:inline-block;padding:7px 12px;border-radius:7px;border:1px solid var(--line);
background:var(--card);color:var(--text);text-decoration:none}h2{font-size:16px;margin:0 0 12px}
nav.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}nav.tabs a{padding:6px 12px;border-radius:99px;
border:1px solid var(--line);background:var(--card);color:var(--text);text-decoration:none}
nav.tabs a.on{background:var(--accent);border-color:var(--accent);color:#fff}.count{opacity:.75;font-size:13px}
dl.facts{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:0}dl.facts dt{color:var(--muted)}
dl.facts dd{margin:0}.card.two{display:grid;grid-template-columns:1fr 1fr;gap:24px}
.card.two form{display:flex;flex-direction:column;gap:8px;align-items:flex-start}.grow{flex:1;min-width:200px}
.grow input{width:100%}label.check{display:flex;gap:6px;align-items:center;color:var(--text);font-size:14px}
@media (max-width:640px){.card.two{grid-template-columns:1fr}dl.facts{grid-template-columns:1fr}dl.facts dt{margin-top:6px}}
"""

_NAV = (("pedidos", "Pedidos"), ("clientes", "Clientes"), ("productos", "Productos"), ("mensajes", "Mensajes"))


def page(title: str, active: str, body: str, *, msg: str = "", err: str = "") -> HTMLResponse:
    nav = "".join(
        f'<a href="/admin/{key}"{" class=on" if key == active else ""}>{label}</a>' for key, label in _NAV
    )
    flash = (f'<div class="flash err">{escape(err)}</div>' if err else "") + (
        f'<div class="flash">{escape(msg)}</div>' if msg else ""
    )
    html = (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)} · Ventas GG</title><style>{_CSS}</style></head><body>"
        f"<header><b>Ventas GG</b><nav>{nav}</nav></header><main><h1>{escape(title)}</h1>{flash}{body}</main>"
        "</body></html>"
    )
    return HTMLResponse(html, headers=_SECURITY_HEADERS)


def redirect(path: str, **params: str) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=status.HTTP_303_SEE_OTHER)


def badge(active: bool) -> str:
    return '<span class="badge">Activo</span>' if active else '<span class="badge off">Inactivo</span>'


def fmt_date(value: str | None, tz: ZoneInfo) -> str:
    """Fecha UTC de la base (ISO 8601) en la zona horaria del negocio."""
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return escape(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tz).strftime("%d/%m/%Y %H:%M")


# --------------------------------------------------------------------------- rutas
