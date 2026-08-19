# `catalogo_publicable` — contrato de stock para feeds de venta

Documento de traspaso para quien construya un feed de marketplace (Fnac,
Amazon, El Corte Inglés…) a partir de los datos de Kalamo.

**Regla corta:** lee `catalogo_publicable`. No leas `libros_proveedor`.

---

## 1. Por qué existe esta tabla

El 19/08/2026 se vendió en Fnac el ISBN `9788419195531` y no se pudo servir.
Distriforma había dejado de listarlo el **25 de mayo**. Odoo lo tenía
correctamente a cero desde entonces; el feed lo publicaba porque leía
`libros_proveedor`.

`libros_proveedor` es una **tabla de trabajo**: guarda lo que manda cada
distribuidor en crudo, tal cual llega, sin aplicar ninguna regla de negocio.
Todas las salvaguardas del sistema —pausas de proveedor, apagado por
ausencia, detección de proveedores que dejan de enviar— estaban aplicadas
**contra Odoo**. Cualquier consumidor que leyera la tabla intermedia se las
saltaba todas sin enterarse.

Medición del 19/08, antes de la limpieza:

| | Líneas con stock |
|---|---:|
| Total en `libros_proveedor` | 674.642 |
| De proveedores **pausados** | 155.209 |
| De proveedores **mudos** (≥5 días sin fichero) | 145.893 |
| **Rancias** (ausentes de su último fichero) | 8.415 |
| **No fiables en total** | **309.517** |

Casi la mitad de lo que se publicaba no era de fiar.

`catalogo_publicable` es un **contrato**: lo que está aquí se puede vender.
Quien la consume no necesita saber nada de proveedores, ficheros, pausas ni
capas de precio.

---

## 2. Esquema

PostgreSQL, esquema `public`.

| Columna | Tipo | Significado |
|---|---|---|
| `isbn` | `text` **PK** | ISBN-13 / EAN. Clave única. |
| `titulo` | `text` | Título en Odoo. Puede ser el propio ISBN si el libro nunca se enriqueció. |
| `stock` | `integer` | **Unidades servibles.** Total de Odoo sumando los 14 almacenes. |
| `precio_odoo` | `numeric(10,2)` | PVP con la Capa 1 ya aplicada. **Punto de partida para tu regla de precio.** |
| `precio_marketplace` | `numeric(10,2)` | Cálculo nuestro: Capa 1 + Capa 3 + céntimo. Referencia. |
| `precio_web` | `numeric(10,2)` | Siempre `NULL`. Ver §5. |
| `proveedor` | `text` | Email del distribuidor que lo sirve más barato. |
| `precio_coste` | `numeric(10,2)` | Lo que nos cuesta a nosotros. |
| `confirmado_en` | `timestamp` | **Última vez que ese libro vino en un fichero del proveedor.** Ver §4. |
| `odoo_id` | `integer` | `product.template.id` en Odoo. Para trazar. |
| `actualizado_en` | `timestamp` | Cuándo recalculamos la fila. Úsalo para pedir incrementales. |

Índices: `isbn` (PK), parcial sobre `stock` donde `stock > 0`, y sobre
`actualizado_en`.

---

## 3. Invariantes

Cosas con las que puedes contar:

1. **Una fila por ISBN.** No hay duplicados.
2. **`stock` es lo servible hoy**, ya filtrado. Si un proveedor está pausado
   o dejó de listar el libro, aquí no aparece con existencias.
3. **Las bajas NO se borran: se ponen a `stock = 0`.** Si la fila
   desapareciera no tendrías forma de saber que hay que retirar el artículo
   del marketplace. **Trata `stock = 0` como orden de baja**, no como
   "ignorar".
4. **`actualizado_en` se mueve en cada refresco**, cambie o no la fila.
   Sirve para incrementales.

Cosas con las que **no** puedes contar:

- Que `titulo` sea legible. Hay libros creados solo con el ISBN.
- Que `precio_odoo` esté informado. **24.931 filas no tienen precio** porque
  el proveedor no lo manda.
- Que todas las filas tengan proveedor. **86 filas tienen `proveedor` NULL**:
  stock que quedó en Odoo y ningún distribuidor confirma hoy. Descártalas.

---

## 4. Frescura — la decisión que te toca a ti

`confirmado_en` dice cuándo vino ese libro en un fichero del proveedor. Es
el campo con el que decides de qué te fías.

Estado a 19/08/2026, sobre **386.554 libros vendibles**:

| | Libros | |
|---|---:|---:|
| Confirmado en 48 h | 270.236 | 70% |
| Más de 7 días | 116.228 | 30% |

Los 116.228 no son un fallo del sistema. Son **tres proveedores cuyo fichero
se sube a mano a Google Drive** y que llevan sin enviarlo:

| Proveedor | Libros | Último fichero |
|---|---:|---|
| `logista.libros@sinli.logista.com` | 94.455 | 23/07/2026 |
| `envios@machadodistribucion.com` | 17.227 | 30/07/2026 |
| `penguin@kalamo.local` | 4.506 | 30/07/2026 |

Los otros diez proveedores enviaron fichero **hoy**.

**Recomendación:** para marketplaces que penalizan cancelaciones (Fnac),
filtra por frescura:

```sql
SELECT isbn, stock, precio_odoo
FROM catalogo_publicable
WHERE stock > 0
  AND proveedor IS NOT NULL
  AND confirmado_en > now() - interval '7 days';
```

Eso deja ~270.240 libros con respaldo verificable. Si priorizas volumen,
puedes coger todo sabiendo que el riesgo está concentrado en esos tres
proveedores concretos y no repartido.

> **Ojo con un error fácil.** No uses `libros_proveedor.stock_actualizado_en`
> como fecha de confirmación. En la vía SINLI ese campo solo se mueve cuando
> **cambia la cantidad**, no cuando el proveedor confirma el libro. Un título
> que Distriforma confirma a diario con 1 unidad conserva la fecha del último
> cambio y parece de hace meses. Ese error hacía que 214.901 filas buenas se
> vieran como rancias. `catalogo_publicable.confirmado_en` ya sale de
> `cegald_isbns_v2`, que sí registra cuándo vino en un fichero.

---

## 5. Precios

Los precios de esta tabla son **de referencia**. La regla de precio la
aplicas tú en tu lado.

Parte de **`precio_odoo`**, que es el PVP del proveedor con la Capa 1 ya
aplicada (suplemento por PVP bajo).

Las capas del sistema, en orden:

```
PVP proveedor → Capa 1 (suplemento) → Capa 3 (descuento marketplace) → −0,01
```

- **Capa 1** — ya está aplicada en `precio_odoo`.
- **Capa 2** — descuento de cesta web. Solo Shopify, **no aplica a
  marketplaces**. Por eso `precio_web` va vacío.
- **Capa 3** — descuento por tramos de precio. Nuestro cálculo está en
  `precio_marketplace` por si quieres contrastar.

Tramos de la Capa 3, tal como los implementamos:

| Precio | Descuento |
|---|---|
| hasta 33,99 € | 0% |
| 34,00 – 40,00 € | 1,5% |
| 40,01 – 50,00 € | 2% |
| 50,01 – 80,00 € | 4% |
| más de 80,00 € | 5% |

> **Detalle sin confirmar:** la tabla original del cliente salta de "0–33" a
> "34–40" y deja fuera el tramo **33,01–33,99**, donde caen **2.844 libros**.
> Nosotros les damos 0% (lo conservador: no bajar el precio). Si tú tienes la
> regla buena, úsala y avísanos.

**Filtra antes de publicar:** 24.931 filas sin precio y 1.437 por debajo de
2,90 €, que según la regla no deberían publicarse.

---

## 6. Acceso

**Por SQL** — disponible ya:

```sql
SELECT isbn, titulo, stock, precio_odoo, proveedor, confirmado_en
FROM catalogo_publicable
WHERE stock > 0;
```

**Por API** — requiere autenticación básica del panel:

```
GET  /api/v1/catalogo-publicable?con_stock=true&limite=5000&offset=0
GET  /api/v1/catalogo-publicable?desde=2026-08-19T00:00:00
GET  /api/v1/catalogo-publicable/estado
POST /api/v1/catalogo-publicable/refrescar
```

`desde` filtra por `actualizado_en` y devuelve **también las filas a 0**, que
es lo que necesitas para procesar bajas. Respuesta:

```json
{
  "total": 386554,
  "devueltos": 5000,
  "limite": 5000,
  "offset": 0,
  "libros": [
    {
      "isbn": "9788466346108",
      "titulo": "La invención de la naturaleza",
      "stock": 51,
      "precio_marketplace": 14.94,
      "precio_web": null,
      "precio_odoo": 14.96,
      "proveedor": "info@azetadistribuciones.es",
      "precio_coste": 14.96,
      "confirmado_en": "2026-08-19T20:01:57",
      "actualizado_en": "2026-08-19T23:58:12"
    }
  ]
}
```

---

## 7. Refresco

La tabla se reconstruye leyendo Odoo entero: **unos 3,5 minutos**.

Cadencia prevista: automática una vez desplegado el cron. Mientras tanto se
lanza a mano. Si necesitas datos frescos para una corrida concreta, pídelo.

Comprueba siempre la antigüedad antes de una publicación grande:

```sql
SELECT max(actualizado_en) AS ultimo_refresco,
       count(*) FILTER (WHERE stock > 0) AS vendibles
FROM catalogo_publicable;
```

---

## 8. Errores que conviene no repetir

**No leas `libros_proveedor` para publicar.** Es la causa del incidente de
Fnac. Ahí no hay ninguna regla aplicada.

**No trates la desaparición de una fila como una baja.** Las bajas llegan
como `stock = 0`, y la fila permanece.

**No uses la pestaña "Compra" de Odoo como señal de disponibilidad.** Esa
pestaña lista **quién podría servir** el libro y a qué precio, no quién lo
tiene. La columna "Cantidad" que aparece ahí es la **cantidad mínima de
pedido**, no el stock. Ha causado dos consultas de cliente esta semana.

**No asumas que el total de Odoo son unidades propias.** Cada uno de los 14
almacenes representa la disponibilidad de un distribuidor distinto, no
inventario nuestro. `stock` es la suma de lo que se puede pedir, que es lo
correcto para vender, pero no significa que haya esas unidades en una nave.

---

## 9. Qué falta

- **Los ficheros de Logista, Machado y Penguin.** Es un proceso manual
  (subida a Drive) y lleva semanas parado. No lo arregla ningún código: son
  116.228 libros publicándose con datos de julio.
- **Confirmar el tramo 33,01–33,99 €** de la Capa 3.
- **Cron de refresco automático**, pendiente de despliegue.

---

*Última actualización: 19/08/2026. Módulo: `catalogo_publicable.py`.*
