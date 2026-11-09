"""Panel: catálogo de productos (alta, edición y activar/desactivar).

El código (SKU) no cambia después del alta: identifica al producto en los pedidos y en
``products.csv``. Desactivar un producto lo saca de las conversaciones sin borrar su
historial de pedidos.
"""

import re
import sqlite3
from html import escape

from fastapi import HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.admin.common import (
    Admin,
    badge,
    csrf_token,
    logger,
    page,
    read_form,
    redirect,
    router,
)
from app.db import repository
from app.db.database import open_db, transaction
from app.models import Product, Unit

UNIT_LABELS = {Unit.KG: "Kilos", Unit.BULTOS: "Bultos", Unit.PIEZAS: "Piezas (cajas, pacas…)"}
_SKU = re.compile(r"^[A-Z0-9][A-Z0-9_\-]{0,39}$")
_ALIAS_SPLIT = re.compile(r"[|,\n]")


class ProductFormError(ValueError):
    pass


def parse_product_form(form, *, sku: str | None = None) -> dict:
    """Valida el formulario y devuelve los argumentos de ``repository.upsert_product``."""
    sku = (sku or form.get("sku", "")).strip().upper()
    if not _SKU.match(sku):
        raise ProductFormError("Código inválido: usa letras, números y guiones (ej. MAIZ-BLANCO).")
    name = " ".join(form.get("name", "").split())[:120]
    if not name:
        raise ProductFormError("Falta el nombre del producto.")
    try:
        units = [Unit(u) for u in form.getlist("units")]
    except ValueError:
        raise ProductFormError("Unidad no válida.") from None
    if not units:
        raise ProductFormError("Elige al menos una unidad (kilos, bultos o piezas).")
    raw_kg = form.get("kg_per_bag", "").strip().replace(",", ".")
    try:
        kg_per_bag = float(raw_kg) if raw_kg else None
    except ValueError:
        raise ProductFormError(f"Kilos por bulto inválido: {raw_kg}") from None
    if kg_per_bag is not None and not 0 < kg_per_bag <= 1000:
        raise ProductFormError("Kilos por bulto debe ser mayor que 0 (y razonable).")
    aliases = [a.strip()[:60] for a in _ALIAS_SPLIT.split(form.get("aliases", "")) if a.strip()][:30]
    return dict(sku=sku, name=name, allowed_units=units, aliases=aliases, kg_per_bag=kg_per_bag,
                active=form.get("active", "1") == "1")


def _form(token: str, product: Product | None) -> str:
    units = product.allowed_units if product else (Unit.KG, Unit.BULTOS)
    checks = "".join(
        f'<label class="check"><input type="checkbox" name="units" value="{u.value}"{" checked" if u in units else ""}> {label}</label>'
        for u, label in UNIT_LABELS.items()
    )
    sku_field = (
        f'<div><label>Código</label><input value="{escape(product.sku)}" disabled></div>' if product else
        '<div><label for="s">Código</label><input id="s" name="sku" required maxlength="40" placeholder="MAIZ-BLANCO" '
        'pattern="[A-Za-z0-9][A-Za-z0-9_\\-]*"></div>'
    )
    action = f"/admin/productos/{product.id}" if product else "/admin/productos"
    kg = "" if not product or product.kg_per_bag is None else f"{product.kg_per_bag:g}"
    active = "" if not product else (
        f'<label class="check"><input type="checkbox" name="active" value="1"{" checked" if product.active else ""}> '
        "Activo (se reconoce en las conversaciones)</label><input type=hidden name=active value=0>"
    )
    return f"""<form method="post" action="{action}" class="row">
<input type="hidden" name="csrf" value="{token}">{sku_field}
<div class="grow"><label for="nm">Nombre</label><input id="nm" name="name" required maxlength="120"
 value="{escape(product.name) if product else ''}" placeholder="Maíz blanco"></div>
<div class="grow"><label for="al">Otras formas de llamarlo (separadas por coma)</label><input id="al" name="aliases"
 value="{escape(', '.join(product.aliases)) if product else ''}" placeholder="maiz bco, blanco"></div>
<div><label for="kg">Kg por bulto</label><input id="kg" name="kg_per_bag" inputmode="decimal" size="6" value="{kg}"
 placeholder="40"></div>
<div><label>Unidades</label>{checks}</div>{active}
<button class="primary">{"Guardar cambios" if product else "Agregar producto"}</button></form>"""


@router.get("/productos", response_class=HTMLResponse)
def products_page(settings: Admin, msg: str = "", err: str = "") -> HTMLResponse:
    with open_db(settings.database_path) as conn:
        products = repository.list_products(conn, active_only=False)
    token = escape(csrf_token(settings))
    rows = "".join(
        f"<tr><td class=nowrap>{escape(p.sku)}</td><td>{escape(p.name)}</td>"
        f"<td class=muted>{escape(', '.join(p.aliases)) or '—'}</td>"
        f"<td>{escape(', '.join(UNIT_LABELS[u].split(' ')[0].lower() for u in p.allowed_units))}</td>"
        f"<td>{'—' if p.kg_per_bag is None else f'{p.kg_per_bag:g}'}</td><td>{badge(p.active)}</td>"
        f"<td><a class=button href='/admin/productos/{p.id}'>Editar</a></td></tr>"
        for p in products
    ) or "<tr><td colspan=7 class=muted>No hay productos.</td></tr>"
    active = sum(p.active for p in products)
    body = f"""
<div class="card"><h2>Nuevo producto</h2>{_form(token, None)}
<p class="muted">Sin «kg por bulto», el asistente nunca convierte bultos a kilos.</p></div>
<div class="card"><p class="muted">{active} activos de {len(products)} productos.</p>
<div class="table"><table><thead><tr><th>Código</th><th>Nombre</th><th>Otras formas</th><th>Unidades</th><th>Kg/bulto</th>
<th>Estado</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></div>"""
    return page("Productos", "productos", body, msg=msg, err=err)


@router.get("/productos/{product_id:int}", response_class=HTMLResponse)
def product_page(product_id: int, settings: Admin, err: str = "") -> HTMLResponse:
    with open_db(settings.database_path) as conn:
        product = repository.get_product(conn, product_id)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    body = f"""<p><a href="/admin/productos">← Productos</a></p>
<div class="card">{_form(escape(csrf_token(settings)), product)}</div>"""
    return page(f"Producto {product.sku}", "productos", body, err=err)


def _save(settings, data: dict, *, must_be_new: bool) -> Product:
    with open_db(settings.database_path) as conn, transaction(conn):
        if must_be_new and repository.get_product_by_sku(conn, data["sku"]) is not None:
            raise ProductFormError(f"Ya existe un producto con el código {data['sku']}.")
        return repository.upsert_product(conn, **data)


@router.post("/productos", include_in_schema=False)
async def create_product(request: Request, settings: Admin) -> RedirectResponse:
    form = await read_form(request, settings)
    try:
        data = parse_product_form(form)
        product = await run_in_threadpool(_save, settings, data, must_be_new=True)
    except (ProductFormError, sqlite3.IntegrityError) as exc:
        return redirect("/admin/productos", err=str(exc))
    logger.info("Producto %s creado desde el panel", product.sku)
    return redirect("/admin/productos", msg=f"Producto agregado: {product.name} ({product.sku})")


@router.post("/productos/{product_id:int}", include_in_schema=False)
async def update_product(product_id: int, request: Request, settings: Admin) -> RedirectResponse:
    form = await read_form(request, settings)

    def current() -> Product | None:
        with open_db(settings.database_path) as conn:
            return repository.get_product(conn, product_id)

    product = await run_in_threadpool(current)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        data = parse_product_form(form, sku=product.sku)
        data["active"] = "1" in form.getlist("active")
        product = await run_in_threadpool(_save, settings, data, must_be_new=False)
    except (ProductFormError, sqlite3.IntegrityError) as exc:
        return redirect(f"/admin/productos/{product_id}", err=str(exc))
    logger.info("Producto %s actualizado desde el panel", product.sku)
    return redirect("/admin/productos", msg=f"Producto actualizado: {product.name} ({product.sku})")
