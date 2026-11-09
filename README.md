# Chatbot de ventas por WhatsApp

Backend (FastAPI + SQLite) que recibe pedidos por WhatsApp, los valida contra un catálogo local y los registra para que el área de ventas asigne y confirme la fecha de entrega. La arquitectura completa está en el plan de arquitectura (v1.0).

**Estado actual:** etapas 1–8 funcionales en desarrollo: webhook de Meta, envío por WhatsApp, agente OpenAI (Responses API + Structured Outputs), motor de pedidos con confirmación, folio y aviso al vendedor, worker con reintentos. Listo para desplegar con Docker en la VM (ver [Despliegue en la VM](#despliegue-en-la-vm-oracle-cloud)). El aviso al vendedor puede enviarse con plantilla de Meta para que llegue fuera de la ventana de 24 h (ver [Aviso al vendedor](#aviso-al-vendedor-plantilla)).

### Cómo responde el bot

| Remitente / mensaje | Respuesta |
|---|---|
| Número no registrado | Mensaje fijo con el teléfono de ventas (`SALES_PHONE`) |
| `SALES_PHONE` | Fuera del flujo de pedidos; administra clientes con comandos (ver [Administrar clientes](#administrar-clientes)) |
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
# crear .env con las variables de la sección «Configuración»
```

## Base de datos

```bash
python -m scripts.init_db          # crea data/sales_bot.db (tablas e índices)
python -m scripts.seed_db          # carga/actualiza clientes y productos desde data/seed/*.csv
```

`seed_db` es idempotente: actualiza productos por SKU y **solo agrega clientes nuevos** (los teléfonos que ya están en la base no se modifican, para no reactivar ni renombrar a quien se dio de baja desde WhatsApp o el panel). Si alguna fila es inválida, no se guarda nada y se indica la línea.

`customers.csv` sirve para la carga inicial; después, los clientes se administran desde WhatsApp o desde el panel `/admin` (ver [Administrar clientes](#administrar-clientes)).

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
| `codigo` | MAIZ-BLANCO | Código (SKU) único; se guarda en mayúsculas. También se acepta la columna `sku` |
| `name` | Maíz blanco | |
| `aliases` | maiz blanco\|blanco | Separados por `\|` |
| `allowed_units` | kg\|bultos | `kg`, `bultos` y/o `piezas` (cajas o pacas se cuentan por pieza) |
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
- Cloudflare Tunnel (fijo): `ventas.proadel.app -> http://127.0.0.1:8003`
- Webhook de Meta: `https://ventas.proadel.app/webhook/whatsapp`

## Conectar con Meta (número de prueba)

1. Levantar el backend en 8003 y el túnel apuntando a `http://127.0.0.1:8003`.
2. Comprobar en la máquina del backend: `curl http://127.0.0.1:8003/health` → `{"status":"ok",...}`. En `ventas.proadel.app` solo se publica `/webhook*`.
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

Variables en `.env` (nunca se versiona ni entra en la imagen Docker). Con `APP_ENV=production` el servicio no arranca si falta alguna obligatoria.

| Variable | Obligatoria en producción | Ejemplo / notas |
|---|---|---|
| `APP_ENV` | | `development` · `production` (compose la fija en `production`) |
| `APP_PORT` | | `8003` |
| `LOG_LEVEL` | | `INFO` |
| `TZ` | | `America/Mexico_City` |
| `DATABASE_PATH` | | Local `data/sales_bot.db`; en Docker la fija compose en `/data/sales_bot.db` |
| `OPENAI_API_KEY` | ✔ | |
| `OPENAI_MODEL` | ✔ | Modelo con Structured Outputs |
| `WHATSAPP_ACCESS_TOKEN` | ✔ | En el servidor, token **permanente** de System User |
| `WHATSAPP_PHONE_NUMBER_ID` | ✔ | API Setup |
| `WHATSAPP_VERIFY_TOKEN` | ✔ | Cadena propia; la misma que se pone en Meta |
| `WHATSAPP_APP_SECRET` | ✔ | App settings → Basic → App secret (no el Client token) |
| `WHATSAPP_API_VERSION` | ✔ | `v25.0` |
| `SALES_PHONE` | ✔ | `+52 33 1234 5678` |
| `SALES_NOTIFICATION_TEMPLATE` | | Nombre (no el texto) de la plantilla aprobada para avisar al vendedor, p. ej. `nuevo_pedido_ventas`. Vacía: el aviso va como texto libre |
| `SALES_NOTIFICATION_TEMPLATE_LANGUAGE` | | Idioma con el que se aprobó la plantilla. `es_MX` por defecto |
| `ADMIN_USERNAME` | | Usuario del panel `/admin`. `admin` por defecto |
| `ADMIN_PASSWORD` | | Contraseña del panel `/admin`. Vacía: el panel queda deshabilitado (404) |
| `WORKER_ENABLED` | | `true` |
| `WORKER_POLL_INTERVAL` | | `1` (segundos) |
| `WORKER_MAX_ATTEMPTS` | | `5` |

## Despliegue en la VM (Oracle Cloud)

Una sola instancia: contenedor `ventasgg-sales-bot`, puerto `127.0.0.1:8003`, base SQLite en el volumen `ventasgg_sales-bot-data` (`/data` dentro del contenedor). `compose.yaml` fija el nombre del proyecto y del volumen, así que la base es la misma aunque el repo se clone en otra carpeta. El acceso público es el túnel **granitoia**, que ya enruta `ventas.proadel.app/webhook` → `http://127.0.0.1:8003`.

### Primera vez

```bash
git clone https://github.com/Enr1queRojas/ventasgg.git
cd ventasgg
nano .env                                    # pegar las variables (ver «Configuración»)
chmod 600 .env
docker compose up -d --build
docker compose run --rm sales-bot python -m scripts.seed_db   # clientes y catálogo
docker compose ps                            # STATUS debe pasar a "healthy"
curl http://127.0.0.1:8003/health            # {"status":"ok","database":"ok"}
docker compose logs -f                       # debe mostrar "Worker iniciado"
```

Después, en Meta → WhatsApp → Configuration → Webhook: Callback URL `https://ventas.proadel.app/webhook/whatsapp`, mismo verify token, campo `messages` suscrito. Si `cloudflared` corre en un contenedor en lugar de en el host, `127.0.0.1` no apunta a la VM: usar la red de Docker o `network_mode: host` para el túnel.

### Actualizar

```bash
cd ventasgg
git pull
docker compose up -d --build
docker compose run --rm sales-bot python -m scripts.seed_db   # solo si cambiaron los CSV
```

Lo único que no está en el repo es `.env`. Clientes, pedidos y mensajes viven en el volumen, no en la carpeta, así que si prefieres clonar en una carpeta nueva basta con copiar `.env` (no hace falta llevarse `data/`):

```bash
git clone https://github.com/Enr1queRojas/ventasgg.git ventasgg-nuevo
cp ventasgg/.env ventasgg-nuevo/ && chmod 600 ventasgg-nuevo/.env
cd ventasgg && docker compose down && cd ../ventasgg-nuevo && docker compose up -d --build
```

Antes de este cambio el volumen tomaba el nombre de la carpeta (`<carpeta>_sales-bot-data`); si alguna vez levantaste el bot desde una carpeta con otro nombre, `docker volume ls` mostrará ese volumen aparte.

Al reiniciar, el worker retoma los mensajes que quedaron a medias; la base vive en el volumen y sobrevive a `down`/`up` y a reconstrucciones. **No usar `docker compose down -v`**: borra el volumen con los pedidos.

### Respaldo de la base

```bash
docker compose exec sales-bot python -c "import sqlite3; sqlite3.connect('/data/sales_bot.db').backup(sqlite3.connect('/data/respaldo.db'))"
docker compose cp sales-bot:/data/respaldo.db ./respaldo-$(date +%F).db
```

### Revisar pedidos y mensajes

```bash
docker compose exec sales-bot python -c "import sqlite3; c=sqlite3.connect('/data/sales_bot.db'); [print(r) for r in c.execute('select folio,status,requested_date,urgency,created_at from orders order by id desc limit 20')]"
docker compose exec sales-bot python -c "import sqlite3; c=sqlite3.connect('/data/sales_bot.db'); [print(r) for r in c.execute(\"select id,direction,status,last_error from messages where status='failed' order by id desc limit 20\")]"
```

## Administrar clientes

Los clientes viven en la base de datos. `customers.csv` solo sirve para la carga inicial.

### Desde WhatsApp (solo el número de ventas)

El número configurado en `SALES_PHONE` escribe al bot:

| Mensaje | Qué hace |
|---|---|
| `alta Juan Pérez 871 123 4567` | Registra al cliente; si el teléfono ya existe, actualiza el nombre y lo reactiva. También acepta el teléfono primero |
| `baja 871 123 4567` | Lo desactiva: deja de poder hacer pedidos y recibe el mensaje de «no registrado» |
| `clientes` | Lista los clientes activos |
| `buscar juan` | Busca por nombre o teléfono (incluye inactivos) |
| `ayuda` (o cualquier otro texto) | Muestra los comandos |

Un teléfono de 10 dígitos se toma como número de México (+52). Los comandos se interpretan con reglas fijas, sin el modelo de OpenAI, y solo los acepta `SALES_PHONE`: si un cliente escribe «alta …», entra al flujo normal de pedidos.

### Panel web `/admin`

Con `ADMIN_PASSWORD` en `.env` (y opcionalmente `ADMIN_USERNAME`), el backend sirve un panel protegido con usuario y contraseña:

- **Clientes:** agregar, editar nombre, activar/desactivar, buscar y filtrar.
- **Productos:** consulta del catálogo (se edita en `products.csv` + `seed_db`).
- **Pedidos:** los últimos 200 con cliente, productos, fecha solicitada, urgencia y estado.

El túnel solo publica `/webhook*`, así que el panel **no queda expuesto a internet**. Para abrirlo desde tu computadora, crea un túnel SSH a la VM y entra a http://localhost:8003/admin:

```bash
ssh -L 8003:127.0.0.1:8003 usuario@ip-de-la-vm
```

Si quieres publicarlo en un dominio, protégelo además con Cloudflare Access; no lo publiques solo con la contraseña.

## Aviso al vendedor (plantilla)

WhatsApp solo entrega texto libre si el destinatario escribió al número del bot en las últimas 24 h; si no, Meta acepta el envío y después lo marca como fallido (error 131047). Para que el aviso de pedido nuevo llegue siempre a `SALES_PHONE`, se envía con una plantilla aprobada cuando `SALES_NOTIFICATION_TEMPLATE` tiene valor (sin ella se envía como texto libre).

Plantilla esperada (categoría **Utility**, idioma `es_MX`, parámetros posicionales):

```text
Encabezado: Nuevo pedido
Cuerpo:
Se registró el pedido {{1}} del cliente {{2}}, teléfono {{3}}.

Productos: {{4}}
Fecha solicitada: {{5}}
Urgencia: {{6}}
Observaciones: {{7}}

Estado: pendiente de ventas. La fecha de entrega aún no está confirmada; asígnala y confírmala con el cliente.
```

| Parámetro | Valor |
|---|---|
| `{{1}}` | Folio (`PED-0001`) |
| `{{2}}` | Nombre del cliente |
| `{{3}}` | Teléfono del cliente |
| `{{4}}` | Productos separados por `; ` (`Maíz blanco: 100 bultos; Alubia: 20 kg`) |
| `{{5}}` | Fecha solicitada (`dd/mm/aaaa`) |
| `{{6}}` | Urgencia (`Alta (me urge)`) |
| `{{7}}` | Observaciones, o `-` si no hay |

Los saltos de línea y espacios repetidos se compactan (Meta los rechaza en parámetros) y cada valor se recorta a 400 caracteres. La versión legible del aviso se guarda en `messages.text`; la plantilla, en `messages.template_payload` (esquema v3, la migración se aplica sola al arrancar). Si cambias el texto o el orden de los parámetros en Meta, ajusta `sales_notification_params` en `app/services/notification_service.py`.

## Decisiones de diseño relevantes

- **Teléfonos:** se guardan como dígitos con código de país (`525512345678`). Los móviles mexicanos que Meta reporta como `521…` se canonicalizan a `52…` para la búsqueda; para enviar se usa el `wa_id` original.
- **Un borrador activo por cliente:** lo garantiza un índice único parcial en `orders`.
- **Pedido registrado (`pending_sales`):** la base exige folio, `confirmed_at`, fecha solicitada y urgencia.
- **Bultos enteros:** el catálogo rechaza cantidades fraccionarias en bultos (`CatalogService.validate_line`).
- **Fechas/horas:** UTC en ISO 8601; `requested_date` como `YYYY-MM-DD` (validado por la base).
