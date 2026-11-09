# Plan de entrega

Estado al 9/11/2026. El flujo completo funciona en producción: un cliente registrado hizo el pedido `PED-0001` por WhatsApp de principio a fin.

## 1. Listo

| Área | Estado |
|---|---|
| Número de WhatsApp | +52 871 233 3190 registrado en Cloud API (cuenta *RGC dev*), conectado a la app *proadel-ventas*, método de pago activo |
| Webhook | `https://ventas.proadel.app/webhook/whatsapp`, firma `X-Hub-Signature-256` verificada |
| Pedidos | Interpretación con OpenAI, validación contra catálogo, confirmación explícita, folio, aviso a ventas |
| Aviso a ventas | Texto libre, o plantilla `nuevo_pedido_ventas` cuando se configura (llega aunque ventas no haya escrito en 24 h) |
| Clientes | Alta/baja/búsqueda por WhatsApp desde el número de ventas y panel `/admin` |
| Panel `/admin` | Clientes (editable), productos, pedidos y mensajes fallidos; usuario y contraseña; no expuesto a internet |
| Datos | SQLite en volumen de Docker con nombre fijo; migraciones automáticas; respaldo diario con rotación |
| Operación | `deploy/deploy.sh` (respaldo → actualización → verificación de salud), `deploy/backup.sh` para cron |
| Calidad | 182 pruebas automáticas; CI en GitHub (pruebas + construcción de la imagen) |
| Seguridad | Secretos solo en `.env` (600), contenedor sin root, puerto solo en `127.0.0.1`, claves rotadas |
| Documentación | README técnico, manual de operación para el cliente |

## 2. Antes de entregar (bloqueantes)

| # | Tarea | Responsable | Notas |
|---|---|---|---|
| 1 | **Publicar la app *proadel-ventas* en Meta** | Nosotros | Sin esto solo llegan mensajes de administradores/testers de la app; los clientes reales no reciben respuesta. Requiere URL de política de privacidad, ícono y categoría |
| 2 | Aprobación de la plantilla `nuevo_pedido_ventas` | Meta | Enviada el 9/11. Al aprobarse: `SALES_NOTIFICATION_TEMPLATE=nuevo_pedido_ventas` en `.env` y `docker compose up -d` |
| 3 | Aprobación del nombre visible «Graneros Guerra» | Meta | Revisar en WhatsApp Manager. Si lo rechazan, ajustar al nombre legal o comercial |
| 4 | Fusionar `claude/kind-rubin-jefe7w` en `main` y desplegar con `deploy/deploy.sh` | Nosotros | Configurar `ADMIN_PASSWORD` y el cron de respaldo |
| 5 | Cargar clientes reales y revisar el catálogo con el cliente | Cliente + nosotros | Altas por WhatsApp o panel; catálogo en `products.csv` |
| 6 | Prueba de aceptación con el cliente | Cliente | Guion en la sección 5 |
| 7 | Completar el contacto técnico en el manual | Nosotros | `docs/MANUAL_OPERACION.md`, sección 8 |

## 3. Decisiones pendientes con el cliente

- **Propiedad de la cuenta de Meta.** El número vive en el portafolio *RGC dev* (nuestro). Para entregar, conviene que la cuenta de WhatsApp quede en un portafolio del cliente (o compartirla como socio), con su método de pago. Decidir antes de que el número tenga historial.
- **Verificación del negocio en Meta.** Sin verificar, WhatsApp limita las conversaciones que el negocio inicia (plantillas) por día. Para los avisos a un solo vendedor alcanza; si más adelante se notifica a clientes, hay que verificar.
- **Costos recurrentes.** OpenAI (por mensaje interpretado), conversaciones de plantilla de WhatsApp (avisos a ventas) y la VM. Acordar quién los paga.
- **Aviso de privacidad.** Publicar uno (se necesita para publicar la app y por la LFPDPPP) y acordar cuánto tiempo se guardan las conversaciones.

## 4. Mejoras propuestas (después de la entrega)

| Prioridad | Mejora | Por qué |
|---|---|---|
| Alta | **Seguimiento de pedidos por ventas**: confirmar fecha de entrega, marcar entregado o cancelar desde el panel o por WhatsApp, y avisar al cliente (con plantilla) | Hoy los pedidos quedan en «Pendiente de ventas» y la confirmación al cliente se hace por fuera |
| Alta | **Respaldo fuera de la VM** (Object Storage de Oracle o similar) | Hoy el respaldo diario queda en la misma VM; si se pierde la VM, se pierden los datos |
| Media | **Alertas**: avisar al administrador si hay mensajes fallidos, el token deja de funcionar o el servicio se cae | Hoy hay que revisar el panel o los logs |
| Media | **Editar el catálogo desde el panel** | Evita depender del administrador técnico para cada producto |
| Media | **Exportar pedidos** a Excel/CSV | Para facturación o reportes |
| Baja | Varios números de ventas / roles en el panel | Si el equipo crece |
| Baja | Depuración automática de conversaciones antiguas | Según la política de privacidad acordada |

## 5. Guion de prueba de aceptación

1. Desde un número **no registrado**: escribir «hola» → responde con el teléfono de ventas.
2. Desde el número de ventas: `alta Prueba Cliente <número>` → confirma el alta.
3. Desde ese número: pedido completo en un mensaje («30 bultos de maíz blanco para el viernes») → resumen.
4. Corregir antes de confirmar («mejor 40») → resumen actualizado.
5. Responder «sí» → folio y aviso de que ventas confirmará la entrega; el aviso llega al número de ventas.
6. Pedido incompleto («quiero frijol») → pregunta cuál frijol, cantidad y fecha.
7. Enviar un audio → pide el pedido por escrito.
8. `baja <número>` desde ventas → ese número vuelve a recibir el mensaje de no registrado.
9. Panel: el cliente y el pedido aparecen; «Mensajes» sin fallos.
10. Reiniciar el servidor (`deploy/deploy.sh`) → los datos siguen ahí y el bot responde.
