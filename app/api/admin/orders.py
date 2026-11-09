"""Panel: gestión de pedidos por el asesor de ventas.

Lista con filtros y detalle de cada pedido con las acciones de seguimiento: programar
(o reprogramar) la entrega, marcar entregado y cancelar. No se envía nada al cliente:
ventas le confirma por su cuenta.
"""

from datetime import date, datetime
from html import escape
from urllib.parse import urlencode

from fastapi import HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.api.admin.common import (
    STATUS_LABELS,
    URGENCY_LABELS,
    Admin,
    csrf_token,
    fmt_date,
    page,
    read_form,
    redirect,
    router,
)
from app.config import Settings
from app.db import repository
from app.db.database import open_db, transaction
from app.models import OrderStatus
from app.phone import InvalidPhoneError, format_phone_display, normalize_phone
from app.services import order_tracking
from app.services.order_export import build_orders_workbook

FILTERS = (
    ("pendientes", "Pendientes", (OrderStatus.PENDING_SALES,)),
    ("programados", "Programados", (OrderStatus.SCHEDULED,)),
    ("entregados", "Entregados", (OrderStatus.CLOSED,)),
    ("cancelados", "Cancelados", (OrderStatus.CANCELLED,)),
    ("todos", "Todos", ()),
)
_FILTER_STATUSES = {key: tuple(s.value for s in statuses) for key, _, statuses in FILTERS}
EXPORT_LIMIT = 20_000
_STATE_CLASS = {"pending_sales": "warn", "scheduled": "", "closed": "", "cancelled": "off"}


def _state(value: str) -> str:
    label = escape(STATUS_LABELS.get(value, value))
    return f'<span class="badge {_STATE_CLASS.get(value, "")}">{label}</span>'


def _short(value: str | None) -> str:
    return date.fromisoformat(value).strftime("%d/%m/%Y") if value else "—"


def _urgency(row: dict) -> str:
    label = URGENCY_LABELS.get(row["urgency"], "—") if row["urgency"] else "—"
    return label + (f" ({row['urgency_note']})" if row["urgency_note"] else "")


@router.get("", include_in_schema=False)
def admin_home(_: Admin) -> RedirectResponse:
    return redirect("/admin/pedidos")


@router.get("/pedidos", response_class=HTMLResponse)
def orders_page(
    settings: Admin, estado: str = "pendientes", q: str = "", desde: str = "", hasta: str = "",
    msg: str = "", err: str = "",
) -> HTMLResponse:
    if estado not in _FILTER_STATUSES:
        estado = "pendientes"
    date_from, date_to = _valid_date(desde), _valid_date(hasta)
    with open_db(settings.database_path) as conn:
        counts = repository.count_orders_by_status(conn)
        orders = repository.list_orders_for_admin(
            conn, statuses=_FILTER_STATUSES[estado] or None, search=q or None,
            date_from=date_from, date_to=date_to, limit=500,
        )
    tz = settings.timezone
    if estado == "programados":  # próximas entregas primero
        orders.sort(key=lambda r: r["delivery_date"] or "")

    tabs = []
    for key, label, statuses in FILTERS:
        count = sum(counts.get(s.value, 0) for s in statuses) if statuses else sum(counts.values())
        cls = " class=on" if key == estado else ""
        query = f"?estado={key}" + (f"&q={escape(q)}" if q else "")
        tabs.append(f'<a href="/admin/pedidos{query}"{cls}>{label} <span class="count">{count}</span></a>')

    def row(o: dict) -> str:
        delivery = _short(o["delivery_date"]) if o["delivery_date"] else "—"
        return (
            f"<tr><td class=nowrap><a href='/admin/pedidos/{o['id']}'><b>{escape(o['folio'])}</b></a></td>"
            f"<td>{escape(o['customer_name'])}<br><span class='muted nowrap'>{escape(format_phone_display(o['customer_phone']))}</span></td>"
            f"<td>{escape(o['items'] or '—')}</td><td class=nowrap>{_short(o['requested_date'])}</td>"
            f"<td>{escape(_urgency(o))}</td><td class=nowrap>{delivery}</td><td>{_state(o['status'])}</td>"
            f"<td class='muted nowrap'>{fmt_date(o['confirmed_at'], tz)}</td>"
            f"<td><a class=button href='/admin/pedidos/{o['id']}'>Gestionar</a></td></tr>"
        )

    rows = "".join(row(o) for o in orders) or "<tr><td colspan=9 class=muted>No hay pedidos con estos filtros.</td></tr>"
    export_query = urlencode({"estado": estado, "q": q, "desde": date_from or "", "hasta": date_to or ""})
    body = f"""
<nav class="tabs">{''.join(tabs)}</nav>
<div class="card"><form method="get" class="row">
<input type="hidden" name="estado" value="{escape(estado)}">
<div><label for="q">Buscar</label><input id="q" name="q" value="{escape(q)}" placeholder="Folio, cliente o teléfono"></div>
<div><label for="d">Registrado desde</label><input id="d" type="date" name="desde" value="{escape(date_from or '')}"></div>
<div><label for="h">hasta</label><input id="h" type="date" name="hasta" value="{escape(date_to or '')}"></div>
<button>Filtrar</button><a class="button" href="/admin/pedidos/exportar.xlsx?{escape(export_query)}">Exportar a Excel</a>
</form>
<div class="table"><table><thead><tr><th>Folio</th><th>Cliente</th><th>Productos</th><th>Solicitada</th><th>Urgencia</th>
<th>Entrega</th><th>Estado</th><th>Registrado</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></div>"""
    return page("Pedidos", "pedidos", body, msg=msg, err=err)


def _valid_date(value: str) -> str | None:
    try:
        return date.fromisoformat(value).isoformat() if value else None
    except ValueError:
        return None


@router.get("/pedidos/exportar.xlsx", include_in_schema=False)
def export_orders(settings: Admin, estado: str = "todos", q: str = "", desde: str = "", hasta: str = "") -> Response:
    statuses = _FILTER_STATUSES.get(estado, ())
    with open_db(settings.database_path) as conn:
        orders = repository.list_orders_for_admin(
            conn, statuses=statuses or None, search=q or None,
            date_from=_valid_date(desde), date_to=_valid_date(hasta), limit=EXPORT_LIMIT,
        )
        items = repository.list_items_for_orders(conn, [o["id"] for o in orders])
    content = build_orders_workbook(orders, items, settings.timezone)
    filename = f"pedidos-{estado}-{datetime.now(settings.timezone):%Y%m%d-%H%M}.xlsx"
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.get("/pedidos/{order_id:int}", response_class=HTMLResponse)
def order_detail_page(order_id: int, settings: Admin, msg: str = "", err: str = "") -> HTMLResponse:
    with open_db(settings.database_path) as conn:
        order = repository.get_order(conn, order_id)
        if order is None or order.folio is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        [info] = [r for r in repository.list_orders_for_admin(conn, search=order.folio, limit=5) if r["id"] == order.id]
        messages = conn.execute(
            "SELECT direction, phone, text, message_type, status, last_error, created_at FROM messages "
            "WHERE order_id = ? ORDER BY id", (order.id,),
        ).fetchall()
    tz = settings.timezone
    token = escape(csrf_token(settings))
    today = datetime.now(tz).date().isoformat()
    open_order = order.status.value in order_tracking.OPEN_STATUSES

    facts = [
        ("Cliente", f"{escape(info['customer_name'])} · {escape(format_phone_display(info['customer_phone']))}"),
        ("Productos", escape(info["items"] or "—").replace("; ", "<br>")),
        ("Fecha solicitada", _short(info["requested_date"])),
        ("Urgencia", escape(_urgency(info))),
        ("Observaciones del cliente", escape(order.notes or "—")),
        ("Estado", _state(order.status.value)),
        ("Entrega programada", _short(info["delivery_date"]) if order.delivery_date else "—"),
        ("Nota interna / motivo", escape(order.sales_note or "—")),
        ("Registrado", fmt_date(order.confirmed_at, tz)),
    ]
    if order.delivered_at:
        facts.append(("Entregado", fmt_date(order.delivered_at, tz)))
    if order.cancelled_at:
        facts.append(("Cancelado", fmt_date(order.cancelled_at, tz)))
    facts_html = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts)

    if open_order:
        current = order.delivery_date.isoformat() if order.delivery_date else (
            order.requested_date.isoformat() if order.requested_date and order.requested_date.isoformat() >= today else today
        )
        actions = f"""
<div class="card"><h2>{"Reprogramar" if order.delivery_date else "Programar"} entrega</h2>
<form method="post" action="/admin/pedidos/{order.id}/programar" class="row">
<input type="hidden" name="csrf" value="{token}">
<div><label for="f">Fecha de entrega</label><input id="f" type="date" name="fecha" value="{current}" min="{today}" required></div>
<div class="grow"><label for="n">Nota interna (opcional)</label>
<input id="n" name="nota" maxlength="300" placeholder="Ej. llega por la tarde" value="{escape(order.sales_note or '')}"></div>
<button class="primary">Guardar fecha</button></form></div>
<div class="card two"><form method="post" action="/admin/pedidos/{order.id}/entregado">
<h2>Marcar como entregado</h2><input type="hidden" name="csrf" value="{token}">
<button class="primary">Pedido entregado</button></form>
<form method="post" action="/admin/pedidos/{order.id}/cancelar">
<h2>Cancelar pedido</h2><input type="hidden" name="csrf" value="{token}">
<label for="m">Motivo (opcional)</label><input id="m" name="motivo" maxlength="300" placeholder="Ej. sin existencia">
<button class="danger">Cancelar pedido</button></form></div>"""
    else:
        actions = '<div class="card muted">Este pedido ya está cerrado; no admite cambios.</div>'

    message_rows = "".join(_message_row(m, settings) for m in messages) or (
        "<tr><td colspan=4 class=muted>Sin mensajes.</td></tr>"
    )

    body = f"""<p><a href="/admin/pedidos">← Pedidos</a></p>
<div class="card"><dl class="facts">{facts_html}</dl></div>
{actions}
<div class="card"><h2>Mensajes del pedido</h2><div class="table"><table><thead><tr><th>Fecha</th><th>Para / de</th>
<th>Texto</th><th>Estado</th></tr></thead><tbody>{message_rows}</tbody></table></div></div>"""
    return page(f"Pedido {order.folio}", "pedidos", body, msg=msg, err=err)


_MESSAGE_STATUS = {"pending": "En cola", "processing": "Enviando", "processed": "Recibido", "sent": "Enviado"}


def _message_row(m, settings: Settings) -> str:
    if m["direction"] == "inbound":
        who = "Del cliente"
    else:
        try:
            to_sales = settings.sales_phone is not None and normalize_phone(m["phone"]) == settings.sales_phone
        except InvalidPhoneError:
            to_sales = False
        who = "A ventas" if to_sales else "Al cliente"
    if m["status"] == "failed":
        state = '<span class="badge off">Fallido</span>'
        if m["last_error"]:
            state += f"<br><span class=muted>{escape(m['last_error'])}</span>"
    else:
        state = escape(_MESSAGE_STATUS.get(m["status"], m["status"]))
    kind = "<span class=muted>[plantilla]</span> " if m["message_type"] == "template" else ""
    return (
        f"<tr><td class='muted nowrap'>{fmt_date(m['created_at'], settings.timezone)}</td><td>{who}</td>"
        f"<td>{kind}{escape((m['text'] or '')[:400])}</td><td>{state}</td></tr>"
    )


async def _apply(order_id: int, request: Request, settings: Settings, action) -> RedirectResponse:
    form = await read_form(request, settings)
    today = datetime.now(settings.timezone).date()

    def run() -> order_tracking.ActionResult:
        with open_db(settings.database_path) as conn, transaction(conn):
            order = repository.get_order(conn, order_id)
            if order is None or order.folio is None:
                raise order_tracking.OrderActionError("El pedido no existe.")
            return action(conn, order, form, today=today)

    path = f"/admin/pedidos/{order_id}"
    try:
        result = await run_in_threadpool(run)
    except order_tracking.OrderActionError as exc:
        return redirect(path, err=str(exc))
    return redirect(path, msg=result.summary)


@router.post("/pedidos/{order_id:int}/programar", include_in_schema=False)
async def schedule(order_id: int, request: Request, settings: Admin) -> RedirectResponse:
    def action(conn, order, form, *, today):
        try:
            delivery = date.fromisoformat(form.get("fecha", ""))
        except ValueError:
            raise order_tracking.OrderActionError("Fecha de entrega inválida.") from None
        return order_tracking.schedule_delivery(
            conn, order, delivery, form.get("nota"), today=today
        )

    return await _apply(order_id, request, settings, action)


@router.post("/pedidos/{order_id:int}/entregado", include_in_schema=False)
async def delivered(order_id: int, request: Request, settings: Admin) -> RedirectResponse:
    def action(conn, order, form, *, today):
        return order_tracking.mark_delivered(conn, order)

    return await _apply(order_id, request, settings, action)


@router.post("/pedidos/{order_id:int}/cancelar", include_in_schema=False)
async def cancel(order_id: int, request: Request, settings: Admin) -> RedirectResponse:
    def action(conn, order, form, *, today):
        return order_tracking.cancel_order(conn, order, form.get("motivo"))

    return await _apply(order_id, request, settings, action)
