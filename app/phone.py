"""Normalización de números telefónicos.

Formato canónico: solo dígitos, con código de país y sin '+' (ej. ``525512345678``).
Es el mismo formato que usa Meta en ``wa_id``, salvo el caso de México descrito abajo.
"""

import re

_NON_DIGITS = re.compile(r"\D")

MIN_DIGITS = 11  # código de país + número; obliga a incluir el código de país
MAX_DIGITS = 15  # máximo E.164


class InvalidPhoneError(ValueError):
    """El texto recibido no es un número internacional válido."""


def normalize_phone(raw: str) -> str:
    """Devuelve el número en formato canónico o lanza ``InvalidPhoneError``.

    México: WhatsApp suele reportar los móviles como ``521`` + 10 dígitos
    (prefijo heredado). Se canonicaliza a ``52`` + 10 dígitos para que la
    búsqueda del cliente funcione sin importar cuál de las dos formas llegue.
    Para enviar mensajes se debe usar el ``wa_id`` tal como lo reporta Meta.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidPhoneError("El teléfono está vacío.")

    digits = _NON_DIGITS.sub("", raw)
    if digits.startswith("00"):  # prefijo internacional marcado como 00
        digits = digits[2:]
    if len(digits) == 13 and digits.startswith("521"):
        digits = "52" + digits[3:]

    if not MIN_DIGITS <= len(digits) <= MAX_DIGITS:
        raise InvalidPhoneError(
            f"Teléfono inválido: '{raw}'. Usa el formato internacional con código "
            "de país, por ejemplo +52 55 1234 5678."
        )
    return digits


# Ciudad de México, Guadalajara y Monterrey usan lada de 2 dígitos; el resto de México, de 3.
_MX_TWO_DIGIT_AREA = {"55", "56", "33", "81"}


def format_phone_display(phone: str) -> str:
    """Formato legible: ``+52 55 1234 5678``, ``+52 871 123 4567`` (México) o ``+<dígitos>``."""
    if len(phone) == 12 and phone.startswith("52"):
        n = phone[2:]
        if n[:2] in _MX_TWO_DIGIT_AREA:
            return f"+52 {n[:2]} {n[2:6]} {n[6:]}"
        return f"+52 {n[:3]} {n[3:6]} {n[6:]}"
    return f"+{phone}"
