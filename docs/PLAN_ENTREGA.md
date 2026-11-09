# Plan de entrega

Estado al 9/11/2026. El flujo completo funciona en producción: un cliente registrado hizo el pedido `PED-0001` por WhatsApp de principio a fin.

## 1. Listo

| Área | Estado |
|---|---|
| Número de WhatsApp | +52 871 233 3190 registrado en Cloud API (cuenta *RGC dev*), conectado a la app *proadel-ventas*, método de pago activo |
| Webhook | `https://ventas.proadel.app/webhook/whatsapp`, firma `X-Hub-Signature-256` verificada |
| Pedidos | Interpretación con OpenAI, validación contra catálogo, confirmación explícita, folio, aviso a ventas |
| Seguimiento | Ventas programa/reprograma la entrega, marca entregado o cancela en el panel (sin mensajes al cliente); por WhatsApp solo consulta el resumen de pedidos |
| Aviso a ventas con plantilla | `nuevo_pedido_ventas` enviada a Meta; se activa con `SALES_NOTIFICATION_TEMPLATE` |
| Clientes | Alta/baja/búsqueda por WhatsApp desde el número de ventas y en el panel |
| Panel `/admin` | Pedidos (gestión + exportación a Excel), clientes, catálogo editable y mensajes fallidos; usuario y contraseña; no expuesto a internet |
| Alertas | Envíos fallidos, token de WhatsApp, errores de procesamiento, caída del servicio o del túnel y respaldos fallidos → ntfy/Slack/Discord |
| Datos | SQLite en volumen de Docker con nombre fijo; migraciones automáticas; respaldo diario local (30 días) y fuera de la VM cifrado (90 días) |
| Operación | `deploy/deploy.sh` (respaldo → actualización → verificación), `deploy/backup.sh` y `deploy/watchdog.sh` para cron, `GET /webhook/health` para monitor externo |
| Calidad | 240+ pruebas automáticas; CI en GitHub (pruebas + construcción y arranque de la imagen) |
| Seguridad | Secretos solo en `.env` (600), contenedor sin root, puerto solo en `127.0.0.1`, CSRF en el panel, claves rotadas |
| Documentación | README técnico, manual de operación para el cliente |

## 2. Antes de entregar (bloqueantes)

| # | Tarea | Responsable | Notas |
|---|---|---|---|
| 1 | **Publicar la app *proadel-ventas* en Meta** | Nosotros | Sin esto solo llegan mensajes de administradores/testers de la app; los clientes reales no reciben respuesta. Requiere URL de política de privacidad, ícono y categoría |
| 2 | Aprobación de la plantilla `nuevo_pedido_ventas` | Meta | Enviada el 9/11. Al aprobarse: `SALES_NOTIFICATION_TEMPLATE=nuevo_pedido_ventas` en `.env` y `docker compose up -d` |
| 3 | Aprobación del nombre visible «Graneros Guerra» | Meta | Revisar en WhatsApp Manager. Si lo rechazan, ajustar al nombre legal o comercial |
| 4 | Fusionar `claude/kind-rubin-jefe7w` en `main` y desplegar con `deploy/deploy.sh` | Nosotros | La migración v4 de la base se aplica sola al arrancar (el despliegue respalda antes) |
| 5 | Configurar en la VM: `ADMIN_PASSWORD`, `ALERT_WEBHOOK_URL`, `WATCHDOG_PUBLIC_URL`, rclone + `BACKUP_REMOTE`, cron de respaldo y watchdog, monitor externo de `/webhook/health` | Nosotros | Pasos en el README («Primera vez», «Respaldo fuera de la VM», «Alertas») |
| 6 | Cargar clientes reales y revisar el catálogo con el cliente | Cliente + nosotros | En el panel |
| 7 | Prueba de aceptación con el cliente | Cliente | Guion en la sección 5 |
| 8 | Completar el contacto técnico en el manual | Nosotros | `docs/MANUAL_OPERACION.md`, sección 9 |

## 3. Decisiones pendientes con el cliente

- **Propiedad de la cuenta de Meta.** El número vive en el portafolio *RGC dev* (nuestro). Para entregar, conviene que la cuenta de WhatsApp quede en un portafolio del cliente (o compartirla como socio), con su método de pago. Decidir antes de que el número tenga historial.
- **Verificación del negocio en Meta.** Sin verificar, WhatsApp limita cuántas conversaciones con plantilla inicia el negocio por día. Para los avisos a un solo vendedor alcanza; conviene verificar de todos modos.
- **Costos recurrentes.** OpenAI (por mensaje interpretado), plantillas de WhatsApp (avisos a ventas) y la VM. Acordar quién los paga.
- **Aviso de privacidad.** Publicar uno (se necesita para publicar la app y por la LFPDPPP) y acordar cuánto tiempo se guardan las conversaciones.

## 4. Mejoras posibles (siguiente etapa)

| Prioridad | Mejora | Por qué |
|---|---|---|
| Media | El asistente responde «¿cuándo llega mi pedido?» con el estado y la fecha confirmada | Hoy el cliente recibe los avisos, pero si pregunta, el asistente no consulta sus pedidos |
| Media | Recordatorio a ventas de pedidos pendientes con más de N horas | Para que ningún pedido se quede sin fecha |
| Baja | Varios números de ventas / usuarios del panel con roles | Si el equipo crece |
| Baja | Depuración automática de conversaciones antiguas | Según la política de privacidad acordada |
| Baja | Prueba automática de restauración del respaldo (mensual) | Además de la verificación de integridad diaria |

## 5. Guion de prueba de aceptación

1. Desde un número **no registrado**: escribir «hola» → responde con el teléfono de ventas.
2. Desde el número de ventas: `alta Prueba Cliente <número>` → confirma el alta.
3. Desde ese número: pedido completo en un mensaje («30 bultos de maíz blanco para el viernes») → resumen.
4. Corregir antes de confirmar («mejor 40») → resumen actualizado.
5. Responder «sí» → folio y aviso de que ventas confirmará la entrega; el aviso llega al número de ventas.
6. Desde ventas: `pedidos` → aparece en el resumen como pendiente; `pedido <n>` muestra el detalle.
7. En el panel: programar la entrega → pasa a *Programados*; reprogramarla → cambia la fecha. El cliente no recibe mensajes.
8. Marcar entregado desde el panel → el pedido pasa a *Entregados*.
9. Exportar a Excel desde *Pedidos* → el archivo abre con las hojas *Pedidos* y *Partidas*.
10. Pedido incompleto («quiero frijol») → pregunta cuál frijol, cantidad y fecha.
11. Enviar un audio → pide el pedido por escrito.
12. Panel → *Productos*: agregar un producto nuevo y pedirlo por WhatsApp → lo reconoce.
13. `baja <número>` desde ventas → ese número vuelve a recibir el mensaje de no registrado.
14. Detener el contenedor (`docker compose stop`) → en ≤10 min llega la alerta «Servicio caído»; arrancarlo → «Servicio restablecido».
15. `deploy/backup.sh` → termina con `OK remoto`; `rclone ls ventasgg-cifrado:` muestra el respaldo.
