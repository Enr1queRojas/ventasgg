"""Panel de administración ``/admin``: clientes, pedidos, productos y mensajes.

HTML generado en el servidor, sin JavaScript ni dependencias extra. Protegido con HTTP Basic
(``ADMIN_USERNAME`` / ``ADMIN_PASSWORD``); sin contraseña responde 404. No se publica en el
túnel (solo ``/webhook*``): se abre por SSH, ver README.
"""

from app.api.admin import customers, messages, orders, products  # noqa: F401  (registran rutas)
from app.api.admin.common import csrf_token, router

__all__ = ["csrf_token", "router"]
