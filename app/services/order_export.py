"""Exportación de pedidos a Excel (.xlsx): hoja «Pedidos» (uno por fila) y «Partidas» (producto por fila)."""

from datetime import UTC, date, datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from app.phone import format_phone_display
from app.services.admin_commands import STATUS_LABELS

URGENCY = {"normal": "Normal", "high": "Alta", "critical": "Crítica"}
UNITS = {"kg": "kg", "bultos": "bultos", "piezas": "piezas"}
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="1F7A4D")


def _local(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tz).replace(tzinfo=None)  # Excel no guarda zona horaria


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _sheet(wb: Workbook, title: str, headers: list[str], rows: list[list], widths: list[int], formats: dict[int, str]):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for cell in ws[1]:
        cell.font, cell.fill = _HEADER_FONT, _HEADER_FILL
    for row in rows:
        ws.append(row)
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
        if index in formats:
            for (cell,) in ws.iter_rows(min_row=2, min_col=index, max_col=index):
                cell.number_format = formats[index]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def build_orders_workbook(orders: list[dict], items: list[dict], tz: ZoneInfo) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    by_id = {o["id"]: o for o in orders}

    order_rows = [
        [
            o["folio"], _local(o["confirmed_at"], tz), o["customer_name"], format_phone_display(o["customer_phone"]),
            o["items"] or "", _day(o["requested_date"]), URGENCY.get(o["urgency"] or "", ""), o["urgency_note"] or "",
            o["notes"] or "", STATUS_LABELS.get(o["status"], o["status"]), _day(o["delivery_date"]),
            o["sales_note"] or "", _local(o["delivered_at"], tz), _local(o["cancelled_at"], tz),
        ]
        for o in orders
    ]
    _sheet(
        wb, "Pedidos",
        ["Folio", "Registrado", "Cliente", "Teléfono", "Productos", "Fecha solicitada", "Urgencia", "Nota de urgencia",
         "Observaciones", "Estado", "Fecha de entrega", "Nota de ventas", "Entregado", "Cancelado"],
        order_rows, [11, 17, 26, 18, 45, 15, 10, 22, 30, 20, 15, 30, 17, 17],
        {2: "dd/mm/yyyy hh:mm", 6: "dd/mm/yyyy", 11: "dd/mm/yyyy", 13: "dd/mm/yyyy hh:mm", 14: "dd/mm/yyyy hh:mm"},
    )

    item_rows = []
    for item in items:
        order = by_id.get(item["order_id"])
        if order is None:
            continue
        kg = item["quantity"] if item["unit"] == "kg" else (
            item["quantity"] * item["kg_per_bag"] if item["unit"] == "bultos" and item["kg_per_bag"] else None
        )
        item_rows.append([
            order["folio"], order["customer_name"], item["sku"], item["name"], item["quantity"],
            UNITS.get(item["unit"], item["unit"]), kg, STATUS_LABELS.get(order["status"], order["status"]),
            _day(order["requested_date"]), _day(order["delivery_date"]),
        ])
    _sheet(
        wb, "Partidas",
        ["Folio", "Cliente", "Código", "Producto", "Cantidad", "Unidad", "Kg equivalentes", "Estado",
         "Fecha solicitada", "Fecha de entrega"],
        item_rows, [11, 26, 18, 26, 10, 9, 15, 20, 15, 15],
        {5: "#,##0.##", 7: "#,##0.##", 9: "dd/mm/yyyy", 10: "dd/mm/yyyy"},
    )

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
