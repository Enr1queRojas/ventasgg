"""Panel: mensajes (fallos de entrega)."""

from html import escape

from fastapi.responses import HTMLResponse

from app.api.admin.common import (
    Admin,
    fmt_date,
    page,
    router,
)
from app.db import repository
from app.db.database import open_db
from app.phone import InvalidPhoneError, format_phone_display
from app.services.admin_commands import parse_admin_phone

MESSAGE_STATUS_LABELS = {
    "pending": "En cola", "processing": "Procesando", "processed": "Procesado", "sent": "Enviado", "failed": "Fallido",
}


@router.get("/mensajes", response_class=HTMLResponse)
def messages_page(settings: Admin, estado: str = "fallidos") -> HTMLResponse:
    with open_db(settings.database_path) as conn:
        messages = repository.list_recent_messages(conn, failed_only=estado != "todos", limit=200)
        failed_24h = repository.count_failed_messages_since(conn, hours=24)
        names = {c.phone: c.name for c in repository.list_customers(conn, limit=5000)}
    tz = settings.timezone

    def who(phone: str) -> str:
        try:
            canonical = parse_admin_phone(phone)
        except InvalidPhoneError:
            canonical = phone
        if settings.sales_phone and canonical == settings.sales_phone:
            label = "Ventas"
        else:
            label = names.get(canonical, "No registrado")
        return f"{escape(label)}<br><span class='muted nowrap'>{escape(format_phone_display(canonical))}</span>"

    def row(m: dict) -> str:
        direction = "Recibido" if m["direction"] == "inbound" else "Enviado"
        state = MESSAGE_STATUS_LABELS.get(m["status"], m["status"])
        badge = f'<span class="badge off">{state}</span>' if m["status"] == "failed" else escape(state)
        text = escape((m["text"] or "")[:300]) or "<span class=muted>(sin texto)</span>"
        if m["message_type"] == "template":
            text = "<span class=muted>[plantilla]</span> " + text
        return (
            f"<tr><td class='muted nowrap'>{fmt_date(m['created_at'], tz)}</td><td>{direction}</td><td>{who(m['phone'])}</td>"
            f"<td>{text}</td><td>{badge}</td><td class=muted>{escape(m['last_error'] or '')}</td></tr>"
        )

    options = "".join(
        f'<option value="{v}"{" selected" if v == estado else ""}>{label}</option>'
        for v, label in (("fallidos", "Solo fallidos"), ("todos", "Todos"))
    )
    rows = "".join(row(m) for m in messages) or "<tr><td colspan=6 class=muted>Sin mensajes.</td></tr>"
    summary = (
        f'<div class="flash err">{failed_24h} mensaje(s) fallido(s) en las últimas 24 h.</div>' if failed_24h
        else '<div class="flash">Sin mensajes fallidos en las últimas 24 h.</div>'
    )
    body = f"""{summary}
<div class="card"><form method="get" class="row"><div><label for="e">Mostrar</label>
<select id="e" name="estado">{options}</select></div><button>Filtrar</button></form>
<p class="muted">Error 131047: el destinatario no ha escrito en 24 h (se necesita plantilla). 131030: la app de Meta
no está publicada o el número no está autorizado. 190: token vencido o inválido.</p>
<div class="table"><table><thead><tr><th>Fecha</th><th>Tipo</th><th>Contacto</th><th>Texto</th><th>Estado</th><th>Error</th>
</tr></thead><tbody>{rows}</tbody></table></div></div>"""
    return page("Mensajes", "mensajes", body)
