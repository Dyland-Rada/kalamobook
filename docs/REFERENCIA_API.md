# Referencia de la API

Generado del codigo. **144 endpoints.** Todos requieren autenticacion (HTTP Basic
o sesion de navegador) salvo `/login`, `/logout` y el webhook de Telegram.

Documentacion interactiva en `/docs` (Swagger) y `/redoc`.

Base: `https://kalamob.reinventaconia.com`

---

## Indice

- [General](#general) — 5 endpoints
- [Shopify](#shopify) — 8 endpoints
- [Proveedores](#proveedores) — 10 endpoints
- [SINLI Sync](#sinli-sync) — 11 endpoints
- [AZETA](#azeta) — 20 endpoints
- [Auto-Scrape](#auto-scrape) — 6 endpoints
- [Auditoria](#auditoria) — 5 endpoints
- [Odoo](#odoo) — 40 endpoints
- [Odoo Tags](#odoo-tags) — 4 endpoints
- [Precios](#precios) — 2 endpoints
- [Relleno Manual](#relleno-manual) — 3 endpoints
- [Distribuidores](#distribuidores) — 3 endpoints
- [Scraping Masivo](#scraping-masivo) — 5 endpoints
- [Biblioteca](#biblioteca) — 3 endpoints
- [Libros](#libros) — 1 endpoints
- [Admin](#admin) — 8 endpoints
- [Diagnostico](#diagnostico) — 5 endpoints
- [Notificaciones](#notificaciones) — 5 endpoints

---

## General

Panel web y autenticacion.

### `GET /`

Render the web search interface.

### `GET /login`

Pantalla de login (publica). Si ya hay sesion, va al panel.

### `POST /login`

Valida credenciales y abre sesion de navegador.

Parametros: `username`, `password`

### `GET /logout`

### `POST /search`

HTML form search (used by the web interface).

Parametros: `query`

---

## Shopify

Auditoria contra la tienda, generacion de fichas con IA y publicacion.

### `POST /api/v1/shopify/auditar`

Compara la tienda con nuestra tabla. Los productos que estan en Shopify y no teniamos fichados se anotan como publicados, para no regenerarlos.

Parametros: `escribir`

### `GET /api/v1/shopify/descargar/{fichero}`

Descarga un XLSX generado. Protegido por la auth de la app.

Parametros: `fichero`

### `GET /api/v1/shopify/estado`

Cuantos productos hay publicados, cuantas fichas generadas sin subir y cuantos libros quedan por publicar, con el motivo de los descartados.

Parametros: `con_tienda`

### `POST /api/v1/shopify/exportar`

Escribe el XLSX Matrixify de las fichas en ese estado.

Parametros: `estado`

### `GET /api/v1/shopify/ficheros`

XLSX disponibles para descargar, del mas reciente al mas antiguo.

### `POST /api/v1/shopify/generar`

Genera con IA las fichas de los libros pendientes y las deja guardadas. NO publica nada: eso es el paso siguiente.

Parametros: `limite`, `concurrencia`

### `POST /api/v1/shopify/publicar`

Sube a Shopify las fichas generadas. Tope diario por el limite de Shopify (1.000 variantes nuevas al dia). Empezar siempre con dry_run.

Parametros: `limite`, `dry_run`

### `POST /api/v1/shopify/stop`

---

## Proveedores

Alta, pausa, conciliacion y reparacion de catalogo por proveedor.

### `GET /api/v1/proveedores`

Proveedores con almacen mapeado: totales en BD (libros / con stock), cuantos existen ya en Odoo, cuantos estan encendidos en su almacen, ultimo CEGALD y ultimo fichero recibido, y estado activo/pausado. Ademas, los que mandan libros pero NO tienen almacen.

Parametros: `con_stats`, `con_odoo`

### `POST /api/v1/proveedores/alta`

Da de alta un proveedor: crea su almacen en Odoo si no existe, guarda el mapeo proveedor->almacen y corrige su nombre en la tabla proveedores. Idempotente.

Parametros: `email`, `nombre`, `warehouse_code`, `warehouse_name`

### `POST /api/v1/proveedores/conciliar`

Compara lo que DEBERIA tener stock (stock > 0 en BD, existe en Odoo, PVP >= 2,90) con los quants reales de cada almacen, y marca para re-empuje solo lo que falta. Detecta libros que entraron con stock y nunca cambiaron de cantidad: el sync no los mira y se que

Parametros: `email`, `dry_run`

### `POST /api/v1/proveedores/crear-nuevos`

Enciende/apaga la creacion automatica de los libros nuevos de un proveedor. En false, el ciclo diario ignora sus novedades (caso PODIPRINT: 100.000 titulos sin ficha en Casa del Libro).

Parametros: `email`, `valor`

### `POST /api/v1/proveedores/empujar-ahora`

Marca los libros del proveedor (los que ya existen en Odoo) como cambiados ahora, para que el sync los empuje sin esperar a su proximo fichero. No cambia stock ni precio, solo el timestamp del marcapaginas.

Parametros: `email`

### `POST /api/v1/proveedores/pausar`

Pausa un proveedor: deja de sincronizarse Y su stock en Odoo va a 0 (solo en SU almacen). Con dry_run=true devuelve cuantos quants apagaria. Reversible con /reactivar, pero el stock solo vuelve con su proximo archivo.

Parametros: `email`, `dry_run`, `motivo`

### `POST /api/v1/proveedores/reactivar`

Quita la pausa y marca sus libros para que el sync vuelva a subir su stock en la proxima pasada. Con empujar=false solo quita la marca (el stock solo volveria para los libros cuya cantidad cambie).

Parametros: `email`, `empujar`

### `POST /api/v1/proveedores/reparar-catalogo`

Arregla los dos estados que impiden que un libro lleve stock: (1) sin Track Inventory (is_storable=False) — Odoo rechaza sus quants; (2) variante archivada con plantilla activa — el sync no encuentra el product.product. Empezar siempre con dry_run=true.

Parametros: `email`, `dry_run`

### `GET /api/v1/proveedores/status`

Estado del job de pausa (apagado de stock en curso).

### `POST /api/v1/proveedores/stop`

---

## SINLI Sync

Subida de stock y precios a Odoo de todos los proveedores menos AZETA.

### `POST /api/v1/sync-stock/cegald-replace`

Reemplazo completo CEGALD para UN proveedor (spec Server A): lo presente en el ultimo CEGALD queda a 1 (via sync normal); lo que tiene stock en Odoo pero YA NO viene en el CEGALD se apaga (stock 0), SOLO en el almacen de ese proveedor. Salvaguardas: si present

Parametros: `proveedor`, `dry_run`

### `GET /api/v1/sync-stock/cegald-status`

### `POST /api/v1/sync-stock/cegald-stop`

### `POST /api/v1/sync-stock/cron/start`

Activa cron 1h del sync SINLI.

### `GET /api/v1/sync-stock/cron/status`

### `POST /api/v1/sync-stock/cron/stop`

### `POST /api/v1/sync-stock/marker-to-now`

Setea ultimo_timestamp = NOW(). Usar SOLO después de terminar el backlog inicial — evita reprocesar todo en la siguiente corrida.

### `POST /api/v1/sync-stock/run-backlog`

Modo backlog: bucle hasta vaciar todos los pendientes (~104k libros la primera vez). Idempotente, se puede detener con /stop. Si pasas solo_proveedor o max_books, NO avanza marcapaginas (validación).

Parametros: `solo_proveedor`, `max_books`, `concurrency`

### `POST /api/v1/sync-stock/run-once`

Una pasada del sync: 1 lote de hasta 2000 libros SINLI (no-AZETA) que hayan cambiado desde el ultimo_timestamp. Si pasas solo_proveedor o max_books, NO avanza el marcapaginas (modo validación). Para procesar normalmente y avanzar marker, no pases filtros.

Parametros: `solo_proveedor`, `max_books`, `concurrency`

### `GET /api/v1/sync-stock/status`

### `POST /api/v1/sync-stock/stop`

---

## AZETA

Descarga del CSV de AZETA y subida a su almacen. Va aparte del resto.

### `POST /api/v1/azeta/absence-shutdown`

Apagado por ausencia AZETA: libros con stock en AZE01 que NO vinieron en el ultimo CSV de stock -> stock 0. Guardas: frescura <6h, completitud >=250k presentes (CSV truncado aborta), tope 15% apagado. SIEMPRE dry_run primero.

Parametros: `dry_run`

### `GET /api/v1/azeta/absence-status`

### `GET /api/v1/azeta/catalog-status`

### `POST /api/v1/azeta/catalog-sync`

Descarga el CATALOGO completo de AZETA (~1M libros, ZIP 200MB) y carga al odoo_books_mirror todos los campos descriptivos: titulo, autor, editorial, precio EUR, peso, dimensiones, encuadernacion, categorias, descripcion, portada, idioma, fecha edicion. Solo ac

Parametros: `batch_size`

### `POST /api/v1/azeta/catalog-sync-stop`

### `POST /api/v1/azeta/push-to-odoo`

Fase 2 AZETA. Lee odoo_books_mirror WHERE azeta_fetched_at IS NOT NULL y escribe en Odoo: - product.template: description, weight (kg), list_price (EUR) - product.template.categ_id (desde cache de odoo_product_categories_cache) - stock.quant en AZE01 (location

Parametros: `test_isbn`, `max_books`, `batch_size`

### `GET /api/v1/azeta/push-to-odoo-status`

### `POST /api/v1/azeta/push-to-odoo-stop`

### `POST /api/v1/azeta/stock-cron/start`

Arranca el cron interno: cada 1h descarga CSV de stock AZETA al mirror y luego pushea stock.quant a Odoo AZE01. Idempotente. El estado vive en memoria — si reinicias el servidor el cron NO se reinicia salvo que AZETA_STOCK_CRON_ENABLED=1 en env.

### `GET /api/v1/azeta/stock-cron/status`

### `POST /api/v1/azeta/stock-cron/stop`

### `POST /api/v1/azeta/stock-cycle`

Ciclo completo AZETA en UNA llamada: fetcher CSV -> libros_proveedor -> push incremental a Odoo AZE01 (solo libros cuyo stock cambio desde el ultimo ciclo, via marker) -> avanza marker. Pensado para schedulers externos (n8n cada 1h). Idempotente. Si ya hay un 

### `GET /api/v1/azeta/stock-marker`

Devuelve el marker actual + cuántos libros pendientes desde ese marker.

### `POST /api/v1/azeta/stock-marker-to-now`

Setea el marker stock_actualizado_en a NOW(). Hacer DESPUES del push inicial completo para que el cron solo procese cambios futuros.

### `POST /api/v1/azeta/stock-push-only`

Push SOLO de stock.quant en AZE01. No toca description/weight/categ. Mucho más rápido que push-to-odoo completo. concurrency: workers en paralelo (default env AZETA_PUSH_CONCURRENCY o 8). Usa libros_proveedor AZETA como fuente de qty (debes haber corrido /api/

Parametros: `test_isbn`, `max_books`, `concurrency`

### `GET /api/v1/azeta/stock-push-only-status`

### `POST /api/v1/azeta/stock-push-only-stop`

### `GET /api/v1/azeta/stock-status`

Estado del job + stats persistentes (cuantos libros AZETA tienen stock).

### `POST /api/v1/azeta/stock-sync`

Descarga el CSV de stock de AZETA y popula libros_proveedor. Idempotente — el patron IS DISTINCT FROM solo mueve actualizado_en si el stock cambio realmente.

Parametros: `batch_size`

### `POST /api/v1/azeta/stock-sync-stop`

---

## Auto-Scrape

Ciclo diario de libros nuevos: detectar, enriquecer, crear y avisar.

### `POST /api/v1/auto-scrape/cron/start`

Activa el ciclo diario de libros nuevos (detectar -> scrapear CDL -> crear en Odoo -> Excel -> webhook a Server A para el correo). Para auto-arrancar tras reboot: AUTO_SCRAPE_CRON_ENABLED=1.

### `GET /api/v1/auto-scrape/cron/status`

### `POST /api/v1/auto-scrape/cron/stop`

### `POST /api/v1/auto-scrape/run`

Ciclo autonomo de libros nuevos: detecta ISBNs con stock que no estan en Odoo -> enriquece via CDL -> crea + etiqueta -> genera reporte Excel -> avisa a Server A por webhook (ellos envian el correo). 1x/dia via n8n. 409 si ya hay uno corriendo.

Parametros: `max_new`, `test_sample`

### `GET /api/v1/auto-scrape/status`

### `GET /api/v1/reportes/{report_id}`

Sirve el Excel de un reporte de auto-scrape. Protegido por el Basic Auth global (Server A lo descarga con las credenciales de la API).

Parametros: `report_id`

---

## Auditoria

Que se recibio y que se actualizo. Solo lectura.

### `GET /api/v1/audit/cegalds`

Auditoría CEGALD por proveedor: último evento (goteo incluido), último CEGALD COMPLETO (corrida grande, >= max(500, 20% del stock)), su tamaño, total con stock y fantasmas (stock > 0 no reportado desde el último CEGALD completo).

### `GET /api/v1/audit/events`

Eventos del event_log, mas reciente primero.

Parametros: `categoria`, `nivel`, `limit`, `offset`

### `GET /api/v1/audit/requests`

Peticiones API entrantes (acciones POST/PUT/DELETE): quien llamo que endpoint (IP, usuario Basic, user-agent), con que query/body, status y duracion. Los GET de polling no se registran.

Parametros: `limit`, `path_like`

### `GET /api/v1/audit/stock-proveedor`

Stock ENCENDIDO por proveedor (stock.quant con quantity>0 en su almacen). Los almacenes salen de proveedor_almacen_odoo, no de una lista fija: un proveedor nuevo aparece aqui solo. Consulta Odoo en paralelo. Solo productos activos (los apagados por la regla de

### `GET /api/v1/audit/summary`

Resumen para auditar: libros recibidos por dia/proveedor (BD), eventos por categoria/dia, stats de hoy, ultimo evento por categoria.

Parametros: `days`

---

## Odoo

Espejo local, enriquecimiento desde Casa del Libro y Google Books, categorias.

### `POST /api/v1/cdl/build-isbn-index`

Lee el sitemap-cdl-libros-tematicas y popula la tabla cdl_isbn_index. Job en background. Una vez completado, el scraper de Odoo puede usar direct-URL en vez de search→click para libros conocidos.

### `GET /api/v1/cdl/isbn-index/status`

Estado del job + cuantos ISBNs hay indexados en BD.

### `POST /api/v1/odoo/categories/assign`

Asigna product.template.categ_id a cada libro del mirror que tenga inferred_categories. Usa el cache local para resolver path -> categ_id. Requiere haber corrido /push antes.

Parametros: `batch_size`

### `GET /api/v1/odoo/categories/assign-status`

### `POST /api/v1/odoo/categories/assign-stop`

### `POST /api/v1/odoo/categories/push`

Crea/encuentra en Odoo todas las product.category necesarias para las inferred_categories del mirror. Construye la jerarquia. Cachea cada path para idempotencia.

### `GET /api/v1/odoo/categories/push-status`

### `POST /api/v1/odoo/categories/push-stop`

### `GET /api/v1/odoo/mirror/book/{odoo_id}`

Vista 360 de un libro: mirror + JOIN con books (CDL) + distributor_books.

Parametros: `odoo_id`

### `GET /api/v1/odoo/mirror/browse`

Lista paginada del mirror con filtros utiles para auditar el progreso del enriquecimiento. Para detalle de un libro -> /odoo/mirror/book/{id}.

Parametros: `page`, `per_page`, `search`, `has_category`, `has_description`, `has_supplier`, `has_image`, `supplier`

### `POST /api/v1/odoo/mirror/cdl-fill`

Bulk scrape Casa del Libro para libros del mirror que estan en cdl_isbn_index. Usa proxies + Playwright (direct URL = fast). Guarda a books table y rellena inferred_categories + description en el mirror. Corre en PARALELO con gbooks-fill sin conflicto.

Parametros: `chunk_size`

### `GET /api/v1/odoo/mirror/cdl-fill-status`

### `POST /api/v1/odoo/mirror/cdl-fill-stop`

### `POST /api/v1/odoo/mirror/cdl-http-fill`

Bulk scrape CDL via aiohttp+BeautifulSoup (sin browser, 60-80x mas rapido). Mantiene TODOS los campos: peso, alto, ancho, encuadernacion, traductor, ilustrador, coleccion, descripcion, categorias, autor, editorial, paginas, idioma, fecha, ISBN, imagen. Through

Parametros: `concurrency`, `chunk_size`

### `GET /api/v1/odoo/mirror/cdl-http-fill-status`

### `POST /api/v1/odoo/mirror/cdl-http-fill-stop`

### `POST /api/v1/odoo/mirror/cdl-search-fill`

Bulk scrape Casa del Libro buscando cada ISBN del mirror via search. NO requiere cdl_isbn_index — cubre TODOS los libros del mirror sin cdl_fetched_at. Mas lento por libro que /cdl-fill (sitemap) pero cubertura total. Corre en PARALELO con /cdl-fill y /gbooks-

Parametros: `chunk_size`

### `GET /api/v1/odoo/mirror/cdl-search-fill-status`

### `POST /api/v1/odoo/mirror/cdl-search-fill-stop`

### `GET /api/v1/odoo/mirror/export.csv`

Descarga TODO el catalogo como CSV. JOIN automatico con books (CDL), distributor_books (XLSX) y datos de Google Books para sacar los campos mas ricos por libro (autor, peso, altura, traductor, etc.). Streamea sin cargar todo a memoria.

Parametros: `only_with_categories`

### `POST /api/v1/odoo/mirror/gbooks-fill`

Bulk fill desde Google Books — para cada libro del mirror sin gbooks_fetched_at, llama a la API y llena description, categorias y otros campos. Async puro, 100-1000x mas rapido que CDL. Tip: settea GOOGLE_BOOKS_API_KEY para subir limite a 100k req/dia.

Parametros: `concurrency`, `chunk_size`

### `GET /api/v1/odoo/mirror/gbooks-fill-status`

### `POST /api/v1/odoo/mirror/gbooks-fill-stop`

### `POST /api/v1/odoo/mirror/gbooks-reset-recent`

Resetea gbooks_fetched_at = NULL para libros marcados como fetched en las ultimas N horas que NO recibieron data real de Google Books. Util tras un episodio de rate limit donde se marcaron miles de libros como 'no match' falsamente.

Parametros: `hours_back`

### `POST /api/v1/odoo/mirror/infer-categories`

Llena inferred_categories en odoo_books_mirror cruzando ISBN con las categorias scrapeadas de CDL (tabla books) y los XLSX de distribuidores (distributor_books). Prefiere books > distribuidores. Util cuando Odoo no tiene public_categ_ids asignados pero el scra

### `GET /api/v1/odoo/mirror/infer-status`

### `POST /api/v1/odoo/mirror/start`

Arranca un job en background que espeja product.template a la tabla local odoo_books_mirror. Idempotente — re-ejecutar actualiza los registros existentes. Tiempo estimado: 15-40 min dependiendo del alcance y la salud de Odoo.

Parametros: `only_pending`, `batch_size`

### `GET /api/v1/odoo/mirror/status`

Estado del job + cuantos libros tenemos espejados localmente.

### `POST /api/v1/odoo/mirror/stop`

### `GET /api/v1/odoo/mirror/suppliers-list`

Top proveedores por conteo. Util como datalist en filtros de browse.

### `POST /api/v1/odoo/mirror/suppliers-sync`

Pulla product.supplierinfo + res.partner desde Odoo y espeja el vendor de cada libro en odoo_books_mirror.supplier_names. SOLO LECTURA en Odoo. Idempotente. Tarda ~5 min para ~200k libros con vendor cargado.

Parametros: `batch_size`

### `GET /api/v1/odoo/mirror/suppliers-sync-status`

### `POST /api/v1/odoo/mirror/suppliers-sync-stop`

### `POST /api/v1/odoo/mirror/sync-categories`

Pulla todas las product.public.category de Odoo y las cachea localmente. Tras esto, public_categ_ids -> public_categ_names se resuelven legibles. Si Odoo no tiene categorias asignadas a los libros, este endpoint no ayuda — usar /infer-categories en su lugar.

### `GET /api/v1/odoo/notfound/count`

### `GET /api/v1/odoo/notfound/export`

Exporta los libros no encontrados en CDL a Excel.

Parametros: `background_tasks`

### `POST /api/v1/odoo/notfound/retry`

Mueve libros marcados notfound de vuelta a la cola para reintento. Util tras un periodo de ban/throttle — muchos notfound son falsos. older_than_hours: solo libros marcados notfound hace mas de X horas (default 12, evita recuperar los que acabamos de descartar

Parametros: `older_than_hours`, `limit`

### `POST /api/v1/odoo/sync/start`

Inicia el job de enriquecimiento de Odoo en background. Lee libros sin description_sale del Odoo, scrapea en CDL, y escribe HTML enriquecido en el campo `description` del producto.

### `GET /api/v1/odoo/sync/status`

Estado del job de enriquecimiento + conteos de la cola.

### `POST /api/v1/odoo/sync/stop`

Detiene el job de enriquecimiento en curso (los workers terminan su tarea actual).

---

## Odoo Tags

Clasificacion por completitud de ficha.

### `POST /api/v1/odoo/tags/classify`

Clasifica los libros del mirror y asigna tags en Odoo: Completo / Web / Foto / Stock. Respeta Bloqueado si esta puesto.

Parametros: `dry_run`

### `GET /api/v1/odoo/tags/classify-status`

### `POST /api/v1/odoo/tags/classify-stop`

### `GET /api/v1/odoo/tags/list`

Lista todos los tags + conteo de libros por tag.

---

## Precios

Motor de precios API-15.

### `POST /api/v1/pricing/mass-update`

Motor de precios (API-15, Capa 1): aplica el suplemento por PVP bajo sobre pvp_base y apaga (active=False) los < 2,90 y sin precio. Idempotente. dry_run=True por defecto. 409 si ya corre.

Parametros: `dry_run`, `limit`

### `GET /api/v1/pricing/status`

---

## Relleno Manual

Fichas incompletas para completar a mano.

### `GET /api/v1/manual/dates`

Parametros: `tipo`

### `GET /api/v1/manual/pending`

Parametros: `tipo`, `fecha`, `page`, `page_size`

### `POST /api/v1/manual/save`

---

## Distribuidores

Importacion de catalogos XLSX.

### `POST /api/v1/distributors/import`

Sube un Excel del catalogo de un distribuidor y lo upserta a la tabla `distributor_books` (PK por ISBN). Si el ISBN ya existe, se actualizan los campos con los del XLSX nuevo. Idempotente: re-subir el mismo archivo no duplica.

Parametros: `file`, `fuente`, `batch_size`

### `GET /api/v1/distributors/stats`

Conteo por fuente + cross-stats vs odoo_books_mirror.

### `GET /api/v1/distributors/status`

Estado del ultimo import + total de filas en distributor_books.

---

## Scraping Masivo

Scraping de Casa del Libro por categorias.

### `GET /api/v1/bulk/categories`

Lista de categorías disponibles para scraping masivo.

### `POST /api/v1/bulk/discover-categories`

Descubre dinámicamente todas las categorías desde casadellibro.com/libros. Actualiza el catálogo interno con categorías nuevas encontradas.

### `POST /api/v1/bulk/start`

Inicia un job de scraping masivo en background. En modo 'all' se hace round-robin infinito: avanza N páginas en cada categoría por ronda y rota hasta agotar todas. bulk_scrape se encarga de descubrir subcategorías al inicio.

Parametros: `category`, `max_books`

### `GET /api/v1/bulk/status/{job_id}`

Estado actual de un job de scraping masivo.

Parametros: `job_id`

### `POST /api/v1/bulk/stop/{job_id}`

Detiene un job de scraping masivo.

Parametros: `job_id`

---

## Biblioteca

Consulta del catalogo scrapeado.

### `GET /api/v1/library`

Lista todos los libros guardados en la BD con paginación.

Parametros: `page`, `per_page`, `search`

### `GET /api/v1/library/count`

Cantidad total de libros en la BD.

### `GET /api/v1/library/export`

Exporta toda la biblioteca a un archivo Excel (.xlsx).

Parametros: `background_tasks`

---

## Libros

Consulta de un libro suelto.

### `GET /api/v1/books`

Buscar libro por ISBN o nombre.

Parametros: `q`

---

## Admin

Diagnostico, esquema y utilidades de mantenimiento.

### `GET /api/v1/admin/diagnose`

Diagnostico completo: shard config + counts reales por filtro. Util cuando "Target: 0" — te dice exactamente por que no encuentra libros.

### `GET /api/v1/admin/inferred-categories-summary`

Resumen de inferred_categories en odoo_books_mirror + readiness por campo para el push a Odoo (Fase 2). Categorias: - total_books_with_categ, distinct_paths, distinct_leaves, distinct_roots - by_source, depth_distribution - top_paths (top_n por conteo), top_ro

Parametros: `top_n`

### `GET /api/v1/admin/odoo-modules`

Comprueba si modulos clave estan instalados en Odoo. Necesario para decidir si push de categorias va a `categ_id` (interna, siempre existe) o a `public_categ_ids` (e-commerce, requiere website_sale).

### `GET /api/v1/admin/odoo-warehouses`

Lista los warehouses de Odoo con su code y lot_stock_id. Util para que el sync SINLI verifique que sus codigos (AZE01, ICA01, LES01...) coinciden con los reales antes de escribir stock.quant.

### `POST /api/v1/admin/run-migrations`

Re-ejecuta init_db() para aplicar todos los ALTER TABLE / CREATE TABLE pendientes. Idempotente — si ya estan aplicados, no hace nada. Sin redeploy. Devuelve la lista de columnas resultante de odoo_books_mirror.

### `GET /api/v1/admin/schema-info`

Lista las columnas reales de una tabla. Usa esto para confirmar que el ALTER TABLE corrio (ej: que existan cdl_author, cdl_image_url, etc.)

Parametros: `table`

### `GET /api/v1/admin/stock-debug`

Devuelve el estado de un ISBN en BD (libros_proveedor + mirror) y en Odoo (product.template + product.product + stock.quant en todas las locations). Util para validar cualquier desincronización.

Parametros: `isbn`

### `GET /api/v1/admin/throughput`

Medidor HONESTO de velocidad basado en cdl_fetched_at / gbooks_fetched_at persistido en la BD. A diferencia de los counters del job (que se resetean con cada redeploy), esto cuenta filas reales con timestamp en los ultimos N minutos. Si lleva dias sin cambiar,

---

## Diagnostico

Salud de proxies y consultas sueltas a Google Books.

### `GET /api/v1/gbooks/lookup`

Prueba directa a Google Books con un ISBN. Devuelve los datos normalizados que el enricher usaria para ese libro, sin escribir nada a Odoo. Util para verificar que la cascada funciona antes de arrancar el job.

Parametros: `isbn`

### `GET /api/v1/proxies/health`

Estado del tracker de health por proxy. Muestra fallos consecutivos, totales, si esta marcada muerta, y desde cuando. Los browsers nuevos saltan automaticamente las proxies muertas y los jobs caen a IP directa si TODAS mueren.

### `POST /api/v1/proxies/health/reset`

Resetea TODAS las proxies a vivas. Util tras un episodio de bloqueo temporal de CDL (cuando Webshare las habilita de nuevo).

### `POST /api/v1/proxies/healthcheck`

Prueba cada proxy contactando https://api.ipify.org para ver que IP aparece desde el lado del destino. Si la IP devuelta es la del proxy, funciona; si timeout o ConnectionError, el proxy esta muerto. Util tras un redeploy para detectar proxies bloqueados antes

### `GET /api/v1/proxies/status`

Lista los proxies cargados desde la env var PROXY_POOL al arrancar. Si esta vacio, el server saldra con su IP directa hacia CDL. Incluye shard info — util para multi-worker (Server A + Server B).

---

## Notificaciones

Telegram.

### `POST /api/v1/notify/delete-webhook`

Quita el webhook (apaga los comandos sin tocar el token).

### `POST /api/v1/notify/setup-webhook`

Registra este server como webhook target del bot de Telegram. Lo llamas UNA VEZ, manualmente, desde el server que quieres que responda los comandos. Telegram solo guarda una URL — si lo llamas desde otro server despues, queda apuntando ahi. Telegram exige HTTP

### `GET /api/v1/notify/status`

¿Esta configurado el Telegram en este server?

### `POST /api/v1/notify/telegram-webhook`

Endpoint publico que recibe updates de Telegram. Telegram llama aqui cuando alguien manda un mensaje al bot. Procesa comandos como /status. Seguridad: 1. Si TELEGRAM_WEBHOOK_SECRET esta seteado, verifica el header. 2. handle_command() verifica que el chat_id s

Parametros: `x_telegram_bot_api_secret_token`

### `POST /api/v1/notify/test`

Manda un mensaje de prueba al Telegram configurado para verificar que TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID son correctos.
