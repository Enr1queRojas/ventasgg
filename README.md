# Chatbot de ventas por WhatsApp

Backend (FastAPI + SQLite) que recibe pedidos por WhatsApp, los valida contra un catálogo local y los registra para que el área de ventas asigne y confirme la fecha de entrega. La arquitectura completa está en el plan de arquitectura (v1.0).

**Estado actual:** etapas 1–8 funcionales en desarrollo: webhook de Meta, envío por WhatsApp, agente OpenAI (Responses API + Structured Outputs), motor de pedidos con confirmación, folio y aviso al vendedor, worker con reintentos. Pendiente: plantilla de Meta para avisos al vendedor fuera de la ventana de 24 h, Docker y despliegue en la VM.

### Cómo responde el bot

| Remitente / mensaje | Respuesta |
|---|---|
| Número no registrado | Mensaje fijo con el teléfono de ventas (`SALES_PHONE`) |
| `SALES_PHONE` | Fuera del flujo de pedidos |
| Audio, foto, documento | Pide el pedido por texto |
| Cliente registrado | El modelo (`OPENAI_MODEL`) interpreta; Python valida contra el catálogo, actualiza el borrador, pregunta lo que falta y, con el pedido completo, muestra un resumen. Solo un "sí" al resumen vigente registra el pedido (`pending_sales`, folio `PED-0001`) y avisa al vendedor |

El modelo solo interpreta y redacta saludos/respuestas a preguntas. Resúmenes, confirmaciones, folios y el aviso de que **ventas asignará y confirmará la fecha de entrega** los genera el backend. Si `OPENAI_API_KEY` no está configurada, los clientes reciben una respuesta provisional.

## Requisitos

- Python 3.12 o 3.13 (desarrollado con 3.13)

## Instalación local

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    |  Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # completar valores; en desarrollo los secretos pueden quedar vacíos
```

## Base de datos

```bash
python -m scripts.init_db          # crea data/sales_bot.db (tablas e índices)
python -m scripts.seed_db          # carga/actualiza clientes y productos desde data/seed/*.csv
```

`seed_db` es idempotente: actualiza por teléfono (clientes) y SKU (productos) sin duplicar. Si alguna fila es inválida, no se guarda nada y se indica la línea.

### `data/seed/customers.csv`

| Columna | Ejemplo | Notas |
|---|---|---|
| `name` | Cliente de prueba | |
| `phone` | +52 55 1234 5678 | Con código de país. `521…` y `52…` se tratan como el mismo número. |
| `active` | true | `true/false`, `1/0`, `sí/no` |

**Antes de probar:** sustituir el teléfono de ejemplo por tu número de WhatsApp.

### `data/seed/products.csv`

| Columna | Ejemplo | Notas |
|---|---|---|
| `sku` | MAIZ-BLANCO | Único; se guarda en mayúsculas |
| `name` | Maíz blanco | |
| `aliases` | maiz blanco\|blanco | Separados por `\|` |
| `allowed_units` | kg\|bultos | `kg` y/o `bultos` |
| `kg_per_bag` | 40 | Opcional. Si está vacío, nunca se convierten bultos a kg |
| `active` | true | Los inactivos no se reconocen en conversaciones |

El catálogo incluido es de ejemplo: reemplazarlo por los productos reales.

## Ejecutar

```bash
python -m app.main                  # usa APP_PORT (8003 por defecto)
# o, con recarga automática en desarrollo:
uvicorn app.main:app --reload --port 8003
curl http://localhost:8003/health   # {"status":"ok","database":"ok"}
```

### Puerto

El backend usa **8003** en todos los entornos para no chocar con otras apps de la VM:

- Dentro del contenedor: `8003`
- Publicado en la VM: `127.0.0.1:8003` (no se expone públicamente)
- Cloudflare Tunnel: `pedidos.tudominio.com -> http://localhost:8003`
- Webhook de Meta: `https://pedidos.tudominio.com/webhook/whatsapp`

## Conectar con Meta (número de prueba)

1. Levantar el backend en 8003 y el túnel apuntando a `http://127.0.0.1:8003`.
2. Comprobar: `curl https://<hostname>/health` → `{"status":"ok",...}`.
3. Meta → WhatsApp → **Configuration** → Webhook:
   - Callback URL: `https://<hostname>/webhook/whatsapp`
   - Verify token: el mismo valor que `WHATSAPP_VERIFY_TOKEN`
   - **Verify and save**, después suscribirse al campo `messages`.
4. Meta → WhatsApp → **API Setup**: agregar tu número a la lista de destinatarios ("To").
5. Desde tu WhatsApp, escribir al número de prueba. El bot responde según el remitente:
   - cliente registrado → respuesta (provisional hasta conectar el agente de pedidos);
   - número no registrado → mensaje con el teléfono de ventas;
   - audio/foto/documento → pide el pedido por texto.

Notas:
- Un túnel rápido (`*.trycloudflare.com`) cambia de URL cada vez que se reinicia; hay que actualizar la Callback URL en Meta. Para algo estable, usar un túnel con nombre y el dominio propio.
- El token temporal de "API Setup" caduca en ~24 h (error 190). Para uso continuo, generar un token de System User.
- Las respuestas de texto libre solo se entregan dentro de las 24 h posteriores al último mensaje del cliente; fuera de esa ventana se necesita una plantilla aprobada.

## Pruebas

```bash
pytest
```

## Configuración

Ver `.env.example`. Con `APP_ENV=production` el servicio no arranca si faltan credenciales de OpenAI, Meta o `SALES_PHONE`. El archivo `.env` nunca se versiona.

## Decisiones de diseño relevantes

- **Teléfonos:** se guardan como dígitos con código de país (`525512345678`). Los móviles mexicanos que Meta reporta como `521…` se canonicalizan a `52…` para la búsqueda; para enviar se usa el `wa_id` original.
- **Un borrador activo por cliente:** lo garantiza un índice único parcial en `orders`.
- **Pedido registrado (`pending_sales`):** la base exige folio, `confirmed_at`, fecha solicitada y urgencia.
- **Bultos enteros:** el catálogo rechaza cantidades fraccionarias en bultos (`CatalogService.validate_line`).
- **Fechas/horas:** UTC en ISO 8601; `requested_date` como `YYYY-MM-DD` (validado por la base).
