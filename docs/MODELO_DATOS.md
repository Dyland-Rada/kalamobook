# Modelo de datos

41 tablas, 478 columnas. Extraido del esquema real.

**Quien es el dueno importa.** Las tablas de `supabase_admin` las escribe Server A: podemos leer e insertar, pero **no podemos hacer `ALTER`**. Cualquier campo nuevo va en tabla propia.

---

## Resumen

| Tabla | Tamano | Filas | Cols | Dueno |
|---|---|---|---|---|
| `shopify_productos` | 2433 MB | 681,263 | 28 | postgres |
| `odoo_books_mirror` | 2005 MB | 1,259,523 | 45 | postgres |
| `cegald_isbns_v2` | 373 MB | 1,543,512 | 7 | supabase_admin |
| `books` | 338 MB | 326,380 | 37 | postgres |
| `libros_proveedor` | 250 MB | 709,160 | 13 | supabase_admin |
| `cdl_isbn_index` | 209 MB | 683,112 | 5 | postgres |
| `mirakl_offer_state` | 200 MB | 1,229,407 | 4 | postgres |
| `fnac_offer_state` | 199 MB | 1,229,407 | 4 | postgres |
| `distributor_books` | 142 MB | 208,767 | 35 | postgres |
| `enrichment_queue` | 120 MB | 59,515 | 10 | postgres |
| `libros_proveedor_nueva` | 56 MB | 494,808 | 13 | supabase_admin |
| `fnac_published_offers` | 53 MB | 511,982 | 3 | postgres |
| `cdl_published_offers` | 53 MB | 512,782 | 4 | postgres |
| `notfound_books` | 22 MB | 113,245 | 7 | postgres |
| `cdl_scraped_data` | 14 MB | 19,980 | 26 | postgres |
| `sinli_estados` | 13 MB | 40,376 | 8 | supabase_admin |
| `sync_errores` | 6792 kB | 17,022 | 12 | supabase_admin |
| `sinli_precios` | 2848 kB | 11,078 | 10 | supabase_admin |
| `event_log` | 1576 kB | 2,502 | 7 | postgres |
| `sinli_envios` | 1440 kB | 3,658 | 15 | supabase_admin |
| `import_job_logs` | 864 kB | 4,589 | 5 | postgres |
| `sinli_auditoria` | 792 kB | 1,820 | 14 | supabase_admin |
| `discovered_categories` | 520 kB | 1,568 | 7 | postgres |
| `api_request_log` | 344 kB | 1,226 | 11 | postgres |
| `sinli_facturas` | 152 kB | 377 | 9 | supabase_admin |
| `proveedores` | 96 kB | 17 | 8 | supabase_admin |
| `sinli_abonos` | 88 kB | 110 | 14 | supabase_admin |
| `scrape_progress` | 80 kB | 82 | 4 | postgres |
| `sync_state` | 48 kB | 2 | 9 | supabase_admin |
| `watchdog_alertas_v2` | 48 kB | 11 | 7 | supabase_admin |
| `proveedor_pausa` | 32 kB | — | 7 | postgres |
| `proveedor_almacen_odoo` | 32 kB | — | 3 | supabase_admin |
| `import_jobs` | 32 kB | 6 | 18 | postgres |
| `pedidos` | 24 kB | — | 12 | supabase_admin |
| `sync_sin_ficha` | 24 kB | — | 5 | supabase_admin |
| `devoluciones` | 24 kB | — | 13 | supabase_admin |
| `odoo_product_categories_cache` | 24 kB | — | 5 | postgres |
| `odoo_public_categories` | 24 kB | — | 5 | postgres |
| `lineas_devolucion` | 16 kB | — | 12 | supabase_admin |
| `sinli_mensajes` | 16 kB | — | 7 | supabase_admin |
| `lineas_pedido` | 16 kB | — | 10 | supabase_admin |

---

## Detalle

### `shopify_productos`

Las 23 columnas Matrixify de cada producto de la tienda, mas su estado (publicado / generado). Control de duplicados y banco de ejemplos para la IA.

*2433 MB, 681,263 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `handle` | text |
| `command` | text |
| `title` | text |
| `vendor` | text |
| `tipo` | text |
| `tags` | text |
| `published` | text |
| `status` | text |
| `body_html` | text |
| `seo_title` | text |
| `seo_description` | text |
| `variant_sku` | text |
| `variant_barcode` | text |
| `variant_price` | text |
| `variant_compare_at_price` | text |
| `variant_inventory_qty` | integer |
| `variant_inventory_tracker` | text |
| `variant_grams` | integer |
| `variant_requires_shipping` | text |
| `image_src` | text |
| `image_alt_text` | text |
| `metafield_autor` | text |
| `metafield_anio` | text |
| `estado` | text |
| `fichero_origen` | text |
| `subido_en` | timestamp without time zone |
| `cargado_en` | timestamp without time zone |
| `generado_en` | timestamp without time zone |

### `odoo_books_mirror`

Espejo local de Odoo mas todo lo enriquecido. Es la tabla central: casi todo se cruza por su `barcode` (el ISBN).

*2005 MB, 1,259,523 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `odoo_id` | integer |
| `barcode` | text |
| `name` | text |
| `description` | text |
| `description_sale` | text |
| `list_price` | numeric |
| `categ_id` | integer |
| `categ_name` | text |
| `synced_at` | timestamp without time zone |
| `public_categ_ids` | text |
| `public_categ_names` | text |
| `inferred_categories` | text |
| `inferred_source` | text |
| `gbooks_publisher` | text |
| `gbooks_language` | text |
| `gbooks_pages` | integer |
| `gbooks_thumbnail` | text |
| `gbooks_fetched_at` | timestamp without time zone |
| `cdl_fetched_at` | timestamp without time zone |
| `cdl_author` | text |
| `cdl_editorial` | text |
| `cdl_image_url` | text |
| `cdl_weight` | text |
| `cdl_height` | text |
| `cdl_width` | text |
| `cdl_binding` | text |
| `cdl_translator` | text |
| `cdl_illustrator` | text |
| `cdl_collection` | text |
| `cdl_pages` | text |
| `cdl_release_date` | text |
| `cdl_url` | text |
| `cdl_price` | text |
| `cdl_language` | text |
| `supplier_partner_ids` | text |
| `supplier_names` | text |
| `supplier_count` | integer |
| `suppliers_synced_at` | timestamp without time zone |
| `azeta_price_eur` | numeric |
| `azeta_price_no_iva` | numeric |
| `azeta_iva` | integer |
| `azeta_codigo` | text |
| `azeta_fetched_at` | timestamp without time zone |
| `nuevo_creado_en` | timestamp without time zone |
| `pvp_base` | numeric |

### `cegald_isbns_v2`

Foto de los ISBN presentes en cada CEGALD recibido. Sustituye a la tabla original, que quedo con la secuencia corrupta.

*373 MB, 1,543,512 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `id` | bigint |
| `proveedor_id` | integer |
| `proveedor_email` | text |
| `archivo_nombre` | text |
| `email_id` | text |
| `isbn` | text |
| `registrado_en` | timestamp with time zone |

### `books`

Fichas scrapeadas de Casa del Libro por ISBN.

*338 MB, 326,380 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `id` | integer |
| `search_query` | text |
| `title` | text |
| `author` | text |
| `editorial` | text |
| `isbn` | text |
| `price` | text |
| `original_price` | text |
| `discount` | text |
| `description` | text |
| `translator` | text |
| `illustrator` | text |
| `language` | text |
| `pages` | text |
| `reading_time` | text |
| `binding` | text |
| `release_date` | text |
| `edition_year` | text |
| `edition_place` | text |
| `collection` | text |
| `height` | text |
| `width` | text |
| `weight` | text |
| `origin` | text |
| `url` | text |
| `image_url` | text |
| `category` | text |
| `categoria_1` | text |
| `categoria_2` | text |
| `categoria_3` | text |
| `timestamp` | timestamp with time zone |
| `price_eur` | text |
| `sinli_situacion` | text |
| `sinli_updated_at` | timestamp with time zone |
| `fuente` | text |
| `categoria_4` | text |
| `categoria_5` | text |

### `libros_proveedor`

Stock y precio por (ISBN, proveedor). La escribe Server A al procesar los ficheros. `actualizado_en` SOLO se mueve cuando cambia el stock.

*250 MB, 709,160 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `id` | integer |
| `isbn` | text |
| `proveedor_id` | integer |
| `proveedor_email` | text |
| `precio_sin_iva` | numeric |
| `precio_con_iva` | numeric |
| `pct_iva` | numeric |
| `tipo_precio` | character |
| `situacion` | text |
| `fecha_precio` | date |
| `actualizado_en` | timestamp without time zone |
| `stock_disponible` | integer |
| `stock_actualizado_en` | timestamp without time zone |

### `cdl_isbn_index`

Indice de que ISBN existen en Casa del Libro, para no pedir en balde.

*209 MB, 683,112 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `isbn` | text |
| `url` | text |
| `title` | text |
| `image_url` | text |
| `populated_at` | timestamp without time zone |

### `mirakl_offer_state`

Estado de las ofertas en el marketplace Mirakl.

*200 MB, 1,229,407 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `sku` | text |
| `last_quantity` | integer |
| `last_pushed_at` | timestamp with time zone |
| `last_price` | numeric |

### `fnac_offer_state`

Estado de las ofertas en FNAC.

*199 MB, 1,229,407 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `sku` | text |
| `last_quantity` | integer |
| `last_pushed_at` | timestamp with time zone |
| `last_price` | numeric |

### `distributor_books`

Catalogos completos que mandan los distribuidores en XLSX. Segunda fuente de titulo, autor y editorial.

*142 MB, 208,767 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `isbn` | text |
| `title` | text |
| `author` | text |
| `editorial` | text |
| `price` | numeric |
| `original_price` | numeric |
| `discount` | text |
| `description` | text |
| `translator` | text |
| `illustrator` | text |
| `language` | text |
| `pages` | text |
| `reading_time` | text |
| `binding` | text |
| `release_date` | text |
| `edition_year` | text |
| `edition_place` | text |
| `collection` | text |
| `height` | text |
| `width` | text |
| `weight` | text |
| `origin` | text |
| `url` | text |
| `image_url` | text |
| `category` | text |
| `categoria_1` | text |
| `categoria_2` | text |
| `categoria_3` | text |
| `categoria_4` | text |
| `categoria_5` | text |
| `price_eur` | numeric |
| `sinli_situacion` | text |
| `sinli_updated_at` | text |
| `fuente` | text |
| `imported_at` | timestamp without time zone |

### `enrichment_queue`

Cola persistente del enriquecimiento, con reclamo `FOR UPDATE SKIP LOCKED` para varios workers.

*120 MB, 59,515 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `odoo_id` | integer |
| `barcode` | text |
| `name` | text |
| `status` | text |
| `attempts` | integer |
| `last_error` | text |
| `scraped_data` | text |
| `queued_at` | timestamp without time zone |
| `updated_at` | timestamp without time zone |
| `claimed_by` | text |

### `libros_proveedor_nueva`

Tabla de trabajo de Server A. No la usamos.

*56 MB, 494,808 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `id` | integer |
| `isbn` | text |
| `proveedor_id` | integer |
| `proveedor_email` | text |
| `precio_sin_iva` | numeric |
| `precio_con_iva` | numeric |
| `pct_iva` | numeric |
| `tipo_precio` | character |
| `situacion` | text |
| `fecha_precio` | date |
| `actualizado_en` | timestamp without time zone |
| `stock_disponible` | integer |
| `stock_actualizado_en` | timestamp without time zone |

### `fnac_published_offers`

Ofertas publicadas en FNAC.

*53 MB, 511,982 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `sku` | text |
| `published_at` | timestamp with time zone |
| `found_in_catalog` | boolean |

### `cdl_published_offers`

Ofertas publicadas en Casa del Libro marketplace.

*53 MB, 512,782 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `sku` | text |
| `found_in_catalog` | boolean |
| `published_at` | timestamp with time zone |
| `checked_at` | timestamp with time zone |

### `notfound_books`

ISBN que Casa del Libro no tiene. Evita reintentar eternamente.

*22 MB, 113,245 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `odoo_id` | integer |
| `barcode` | text |
| `name` | text |
| `reason` | text |
| `attempts` | integer |
| `first_seen` | timestamp without time zone |
| `last_attempt` | timestamp without time zone |

### `cdl_scraped_data`

Datos crudos del scraping por categorias.

*14 MB, 19,980 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `isbn` | text |
| `url` | text |
| `title` | text |
| `author` | text |
| `editorial` | text |
| `description` | text |
| `image_url` | text |
| `weight` | text |
| `height` | text |
| `width` | text |
| `binding` | text |
| `pages` | text |
| `language` | text |
| `translator` | text |
| `illustrator` | text |
| `collection` | text |
| `release_date` | text |
| `price` | text |
| `categoria_1` | text |
| `categoria_2` | text |
| `categoria_3` | text |
| `categoria_4` | text |
| `categoria_5` | text |
| `inferred_categories` | text |
| `grupo` | text |
| `scraped_at` | timestamp without time zone |

### `sinli_estados`

Mensajes de estado de pedidos que mandan los proveedores.

*13 MB, 40,376 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `id` | integer |
| `isbn` | character varying |
| `ean` | character varying |
| `titulo` | text |
| `estado` | character |
| `fecha_estado` | date |
| `proveedor_email` | text |
| `recibido_en` | timestamp without time zone |

### `sync_errores`

Errores por libro del sync, para diagnostico.

*6792 kB, 17,022 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `id` | bigint |
| `entidad` | text |
| `isbn` | text |
| `proveedor_email` | text |
| `payload` | jsonb |
| `mensaje_error` | text |
| `intentos` | integer |
| `next_retry_at` | timestamp with time zone |
| `resuelto` | boolean |
| `resuelto_en` | timestamp with time zone |
| `creado_en` | timestamp with time zone |
| `actualizado_en` | timestamp with time zone |

### `sinli_precios`

Cambios de precio comunicados por SINLI.

*2848 kB, 11,078 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `isbn` | character varying |
| `ean` | character varying |
| `precio_sin_iva` | numeric |
| `precio_con_iva` | numeric |
| `pct_iva` | numeric |
| `titulo` | text |
| `tipo_precio` | character |
| `proveedor_email` | text |
| `fecha_aplicacion` | date |
| `recibido_en` | timestamp without time zone |

### `event_log`

Que hizo cada job: categoria, evento, resumen y detalle en JSON.

*1576 kB, 2,502 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `id` | bigint |
| `ts` | timestamp with time zone |
| `categoria` | text |
| `evento` | text |
| `resumen` | text |
| `detalle` | jsonb |
| `nivel` | text |

### `sinli_envios`

Albaranes de envio.

*1440 kB, 3,658 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `id` | integer |
| `isbn` | character varying |
| `ean` | character varying |
| `titulo` | text |
| `cantidad` | integer |
| `precio_sin_iva` | numeric |
| `precio_con_iva` | numeric |
| `descuento` | numeric |
| `pct_iva` | numeric |
| `novedad` | character |
| `num_albaran` | character varying |
| `fecha_doc` | date |
| `proveedor` | text |
| `actualizado_en` | timestamp without time zone |
| `proveedor_email` | text |

### `import_job_logs`

Detalle linea a linea de esas importaciones.

*864 kB, 4,589 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `id` | bigint |
| `job_id` | integer |
| `ts` | timestamp with time zone |
| `level` | text |
| `message` | text |

### `sinli_auditoria`

Registro de cada fichero SINLI recibido, con su tipo y numero de registros. La verdad sobre cuando llego el ultimo CEGALD.

*792 kB, 1,820 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `id` | bigint |
| `email_id` | text |
| `email_asunto` | text |
| `email_de` | text |
| `archivo_nombre` | text |
| `file_type` | text |
| `proveedor` | text |
| `buzon_emisor` | text |
| `registros` | integer |
| `estado` | text |
| `mensaje` | text |
| `procesado_en` | timestamp with time zone |
| `email_canonico` | text |
| `proveedor_nuevo` | boolean |

### `discovered_categories`

Categorias descubiertas en el sitemap de Casa del Libro.

*520 kB, 1,568 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `key` | text |
| `name` | text |
| `url` | text |
| `parent_key` | text |
| `depth` | integer |
| `book_count` | integer |
| `exhausted` | integer |

### `api_request_log`

Peticiones POST entrantes: quien, que y con que resultado.

*344 kB, 1,226 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `id` | bigint |
| `ts` | timestamp with time zone |
| `method` | text |
| `path` | text |
| `query` | text |
| `body` | text |
| `status_code` | integer |
| `duration_ms` | integer |
| `client_ip` | text |
| `username` | text |
| `user_agent` | text |

### `sinli_facturas`

*152 kB, 377 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `id` | integer |
| `num_factura` | character varying |
| `num_albaran` | character varying |
| `fecha` | date |
| `importe` | numeric |
| `fecha_factura` | date |
| `proveedor` | text |
| `actualizado_en` | timestamp without time zone |
| `proveedor_email` | text |

### `proveedores`

Los 17 proveedores dados de alta, con su email SINLI.

*96 kB, 17 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `id` | integer |
| `nombre` | text |
| `email_sinli` | text |
| `codigo_sinli` | character varying |
| `activo` | boolean |
| `creado_en` | timestamp without time zone |
| `emails_alternativos` | ARRAY |
| `ultimo_contacto` | timestamp with time zone |

### `sinli_abonos`

*88 kB, 110 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `id` | integer |
| `isbn` | character varying |
| `ean` | character varying |
| `titulo` | text |
| `cantidad` | integer |
| `precio_sin_iva` | numeric |
| `precio_con_iva` | numeric |
| `descuento` | numeric |
| `pct_iva` | numeric |
| `num_albaran` | text |
| `fecha_doc` | date |
| `proveedor` | text |
| `recibido_en` | timestamp without time zone |
| `proveedor_email` | text |

### `scrape_progress`

Progreso del scraping masivo por categoria.

*80 kB, 82 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `id` | integer |
| `category_key` | text |
| `last_page` | integer |
| `updated_at` | timestamp with time zone |

### `sync_state`

Marcapaginas y lock de cada proceso de sincronizacion.

*48 kB, 2 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `id` | integer |
| `entidad` | text |
| `ultimo_timestamp` | timestamp with time zone |
| `ultima_ejecucion` | timestamp with time zone |
| `ultima_ejecucion_ok` | boolean |
| `items_procesados` | integer |
| `lock_activo` | boolean |
| `lock_desde` | timestamp with time zone |
| `notas` | text |

### `watchdog_alertas_v2`

Alertas del vigilante de Server A.

*48 kB, 11 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `id` | bigint |
| `feed` | text |
| `tipo` | text |
| `horas_sin_datos` | numeric |
| `ultimo_dato` | timestamp with time zone |
| `mensaje` | text |
| `detectado_en` | timestamp with time zone |

### `proveedor_pausa`

Estado de pausa y el interruptor `crear_nuevos`, en tabla propia porque no podemos alterar la de Supabase.

*32 kB, -1 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `proveedor_email` | text |
| `activo` | boolean |
| `pausado_en` | timestamp without time zone |
| `pausado_motivo` | text |
| `reactivado_en` | timestamp without time zone |
| `actualizado_en` | timestamp without time zone |
| `crear_nuevos` | boolean |

### `proveedor_almacen_odoo`

Mapeo proveedor -> almacen de Odoo. Sin fila aqui, su stock no llega a ningun sitio.

*32 kB, -1 filas, dueno `supabase_admin`*

| Columna | Tipo |
|---|---|
| `proveedor_email` | text |
| `warehouse_code` | text |
| `nombre_proveedor` | text |

### `import_jobs`

Trabajos de importacion de catalogos XLSX.

*32 kB, 6 filas, dueno `postgres`*

| Columna | Tipo |
|---|---|
| `id` | integer |
| `kind` | text |
| `proveedor` | text |
| `excel_path` | text |
| `excel_filename` | text |
| `tags` | ARRAY |
| `status` | text |
| `total_rows` | integer |
| `processed` | integer |
| `created` | integer |
| `updated` | integer |
| `errors` | integer |
| `started_at` | timestamp with time zone |
| `finished_at` | timestamp with time zone |
| `created_at` | timestamp with time zone |
| `options` | jsonb |
| `summary` | jsonb |
| `last_isbn` | text |
