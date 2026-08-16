# La API de Shopify en Kalamo

Todo lo que hace falta para usarla: qué credenciales, qué permisos, qué
límites, y qué hace cada pieza nuestra.

---

## 1. Credenciales

La app es del **Dev Dashboard nuevo** de Shopify, no una app privada clásica.
La diferencia importa: **no hay un token fijo que puedas pegar**. Se pide uno
cada vez con el `client_id` y el `client_secret`, y caduca a las 24 horas.

### Variables de entorno

| Variable | Obligatoria | Por defecto | Para qué |
|---|---|---|---|
| `SHOPIFY_CLIENT_ID` | **sí** | — | Identificador de la app |
| `SHOPIFY_CLIENT_SECRET` | **sí** | — | Secreto de la app |
| `SHOPIFY_TIENDA` | no | `kalamobooks.myshopify.com` | Dominio de la tienda |
| `SHOPIFY_API_VERSION` | no | `2026-07` | Versión de la API |
| `SHOPIFY_TIMEOUT_S` | no | `120` | Segundos de espera por petición |
| `SHOPIFY_TOPE_DIARIO` | no | `900` | Tope de altas por día (ver §3) |
| `SHOPIFY_LOCATION_GID` | no | se consulta | Ubicación donde escribir stock |
| `SHOPIFY_STOCK_LOTE` | no | `250` | Cantidades por llamada de inventario |
| `SHOPIFY_STOCK_TOPE` | no | `60000` | Freno de mano del sync de stock |
| `SHOPIFY_STOCK_CAMPO` | no | `available` | `available` o `on_hand` |

Dónde se ponen: variables de entorno del servicio en EasyPanel. **No van al
repositorio.**

### Cómo se obtiene el token

```
POST https://{tienda}/admin/oauth/access_token
     client_id, client_secret, grant_type=client_credentials
```

Se cachea en memoria y se renueva solo cuando le quedan menos de 5 minutos.
No hay que gestionarlo a mano.

### Permisos (esto es lo que más falla)

El token llega con los alcances que tenga **publicada** la app. Si añades un
permiso en el Dev Dashboard **no basta con guardar**: hay que publicar una
versión nueva y **reinstalar la app en la tienda**. Hasta entonces el token
sigue viniendo con los permisos viejos y las escrituras fallan con un 403 que
no explica gran cosa.

El cliente avisa de ello por consola:

```
[Shopify] AVISO: el token no trae permisos. Hay que publicar una version
con los alcances y REINSTALAR la app.
```

Lo que necesita cada cosa:

| Para | Permiso |
|---|---|
| Leer catálogo, auditar, exportar | `read_products` |
| Publicar libros | `write_products` |
| Leer inventario | `read_inventory` |
| **Escribir stock** | `write_inventory` |
| Saber la ubicación de la tienda | `read_locations` |

---

## 2. Las dos APIs, y cuándo cada una

Shopify tiene REST y GraphQL. Usamos las dos, a propósito.

**REST** para cosas sueltas y sencillas: datos de la tienda, contar productos,
listar canales de venta.

```python
sa.rest("shop.json")
sa.rest("products/count.json")
sa.rest("publications.json")
```

**GraphQL** para todo lo demás, porque es lo único que permite escribir
inventario y hacer operaciones masivas.

```python
sa.graphql(consulta, variables)
```

Ambas pasan por el mismo transporte, que reintenta solo ante **429** (límite de
peticiones) y **5xx**, con espera que se va doblando. Ante un **401** pide token
nuevo y reintenta. El resto de errores se lanzan tal cual.

### Operaciones masivas

Para leer el catálogo entero **no se pagina**. Paginar 747.000 productos serían
unas 3.000 peticiones y media hora. En su lugar se lanza una *bulk operation*:
Shopify la resuelve en su lado y deja un fichero JSONL para descargar.

```
mutation { bulkOperationRunQuery(query: "...") { bulkOperation { id status } } }
{ currentBulkOperation { status objectCount url errorCode } }
```

Se consulta el estado cada 10 segundos hasta que sale `COMPLETED` y da la URL.
Tarda entre uno y tres minutos para el catálogo completo.

**Solo puede haber una corriendo a la vez** por tienda. Si lanzas otra mientras
hay una en marcha, la primera se cancela.

---

## 3. Límites que condicionan el diseño

**1.000 variantes nuevas al día.** Por encima de 50.000 productos, Shopify corta
las altas. Por eso publicar libros va con tope diario (`SHOPIFY_TOPE_DIARIO`,
900 para dejar margen) y por eso existe la vía del XLSX de Matrixify, que no
tiene ese límite.

**250 cantidades por llamada de inventario.** El sync de stock agrupa en lotes
de 250.

**Coste por consulta en GraphQL.** No es un número de peticiones sino un
presupuesto que se recupera con el tiempo. El reintento ante 429 lo absorbe.

**Una operación masiva simultánea.** Ver arriba.

---

## 4. Nuestras piezas

### `shopify_api.py` — el cliente

Lo básico. No sabe nada del negocio.

| Función | Qué hace |
|---|---|
| `token(forzar=False)` | Token vigente, renovándolo si toca |
| `rest(ruta, datos=None)` | Llamada REST |
| `graphql(consulta, variables=None)` | Llamada GraphQL |
| `info_tienda()` | Nombre, dominio, moneda, plan |
| `contar_productos()` | Cuántos productos hay |
| `canales()` | Canales de venta (Online Store, Shop…) |
| `exportar_handles()` | Todos los productos: id, handle, estado |
| `crear_producto(fila, canal_ids)` | Alta desde una fila Matrixify |

Se puede usar desde la línea de órdenes para comprobar que la conexión va:

```bash
python shopify_api.py info      # tienda, nº de productos, canales
python shopify_api.py handles   # exporta el catálogo entero
```

### `shopify_pub.py` — publicar libros

Lleva la tabla `shopify_productos` (23 columnas de Matrixify más el estado).

| Función | Qué hace |
|---|---|
| `auditar()` | Lee la tienda y marca qué está ya publicado |
| `generar_fichas(n)` | Redacta n fichas con IA |
| `publicar(dry_run, limite)` | Sube por API |
| `exportar_xlsx()` | Genera el fichero para Matrixify |
| `candidatos_sin_publicar()` | Los que cumplen requisitos |

**Auditar antes de generar es obligatorio.** La primera vez descartó 11.697
libros que ya estaban publicados y que habrían costado unos 34 millones de
tokens.

### `shopify_stock.py` — el stock

Sustituye al conector, que sincronizaba unos 3.144 productos al día mientras
Odoo genera 16.257 cambios diarios.

| Función | Qué hace |
|---|---|
| `exportar_inventario()` | Trae el `inventoryItemId` de cada variante |
| `sincronizar(dry_run, completo, limite)` | Lleva el stock que ha cambiado |

**Por qué hace falta el primer paso:** para escribir stock, Shopify no acepta
el ISBN ni el handle. Exige el identificador de inventario de la variante, que
es un dato suyo. Hay que traerlo una vez, y repetirlo cuando se publican libros
nuevos.

La mutación que escribe:

```graphql
mutation ($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { createdAt reason }
    userErrors { field message }
  }
}
```

Con `name: "available"`, `reason: "correction"`, `ignoreCompareQuantity: true`
y hasta 250 cantidades por llamada.

---

## 5. Endpoints del panel

### Publicación

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/api/v1/shopify/estado` | Contadores. `?con_tienda=true` consulta en vivo |
| POST | `/api/v1/shopify/auditar` | Cruza la tienda con nuestra tabla |
| POST | `/api/v1/shopify/generar` | Genera fichas con IA |
| POST | `/api/v1/shopify/publicar` | Sube por API |
| POST | `/api/v1/shopify/exportar` | Genera el XLSX |
| GET | `/api/v1/shopify/ficheros` | Lista los XLSX generados |
| GET | `/api/v1/shopify/descargar/{fichero}` | Descarga uno |
| POST | `/api/v1/shopify/stop` | Para lo que esté corriendo |

### Stock

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/api/v1/shopify/stock/exportar-inventario` | Trae los identificadores |
| POST | `/api/v1/shopify/stock/sincronizar` | `?dry_run=true` por defecto |
| GET | `/api/v1/shopify/stock/estado` | Avance y marcapáginas |
| POST | `/api/v1/shopify/stock/parar` | Detiene la corrida |

Parámetros de `sincronizar`:

- `dry_run` (por defecto **true**) — dice qué haría sin tocar la tienda
- `completo` — ignora el marcapáginas y repasa el catálogo entero
- `limite` — corta a los primeros n

---

## 6. Puesta en marcha, en orden

**1. Comprobar la conexión.**

```bash
python shopify_api.py info
```

Si falla aquí, el problema son las credenciales o los permisos. No sigas.

**2. Traer los identificadores de inventario.** Botón *Traer identificadores*
o el endpoint. Solo lee. Unos minutos.

Al acabar, «Libros mapeados» debe estar cerca del número de productos de la
tienda. Si sale muy por debajo, faltan por publicar o la exportación se cortó.

**3. Ensayo del catálogo completo.** No escribe nada. Da cuántos libros tienen
en la tienda un número distinto al de Odoo, cuántos suben, cuántos bajan.

**Mira ese número antes de seguir.** Si es sospechosamente pequeño, la
comparación no está leyendo bien y no conviene escribir.

**4. Sincronizar.** Ahora sí escribe.

**5. Repetir el paso 2 de vez en cuando**, para recoger los libros publicados
desde la última vez.

---

## 7. Qué se publica como stock, y por qué

La cantidad es el **total de Odoo sumando los catorce almacenes**.

Cada almacén de Odoo es la disponibilidad de un proveedor, **no inventario
propio**. Un libro con 16 en AZE01 y 1 en PEN01 no son 17 libros nuestros: son
17 que podríamos pedir entre dos proveedores. Lo que se puede servir es la
suma, y eso es lo que ve el comprador.

Tres decisiones más, que están comentadas en el módulo:

**Un libro que está en Shopify y no en Odoo no se toca.** Que falte en Odoo
puede ser un fallo nuestro, y poner a cero por si acaso es como se vacía una
tienda.

**El marcapáginas solo avanza si no hubo fallos.** Lo que no se escribió se
reintenta en la siguiente corrida.

**Tope de 60.000 cambios por corrida.** Por encima no escribe y avisa. Casi
siempre significa que la lectura de Odoo vino mal, no que haya cambiado medio
catálogo.

---

## 8. Cuando algo falla

| Síntoma | Causa casi siempre | Solución |
|---|---|---|
| `Faltan SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET` | No están en el entorno | Ponerlas en EasyPanel y reiniciar |
| `La app no esta instalada en la tienda` | Se despublicó o se desinstaló | Reinstalar desde el Dev Dashboard |
| `AVISO: el token no trae permisos` | Se añadieron alcances sin publicar | Publicar versión **y reinstalar** |
| HTTP 403 al escribir | Falta `write_products` o `write_inventory` | Lo mismo de arriba |
| `Handle already in use` | El libro ya está publicado | No es un fallo: se marca y se sigue |
| La operación masiva no termina | Hay otra corriendo | Esperar; solo cabe una por tienda |
| El ensayo saca 0 cambios | Faltan los identificadores | Paso 2 |
| Sale AGOTADO con stock en Odoo | El stock no se ha sincronizado | Ensayo y sincronizar |

---

## 9. Seguridad

El secreto de la app **da acceso de escritura al catálogo y al inventario**.
Trátalo como una contraseña de producción.

A su favor: el token que genera **caduca a las 24 horas**, así que rotar el
secreto es barato. Se cambia en el Dev Dashboard y en las variables de entorno
del servidor, y no hay que tocar nada más.

Si el secreto se ha expuesto alguna vez —un chat, una captura, un log—, rótalo.
