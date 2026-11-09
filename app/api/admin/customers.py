"""Panel: clientes."""

from html import escape

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.admin.common import (
    Admin,
    badge,
    csrf_token,
    fmt_date,
    logger,
    page,
    read_form,
    redirect,
    router,
)
from app.db import repository
from app.db.database import open_db, transaction
from app.models import Customer
from app.phone import InvalidPhoneError, format_phone_display
from app.services.admin_commands import parse_admin_phone


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
    tz = settings.timezone

    def row(c: Customer) -> str:
        state = badge(c.active)
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
            f"<tr><td>{rename}</td><td class=nowrap>{escape(format_phone_display(c.phone))}</td><td>{state}</td>"
            f"<td>{orders.get(c.id, 0)}</td><td class='muted nowrap'>{fmt_date(c.created_at, tz)}</td><td>{toggle}</td></tr>"
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
    return page("Clientes", "clientes", body, msg=msg, err=err)


@router.post("/clientes", include_in_schema=False)
async def save_customer(request: Request, settings: Admin) -> RedirectResponse:
    form = await read_form(request, settings)
    name = form.get("name", "").strip()[:120]
    try:
        phone = parse_admin_phone(form.get("phone", ""))
    except InvalidPhoneError:
        return redirect("/admin/clientes", err=f"Teléfono inválido: {form.get('phone', '')}")
    if not name:
        return redirect("/admin/clientes", err="Falta el nombre")
    if settings.sales_phone and phone == settings.sales_phone:
        return redirect("/admin/clientes", err="Ese es el número de ventas; no se puede registrar como cliente")

    def save() -> tuple[Customer, bool]:
        with open_db(settings.database_path) as conn, transaction(conn):
            existed = repository.get_customer_by_phone(conn, phone, active_only=False) is not None
            return repository.upsert_customer(conn, name=name, phone=phone, active=True), existed

    customer, existed = await run_in_threadpool(save)
    logger.info("Cliente %s %s desde el panel", customer.id, "actualizado" if existed else "registrado")
    verb = "actualizado" if existed else "registrado"
    return redirect("/admin/clientes", msg=f"Cliente {verb}: {customer.name} ({format_phone_display(customer.phone)})")


@router.post("/clientes/{customer_id}/estado", include_in_schema=False)
async def toggle_customer(customer_id: int, request: Request, settings: Admin) -> RedirectResponse:
    form = await read_form(request, settings)
    active = form.get("active") == "1"

    def update() -> Customer | None:
        with open_db(settings.database_path) as conn, transaction(conn):
            customer = repository.get_customer(conn, customer_id)
            return repository.set_customer_active(conn, customer.phone, active) if customer else None

    customer = await run_in_threadpool(update)
    if customer is None:
        return redirect("/admin/clientes", err="El cliente ya no existe")
    logger.info("Cliente %s %s desde el panel", customer.id, "activado" if active else "desactivado")
    return redirect("/admin/clientes", msg=f"{customer.name}: {'activado' if active else 'desactivado'}")
