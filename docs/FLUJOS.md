# Flujos del sistema

Cada proceso, paso a paso, con sus salvaguardas y sus puntos de fallo.

---

## 1. Entrada de stock

Hay tres vías, y ninguna la controlamos del todo.

### CEGALD por SINLI (11 proveedores)

```
proveedor  ──email SINLI──>  buzón de Kalamo  ──n8n Server A──>  libros_proveedor
                                                      │
                                                      └──>  sinli_auditoria (registro del fichero)
                                                      └──>  cegald_isbns_v2 (foto de los ISBN)
```

Un CEGALD es **una foto de lo disponible**: el proveedor solo lista lo que
tiene. Lo que no aparece, no lo tiene.

**Punto de fallo:** si Server A deja de procesar correo, aquí no se nota más
que por el envejecimiento de `Último CEGALD` en la tabla de proveedores.

### Ficheros a mano (Logista, Machado, Penguin)

Estos proveedores no mandan CEGALD. Los ficheros se suben a una carpeta de
Drive y el n8n de Server A los ingesta. Mismo destino: `libros_proveedor`.

**Consecuencia:** no registran foto en `cegald_isbns_v2`, así que **el apagado
por ausencia no les aplica**.

### AZETA, por su cuenta

```
CSV HTTP de AZETA  ──azeta_stock.py──>  libros_proveedor  ──azeta_push_odoo.py──>  AZE01
```

AZETA es el único que leemos nosotros directamente. Va aparte de todo el
resto para no duplicar escrituras en Odoo.

---

## 2. Sincronización a Odoo

Cada hora, `sync_stock_sinli.run_once()`:

```
1. Coger el lock         sync_state.lock_activo
2. Leer el marcapáginas  hasta qué actualizado_en se procesó
3. Traer un lote         2.000 filas de libros_proveedor con JOIN al mirror
4. Por cada libro:
     ¿su proveedor está pausado?      -> saltar, sin error
     ¿tiene almacén mapeado?          -> si no, error registrado
     ¿el almacén es la ubicación 14?  -> bloqueado, es de AZETA
5. Resolver product.product de cada plantilla   (en lote)
6. Buscar los quants que ya existen             (en lote)
7. Agrupar por (ubicación, cantidad) y escribir una vez por grupo
8. Aplicar inventario en tandas de 500
9. Escribir precios, agrupados por valor
10. Desarchivar las variantes de lo reactivado
11. Avanzar el marcapáginas y soltar el lock
```

**Por qué se agrupa tanto:** escribir libro a libro serían cientos de miles de
llamadas a Odoo. Agrupando por cantidad, un lote de 2.000 se resuelve en unas
pocas decenas.

**El fix del marcapáginas.** Las inserciones masivas comparten el mismo
`actualizado_en` al milisegundo. Si el `LIMIT` corta a mitad de un grupo con
timestamp idéntico, el siguiente lote con `> marcapáginas` se saltaría el
resto **en silencio**. Pasó el 16/07 con DISBOOK y DISTRIFER: 17.000 libros
perdidos. Por eso el lote se extiende con todas las filas que compartan el
timestamp de la última.

**El lock huérfano.** Si un deploy mata el proceso a mitad, el lock queda
cogido. Pasó el 14/07: dos días sin sincronizar y el n8n recibiendo "started"
tan tranquilo. Ahora los locks de más de 30 minutos se roban.

---

## 3. Apagado por ausencia (CEGALD)

Un CEGALD dice lo que **hay**. Lo que ya no viene, hay que apagarlo.

```
presentes  = ISBN del último CEGALD del proveedor  (de cegald_isbns_v2)
con stock  = quants > 0 en su almacén de Odoo
a apagar   = con stock - presentes
```

**Tres salvaguardas antes de apagar nada:**

1. Si los presentes son menos del 50% de lo que hay con stock, no apaga:
   el CEGALD probablemente venía truncado
2. Si el CEGALD tiene más de 48 horas, no apaga: dato rancio
3. Nunca toca la ubicación 14 (AZETA)

Se lanza a mano, por proveedor, y **siempre con dry-run primero**.

---

## 4. Enriquecimiento de fichas

```
ISBN sin ficha
     │
     ├──> Casa del Libro   (solo ISBN 978-84 y 979-13, que son los españoles)
     │         │
     │         └── ¿lo tiene? ──no──> notfound_books, para no reintentar
     │
     ├──> Google Books     (cascada, cubre catálogo extranjero)
     │
     └──> distributor_books (el catálogo que mandó el propio distribuidor)
```

**El filtro de prefijos importa.** Casa del Libro es una librería española: con
un ISBN extranjero no encuentra nada. Pero el filtro original solo miraba
`978-84` e ignoraba el rango **979-13**, que la agencia asignó a España en
2022. Eran 13.966 libros españoles quedándose sin ficha pudiendo tenerla.

**La cascada de fuentes salva la mitad de las fichas.** Mirando solo los campos
de Casa del Libro, la mitad de los libros publicables salían sin autor ni
editorial. Con la cascada, el 100% los tiene.

---

## 5. Ciclo diario de libros nuevos

`auto_scrape.run_auto_scrape_cycle()`, una vez al día:

```
1. Detectar     ISBN con stock que no están en Odoo
                   menos los proveedores pausados
                   menos los que tienen crear_nuevos = false
2. Enriquecer   scraping de Casa del Libro para los españoles sin título
3. Crear        product.template en Odoo, con is_storable
                   precio con el suplemento API-15
                   apagados si el PVP no llega a 2,90
                   etiqueta según completitud
4. Reportar     Excel con todos, marcando los que no se encontraron
5. Avisar       webhook a Server A, que manda el correo
```

**El interruptor `crear_nuevos` no es un lujo.** Sin él, la primera corrida
habría creado 99.961 productos, 99.830 de ellos **sin título**: PODIPRINT
manda 100.000 títulos de impresión bajo demanda, casi todos extranjeros, que
Casa del Libro no tiene.

**El webhook necesita paciencia.** Server A valida el token, se descarga el
Excel por Basic auth y manda el correo antes de responder. Con 30 segundos de
espera fallaba; ahora son 180 con tres intentos.

---

## 6. Publicación en Shopify

Tres pasos, todos manuales.

### Auditar

```
Shopify  ──operación masiva GraphQL──>  JSONL con los 746.925 handles
                                              │
                                        comparar con shopify_productos
                                              │
                                        anotar los que faltaban
```

76 segundos. Paginando por REST serían 3.000 peticiones.

**Es el paso que más dinero ahorra.** La primera auditoría descubrió que
11.697 de nuestros 38.156 candidatos ya estaban publicados — los había subido
la app *Odoo Integration*, no los ficheros Matrixify. Generarles ficha habría
costado unos 34 millones de tokens en balde.

### Generar

Por cada libro pendiente:

```
Sin gastar tokens:
   ficha técnica (9 campos)          de nuestros datos
   cat:novedades                     por año de edición
   cat:literatura-en-otros-idiomas   por idioma
   madre:                            deducida de la categoría
   precio, peso, imagen, autor, año, SKU

Con DeepSeek, una sola llamada:
   6 bloques narrativos
   SEO Title y SEO Description
   categorías temáticas, de una lista cerrada de 97
```

Unos 2.900 tokens y un segundo por ficha con 10 en paralelo. Se guardan como
`generado` conforme salen, así que se puede parar y continuar.

**Se recorta en vez de repetir.** Si el SEO se pasa de largo, se corta por la
última palabra entera. Repetir la llamada costaba 2.000 tokens por unos pocos
caracteres.

### Publicar

Dos caminos con el mismo resultado:

- **API**: dry-run que dice cuántos y cuáles, y solo tras confirmar toca la
  tienda. Tope de 900 al día.
- **XLSX Matrixify**: fichero descargable, sin tope.

**El tope no es nuestro.** Shopify corta en 1.000 variantes nuevas al día por
encima de 50.000 productos. Por eso la carga grande va por fichero.

---

## 7. Gestión de proveedores

### Pausar

```
1. Marcar activo = false        (antes de nada, para que el sync no reencienda)
2. Poner a 0 todos sus quants en SU almacén
3. El sync empieza a saltar sus libros
```

Solo su almacén. Un libro que sirvan dos proveedores sigue disponible.

### Reactivar

```
1. Quitar la marca
2. Marcar sus libros para re-empuje
```

**El segundo paso no es opcional.** La ingesta solo mueve `actualizado_en`
cuando el stock **cambia** — medido: AKAL movió 1 fila de 3.808 en un día. Un
proveedor que vuelve de vacaciones con el mismo stock nunca se re-sincronizaría.

### Conciliar

Compara libro a libro lo que debería estar encendido con los quants reales, y
marca solo lo que falta. Detecta el caso del libro que entró con stock y nunca
varió: el sync no lo mira nunca y se queda sin quant para siempre.

### Reparar catálogo

Arregla los dos estados que impiden llevar stock:

- Sin `is_storable`: Odoo rechaza sus quants
- Variante archivada con plantilla activa: el sync no encuentra el producto

---

## 8. Motor de precios

Se aplica en cada escritura, no en una pasada aparte.

```
PVP crudo del proveedor
     │
     ├── < 2,90        -> no publicar, producto apagado
     ├── 2,90 - 4,99   -> +2,00
     ├── 5,00 - 6,00   -> +1,50
     ├── 6,01 - 7,50   -> +1,00
     └── > 7,50        -> sin suplemento
```

**Idempotente por diseño:** el precio web se calcula siempre desde `pvp_base`,
que guarda el PVP crudo. Nunca sobre un precio ya suplementado.

**El origen del precio va en cascada:** `list_price` → `pvp_base` →
`libros_proveedor.precio_con_iva`. Mirando solo el primero se caían 2.260
libros que sí tenían precio del proveedor.

---

## 9. Qué corre solo

| Proceso | Cada | Variable que lo enciende |
|---|---|---|
| Ciclo de stock AZETA | 1 hora | `AZETA_STOCK_CRON_ENABLED` |
| Sync SINLI | 1 hora | `SYNC_STOCK_CRON_ENABLED` |
| Libros nuevos | 24 horas | `AUTO_SCRAPE_CRON_ENABLED` |

Sin esas variables, los crones no arrancan tras un reinicio aunque se hayan
activado a mano.

**Nada de Shopify corre solo.**
