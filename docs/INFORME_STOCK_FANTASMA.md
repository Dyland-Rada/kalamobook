# Stock fantasma: qué pasa, qué hemos arreglado y qué falta

Informe del 8 de septiembre de 2026. Todas las cifras están medidas en producción
ese día; cuando algo es una estimación, se dice.

Sustituye y resume a `CATALOGO_PUBLICABLE_CONFIRMADO_EN.md`, que se escribió a lo
largo del día y contiene un diagnóstico que luego resultó equivocado. Los
apartados 2 y 3 de este informe corrigen aquello.

---

## 1. El error, en una frase

**Un proveedor declara un ejemplar que no tiene, lo vendemos, y nada en el sistema
registra que falló — así que mañana lo volvemos a vender.**

No se ha roto nada. El sistema nunca ha tenido memoria de sus propios fallos, y
por eso el problema se repite todos los días con libros distintos.

## 2. Qué pasa exactamente

La cadena, con el caso real de *Dentro del cubo blanco* (ISBN 9788496898707):

1. **Distriforma declara 1 ejemplar.** Su fichero lo trae con cantidad 1, tres
   veces al día, todos los días. Ese 1 **no cambia desde el 25 de mayo**: llevan
   106 días equivocados en su propio inventario.
2. **Nosotros lo ofertamos.** Y con razón: el proveedor lo está confirmando hoy.
   Nada en nuestros datos distingue «tengo uno en la estantería» de «mi catálogo
   dice que puedo conseguirlo».
3. **Un cliente lo compra** en Casa del Libro o en Fnac.
4. **El libro no existe.** El pedido no se puede servir.
5. **Odoo lo registra como servido desde WH/Stock**, el almacén propio de Grupo
   Ansa, que no guarda libros. Queda a **−1**.
6. **Ese −1 cancela el +1 del proveedor.** Odoo suma 0 y el libro sale del
   catálogo. Es lo único que funcionaba bien de toda la cadena: al menos deja de
   ofertarse.
7. **Mañana vuelve a empezar** con otro título, porque nada quedó anotado.

Los cuatro casos verificados uno por uno tienen la firma idéntica:

| ISBN | almacén del proveedor | WH/Stock | hora del −1 | días que el proveedor llevaba sin mover ese 1 |
|---|---|---|---|---|
| 9791259801418 | ARC01 +1 (Arcobaleno) | **−1** | 15:50:35 | 15 |
| 9788487715860 | LES01 +1 (Les Punxes) | **−1** | 15:55:34 | 61 |
| 9788496898707 | DIS03 +1 (Distriforma) | **−1** | 16:05:34 | **106** |
| 9788479916268 | DIS03 +1 (Distriforma) | **−1** | 16:10:34 | 7 |

Otros dos reportados, 9788411573832 y 9788436276756, encajan en el mismo patrón.

## 3. Dos diagnósticos que se descartaron por el camino

Se dejan escritos porque los dos parecían correctos y no lo eran.

**«`confirmado_en` miente: es la fecha de regeneración de la tabla.»** No lo es.
Ese campo tiene **6.933 valores distintos** y el más antiguo es del **18 de
julio**, un mes anterior a la regeneración más vieja que existe. La fecha de
regeneración es `actualizado_en`, con 405 valores. Y de las 499.681 filas
vendibles con fecha fresca, las **223.603** que tienen el stock del proveedor sin
cambiar desde hace más de 7 días están **todas** respaldadas por un fichero
recibido esta semana: **ninguna sin justificar**. La fecha dice la verdad sobre lo
que el proveedor afirma; lo que no puede saber es si el proveedor se equivoca.

La corrección que se proponía —usar `stock_actualizado_en`— habría apagado 112.000
libros que los proveedores confirman a diario. Ya se probó una vez: el comentario
del código registra que aquella versión marcaba como rancias 214.901 líneas
cuando los diez proveedores habían mandado fichero ese mismo día.

**«El "1" atascado es la causa de que el Cubo Blanco marque 0.»** Tampoco. Ese
libro marca 0 por el −1 de WH/Stock del apartado anterior. El «1» atascado es un
problema real y grande, pero es la causa de la **venta**, no del 0.

## 4. Cuánto pesa

| | libros |
|---|---|
| Vendibles en el catálogo | 499.681 |
| Con **exactamente 1 ejemplar** de stock | **285.890** (57,2%) |
| Respaldados por una línea que no se mueve desde hace 60+ días | **111.976** (22,4%) |
| …de esos, con cantidad exactamente 1 | **111.976** (todos) |
| A 0 en el catálogo aunque un proveedor con almacén declare stock | 3.802 |
| …de esos, sin estar archivados | **1.278** |

**Los 111.976 son todos un «1» atascado.** No hay un solo caso de cantidad mayor
que uno congelada: el patrón es siempre el mismo.

Y el reparto de las ofertas vivas con una sola unidad dice dónde está el riesgo de
verdad. De las **37.727** vivas en Casa del Libro:

| proveedor | vivas con 1 ud. | y sin moverse en 30+ días |
|---|---|---|
| **AZETA** | **35.884** | **0** |
| sinli@* (Punxes, Akal, Alfaomega…) | 739 | 447 |
| Distriforma | 427 | 412 |
| Ícaro | 223 | 218 |
| Machado | 177 | 0 |
| Distrifer | 113 | 82 |
| Disbook | 91 | 90 |
| Logista | 67 | 0 |
| Arcobaleno | 6 | 4 |

**El 95% son de AZETA y ninguna está rancia**: actualiza a diario con cantidades
reales, así que su «1» es un último ejemplar honesto. El conjunto peligroso son
**1.253 ofertas**, un 0,57% de las 220.004 vivas. Pequeño y quirúrgico.

Un aviso sobre la columna de unidades: **no se puede sumar entre proveedores**.
Doce de los quince mandan 1 o 0 —«hay o no hay»— y solo AZETA (7,4 uds. de media),
Arcobaleno (5,6) y Penguin Random House (79,7) dan cantidades reales.

## 5. Lo que se ha arreglado hoy

Siete commits, todos con el SQL validado contra la base de producción antes de
comprometer.

### 5.1 Registro de libros vendidos sin stock — `d690b9e`

**Es el arreglo de fondo.** Tabla `libro_fallido`. Cuando el catálogo lee Odoo y
encuentra un quant en negativo —la prueba de que se sirvió un pedido contra un
almacén sin la unidad— anota el ISBN junto al **stock que el proveedor declaraba
en ese momento**. Ese libro pasa a 0 en `catalogo_publicable`, y como esa tabla es
el contrato, **Casa del Libro, Fnac y Shopify heredan el corte sin tocar tres
feeds**.

Lo que lo hace funcionar es la condición de salida: el castigo **no** se levanta
porque el proveedor vuelva a mandar el libro —eso lo hace a diario, es justo el
problema— sino **solo cuando cambia el número**.

```sql
-- sigue castigado mientras el proveedor repita exactamente lo mismo
AND NOT EXISTS (
    SELECT 1 FROM libro_fallido f
    WHERE f.isbn = m.barcode
      AND f.resuelto_en IS NULL
      AND f.stock_declarado IS NOT DISTINCT FROM lp.stock_disponible
)
```

El día que Distriforma diga 2, o 0, es información nueva y el libro vuelve solo.
Sin listas que mantener a mano.

Probado en producción: los cuatro casos reales quedan marcados; la liberación no
los suelta mientras el proveedor siga diciendo 1; y una fila de prueba con otro
número se libera en la pasada siguiente. La fila de prueba se borró.

### 5.2 El catálogo ya no acredita stock a un proveedor sin almacén — `8e37776`

La puja por precio no comprobaba que el proveedor tuviera almacén en Odoo. **Penguin
Random House, que no tiene ninguno**, ganaba en 22.193 libros por dar el precio más
bajo: el catálogo acreditaba la venta a quien no podía servirla. Ahora se exige
entrada en `proveedor_almacen_odoo` y se excluye a los pausados.

El daño real era 1 libro —PEN01 respaldaba los otros 22.192— pero quedaba la avería
latente: si PEN01 deja de llegar, esos 22.000 se quedan publicados sin respaldo.

### 5.3 Elección de proveedor reproducible — `8e37776`

Al validar el cambio anterior salió que 30.489 filas cambiaban de proveedor, más
que las 22.193 de Penguin. La causa: con precios empatados, `LIMIT 1` sin desempate
alternaba entre corridas y la columna `proveedor` bailaba sin que nada hubiera
cambiado. Añadido desempate por email.

### 5.4 El cron de publicación ya falla ruidosamente — `8e37776`

El ciclo diario solo miraba los fallos al **subir**. El del 8 de septiembre
registró `"0 fichas generadas, 1 publicada, 0 fallidas"` y **no marcó error**,
cuando las 899 generaciones habían muerto con HTTP 401 de DeepSeek. Ahora los
fallos de generar van aparte y generar en seco marca error.

### 5.5 Los títulos limpiados ya no vuelven malos — `8e37776`

`manual_fill.py` guardaba `title or isbn`: abrir una ficha sin título y darle a
guardar reescribía el EAN como nombre, en el espejo y en Odoo. Por eso el otro
equipo limpiaba títulos y reaparecían. Sin título de verdad ya no se toca el
nombre, y un título que son sólo dígitos se rechaza.

Además, la cascada de títulos de `crear_faltantes.py` y `auto_scrape.py` sólo
miraba `books` y `distributor_books`. Se le añaden `cdl_isbn_index` (683.112
títulos de Casa del Libro) y `sinli_precios` (del propio proveedor), que cubren
**18.570 de los 67.091** productos que hoy llevan el EAN por nombre.

### 5.6 Dos endpoints de medición — `d690b9e`

El daño era inmedible desde el panel: los negativos se encontraban de uno en uno,
mirando fichas a mano.

- `GET /api/v1/audit/quants-negativos` — los quants en negativo de Odoo agrupados
  por ubicación, con los últimos en detalle. Dice de una vez cuántos libros se han
  vendido sin existir y dónde se están apuntando.
- `GET /api/v1/audit/libros-fallidos` — los castigos abiertos, con lo que declaraba
  el proveedor al fallar y lo que declara ahora.

### 5.7 Un arreglo que se hizo y se revirtió — `78548fb` → `caefd01`

Se deja escrito porque la lección importa. Se tapó cada quant a 0 antes de sumar,
creyendo que un −1 en WH borraba stock real de un proveedor. **Era al revés**: el
−1 es la huella de la venta fallida, y taparlo habría devuelto los seis libros a la
venta para fallar otra vez. La suma en crudo estaba dando el resultado correcto.
Quedó sólo el contador de negativos, que es lo que sí faltaba.

---

## 6. Lo que falta por arreglar

### 6.1 El albarán se valida contra el almacén equivocado — **es de ellos, y es lo urgente**

Los workflows `CdL - Descontar stock al SHIPPED (validar albaran)` y su gemelo de
Fnac se crearon el 8 de septiembre a las **15:29 y 15:31**. El primer quant
negativo apareció a las **15:50:35**, y luego uno cada cinco minutos.

La intención es correcta —descontar el stock al enviar, que antes no se hacía— pero
**validan contra WH en vez de contra el almacén del proveedor que tiene la
unidad**. Y sobre todo: si el almacén del proveedor no tiene la unidad, **no
debería validar nada**; debería parar y avisar. Un quant negativo tendría que ser
imposible. Hoy es la forma que tiene el sistema de tragarse un pedido que no puede
servir.

**Trampa importante si lo arreglan:** el registro de fallidos (5.1) se alimenta
precisamente de esos negativos. Si dejan de crearse, **se queda sin fuente**. Hay
que alimentarlo también desde el pedido cancelado del marketplace, que es la señal
buena. Conviene montar las dos cosas a la vez.

### 6.2 Los dos sync de stock están apagados

`CdL - Sync Stock (Working)` y `Fnac - Sync Stock` están en `active: false`, con
fecha de modificación del 8 de septiembre a las 16:50. Los últimos envíos son de
las 16:44. **Mientras sigan así no se actualiza stock en ninguno de los dos
marketplaces**, ni para encender ni para apagar. Si no fue deliberado, hay que
volver a activarlos.

### 6.3 El corte del «1 rancio» — red de seguridad

El registro de fallidos impide el segundo fallo. Para evitar el primero hace falta
una heurística: **no ofertar en marketplace un libro cuyo stock sea exactamente 1
y cuya línea de proveedor no se haya movido en 7 días o más, salvo AZETA**. Los
cuatro casos verificados llevaban 106, 61, 15 y 7 días quietos, así que el umbral
de 7 los coge todos. Techo del recorte: 1.843 ofertas en Casa del Libro, un 0,8%.

Va en los feeds de n8n. Los de AZETA con una unidad son 35.884 y también pueden
fallar, pero su dato es fiable y se actualiza a diario: la recomendación es no
tocarlos.

### 6.4 Separar «visto» de «cambiado» — el arreglo limpio

`stock_actualizado_en` **no significa lo mismo según por dónde entre el fichero**,
y eso es un fallo nuestro:

- Por la vía **SINLI** (Ícaro, Distriforma, Disbook, Distrifer, Akal, Alfaomega,
  Les Punxes) el upsert sólo toca la fecha **si la cantidad cambia**. Correcto.
- Por la vía de **correo con n8n** (Logista, Machado, Penguin) nuestro upsert hace
  `stock_actualizado_en = NOW()` **siempre**, cambie o no la cantidad.

Por eso Logista, Machado y Penguin aparecen con un 0% de líneas quietas: no es que
su stock se mueva, es que les sellamos la fecha en cada carga. Como señal
comparable entre proveedores, el campo no vale.

El arreglo es **añadir** una columna, no reinterpretar la que hay:

| columna | qué guarda | quién la escribe |
|---|---|---|
| `stock_visto_en` | **siempre** `NOW()` en cada carga | todas las vías |
| `stock_cambiado_en` | sólo cuando la cantidad **difiere** | todas las vías |

`ALTER TABLE libros_proveedor ADD COLUMN stock_visto_en TIMESTAMP` es instantáneo
en PostgreSQL 15 al no llevar `DEFAULT`. Con las dos columnas, el fantasma es una
consulta trivial y consistente para los quince proveedores.

**No se ha hecho todavía a propósito**, porque tiene un riesgo que hay que decidir
antes: el push a Odoo selecciona qué mandar con `stock_actualizado_en > marcador`.
Si dejamos de sellarlo cuando la cantidad no cambia, un libro cuyo quant se perdió
en Odoo —los quants desaparecen al llegar a 0— dejaría de reenviarse nunca.

### 6.5 Pendientes que no son de stock

- **Las claves de DeepSeek dan HTTP 401.** Cero fichas generadas desde el 21 de
  agosto; 899 fallos por noche, 0 tokens gastados. Bloquea los 24.699 libros
  pendientes de publicar. Hacen falta claves nuevas en `DEEPSEEK_API_KEYS`. Coste
  medido: 2.217 tokens de entrada y 778 de salida por ficha, o unos 54,8M + 19,2M
  para los 24.699.
- **Penguin está dado de alta dos veces**: `penguin@kalamo.local` (26.868 libros,
  almacén PEN01) y `sinli@penguinrandomhouse.com` (27.053 libros, sin almacén)
  comparten **26.764 ISBN**, y en 319 títulos los dos registros se contradicen. Hay
  que decidir cuál es la fuente buena antes de tocar nada: crearle almacén al
  segundo duplicaría el stock.
- **49.297 filas de stock no llegan a Odoo.** 23.700 son Penguin Random House, sin
  almacén donde escribirlas; las otras 25.597 encajan con los **198.473 errores de
  `product.product no encontrado`** que hay sin resolver.
- **23.852 libros tienen stock en Odoo y no tienen precio**, así que el catálogo no
  los puede vender aunque el stock esté bien.
- **`shopify_inventario` no se relee desde el 19 de agosto.** Se escribe a diario
  pero nunca se vuelve a leer de la tienda, así que el sync compara contra su
  propia creencia.
- **Ocho de los quince proveedores no tienen forma de verificar si la foto vino
  completa.** `sinli_auditoria` sólo cubre siete. Para Logista (60.980 libros), los
  dos Penguin, Machado, Anaya, Arcobaleno, Podiprint y AZETA, el apagado por
  ausencia trabaja a ciegas.
- **Tres proveedores fantasma de alta**, uno de ellos `kalamo.web@gmail.com`
  registrado como «ICARO DISTRIBUIDORA», y `libros_proveedor_nueva` con 494.808
  filas muertas de una migración.
- **Rotar credenciales**: el token de Dokploy que se usó hoy, y las que quedaban de
  antes (clave de API de Odoo, contraseña del panel, secreto de cliente de
  Shopify).

---

## 7. Qué mirar mañana para saber si funciona

Con el deploy hecho, estos tres números cuentan la historia:

1. `GET /api/v1/audit/quants-negativos` — si sigue creciendo, el punto 6.1 no está
   arreglado y se siguen vendiendo libros que no existen.
2. `GET /api/v1/audit/libros-fallidos` — cuántos castigos hay abiertos. Debería
   subir los primeros días y estabilizarse.
3. El resumen del cron del catálogo, que ahora incluye `quants_negativos` y
   `fallidos_abiertos` en cada vuelta.
