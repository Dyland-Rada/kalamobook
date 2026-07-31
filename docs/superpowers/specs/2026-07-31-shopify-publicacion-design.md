# Publicación automática en Shopify

Fecha: 2026-07-31

## Problema

Los libros que entran a diario en Odoo con la ficha completa no llegan a la
tienda. Hoy la publicación es manual: alguien genera un XLSX y lo sube con
Matrixify. Hay **52.773 libros con stock, imagen y descripción que deberían
estar publicados y no lo están**, y el goteo diario (~350/día) se acumula.

## Situación de partida (medida el 31/07/2026)

| Dato | Valor |
|---|---|
| ISBN ya publicados en Shopify | 734.957 |
| Candidatos con stock (imagen + descripción) | 340.526 |
| Pendientes de publicar | **52.773** (40.249 con peso, 12.524 sin) |
| Ficheros Matrixify de lo ya subido | 23 xlsx, 869 MB, en `todos los libros webs/` |

De los ficheros: todos `Command=MERGE`, sin ISBN repetidos, y **ninguno** sin
Body HTML, SEO Description ni imagen. Type es `Libro` en 734.948 de 734.957
(8 audiolibros y 1 ebook).

**Límite de Shopify:** por encima de 50.000 variantes, máximo 1.000 variantes
nuevas al día. Los 52.773 pendientes por API serían 53 días, así que la puesta
al día va por fichero y solo el goteo diario va por API.

## Piezas

### 1. Tabla `shopify_productos`

Las 23 columnas Matrixify + `fichero_origen` + `cargado_en`. Clave primaria
`handle` (el ISBN). Carga en streaming desde los 23 xlsx; ante ISBN repetido
gana la tanda más reciente (`ON CONFLICT DO UPDATE`).

~735.000 filas, ~1 GB (el Body HTML son 3.796 caracteres de media; Postgres
lo comprime con TOAST).

Sirve para tres cosas a la vez: control de duplicados, diccionario de la
taxonomía real, y banco de ejemplos para afinar el prompt.

### 2. Generador de fichas (`shopify_ficha.py`)

Dado un ISBN, devuelve las 23 columnas.

**Sin gastar tokens** — todo esto sale de `odoo_books_mirror`:

- *Ficha técnica* (9 campos): autor, editorial, idioma, tema, colección,
  encuadernación, fecha de edición, páginas, peso
- `cat:novedades` si el año de edición es 2025 o posterior (verificado: 28.879
  de los marcados son de 2025-2026 frente a 116 de 2024)
- `cat:literatura-en-otros-idiomas` si el idioma no es castellano
- `madre:` se deduce de la `cat:` principal (cada categoría tiene su madre
  dominante al 89-96%)
- Peso: `cdl_weight` viene como texto `'210.0 gr'` → parsear a entero. Si falta,
  **350 g** (regla del cliente para los de etiqueta 2)
- Precio, imagen, autor, año, SKU, código de barras, Type `Libro`

**Con DeepSeek** (`deepseek-chat`, sale `deepseek-v4-flash`):

- Los 6 bloques narrativos del Body HTML: *Resumen del libro*, *¿De qué trata?*,
  *Temas principales* (5 viñetas), *¿Para quién está recomendado?*, *Qué aporta
  este libro* (4-5 viñetas), *Valoración editorial*
- SEO Title (≤60) y SEO Description (140-158)
- Las `cat:` temáticas, eligiendo de la lista cerrada de 94

Entrada: título, autor, editorial, tema del distribuidor, idioma, páginas y la
sinopsis que ya tenemos. Salida en JSON validado antes de escribir nada.

**Dos claves con relevo:** si una falla, se reintenta con la otra.

**Caché de prompt:** la lista de 94 categorías va en el mensaje de sistema,
que es idéntico en todas las llamadas, para que DeepSeek la sirva de caché.
Es la mayor parte de los 1.411 tokens de entrada medidos.

**Validaciones antes de dar por buena una ficha:**

- SEO Description entre 140 y 158 caracteres (en la prueba salió 164 → reintento)
- SEO Title ≤ 60
- Las `cat:` devueltas existen en la lista cerrada
- Los 6 bloques no vienen vacíos

### 3. Salidas

El mismo generador, dos destinos:

- **XLSX Matrixify** con `openpyxl` en modo `write_only`, una sola hoja
  `Products`, las 23 cabeceras exactas, todo el texto pasado por `clean_xml`.
  Para los 52.773 pendientes.
- **API de Shopify** para el goteo diario. Pendiente del token de la app del
  Dev Dashboard (Client ID + Secret → `client_credentials`).

## Control de estado

Cada ISBN en `shopify_productos` queda como:

- `publicado` — está en la tienda (los 734.957 de la carga inicial)
- `generado` — ficha lista, aún no subida
- `pendiente` — detectado como candidato, sin ficha

El ciclo diario busca candidatos: etiqueta Completo o Web, con stock, sin fila
en la tabla.

## Rendimiento

8,5 s y 2.188 tokens por ficha. Con 15 peticiones en paralelo, los 52.773
pendientes salen en unas 8 horas. El goteo diario (~350) en 4 minutos.

## Qué NO entra

- Portadas: los pendientes ya tienen imagen (es requisito para ser candidato)
- Los 393.000 candidatos sin stock: no se publican
- Audiolibros y ebooks: 9 de 735.000, no merece caso especial
- La carga masiva por API: la impide el límite de 1.000/día
