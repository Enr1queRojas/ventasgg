"""Etapa 3: catálogo, unidades y carga de CSV."""

from pathlib import Path

import pytest

from app.db import repository
from app.models import Unit
from app.services.catalog_service import (
    CatalogError,
    CatalogErrorCode,
    CatalogService,
    MatchStatus,
    normalize_text,
)
from scripts.seed_db import SEED_DIR, SeedError, seed_customers, seed_products


@pytest.fixture
def catalog(conn, products) -> CatalogService:
    return CatalogService.from_db(conn)


def test_normalize_text():
    assert normalize_text("  Maíz   BLANCO!! ") == "maiz blanco"


@pytest.mark.parametrize("text", ["Maíz blanco", "maiz blanco", "MAIZ-BLANCO", "maíz bco", "blanco"])
def test_resolve_exact_name_sku_alias(catalog, text):
    match = catalog.resolve(text)
    assert match.status is MatchStatus.FOUND
    assert match.product.sku == "MAIZ-BLANCO"


def test_resolve_partial_unique(catalog):
    assert catalog.resolve("trigo").product.sku == "SALVADO-TRIGO"


def test_resolve_ambiguous(catalog):
    match = catalog.resolve("maíz")
    assert match.status is MatchStatus.AMBIGUOUS
    assert {p.sku for p in match.candidates} == {"MAIZ-BLANCO", "MAIZ-AMARILLO"}


@pytest.mark.parametrize("text", ["frijol", "", "descontinuado"])
def test_resolve_not_found_or_inactive(catalog, text):
    assert catalog.resolve(text).status is MatchStatus.NOT_FOUND


@pytest.mark.parametrize(
    ("text", "unit"),
    [("kilos", Unit.KG), ("Kg", Unit.KG), ("kilogramos", Unit.KG), ("bulto", Unit.BULTOS), ("BULTOS", Unit.BULTOS), ("toneladas", None)],
)
def test_parse_unit(text, unit):
    assert CatalogService.parse_unit(text) is unit


def test_validate_line_ok(catalog):
    line = catalog.validate_line("maiz-blanco", 30, "bultos")
    assert (line.product.sku, line.quantity, line.unit) == ("MAIZ-BLANCO", 30.0, Unit.BULTOS)
    assert catalog.validate_line("MAIZ-BLANCO", 200.5, "kilos").unit is Unit.KG


@pytest.mark.parametrize(
    ("sku", "qty", "unit", "code"),
    [
        ("FRIJOL", 10, "kg", CatalogErrorCode.UNKNOWN_PRODUCT),
        ("DESCONTINUADO", 10, "kg", CatalogErrorCode.UNKNOWN_PRODUCT),
        ("MAIZ-BLANCO", 10, "litros", CatalogErrorCode.INVALID_UNIT),
        ("SALVADO-TRIGO", 10, "kg", CatalogErrorCode.UNIT_NOT_ALLOWED),
        ("MAIZ-BLANCO", 0, "kg", CatalogErrorCode.INVALID_QUANTITY),
        ("MAIZ-BLANCO", -5, "kg", CatalogErrorCode.INVALID_QUANTITY),
        ("MAIZ-BLANCO", 2.5, "bultos", CatalogErrorCode.INVALID_QUANTITY),
        ("MAIZ-BLANCO", True, "kg", CatalogErrorCode.INVALID_QUANTITY),
    ],
)
def test_validate_line_errors(catalog, sku, qty, unit, code):
    with pytest.raises(CatalogError) as exc:
        catalog.validate_line(sku, qty, unit)
    assert exc.value.code is code


def test_to_kg_only_when_configured(catalog):
    blanco, amarillo = catalog.get_by_sku("MAIZ-BLANCO"), catalog.get_by_sku("MAIZ-AMARILLO")
    assert CatalogService.to_kg(blanco, 200, Unit.KG) == 200
    assert CatalogService.to_kg(blanco, 10, Unit.BULTOS) is None  # sin kg_per_bag: no se convierte
    assert CatalogService.to_kg(amarillo, 10, Unit.BULTOS) == 400


def test_prompt_context_excludes_inactive(catalog):
    assert "DESCONTINUADO" not in {p["sku"] for p in catalog.as_prompt_context()}


def test_seed_files_load_and_are_idempotent(conn):
    first_c = seed_customers(conn, SEED_DIR / "customers.csv")
    first_p = seed_products(conn, SEED_DIR / "products.csv")
    assert first_c.inserted >= 1 and first_p.inserted >= 1

    second_c = seed_customers(conn, SEED_DIR / "customers.csv")
    second_p = seed_products(conn, SEED_DIR / "products.csv")
    assert (second_c.inserted, second_c.updated, second_c.skipped) == (0, 0, first_c.inserted)
    assert (second_p.inserted, second_p.updated, second_p.skipped) == (0, 0, first_p.inserted)
    forced = seed_products(conn, SEED_DIR / "products.csv", update_existing=True)
    assert (forced.inserted, forced.updated) == (0, first_p.inserted)
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == first_p.inserted


def test_seed_does_not_touch_existing_customers(conn, tmp_path: Path):
    """Un cliente dado de baja (o renombrado) desde WhatsApp o el panel no se reactiva al recargar el CSV."""
    repository.upsert_customer(conn, name="Nombre editado", phone="+52 55 1234 5678", active=False)
    csv_file = tmp_path / "customers.csv"
    csv_file.write_text("name,phone,active\nNombre CSV,+52 55 1234 5678,true\nNuevo,+52 55 8765 4321,true\n", encoding="utf-8")
    result = seed_customers(conn, csv_file)
    assert (result.inserted, result.skipped) == (1, 1)
    existing = repository.get_customer_by_phone(conn, "525512345678", active_only=False)
    assert existing.name == "Nombre editado" and not existing.active


def test_seed_reports_bad_line(conn, tmp_path: Path):
    bad = tmp_path / "products.csv"
    bad.write_text("sku,name,aliases,allowed_units,kg_per_bag,active\nOK,Ok,,kg,,true\nMALO,Malo,,litros,,true\n", encoding="utf-8")
    with pytest.raises(SeedError, match="línea 3"):
        seed_products(conn, bad)


def test_seed_rejects_duplicate_phone_in_file(conn, tmp_path: Path):
    bad = tmp_path / "customers.csv"
    bad.write_text("name,phone,active\nA,+52 55 1234 5678,1\nB,5215512345678,1\n", encoding="utf-8")
    with pytest.raises(SeedError, match="repetido"):
        seed_customers(conn, bad)


@pytest.fixture
def paper_catalog(conn) -> CatalogService:
    repository.upsert_product(conn, sku="CAJA-PAPEL", name="Caja de papel", aliases=["cajas de papel"], allowed_units=[Unit.PIEZAS])
    repository.upsert_product(conn, sku="PACA-PAPEL", name="Paca de papel", aliases=["pacas de papel"], allowed_units=[Unit.PIEZAS])
    return CatalogService.from_db(conn)


@pytest.mark.parametrize("text", ["piezas", "pieza", "pzas", "cajas", "caja", "pacas"])
def test_parse_unit_piezas(text):
    assert CatalogService.parse_unit(text) is Unit.PIEZAS


def test_piezas_validation(paper_catalog):
    assert paper_catalog.validate_line("CAJA-PAPEL", 20, "cajas").unit is Unit.PIEZAS
    with pytest.raises(CatalogError) as exc:
        paper_catalog.validate_line("CAJA-PAPEL", 2.5, "piezas")
    assert exc.value.code is CatalogErrorCode.INVALID_QUANTITY
    with pytest.raises(CatalogError) as exc:
        paper_catalog.validate_line("CAJA-PAPEL", 20, "kg")
    assert exc.value.code is CatalogErrorCode.UNIT_NOT_ALLOWED
    assert CatalogService.to_kg(paper_catalog.get_by_sku("CAJA-PAPEL"), 20, Unit.PIEZAS) is None


def test_resolve_paper(paper_catalog):
    assert paper_catalog.resolve("cajas").product.sku == "CAJA-PAPEL"
    assert paper_catalog.resolve("papel").status is MatchStatus.AMBIGUOUS


def test_seed_accepts_legacy_sku_column(conn, tmp_path: Path):
    legacy = tmp_path / "products.csv"
    legacy.write_text("sku,name,aliases,allowed_units,kg_per_bag,active\nX-1,Producto X,,kg,,true\n", encoding="utf-8")
    assert seed_products(conn, legacy).inserted == 1
