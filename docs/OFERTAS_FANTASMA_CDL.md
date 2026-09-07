# Ofertas fantasma en Casa del Libro

**Fecha:** 7 de septiembre de 2026
**Síntoma reportado:** se venden libros que Casa del Libro dice tener con stock, pero en Odoo
no hay, y la venta se bloquea.

Este documento recoge lo que encontramos al investigarlo, con los números medidos contra la
base de datos de producción, y las opciones de arreglo evaluadas una por una.

---

## Resumen en tres líneas

1. **El diagnóstico que circulaba apunta al sitio equivocado.** El sync de CdL sí lee
   `catalogo_publicable`, y una oferta que sale de esa tabla **sí** se puede bajar a 0.
2. La causa real es el filtro `WHERE m.list_price >= 2.90` del sync: cuando el PVP baja de
   2,90 €, la fila **desaparece del universo de la consulta** y ya nunca se le manda un 0.
3. Se enlaza con la regla API-15, que archiva en Odoo esos mismos libros. El resultado es una
   oferta viva en CdL sobre un producto archivado: el cliente compra y la venta se bloquea.

**Tamaño del agujero: 20.120 ofertas vivas con 86.075 unidades ofertadas** que el sync ya no
puede tocar.

---

## Lo que se comprobó, y lo que resultó falso

### "El sync lee `libros_proveedor`, no `catalogo_publicable`"

**Falso.** Lo dice la descripción del workflow `CdL - Sync Stock (Working)`
(`eB5VlnqcQZEh80o7`), pero la consulta real hace:

```sql
FROM odoo_books_mirror m
LEFT JOIN catalogo_publicable cp ON cp.isbn = m.barcode
LEFT JOIN odoo_productos_archivados a ON a.barcode = m.barcode
```

Alguien migró la fuente y no actualizó el texto de la descripción. **Conviene corregir esa
descripción**, porque es la que llevó al diagnóstico equivocado.

### "Si una oferta sale de `catalogo_publicable`, el sync ya no puede bajarla a 0"

**Falso.** Es un `LEFT JOIN`: si el ISBN no está en la tabla, `cp.proveedor` es `NULL`, el
`CASE` cae al `COALESCE(..., 0)` y **se envía cantidad 0**. Ese camino funciona.

```sql
CASE
  WHEN a.barcode IS NOT NULL THEN 0
  ELSE LEAST(50, COALESCE(MAX(CASE WHEN cp.proveedor IS NOT NULL
       AND cp.confirmado_en > now() - interval '7 days' THEN cp.stock END), 0))::INTEGER
END AS quantity
```

Ese `CASE` está bien construido: contempla archivados, ausentes y datos rancios de más de
7 días. El problema no está ahí.

### "¿`catalogo_publicable` se actualiza?"

**Sí.** Cron horario, sin fallos. La tabla no es el problema.

### "¿Se han archivado libros con stock en Odoo?"

**Sí, pero no a mano: lo hace el sistema solo, cada hora.** En `sync_stock_sinli.py`:

```python
# ── 8b. APAGAR los de PVP < 2,90 (regla API-15) ──
await odoo.write("product.template", chunk, {"active": False})
```

`pricing_engine.web_price()` devuelve `None` cuando el PVP es menor de 2,90 € **o cuando no
hay precio**, y todo lo que cae ahí se archiva. También se desarchiva solo (`active: True`)
cuando el precio vuelve a ser válido.

Estado actual: **113.795 productos archivados**, de los cuales **2.877 tienen stock de
proveedor** y **1.051 tienen oferta viva en CdL**.

---

## El mecanismo real, paso a paso

```
1. El PVP de un libro baja de 2,90 EUR (o llega sin precio)
        |
        v
2. El sync SINLI horario lo ARCHIVA en Odoo  (product.template.active = False)
   -> el producto ya no admite stock
   -> cualquier venta se bloquea, en TODOS los canales
        |
        v
3. Ese mismo PVP < 2,90 hace que el sync de CdL lo EXCLUYA de su universo:
        WHERE m.list_price::NUMERIC >= 2.90
   -> la fila no existe en la consulta
   -> nunca se compara con mirakl_offer_state
   -> nunca se le envia un 0
        |
        v
4. La oferta sigue publicada en CdL con la ULTIMA cantidad que tuvo, para siempre
        |
        v
5. Un cliente la compra  ->  VENTA BLOQUEADA
```

Lo perverso es que **la red de seguridad existe y queda desactivada por el mismo filtro**. La
tabla `odoo_productos_archivados` está pensada exactamente para esto, y el `CASE` la usa
(`WHEN a.barcode IS NOT NULL THEN 0`). Pero un producto archivado por la regla API-15 tiene,
por definición, PVP < 2,90, así que el `WHERE` lo saca de la consulta antes de que el `CASE`
llegue a mirarlo.

---

## Los números (medidos el 07/09/2026)

Ofertas vivas (`mirakl_offer_state.last_quantity > 0`) que han quedado fuera del universo del
sync:

| Concepto | Ofertas |
|---|---|
| **Total fuera de control** | **20.120** |
| por PVP < 2,90 € o sin precio | 17.432 |
| rechazadas por CdL (`cdl_sku_rechazado`) | 2.688 |
| de esas, además archivadas en Odoo | 1.051 |
| **Unidades ofertadas** | **86.075** |

Contexto:

| Concepto | Valor |
|---|---|
| Ofertas registradas en `mirakl_offer_state` | 1.250.167 |
| Con stock publicado (> 0) | 481.727 |
| Unidades publicadas | 2.035.493 |
| Empujón más antiguo | 01/07/2026 |
| Empujón más reciente | 07/09/2026 18:00 |

Son un **4,2 % de las ofertas con stock**, pero son justo el 4 % que produce cancelaciones.

Y el otro lado del mismo problema:

| Concepto | Valor |
|---|---|
| Productos archivados en Odoo | 113.795 |
| Archivados **con stock de proveedor** | 2.877 |
| Archivados **con oferta viva en CdL** | 1.051 |

---

## Opciones de arreglo, evaluadas

### A. Mover el filtro de precio del `WHERE` al `CASE` — *recomendada, NO es nuestra*

Que un PVP bajo produzca **cantidad 0** en vez de sacar la fila del universo:

```sql
CASE
  WHEN a.barcode IS NOT NULL THEN 0
  WHEN m.list_price IS NULL OR m.list_price::numeric < 2.90 THEN 0
  ELSE LEAST(50, COALESCE(MAX(CASE WHEN cp.proveedor IS NOT NULL
       AND cp.confirmado_en > now() - interval '7 days' THEN cp.stock END), 0))::INTEGER
END AS quantity
```

y quitar `AND m.list_price::NUMERIC >= 2.90` del `WHERE`.

**Trampa que no se ve en el SQL.** No basta con tocar la consulta. El nodo `Generar CSV stock`
descarta esas mismas filas por su cuenta:

```javascript
const precioFinal = calcularPrecio(p.price);
if (precioFinal === null) { descartados++; continue; }   // <-- aqui se pierden
```

`calcularPrecio` devuelve `null` para la regla de Capa 1 "NO PUBLICAR" (< 2,90). Si solo se
cambia el SQL, **el arreglo no funciona**: las filas llegan pero el CSV las tira. Hay que
dejar pasar las de cantidad 0 con un precio simbólico, porque Mirakl exige el campo precio
aunque la cantidad sea cero:

```javascript
const precioFinal = calcularPrecio(p.price);
const qty = Math.max(0, Math.floor(p.quantity));
if (precioFinal === null) {
  if (qty > 0) { descartados++; continue; }   // sin precio valido no se publica
  csv += `KALAMO-${p.sku};${p.sku};EAN;9999.00;0;5;update\n`;  // pero SI se apaga
  skus.push(p.sku);
  continue;
}
```

- **Coste:** una pasada de ~20.120 líneas la primera vez, luego nada.
- **Riesgo:** bajo, pero **hay que medirlo antes con un dry run**: generar el CSV y contar
  líneas sin enviarlo a Mirakl.
- **Aplica al mismo problema en Fnac**, que casi seguro comparte el patrón.

### B. Tratar `cdl_sku_rechazado` igual — *complementaria, NO es nuestra*

Las 2.688 rechazadas tienen el mismo defecto: `NOT EXISTS (...)` las saca del universo, así
que tampoco se pueden apagar nunca. Si CdL rechaza un SKU, la oferta previa se queda viva.

Mismo tratamiento: sacarlas del `WHERE` y forzarles cantidad 0.

### C. Barrido de una sola vez para las 20.120 ya existentes — *NO es nuestra*

Aunque se arregle la consulta, conviene un barrido explícito que las ponga a 0 de golpe, en
vez de esperar a que el delta las detecte. Es un CSV de 20.120 líneas con cantidad 0.

**Ojo:** hacerlo *después* de A, no antes; si no, el sync las volverá a dejar como estaban.

### D. Dejar de archivar en Odoo los libros con stock — *evaluada y DESCARTADA*

Sería tentador: si no se archivan, no se bloquea la venta. Pero:

- La regla API-15 es una decisión comercial acordada, no un fallo. Cambiarla no nos toca.
- No resuelve el problema de fondo: la oferta de CdL seguiría sin poder bajarse a 0, porque
  el filtro de precio del sync no depende de si el producto está archivado.
- Y desarchivar 113.795 productos tendría efectos en cadena en todos los canales.

Queda descartada. Se documenta para que no se vuelva a proponer.

### E. Filtrar los archivados en `catalogo_publicable` — *APLICADA, sí es nuestra*

Ver la sección siguiente.

---

## Lo que ya hemos arreglado

### `catalogo_publicable` incluía productos archivados

**Es un fallo nuestro y está corregido.** El contrato de esa tabla, escrito en su propio
docstring, es *"lo que se puede vender AHORA"*. Un producto archivado no se puede vender.

`_totales_odoo()` leía los quants sin filtrar:

```python
# antes
"stock.quant", [["location_id.usage", "=", "internal"]],

# ahora
"stock.quant", [["location_id.usage", "=", "internal"],
                ["product_id.active", "=", True]],
```

Odoo conserva los quants de un producto archivado, así que entraban en la tabla como
vendibles. Eran **2.877** el 07/09/2026, y no es un caso raro: la regla API-15 archiva
continuamente todo lo que baja de 2,90 € o llega sin precio, así que es un goteo permanente.

**Efecto esperado tras el despliegue:** unas 2.877 filas menos en `catalogo_publicable`, y
cualquier feed que lea esa tabla deja de ofertar productos imposibles de servir.

**Lo que este arreglo NO resuelve:** las 17.432 ofertas de CdL con PVP < 2,90. Esas están
fuera del universo de la consulta del sync por el filtro de precio, así que da igual lo que
diga nuestra tabla: el sync no llega a mirarla. Para eso hace falta la opción A.

---

## Reparto de responsabilidades

| Arreglo | Dónde vive | Estado |
|---|---|---|
| E. Filtrar archivados en `catalogo_publicable` | `catalogo_publicable.py` (nuestro) | **hecho**, pendiente de desplegar |
| A. Filtro de precio al `CASE` + CSV | n8n `CdL - Sync Stock (Working)` | sugerencia |
| B. Mismo trato a `cdl_sku_rechazado` | n8n, misma consulta | sugerencia |
| C. Barrido de las 20.120 | n8n, ejecución única | sugerencia |
| Corregir la descripción del workflow | n8n, campo descripción | sugerencia |
| Revisar si Fnac tiene el mismo patrón | n8n `Fnac - Sync Stock` | sin comprobar |

---

## Cómo reproducir los números

Ofertas vivas fuera del universo del sync:

```sql
SELECT count(*) AS ofertas_vivas_fuera_de_control,
       count(*) FILTER (WHERE m.barcode IS NULL) AS no_estan_en_el_mirror,
       count(*) FILTER (WHERE m.barcode IS NOT NULL
              AND (m.list_price IS NULL OR m.list_price::numeric < 2.90)) AS pvp_bajo_2_90,
       count(*) FILTER (WHERE r.sku IS NOT NULL) AS rechazados_por_cdl,
       count(*) FILTER (WHERE a.barcode IS NOT NULL) AS archivados_en_odoo,
       COALESCE(sum(s.last_quantity),0) AS unidades_ofertadas
FROM mirakl_offer_state s
LEFT JOIN odoo_books_mirror m ON m.barcode = s.sku
LEFT JOIN cdl_sku_rechazado r ON r.sku = s.sku
LEFT JOIN odoo_productos_archivados a ON a.barcode = s.sku
WHERE s.last_quantity > 0
  AND (m.barcode IS NULL OR m.list_price IS NULL
       OR m.list_price::numeric < 2.90 OR r.sku IS NOT NULL);
```

Archivados que siguen apareciendo como vendibles:

```sql
SELECT count(*) AS archivados_totales,
       count(*) FILTER (WHERE cp.isbn IS NOT NULL AND cp.stock > 0) AS archivados_pero_con_stock,
       count(*) FILTER (WHERE s.sku IS NOT NULL AND s.last_quantity > 0) AS archivados_con_oferta_viva
FROM odoo_productos_archivados a
LEFT JOIN catalogo_publicable cp ON cp.isbn = a.barcode
LEFT JOIN mirakl_offer_state s ON s.sku = a.barcode;
```
