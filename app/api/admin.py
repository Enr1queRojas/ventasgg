"""Panel de administración: clientes (alta, edición, activar/desactivar), productos y pedidos.

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
from html import escape
from typing import Annotated
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import Settings
from app.db import repository
from app.db.database import open_db, transaction
from app.models import Customer, OrderStatus, Urgency
from app.phone import InvalidPhoneError, format_phone_display
from app.services.admin_commands import parse_admin_phone

logger = logging.getLogger("sales_bot.admin")

_basic = HTTPBasic(realm="Ventas GG", auto_error=False)

STATUS_LABELS = {
    OrderStatus.DRAFT: "Borrador",
    OrderStatus.AWAITING_CONFIRMATION: "Esperando confirmación",
    OrderStatus.PENDING_SALES: "Pendiente de ventas",
    OrderStatus.CANCELLED: "Cancelado",
    OrderStatus.CLOSED: "Cerrado",
}
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


async def _form(request: Request, settings: Settings) -> dict[str, str]:
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    data = {k: v[0] for k, v in parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True).items()}
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
"""

_NAV = (("clientes", "Clientes"), ("productos", "Productos"), ("pedidos", "Pedidos"))


def _page(title: str, active: str, body: str, *, msg: str = "", err: str = "") -> HTMLResponse:
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


def _redirect(path: str, **params: str) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=status.HTTP_303_SEE_OTHER)


def _badge(active: bool) -> str:
    return '<span class="badge">Activo</span>' if active else '<span class="badge off">Inactivo</span>'


def _date(value: str | None) -> str:
    return escape(value[:16].replace("T", " ")) if value else "—"


# --------------------------------------------------------------------------- rutas


@router.get("", include_in_schema=False)
def admin_home(_: Admin) -> RedirectResponse:
    return _redirect("/admin/clientes")


@router.get("/clientes", response_class=HTMLResponse)
def customers_page(settings: Admin, q: str = "", estado: str = "todos", msg: str = "", err: str = "") -> HTMLResponse:
    with open_db(settings.database_path) as conn:
        customers = repository.list_customers(conn, search=q or None, limit=1000)
        active, total = repository.count_customers(conn)
        orders = dict(conn.execute(
            "SELECT customer_id, COUNT(*) FROM orders WHERE status IN ('pending_sales', 'closed') GROUP BY customer_id"
        ).fetchall())
    if estado == "activos":
        customers = [c for c in customers if c.active]
    elif estado == "inactivos":
        customers = [c for c in customers if not c.active]
    token = escape(csrf_token(settings))

    def row(c: Customer) -> str:
        badge = _badge(c.active)
        toggle = (
            f'<form class="inline" method="post" action="/admin/clientes/{c.id}/estado">'
            f'<input type="hidden" name="csrf" value="{token}"><input type="hidden" name="active" value="{0 if c.active else 1}">'
            f'<button class="{"danger" if c.active else ""}">{"Desactivar" if c.active else "Activar"}</button></form>'
        )
        rename = (
            f'<form class="inline row" method="post" action="/admin/clientes">'
            f'<input type="hidden" name="csrf" value="{token}"><input type="hidden" name="phone" value="{escape(c.phone)}">'
            f'<input name="name" value="{escape(c.name)}" aria-label="Nombre" required maxlength="120">'
            f'<button>Guardar</button></form>'
        )
        return (
            f"<tr><td>{rename}</td><td class=nowrap>{escape(format_phone_display(c.phone))}</td><td>{badge}</td>"
            f"<td>{orders.get(c.id, 0)}</td><td class='muted nowrap'>{_date(c.created_at)}</td><td>{toggle}</td></tr>"
        )

    options = "".join(
        f'<option value="{v}"{" selected" if v == estado else ""}>{label}</option>'
        for v, label in (("todos", "Todos"), ("activos", "Activos"), ("inactivos", "Inactivos"))
    )
    rows = "".join(row(c) for c in customers) or "<tr><td colspan=6 class=muted>Sin resultados.</td></tr>"
    body = f"""
<div class="card"><form method="post" action="/admin/clientes" class="row">
<input type="hidden" name="csrf" value="{token}">
<div><label for="n">Nombre</label><input id="n" name="name" required maxlength="120" placeholder="Nombre del cliente"></div>
<div><label for="p">Teléfono</label><input id="p" name="phone" required placeholder="871 123 4567" inputmode="tel"></div>
<button class="primary">Agregar cliente</button></form>
<p class="muted">10 dígitos se toman como número de México. Si el teléfono ya existe, se actualiza el nombre y se reactiva.</p></div>
<div class="card"><form method="get" class="row">
<div><label for="q">Buscar</label><input id="q" name="q" value="{escape(q)}" placeholder="Nombre o teléfono"></div>
<div><label for="e">Estado</label><select id="e" name="estado">{options}</select></div>
<button>Filtrar</button></form>
<p class="muted">{active} activos de {total} clientes.</p>
<div class="table"><table><thead><tr><th>Nombre</th><th>Teléfono</th><th>Estado</th><th>Pedidos</th><th>Alta</th><th></th></tr></thead>
<tbody>{rows}</tbody></table></div></div>"""
    return _page("Clientes", "clientes", body, msg=msg, err=err)


@router.post("/clientes", include_in_schema=False)
async def save_customer(request: Request, settings: Admin) -> RedirectResponse:
    form = await _form(request, settings)
    name = form.get("name", "").strip()[:120]
    try:
        phone = parse_admin_phone(form.get("phone", ""))
    except InvalidPhoneError:
        return _redirect("/admin/clientes", err=f"Teléfono inválido: {form.get('phone', '')}")
    if not name:
        return _redirect("/admin/clientes", err="Falta el nombre")
    if settings.sales_phone and phone == settings.sales_phone:
        return _redirect("/admin/clientes", err="Ese es el número de ventas; no se puede registrar como cliente")

    def save() -> tuple[Customer, bool]:
        with open_db(settings.database_path) as conn, transaction(conn):
            existed = repository.get_customer_by_phone(conn, phone, active_only=False) is not None
            return repository.upsert_customer(conn, name=name, phone=phone, active=True), existed

    customer, existed = await run_in_threadpool(save)
    logger.info("Cliente %s %s desde el panel", customer.id, "actualizado" if existed else "registrado")
    verb = "actualizado" if existed else "registrado"
    return _redirect("/admin/clientes", msg=f"Cliente {verb}: {customer.name} ({format_phone_display(customer.phone)})")


@router.post("/clientes/{customer_id}/estado", include_in_schema=False)
async def toggle_customer(customer_id: int, request: Request, settings: Admin) -> RedirectResponse:
    form = await _form(request, settings)
    active = form.get("active") == "1"

    def update() -> Customer | None:
        with open_db(settings.database_path) as conn, transaction(conn):
            customer = repository.get_customer(conn, customer_id)
            return repository.set_customer_active(conn, customer.phone, active) if customer else None

    customer = await run_in_threadpool(update)
    if customer is None:
        return _redirect("/admin/clientes", err="El cliente ya no existe")
    logger.info("Cliente %s %s desde el panel", customer.id, "activado" if active else "desactivado")
    return _redirect("/admin/clientes", msg=f"{customer.name}: {'activado' if active else 'desactivado'}")


@router.get("/productos", response_class=HTMLResponse)
def products_page(settings: Admin) -> HTMLResponse:
    with open_db(settings.database_path) as conn:
        products = repository.list_products(conn, active_only=False)
    rows = "".join(
        f"<tr><td class=nowrap>{escape(p.sku)}</td><td>{escape(p.name)}</td>"
        f"<td class=muted>{escape(', '.join(p.aliases)) or '—'}</td><td>{escape(', '.join(p.allowed_units))}</td>"
        f"<td>{'' if p.kg_per_bag is None else f'{p.kg_per_bag:g}'}</td>"
        f"<td>{_badge(p.active)}</td></tr>"
        for p in products
    ) or "<tr><td colspan=6 class=muted>No hay productos. Cárgalos con scripts.seed_db.</td></tr>"
    body = f"""
<div class="card"><p class="muted">{len(products)} productos. El catálogo se edita en <code>data/seed/products.csv</code>
y se carga con <code>python -m scripts.seed_db</code>.</p>
<div class="table"><table><thead><tr><th>Código</th><th>Nombre</th><th>Alias</th><th>Unidades</th><th>Kg/bulto</th>
<th>Estado</th></tr></thead><tbody>{rows}</tbody></table></div></div>"""
    return _page("Productos", "productos", body)


@router.get("/pedidos", response_class=HTMLResponse)
def orders_page(settings: Admin) -> HTMLResponse:
    with open_db(settings.database_path) as conn:
        orders = repository.list_recent_orders(conn, limit=200)

    def row(o: dict) -> str:
        urgency = URGENCY_LABELS.get(o["urgency"], "—") if o["urgency"] else "—"
        if o["urgency_note"]:
            urgency += f" ({o['urgency_note']})"
        return (
            f"<tr><td class=nowrap>{escape(o['folio'] or '—')}</td>"
            f"<td>{escape(o['customer_name'])}<br><span class='muted nowrap'>{escape(format_phone_display(o['customer_phone']))}</span></td>"
            f"<td>{escape(o['items'] or '—')}</td><td class=nowrap>{escape(o['requested_date'] or '—')}</td>"
            f"<td>{escape(urgency)}</td><td>{escape(STATUS_LABELS.get(o['status'], o['status']))}</td>"
            f"<td class=muted>{escape(o['notes'] or '')}</td><td class='muted nowrap'>{_date(o['confirmed_at'] or o['created_at'])}</td></tr>"
        )

    rows = "".join(row(o) for o in orders) or "<tr><td colspan=8 class=muted>Todavía no hay pedidos.</td></tr>"
    body = f"""
<div class="card"><p class="muted">Últimos {len(orders)} pedidos (horas en UTC).</p>
<div class="table"><table><thead><tr><th>Folio</th><th>Cliente</th><th>Productos</th><th>Fecha solicitada</th>
<th>Urgencia</th><th>Estado</th><th>Observaciones</th><th>Registrado</th></tr></thead><tbody>{rows}</tbody></table></div></div>"""
    return _page("Pedidos", "pedidos", body)
