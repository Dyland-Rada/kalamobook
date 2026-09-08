# Cómo alimentar Casa del Libro y Fnac

Especificación para reconstruir los feeds desde cero. Escrita el 8 de septiembre
de 2026, con todas las cifras medidas en producción ese día para que la primera
corrida se pueda contrastar.

---

## 1. De dónde se lee: una sola tabla

**`catalogo_publicable`.** Nada más. Una fila por ISBN, con el stock y el precio
final ya calculados.

```
isbn                el ISBN. Clave primaria
titulo              para revisar, no se envía
stock               unidades vendibles AHORA
precio_marketplace  el precio a enviar, ya con todas las reglas aplicadas
precio_odoo         el PVP web (Capa 1 aplicada), como referencia
precio_web          vacío a propósito, ignorar
proveedor           quién lo sirve, para auditar
precio_coste        el PVP que declara el proveedor. NO es un coste (ver 3)
confirmado_en        cuándo lo confirmó por última vez el proveedor
odoo_id             el producto en Odoo
actualizado_en      cuándo se regeneró esta fila
```

Esa tabla se refresca cada hora desde Odoo y **ya tiene aplicadas** las pausas de
proveedor, los apagados por ausencia, el filtro de productos archivados y las tres
capas de precio. Es un contrato: lo que está ahí con stock se puede vender.

### Por qué no leer de otro sitio

**`libros_proveedor` no.** Es la tabla de trabajo: lo que manda cada proveedor en
crudo, sin ninguna regla aplicada. El feed de Fnac leía de ahí y por eso vendió un
libro que no existía. Medido el 19 de agosto: de 674.642 libros con stock en esa
tabla, **309.517 eran de proveedores pausados, mudos o con el dato rancio**.

**Odoo directamente tampoco.** Son 1,27 millones de productos y hay que sumar
catorce almacenes por producto, restar archivados y aplicar las capas de precio.
Eso es exactamente lo que hace el refresco horario: si cada feed lo repite por su
cuenta, acabarán comportándose distinto entre ellos. Ya pasó.

**`odoo_books_mirror` sólo para el PVP si hace falta.** `precio_odoo` de
`catalogo_publicable` ya sale de ahí. No hay motivo para volver a mirarlo.

---

## 2. La consulta, lista para copiar

```sql
SELECT
    'KALAMO-' || c.isbn                       AS sku,
    c.isbn                                    AS ean,
    -- La cantidad. Cada guarda pone 0, NO excluye la fila. Ver apartado 4.
    CASE
        WHEN c.precio_marketplace IS NULL           THEN 0
        WHEN c.precio_marketplace <= 0              THEN 0
        WHEN c.confirmado_en IS NULL                THEN 0
        WHEN c.confirmado_en < now() - interval '7 days' THEN 0
        WHEN EXISTS (SELECT 1 FROM odoo_productos_archivados a
                     WHERE a.barcode = c.isbn)      THEN 0
        WHEN EXISTS (SELECT 1 FROM productos_sin_titulo t
                     WHERE t.ean = c.isbn)          THEN 0
        WHEN EXISTS (SELECT 1 FROM libro_fallido f
                     WHERE f.isbn = c.isbn
                       AND f.resuelto_en IS NULL)   THEN 0
        ELSE GREATEST(c.stock, 0)
    END                                       AS cantidad,
    -- El precio va SIEMPRE, incluso cuando la cantidad es 0: Mirakl y Fnac
    -- rechazan una oferta sin precio, y sin oferta no se puede apagar nada.
    COALESCE(NULLIF(c.precio_marketplace, 0), 0.01) AS precio,
    c.titulo,
    c.proveedor,
    c.confirmado_en
FROM catalogo_publicable c
ORDER BY
    -- Los apagados primero: si el lote se corta, que al menos se hayan
    -- retirado las ofertas que ya no se pueden servir.
    (CASE WHEN c.stock > 0 THEN 1 ELSE 0 END),
    c.isbn
```

Y el delta contra lo último enviado, para no mandar 500.000 líneas cada hora:

```sql
-- se envía sólo lo que cambia de cantidad o de precio
LEFT JOIN mirakl_offer_state m ON m.sku = c.isbn   -- ¡sin prefijo! ver apartado 5
WHERE m.sku IS NULL
   OR m.last_quantity IS DISTINCT FROM <cantidad calculada>
   OR m.last_price    IS DISTINCT FROM <precio calculado>
```

---

## 3. Las reglas de precio

**No hay que implementarlas.** `precio_marketplace` ya las trae aplicadas. Se
documentan para poder verificar y para saber qué se está enviando.

La cadena completa es: **PVP del proveedor → Capa 1 → Capa 3 → menos el céntimo.**

### Capa 1 — suplemento por PVP bajo (API-15)

Se aplica sobre `pvp_base`, el PVP crudo del proveedor. Vive en
`pricing_engine.web_price()` y el resultado es el `list_price` de Odoo, que es el
precio de la web.

| PVP del proveedor | suplemento | resultado |
|---|---|---|
| menos de 2,90 € | — | **no se publica**, el producto se archiva |
| sin precio (0 o vacío) | — | **no se publica**, el producto se archiva |
| 2,90 – 4,99 | +2,00 | |
| 5,00 – 6,00 | +1,50 | |
| 6,01 – 7,50 | +1,00 | |
| más de 7,50 | sin suplemento | |

Es idempotente: siempre se calcula desde `pvp_base`, nunca sobre un precio ya
suplementado, así que reejecutar no acumula.

### Capa 3 — descuento de marketplace

Se aplica **sobre el precio ya suplementado** por la Capa 1.

| precio web | descuento |
|---|---|
| hasta 33,99 | 0 % |
| 34,00 – 40,00 | 1,5 % |
| 40,01 – 50,00 | 2 % |
| 50,01 – 80,00 | 4 % |
| más de 80 | 5 % |

**Aviso pendiente de confirmar con el cliente:** su tabla salta de «0–33» a
«34–40» y deja sin definir el tramo **33,01–33,99**, donde caen 2.844 libros. En
el código se les aplica 0 %, que es lo conservador (no bajar el precio), pero es
una decisión nuestra, no suya.

### El céntimo (API-16)

Al final, **−0,01 €** sobre el resultado. Es capa de exportación: sólo marketplace,
nunca en Odoo ni en la web.

### `precio_coste` no es un coste

Lo primero que hay que entender de esa columna, porque induce a error: **es el PVP
que declara el proveedor** —el precio de cubierta— no lo que pagamos nosotros. El
descuento de distribuidor no está en ninguna tabla.

Por eso `precio_odoo` es igual a `precio_coste` en el 96 % de los libros y **eso es
correcto**: en el tramo de más de 7,50 € la Capa 1 no suma nada, así que el precio
web tiene que ser igual al PVP. Los 447.521 libros de ese tramo tienen una
diferencia media de 0,20 € y no es un problema de margen: es la regla funcionando.

### Ejemplo real de hoy

ISBN 9788477890485, PVP del proveedor 2,92 €. `precio_odoo` = 2,92 y
`precio_marketplace` = **2,91**. En el tramo de 0 a 33,99 el descuento de Capa 3 es
0 %, así que la única diferencia es el céntimo.

Este libro, además, ilustra el punto siguiente: con un PVP de 2,92 le tocaría el
suplemento de +2,00 y no lo tiene.

### La Capa 2 no existe aquí

Es el descuento de cesta de la web, sólo de Shopify, y no está escrito en ningún
sitio. Por eso `precio_web` está vacío en la tabla: **no lo uséis, no lo
rellenéis**. Nadie lo espera.

---

## 4. La regla más importante de todas

**Una guarda pone la cantidad a 0. NUNCA excluye la fila del universo.**

Si un libro deja de cumplir una condición y lo sacáis del `SELECT`, la oferta que
ya está publicada en el marketplace **se queda viva para siempre**, porque no se
envía nada que la apague. Eso es exactamente lo que produjo el problema de las
ofertas fantasma: 20.120 ofertas vivas en Casa del Libro y 86.075 unidades que el
sync no podía alcanzar, porque los filtros de precio estaban en el `WHERE` en vez
de en el `CASE`.

Con la guarda en el `CASE` el libro sale en el lote con cantidad 0 y la oferta se
apaga. Con la guarda en el `WHERE`, desaparece del lote y la oferta sobrevive.

Lo mismo aplica a `cdl_sku_rechazado`: es tentador excluirlos para no gastar cuota,
pero excluirlos impide apagarlos. Cantidad 0, y que se vayan.

### Las guardas, con su tabla y su volumen a día de hoy

| guarda | tabla | libros afectados |
|---|---|---|
| Sin precio | `catalogo_publicable.precio_marketplace IS NULL` | **23.851** |
| Precio 0 o negativo | `precio_marketplace <= 0` | existe al menos 1 |
| Dato rancio (más de 7 días) | `confirmado_en` | 0 hoy |
| Producto archivado en Odoo | `odoo_productos_archivados` (barcode) | 113.898 en total |
| Título que es el EAN | `productos_sin_titulo` (ean) | 64.688 |
| Ya se vendió sin estar | `libro_fallido` (isbn, abiertos) | nuevo, se llena solo |
| Rechazado por el marketplace | `cdl_sku_rechazado` (sku) | 88.058 |

Sobre el catálogo vendible de hoy —**499.542 libros**— eso deja fuera 23.851 por
precio y **51.694 por no tener título** (el 10,3 %: son los productos creados con
el EAN por nombre; publicar eso hunde la conversión).

---

## 5. El formato del SKU: la trampa que cuesta un día

**Verificado hoy contra los export reales de los dos marketplaces:**

| | filas | con `KALAMO-` | ISBN desnudo |
|---|---|---|---|
| `cdl_barrido` (lo que CdL dice tener) | 322.400 | **322.400** | 0 |
| `fnac_barrido` (lo que Fnac dice tener) | 358.802 | **358.802** | 0 |
| `mirakl_offer_state` (lo que creemos enviado) | 1.279.365 | **0** | 1.260.357 |

Es decir:

- Lo que viaja al marketplace es **`KALAMO-<ean>`**.
- Lo que guardan nuestras tablas de estado es **el EAN desnudo**.

Al comparar un export del marketplace con nuestras tablas hay que quitar el
prefijo, o no cuadra ni una fila. Este detalle ya ha hecho perder tiempo dos veces.

---

## 6. Qué enviar a cada uno

### Casa del Libro (Mirakl), por CSV de import de ofertas

- `sku` = `KALAMO-<ean>`
- `price` = `precio_marketplace`
- `quantity` = el `CASE` del apartado 2
- `state_code` = **11** (libro nuevo). Nuestro feed no manda otro estado nunca.
  Si aparecen ofertas con otro `state_code`, no son nuestras.
- Lotes de **50.000 líneas** como máximo, que es el tope con el que venía
  funcionando.
- El resultado del import se consulta después y se anota en `cdl_import_log`
  (`import_id`, `lineas_enviadas`, `lineas_ok`, `lineas_error`). **Hacedlo**: es lo
  único que permite enterarse de que un envío se rechazó.

### Fnac, por XML `offers_update`

Misma cantidad y mismo precio. La única diferencia real está en el estado que
acepta y en el tamaño de lote; el resto de la lógica es idéntica, y **debe seguir
siendo idéntica**. Hoy los dos feeds difieren en detalles que nadie decidió: por
eso Fnac apagó un libro a las 12:23 y Casa del Libro lo siguió ofertando.

### Después de enviar

Anotad lo enviado en `mirakl_offer_state` / `fnac_offer_state`
(`sku` desnudo, `last_quantity`, `last_price`, `last_pushed_at`). Sirve para el
delta de la vuelta siguiente.

**Pero teniendo claro qué son:** esas tablas guardan **lo que nosotros enviamos**,
no lo que el marketplace aplicó. Si un import se rechaza, la tabla dirá que se
envió y el delta no lo reintentará nunca. De ahí el punto siguiente.

---

## 7. Cerrar el círculo: la pieza que ya existe y no se usa

`cdl_barrido` y `fnac_barrido` guardan **el export real de ofertas** de cada
marketplace: lo que ellos dicen tener publicado. Entre las dos hay 681.202 filas de
un barrido que se hizo una vez y no se repitió.

Descargar ese export **una vez por semana** y compararlo con
`catalogo_publicable` es lo que detecta:

- ofertas vivas que ya no deberían estar (las fantasma),
- ofertas que creemos enviadas y no llegaron,
- precios que no coinciden con los nuestros.

No hay que construir nada. Hay que encenderlo.

---

## 8. Lo que NO hay que hacer

- **No leer `libros_proveedor`.** Ver apartado 1.
- **No poner guardas en el `WHERE`.** Ver apartado 4.
- **No recalcular los precios en el feed.** `precio_marketplace` ya los trae. Si se
  reimplementan, en tres meses habrá dos verdades.
- **No enviar sin precio.** Mirakl y Fnac rechazan la oferta, y una oferta
  rechazada no se puede apagar. Precio simbólico y cantidad 0.
- **No publicar libros sin título.** Son 51.694 y el nombre que llevan es su EAN.
- **No confiar en `stock_actualizado_en` como señal de frescura.** No significa lo
  mismo según por dónde entre el fichero del proveedor: por la vía SINLI sólo se
  mueve cuando cambia la cantidad, y por la vía de correo se sella en cada carga.
  La frescura buena es `confirmado_en`.
- **No fiarse de un stock de 1 sin más.** El 57 % del catálogo tiene exactamente un
  ejemplar y ahí está casi todo el riesgo de venta fallida. Ver el apartado
  siguiente.

---

## 9. La decisión que queda por tomar

De las ofertas con **una sola unidad**, el 95 % son de AZETA, que actualiza a
diario con cantidades reales: su «1» es un último ejemplar honesto. El resto son
**1.253 ofertas** cuyo proveedor declara 1 y no lo ha movido en más de un mes.
Ahí es donde se producen las ventas de libros que no existen.

La recomendación es **no ofertar en marketplace un libro cuyo stock sea exactamente
1 y cuya línea de proveedor lleve 7 días o más sin cambiar, salvo AZETA**. Techo
del recorte: 1.843 ofertas, un 0,8 % de las que había vivas.

Es una decisión de negocio, no técnica: se cambian 1.843 ofertas por dejar de
vender libros que no se pueden servir.

---

## 9-bis. Un hallazgo que hay que resolver antes de resubir

**36.824 productos con PVP entre 2,90 y 4,99 no tienen aplicado el suplemento de
la Capa 1.** En el espejo, su `list_price` es exactamente igual a su `pvp_base`,
cuando la regla dice que debería ser `pvp_base + 2,00`.

Son libros baratos, que es justo donde el suplemento existe para que la venta no
salga a pérdida después de comisión y envío. Si se resube el catálogo tal cual, se
publican 36.824 ofertas con el precio sin corregir.

Se arregla ejecutando la actualización de precios, que es idempotente y sólo
escribe lo que cambia:

```
POST /api/v1/pricing/mass-update?dry_run=true    # en seco, dice cuántos toca
POST /api/v1/pricing/mass-update?dry_run=false
GET  /api/v1/pricing/status                      # seguimiento
```

Conviene hacerlo **antes** de la primera corrida del feed, para no tener que
reenviar precios corregidos justo después.

Dos cifras más del mismo repaso, por si sirven de contexto: 93.872 productos del
espejo no tienen `pvp_base` (sin PVP no hay precio que calcular, y la regla los
archiva), y 1.032.942 tienen `list_price = pvp_base`, que es lo esperado para todo
lo que pasa de 7,50 €.

## 10. Números para contrastar la primera corrida

Medidos el 8 de septiembre de 2026 a las 19:50.

| | valor |
|---|---|
| Libros vendibles en `catalogo_publicable` | **499.542** |
| Con precio de marketplace válido | 475.691 |
| Sin precio (van a 0) | 23.851 |
| Sin título (van a 0) | 51.694 |
| Con dato rancio a 7 días | **0** |
| Precio de marketplace más bajo | 0,00 € ← hay que taparlo |
| Precio de marketplace más alto | 1.141,47 € |
| Con exactamente 1 ejemplar | 285.890 (57,2 %) |
| Con 2 o más | 213.791 |

Si la primera corrida da un número de líneas muy distinto de **499.542**, algo está
filtrando en el `WHERE` que debería estar en el `CASE`.
