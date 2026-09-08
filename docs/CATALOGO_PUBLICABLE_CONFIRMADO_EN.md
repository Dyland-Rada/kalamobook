# El caso del Cubo Blanco: qué falla de verdad en `catalogo_publicable`

Respuesta al diagnóstico recibido el 8 de septiembre de 2026.
Todas las cifras son medidas en producción ese día entre las 16:50 y las 16:55.

El diagnóstico **acierta en el síntoma y en la gravedad**, pero se equivoca en el
mecanismo, y la corrección que propone haría el problema peor. Abajo está la
prueba de cada cosa y cuál es el arreglo que sí funciona.

---

## 1. `confirmado_en` no es la fecha de regeneración

Esto es comprobable en dos segundos y es importante, porque de ahí sale la
propuesta de arreglo.

```sql
SELECT count(*), count(DISTINCT confirmado_en), count(DISTINCT actualizado_en),
       min(confirmado_en), max(confirmado_en),
       min(actualizado_en), max(actualizado_en)
FROM catalogo_publicable;
```

| campo | valores distintos | más viejo | más nuevo |
|---|---|---|---|
| `confirmado_en` | **6.933** | 18/07/2026 21:09 | 08/09/2026 16:31 |
| `actualizado_en` | 405 | 21/08/2026 15:23 | 08/09/2026 16:39 |

`actualizado_en` **sí** es la fecha de regeneración: 405 valores, uno por corrida.
`confirmado_en` tiene 6.933 valores distintos y llega hasta el **18 de julio**,
un mes antes de la regeneración más antigua que existe. No puede ser un sello de
regeneración.

Lo que es, según el código que la genera (`catalogo_publicable.py`):

```sql
GREATEST(lp.stock_actualizado_en, ce.visto) AS confirmado
-- ce.visto = MAX(registrado_en) FROM cegald_isbns_v2
--            WHERE isbn = ... AND proveedor_email = ...
```

Es **la fecha más reciente entre dos hechos reales**: cuándo cambió el stock de ese
proveedor, y cuándo fue la última vez que ese ISBN vino en un fichero suyo.

## 2. No hay ni una fila con fecha fresca inventada

La afirmación de que el 34% del catálogo tiene fecha fresca falsa se puede
contrastar directamente. Nuestra medición es incluso mayor en el primer tramo:

| | filas |
|---|---|
| Vendibles con `confirmado_en` < 7 días | 499.681 |
| …de las que el proveedor no ha *cambiado* el stock en 7 días | **223.603** (44,7%) |
| …de esas, respaldadas por un CEGALD recibido en los últimos 7 días | **223.603** |
| …de esas, **sin ninguna justificación** | **0** |

Las 223.603 no son un error de la tabla: son libros que el proveedor **volvió a
declarar en su fichero esta semana** con la misma cantidad que ya tenía. La fecha
dice la verdad sobre lo que el proveedor afirma.

## 3. No elige "el proveedor con más stock"

El criterio es el **precio más bajo**, no la cantidad:

```sql
ORDER BY precio_con_iva NULLS LAST
LIMIT 1
```

Y hay un detalle que cambia el análisis: **la cantidad ofertada no sale del
proveedor, sale de Odoo**, que suma todos los almacenes. La columna `proveedor` es
sólo atribución: dice quién es el más barato de los que declaran stock. Cambiar
quién aparece ahí **no cambia el stock que se oferta**. Elegir AZETA en vez de
Distriforma no habría salvado la venta de Javier.

---

## 4. Lo que de verdad pasó con el Cubo Blanco

ISBN 9788496898707, *Dentro del cubo blanco. La ideología del espacio expositivo*.

| dato | valor |
|---|---|
| stock en el catálogo **ahora** | 0 *(ya se apagó solo)* |
| proveedor atribuido | Distriforma |
| `confirmado_en` | 08/09 10:41 |
| stock que declara Distriforma | **1** |
| última vez que ese 1 **cambió** | **25/05/2026** |
| última vez que vino en un fichero de Distriforma | **08/09 16:12** |
| veces que Distriforma lo ha reafirmado por día | **3 al día, todos los días** |

Distriforma manda ese libro con cantidad 1 en cada fichero, tres veces al día,
desde mayo. El 1 nunca se mueve porque **para Distriforma no ha cambiado nada**:
su propio inventario dice 1 y lleva equivocado 106 días.

`confirmado_en` no miente. Registra fielmente «Distriforma dijo 1 hoy». Lo que no
puede saber es que el inventario de Distriforma está mal.

**Esto es el bug de raíz: un "1" atascado que el proveedor reafirma para siempre
es, en nuestros datos, indistinguible de stock real confirmado a diario.**

## 5. Cuánto pesa

| | libros |
|---|---|
| Vendibles en el catálogo | 499.681 |
| Respaldados por una línea que no se mueve desde hace 60+ días | **111.976** (22,4%) |
| …de esos, con cantidad exactamente 1 | **111.976** (todos) |
| Respaldados por una línea quieta desde hace 120+ días | 32 |

**Los 111.976 son todos un "1" atascado.** No hay un solo caso de cantidad mayor
que uno congelada. El patrón es exactamente el del Cubo Blanco, y es un quinto
del catálogo.

Por proveedor, sobre sus líneas con stock:

| proveedor | líneas con stock | quietas 60+ días | % | uds. media |
|---|---|---|---|---|
| ICARO | 45.152 | 44.522 | **98,6%** | 1,0 |
| DISBOOK | 7.888 | 7.571 | **96,0%** | 1,0 |
| DISTRIFORMA | 36.469 | 34.650 | **95,0%** | 1,0 |
| DISTRIFER | 19.220 | 13.258 | **69,0%** | 1,0 |
| AZETA | 255.111 | 0 | 0% | 7,4 |
| LOGISTA | 60.980 | 0 | 0% | 1,0 |
| MACHADO | 24.087 | 0 | 0% | 1,0 |
| PENGUIN (PEN01) | 23.604 | 0 | 0% | 1,0 |
| ANAYA | 18.307 | 0 | 0% | 1,0 |
| ARCOBALENO | 7.392 | 0 | 0% | 5,6 |

---

## 6. Por qué la corrección propuesta empeoraría las cosas

> «Que `confirmado_en` refleje la fecha real del proveedor, no la de regeneración»

Esa fecha ya está en la tabla y **es la que se usaba antes**. Se cambió porque
rompía el catálogo. El comentario que quedó en el código dice la cifra:

```
-- stock_actualizado_en, en la via SINLI, solo se mueve cuando CAMBIA la
-- cantidad: un libro que Distriforma confirma a diario con 1 unidad se
-- quedaba con la fecha del ultimo cambio y parecia rancio. Medido:
-- 214.901 lineas salian con "mas de 7 dias" cuando en realidad los diez
-- proveedores habian mandado fichero hoy.
```

Volver a `stock_actualizado_en` apagaría hoy **112.000 libros que el proveedor está
confirmando activamente**. Algunos son fantasmas y otros no, y ese campo no
distingue entre los dos.

Y hay una razón peor, que **es un fallo nuestro**: el campo no significa lo mismo
según por dónde entre el fichero.

- Por la vía **SINLI** (ICARO, Distriforma, Disbook, Distrifer, Akal, Alfaomega,
  Les Punxes) el upsert sólo toca la fecha **si la cantidad cambia**. Correcto.
- Por la vía de **correo con n8n** (Logista, Machado, Penguin) nuestro upsert hace
  `stock_actualizado_en = NOW()` **siempre**, cambie o no la cantidad.

Por eso Logista, Machado y Penguin salen con un 0% de líneas quietas: no es que su
stock se mueva, es que les sellamos la fecha en cada carga. **El campo está roto en
las dos direcciones opuestas a la vez**: siempre fresco para tres proveedores,
falsamente rancio para cuatro. Como señal comparable entre proveedores no vale, y
ése es justo el motivo por el que la tabla usa `cegald_isbns_v2`.

> «Que excluya proveedores no fiables (Podiprint, Ícaro, Distriforma...)»

La intención está bien dirigida pero las etiquetas no. Ícaro y Distriforma
**actualizan a diario** — hoy a las 13:17 y a las 16:12. Podiprint no es que «casi
no actualice»: es impresión bajo demanda con un 100.000 plano que no se mueve por
diseño. La distinción que sirve no es proveedor fiable contra no fiable, sino
**cantidad real contra semáforo 1/0**: sólo AZETA (7,4 uds. de media), Arcobaleno
(5,6) y Penguin Random House (79,7) mandan cantidades; los otros doce mandan «hay
o no hay».

---

## 7. El arreglo que sí funciona

**Separar las dos fechas, que hoy están mezcladas en un solo campo.** En
`libros_proveedor`:

| columna | qué guarda | quién la escribe |
|---|---|---|
| `stock_visto_en` | **siempre** `NOW()` en cada carga | todas las vías |
| `stock_cambiado_en` | sólo cuando la cantidad **difiere** | todas las vías |

Con las dos, el fantasma se vuelve una consulta trivial y consistente para los
quince proveedores:

```sql
-- el patron del Cubo Blanco
WHERE stock_visto_en   > now() - interval '2 days'   -- lo sigue reafirmando
  AND stock_cambiado_en < now() - interval '60 days'  -- y no se mueve
  AND stock_disponible  = 1                           -- y es el semaforo
```

Y entonces se puede decidir con criterio: no ofertar esos 111.976 en marketplace,
u ofertarlos con plazo ampliado, o pedir a esos cuatro proveedores que revisen su
inventario. Eso es negocio, no código.

Hace falta en tres sitios:

1. `ALTER TABLE libros_proveedor ADD COLUMN stock_visto_en TIMESTAMP` — instantáneo
   en PostgreSQL 15 al no llevar `DEFAULT`.
2. `sync_stock_sinli.py`: escribir `stock_visto_en = NOW()` siempre, y renombrar el
   uso actual a `stock_cambiado_en`.
3. El workflow de n8n de stock manual: dejar de sellar la fecha de cambio cuando la
   cantidad no cambia, y sellar `stock_visto_en` en su lugar.

**No se ha hecho todavía a propósito.** El punto 3 tiene un riesgo real que hay que
decidir antes: hoy el push a Odoo selecciona lo que mandar con
`stock_actualizado_en > marcador`. Si dejamos de sellarlo cuando la cantidad no
cambia, un libro cuyo quant se perdió en Odoo (los quants desaparecen al llegar a
0) dejaría de reenviarse nunca. Por eso el arreglo es **añadir** una columna y no
cambiar el significado de la que hay.

## 8. Lo que sí se cambió hoy

En el commit `8e37776`, y no tiene que ver con las fechas:

La puja por precio no comprobaba que el proveedor tuviera almacén en Odoo. Penguin
Random House, que no tiene, ganaba en 22.193 libros: el catálogo acreditaba la
venta a quien no podía servirla. Ahora se exige `proveedor_almacen_odoo` y se
excluye a los pausados. También se añadió desempate por email, porque con precios
iguales la columna `proveedor` bailaba entre corridas sin que nada hubiera
cambiado.

## 9. Resumen en una frase

El filtro por `cp.confirmado_en` no era un punto ciego por usar una fecha
inventada, sino por usar **una fecha honesta que responde a otra pregunta**: dice
si el proveedor lo ha vuelto a declarar, no si su declaración es de fiar. Ir
directo a `libros_proveedor` de AZETA funciona porque AZETA manda cantidades
reales y las mueve a diario, no porque la tabla mienta.
