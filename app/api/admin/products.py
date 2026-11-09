"""Panel: catálogo de productos."""

from html import escape

from fastapi.responses import HTMLResponse

from app.api.admin.common import (
    Admin,
    badge,
    page,
    router,
)
from app.db import repository
from app.db.database import open_db


@router.get("/productos", response_class=HTMLResponse)
def products_page(settings: Admin) -> HTMLResponse:
    with open_db(settings.database_path) as conn:
        products = repository.list_products(conn, active_only=False)
    rows = "".join(
        f"<tr><td class=nowrap>{escape(p.sku)}</td><td>{escape(p.name)}</td>"
        f"<td class=muted>{escape(', '.join(p.aliases)) or '—'}</td><td>{escape(', '.join(p.allowed_units))}</td>"
        f"<td>{'' if p.kg_per_bag is None else f'{p.kg_per_bag:g}'}</td>"
        f"<td>{badge(p.active)}</td></tr>"
        for p in products
    ) or "<tr><td colspan=6 class=muted>No hay productos. Cárgalos con scripts.seed_db.</td></tr>"
    body = f"""
<div class="card"><p class="muted">{len(products)} productos. El catálogo se edita en <code>data/seed/products.csv</code>
y se carga con <code>python -m scripts.seed_db</code>.</p>
<div class="table"><table><thead><tr><th>Código</th><th>Nombre</th><th>Alias</th><th>Unidades</th><th>Kg/bulto</th>
<th>Estado</th></tr></thead><tbody>{rows}</tbody></table></div></div>"""
    return page("Productos", "productos", body)
