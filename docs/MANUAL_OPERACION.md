# Manual de operación — Pedidos por WhatsApp (Graneros Guerra)

Este manual es para el equipo de ventas y para quien administre el servicio. No hace falta saber programar.

## 1. Qué hace el servicio

Los clientes registrados escriben su pedido por WhatsApp al número de pedidos (**+52 871 233 3190**, nombre visible *Graneros Guerra*). El asistente:

1. Entiende el pedido en lenguaje natural («100 bultos de maíz blanco para el miércoles, me urge»).
2. Revisa que los productos y unidades existan en el catálogo y pregunta lo que falte (producto, cantidad, fecha).
3. Muestra un resumen y solo lo registra cuando el cliente responde **sí**.
4. Asigna un folio (`PED-0001`, `PED-0002`, …) y avisa al número de ventas.

El asistente **nunca promete fechas de entrega**: siempre indica que *ventas asignará y confirmará la fecha de entrega*.

| Quién escribe | Qué recibe |
|---|---|
| Cliente registrado y activo | El asistente de pedidos |
| Número no registrado o dado de baja | Un mensaje con el teléfono de ventas para solicitar su alta |
| Audio, foto o documento | Una petición de escribir el pedido como texto |
| Número de ventas | Los comandos de pedidos y clientes (sección 4) |

## 2. Avisos de pedidos nuevos

Cada pedido confirmado llega al número de ventas (**+52 871 342 7556**) con folio, cliente, teléfono, productos, fecha solicitada, urgencia y observaciones, y el comando para programar la entrega.

> Mientras la plantilla de aviso no esté aprobada por Meta, WhatsApp solo entrega el aviso si el número de ventas escribió al número de pedidos en las últimas 24 horas. Basta con mandar cualquier mensaje (por ejemplo «ayuda») una vez al día. Con la plantilla aprobada esto ya no es necesario.

## 3. Seguimiento de pedidos

Cada pedido pasa por estos estados:

| Estado | Qué significa | Qué hace ventas |
|---|---|---|
| **Pendiente de ventas** | El cliente lo confirmó | Revisar existencia y fijar la fecha de entrega |
| **Entrega programada** | Ventas fijó la fecha | Entregar; si cambia la fecha, reprogramar |
| **Entregado** | Pedido cerrado | — |
| **Cancelado** | No se surtirá | — |

Al programar, reprogramar, marcar entregado o cancelar, **el cliente recibe un WhatsApp automático** con el folio, la fecha y la nota (o el motivo de cancelación). Se puede hacer desde el panel o desde el WhatsApp de ventas.

## 4. Comandos desde el WhatsApp de ventas

Solo el número de ventas puede usar estos comandos. Se escriben como un mensaje normal al número de pedidos. El número de pedido es el folio sin ceros: `12` = `PED-0012`.

**Pedidos**

| Escribe | Qué pasa |
|---|---|
| `pedidos` | Lista los pendientes y las entregas programadas |
| `pedido 12` | Muestra el detalle del pedido |
| `programar 12 viernes` | Fija la entrega para el próximo viernes y avisa al cliente |
| `programar 12 15/11 llega por la tarde` | Igual, con una nota que también recibe el cliente |
| `entregado 12` | Lo marca como entregado y avisa al cliente |
| `cancelar 12 sin existencia` | Lo cancela, con el motivo, y avisa al cliente |

Fechas que entiende: `hoy`, `mañana`, `pasado mañana`, un día de la semana (`lunes` … `domingo`, siempre el próximo), `15/11` o `15/11/2026`. Para cambiar una fecha ya programada, vuelve a usar `programar`.

**Clientes**

| Escribe | Qué pasa |
|---|---|
| `alta Juan Pérez 871 123 4567` | Registra al cliente. Si ya existía, actualiza su nombre y lo reactiva |
| `baja 871 123 4567` | Lo da de baja: ya no puede hacer pedidos |
| `clientes` | Lista los clientes activos |
| `buscar juan` | Busca por nombre o por parte del teléfono (incluye los dados de baja) |
| `ayuda` | Muestra todos los comandos |

- El teléfono puede escribirse con o sin espacios, guiones o `+52`. Diez dígitos se toman como número de México.
- El nombre puede ir antes o después del teléfono.
- El cliente puede escribir en cuanto recibes la confirmación del alta.

## 5. Panel de administración

Desde una computadora con acceso al servidor se abre un panel con usuario y contraseña:

- **Pedidos** (pantalla inicial): pestañas *Pendientes*, *Programados*, *Entregados*, *Cancelados* y *Todos* con el número de pedidos en cada una; búsqueda por folio, cliente o teléfono; filtro por fecha. **Gestionar** abre el pedido para programar la entrega (fecha y nota), marcarlo entregado o cancelarlo, y muestra los mensajes enviados al cliente y si llegaron. La casilla *Avisar al cliente por WhatsApp* viene marcada; desmárcala si ya le avisaste por otro medio.
- **Exportar a Excel:** botón en *Pedidos*; descarga los pedidos del filtro actual en dos hojas: *Pedidos* (uno por fila) y *Partidas* (un producto por fila, con kilos equivalentes).
- **Clientes:** agregar, corregir nombres, activar/desactivar y buscar.
- **Productos:** agregar, editar y activar/desactivar productos (sección 6).
- **Mensajes:** los mensajes que no se pudieron entregar y el motivo.

El panel no está publicado en internet. Para abrirlo, el administrador técnico crea un túnel seguro (ver README, «Panel web /admin») y entra a `http://localhost:8003/admin`.

## 6. Catálogo de productos

Se administra en el panel → **Productos**:

- **Código:** identificador único (por ejemplo `MAIZ-BLANCO`). No se puede cambiar después.
- **Nombre** y **otras formas de llamarlo** (separadas por coma): cómo lo escriben los clientes («blanco», «maíz bco»). Mientras más formas, mejor lo reconoce el asistente.
- **Unidades:** kilos, bultos y/o piezas (cajas, pacas). El asistente rechaza pedidos en otras unidades.
- **Kg por bulto:** si se llena, el asistente puede convertir bultos a kilos; si no, nunca lo hace.
- **Activo:** desmarcar un producto lo saca de las conversaciones sin borrar los pedidos anteriores.

## 7. Problemas frecuentes

| Situación | Qué revisar |
|---|---|
| Un cliente dice que el bot le contesta que no está registrado | Que esté dado de alta y activo: `buscar <su teléfono>`. Si aparece «(inactivo)», usa `alta` para reactivarlo |
| No llegan avisos de pedidos a ventas | Que la plantilla esté aprobada, o que ventas haya escrito en las últimas 24 h. En el panel → Mensajes aparece el motivo |
| El bot no contesta a nadie | Avisar al administrador técnico (puede ser el servidor, el token de Meta o la app de Meta) |
| El bot entendió mal un pedido | El cliente puede corregirlo antes de decir «sí» («no, eran 50 bultos») o escribir «cancelar» para empezar de nuevo |
| Un cliente no recibió el aviso de su entrega | Panel → el pedido → *Mensajes del pedido* muestra si se envió o el motivo del fallo |
| Me equivoqué de fecha al programar | Vuelve a programar con la fecha correcta; el cliente recibe el aviso de reprogramación |

## 8. Respaldos, alertas y datos

- La base de datos (clientes, pedidos y conversaciones) se respalda automáticamente cada noche en el servidor y una copia cifrada se guarda fuera de él.
- El administrador técnico recibe alertas en su celular si el servicio se cae, si un mensaje no se pudo entregar o si falla un respaldo.
- Los mensajes de los clientes se guardan para dar seguimiento a los pedidos. Sólo el personal autorizado tiene acceso al panel.

## 9. Contacto técnico

Administrador técnico: _(nombre, teléfono y correo)_
