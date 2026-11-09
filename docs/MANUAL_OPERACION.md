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
| Número de ventas | Los comandos de administración (sección 3) |

## 2. Avisos de pedidos nuevos

Cada pedido confirmado llega al número de ventas (**+52 871 342 7556**) con folio, cliente, teléfono, productos, fecha solicitada, urgencia y observaciones. Con eso ventas contacta al cliente para confirmar la entrega.

> Mientras la plantilla de aviso no esté aprobada por Meta, WhatsApp solo entrega el aviso si el número de ventas escribió al número de pedidos en las últimas 24 horas. Basta con mandar cualquier mensaje (por ejemplo «ayuda») una vez al día. Con la plantilla aprobada esto ya no es necesario.

## 3. Administrar clientes desde WhatsApp

Solo el número de ventas puede usar estos comandos. Se escriben como un mensaje normal al número de pedidos:

| Escribe | Qué pasa |
|---|---|
| `alta Juan Pérez 871 123 4567` | Registra al cliente. Si ya existía, actualiza su nombre y lo reactiva |
| `baja 871 123 4567` | Lo da de baja: ya no puede hacer pedidos |
| `clientes` | Lista los clientes activos |
| `buscar juan` | Busca por nombre o por parte del teléfono (incluye los dados de baja) |
| `ayuda` | Muestra los comandos |

- El teléfono puede escribirse con o sin espacios, guiones o `+52`. Diez dígitos se toman como número de México.
- El nombre puede ir antes o después del teléfono.
- El cliente puede escribir en cuanto recibes la confirmación.

## 4. Panel de administración

Desde una computadora con acceso al servidor se abre un panel con usuario y contraseña:

- **Clientes:** agregar, corregir nombres, activar/desactivar y buscar.
- **Productos:** el catálogo vigente (código, nombre, alias, unidades, kg por bulto).
- **Pedidos:** los últimos 200 con cliente, productos, fecha solicitada, urgencia y estado.
- **Mensajes:** los mensajes que no se pudieron entregar y el motivo.

El panel no está publicado en internet. Para abrirlo, el administrador técnico crea un túnel seguro (ver README, «Panel web /admin») y entra a `http://localhost:8003/admin`.

## 5. Catálogo de productos

El catálogo lo mantiene el administrador técnico (archivo `data/seed/products.csv`). Para agregar o cambiar un producto, envíale:

- Código (por ejemplo `MAIZ-BLANCO`), nombre y otras formas en que los clientes lo llaman (alias).
- Unidades permitidas: kilos, bultos y/o piezas.
- Kilos por bulto, si aplica.

Los productos inactivos dejan de reconocerse en las conversaciones.

## 6. Problemas frecuentes

| Situación | Qué revisar |
|---|---|
| Un cliente dice que el bot le contesta que no está registrado | Que esté dado de alta y activo: `buscar <su teléfono>`. Si aparece «(inactivo)», usa `alta` para reactivarlo |
| No llegan avisos de pedidos a ventas | Que la plantilla esté aprobada, o que ventas haya escrito en las últimas 24 h. En el panel → Mensajes aparece el motivo |
| El bot no contesta a nadie | Avisar al administrador técnico (puede ser el servidor, el token de Meta o la app de Meta) |
| El bot entendió mal un pedido | El cliente puede corregirlo antes de decir «sí» («no, eran 50 bultos») o escribir «cancelar» para empezar de nuevo |

## 7. Respaldos y datos

- La base de datos (clientes, pedidos y conversaciones) se respalda automáticamente cada noche en el servidor.
- Los mensajes de los clientes se guardan para dar seguimiento a los pedidos. Sólo el personal autorizado tiene acceso al panel.

## 8. Contacto técnico

Administrador técnico: _(nombre, teléfono y correo)_
