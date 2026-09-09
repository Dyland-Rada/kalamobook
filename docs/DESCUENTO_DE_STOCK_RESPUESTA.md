# Descuento de stock al enviar: respuesta del backend

Respuesta a la especificación del 9 de septiembre de 2026.
Verificado en el código y en producción antes de contestar.

---

## Resumen

**Las cuatro preguntas tienen la misma raíz, y la respuesta corta es que en dropship
el stock no es nuestro para descontarlo.** El número que hay en Odoo no es un
inventario: es el eco del fichero que mandó el proveedor esta mañana, y se
sobrescribe entero en cada sync. Restarle uno es escribir sobre algo que va a ser
pisado.

Y una buena noticia para vuestro punto bloqueante: **5.3 ya no bloquea.** El
registro de fallidos guarda los 135 casos en tabla propia, así que la información
ya no vive en el quant negativo. Además queda montado el endpoint que pedís.

---

## 5.1 — ¿Descontar del almacén del proveedor en vez de WH?

**No, y no funcionaría.**

El sync escribe `stock.quant.inventory_quantity`, que es una **cantidad absoluta**,
no un movimiento:

```python
# sync_stock_sinli.py
"inventory_quantity": qty,
```

Si descontamos una unidad de `DIS03/Stock`, el siguiente fichero de Distriforma
vuelve a poner la cantidad que ellos declaran y **borra el descuento**. Para AZETA
eso pasa cada hora; para el resto, cada día. El descuento dura lo que tarde el
siguiente fichero.

**Y descontar de WH, que es lo que se hace ahora, es peor**, porque el efecto sí es
permanente y va en la dirección equivocada. `catalogo_publicable` suma **todas** las
ubicaciones internas, y `WH/Stock` es una de ellas:

```python
# catalogo_publicable.py
"stock.quant", [["location_id.usage", "=", "internal"], ...]
totales[t] = totales.get(t, 0.0) + cantidad
```

Así que cada venta resta 1 a ese libro **para siempre**. Con dos ventas de un libro
del que el proveedor declara 1, el total sale a −1 y el libro no vuelve a ofertarse
aunque el proveedor lo reponga. El error crece con el volumen de ventas y no se
corrige solo.

Ese es el mecanismo exacto por el que hoy hay **135 libros inutilizados**.

### Lo que sí hace falta

El único hueco real es la ventana entre la venta y el siguiente fichero del
proveedor: hasta una hora con AZETA, hasta un día con los demás. En esa ventana sí
se puede vender dos veces lo mismo.

Eso no se cubre con un movimiento de stock, sino con una **reserva de corta vida**:
apuntar «esta unidad está comprometida» y restarla del stock publicable hasta que
llegue el fichero siguiente, que ya viene con la realidad del proveedor.

Es una tabla pequeña (`isbn`, `pedido`, `unidades`, `creada_en`) y un `-` en el
cálculo del catálogo. **No toca Odoo.** Lo podemos montar nosotros.

## 5.2 — La regla de «parar y avisar»

Con el modelo de reserva la pregunta cambia de forma: no hay albarán que validar
contra un almacén vacío, así que no hay nada que «parar». El caso a cubrir es otro:
**llega un pedido de algo que ya no está disponible.**

Cuando eso pase, el pedido no debe intentar servirse: debe marcarse y salir del
catálogo. Eso es exactamente lo que hace el endpoint del apartado siguiente, y es
la misma acción que ya cubre 5.3. Una sola llamada resuelve las dos.

Quién lo resuelve a mano es decisión del cliente, no nuestra. Lo que sí ponemos es
el sitio donde queda anotado y consultable: `GET /api/v1/audit/libros-fallidos`.

## 5.3 — La segunda fuente de `libro_fallido` — **ya no bloquea**

Dos cosas que cambian vuestro análisis:

**La información ya no vive en el quant.** El refresco del catálogo detectó los 135
negativos y los registró en `libro_fallido` con el proveedor y la cantidad que
declaraba cada uno. Antes teníamos 4 —los que llegaron por correo— y ahora están
los 135, incluidos 131 de días anteriores que nadie había contado nunca.

**Por eso los 135 quants negativos de `WH/Stock` se pueden limpiar sin perder
nada.** Poned esos quants a 0 cuando queráis: el registro ya no depende de ellos, y
mientras sigan ahí están restando del stock publicable de 135 libros.

**Y el endpoint que pedís ya está hecho:**

```
POST /api/v1/audit/libro-fallido?isbn=<ean>&motivo=cancelado_marketplace
```

Llamadlo cuando un pedido se cancele o entre en incidencia por falta de stock. Da
igual el marketplace. Devuelve el proveedor y la cantidad que quedó registrada.

Guarda el stock que el proveedor declara **en ese momento**, que es lo que permite
soltarlo después sin listas a mano: el castigo se levanta cuando ese número
**cambie**, no cuando el proveedor vuelva a mandar el libro. Distriforma reafirma
su «1» tres veces al día desde mayo; reafirmarlo no es información nueva.

## 5.4 — ¿Reactivar los dos workflows tal cual como puente?

**No.**

El argumento a favor sería «así al menos el stock se descuenta y el detector sigue
vivo». Ninguna de las dos partes se sostiene:

- **El detector ya no los necesita** (5.3). Tiene los 135 en tabla y un endpoint
  propio.
- **El descuento que hacen no sirve de nada bueno.** No refleja la realidad del
  proveedor y sí introduce un error permanente que inutiliza el libro.

Y el coste de dejarlos apagados es **cero respecto a hoy**: con ellos encendidos, el
stock tampoco se descontaba de forma útil. El riesgo de vender dos veces en la
ventana entre la venta y el fichero siguiente existe igual en los dos casos, y es lo
que resuelve la reserva de 5.1.

Reactivarlos sólo añadiría negativos nuevos a los 135 que hay que limpiar.

---

## Lo que proponemos hacer, por orden

1. **Limpiar los 135 quants negativos de `WH/Stock`** (ponerlos a 0). Devuelve al
   catálogo 135 libros que hoy están suprimidos por aritmética, sin perder el
   registro de que fallaron. Es vuestro lado, en Odoo.
2. **Conectar el endpoint** `POST /api/v1/audit/libro-fallido` a la cancelación del
   marketplace. Es una llamada por pedido cancelado. Vuestro lado, en n8n.
3. **Montar la reserva de corta vida** para la ventana venta → fichero siguiente.
   Nuestro lado. Tabla nueva y un ajuste en el cálculo del catálogo; no toca Odoo.
4. **Dejar los dos workflows de descuento apagados** y, cuando 3 esté hecho,
   borrarlos: el descuento deja de tener sentido como concepto.

## Una corrección a vuestro apartado 2

Los **498.665 negativos de «Inventory adjustment»** no entran en ningún cálculo
nuestro, así que no hay riesgo de confundirlos. `catalogo_publicable` filtra por
`location_id.usage = 'internal'`, y esa ubicación es de tipo `inventory`. Lo mismo
el endpoint de auditoría: por eso devuelve 135 y no 498.800.

Buena observación de todas formas — conviene que quede escrito, porque a simple
vista en Odoo esos 498.665 asustan.
