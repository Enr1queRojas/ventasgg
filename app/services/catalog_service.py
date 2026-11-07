"""Búsqueda de productos, unidades y validación de partidas contra el catálogo local."""

import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from app.db import repository
from app.models import Product, Unit

# Expresiones que el cliente usa para cada unidad. Ampliar aquí si el negocio usa otras
# (p. ej. "costal"), solo si realmente equivalen a la unidad.
UNIT_SYNONYMS: dict[str, Unit] = {
    "kg": Unit.KG,
    "kgs": Unit.KG,
    "kilo": Unit.KG,
    "kilos": Unit.KG,
    "kilogramo": Unit.KG,
    "kilogramos": Unit.KG,
    "bulto": Unit.BULTOS,
    "bultos": Unit.BULTOS,
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_text(text: str) -> str:
    """Minúsculas, sin acentos y con espacios simples: ``'Maíz  Blanco!'`` -> ``'maiz blanco'``."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM.sub(" ", without_accents).strip()


class MatchStatus(StrEnum):
    FOUND = "found"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class ProductMatch:
    status: MatchStatus
    product: Product | None = None
    candidates: tuple[Product, ...] = field(default_factory=tuple)


class CatalogErrorCode(StrEnum):
    UNKNOWN_PRODUCT = "unknown_product"
    INVALID_UNIT = "invalid_unit"
    UNIT_NOT_ALLOWED = "unit_not_allowed"
    INVALID_QUANTITY = "invalid_quantity"


class CatalogError(ValueError):
    def __init__(self, code: CatalogErrorCode, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ValidatedLine:
    product: Product
    quantity: float
    unit: Unit


class CatalogService:
    """Índice en memoria de los productos activos. Construir uno nuevo por mensaje
    (``from_db``) para reflejar cambios de catálogo sin reiniciar."""

    def __init__(self, products: Iterable[Product]):
        self._by_sku: dict[str, Product] = {}
        self._keys: dict[str, set[str]] = {}  # texto normalizado -> SKUs
        for p in products:
            if not p.active:
                continue
            self._by_sku[p.sku] = p
            for term in (p.sku, p.name, *p.aliases):
                key = normalize_text(term)
                if key:
                    self._keys.setdefault(key, set()).add(p.sku)

    @classmethod
    def from_db(cls, conn: sqlite3.Connection) -> "CatalogService":
        return cls(repository.list_products(conn, active_only=True))

    @property
    def products(self) -> list[Product]:
        return sorted(self._by_sku.values(), key=lambda p: p.name)

    def get_by_sku(self, sku: str) -> Product | None:
        return self._by_sku.get(sku.strip().upper())

    def resolve(self, text: str) -> ProductMatch:
        """Resuelve SKU, nombre o alias.

        1. Coincidencia exacta (normalizada) con SKU, nombre o alias.
        2. Si no hay, productos cuyo término contenga todas las palabras buscadas
           (``'maiz'`` -> maíz blanco y maíz amarillo => ambiguo).
        """
        query = normalize_text(text)
        if not query:
            return ProductMatch(MatchStatus.NOT_FOUND)

        skus = self._keys.get(query, set())
        if not skus:
            words = set(query.split())
            skus = {sku for key, owners in self._keys.items() if words <= set(key.split()) for sku in owners}

        candidates = tuple(sorted((self._by_sku[s] for s in skus), key=lambda p: p.name))
        if len(candidates) == 1:
            return ProductMatch(MatchStatus.FOUND, candidates[0], candidates)
        if candidates:
            return ProductMatch(MatchStatus.AMBIGUOUS, None, candidates)
        return ProductMatch(MatchStatus.NOT_FOUND)

    @staticmethod
    def parse_unit(text: str | Unit) -> Unit | None:
        if isinstance(text, Unit):
            return text
        return UNIT_SYNONYMS.get(normalize_text(text))

    def validate_line(self, sku: str, quantity: float, unit: str | Unit) -> ValidatedLine:
        """Valida una partida; lanza ``CatalogError`` con un código y mensaje en español."""
        product = self.get_by_sku(sku)
        if product is None:
            raise CatalogError(CatalogErrorCode.UNKNOWN_PRODUCT, f"El producto '{sku}' no está en el catálogo.")

        parsed_unit = self.parse_unit(unit)
        if parsed_unit is None:
            raise CatalogError(CatalogErrorCode.INVALID_UNIT, f"Unidad no reconocida: '{unit}'. Usa kg o bultos.")
        if parsed_unit not in product.allowed_units:
            allowed = " o ".join(u.value for u in product.allowed_units)
            raise CatalogError(
                CatalogErrorCode.UNIT_NOT_ALLOWED,
                f"{product.name} se vende en {allowed}, no en {parsed_unit.value}.",
            )

        if isinstance(quantity, bool) or not isinstance(quantity, int | float) or not quantity > 0:
            raise CatalogError(CatalogErrorCode.INVALID_QUANTITY, "La cantidad debe ser un número mayor que cero.")
        if parsed_unit is Unit.BULTOS and float(quantity) != int(quantity):
            raise CatalogError(CatalogErrorCode.INVALID_QUANTITY, "Los bultos deben pedirse en cantidades enteras.")

        return ValidatedLine(product=product, quantity=float(quantity), unit=parsed_unit)

    @staticmethod
    def to_kg(product: Product, quantity: float, unit: Unit) -> float | None:
        """Kilogramos equivalentes, o ``None`` si son bultos sin ``kg_per_bag`` configurado."""
        if unit is Unit.KG:
            return quantity
        if product.kg_per_bag is None:
            return None
        return quantity * product.kg_per_bag

    def as_prompt_context(self) -> list[dict]:
        """Catálogo compacto para incluir en el contexto del LLM (etapa 6)."""
        return [
            {
                "sku": p.sku,
                "name": p.name,
                "aliases": list(p.aliases),
                "allowed_units": [u.value for u in p.allowed_units],
                "kg_per_bag": p.kg_per_bag,
            }
            for p in self.products
        ]
