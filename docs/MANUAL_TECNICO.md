# Manual técnico — Kalamo

Arquitectura, API, base de datos y operación. Complementa la
[Guía operativa](GUIA_OPERATIVA.md), que cubre el uso diario sin tecnicismos.

Última revisión: 31 de julio de 2026.

---

## 1. Arquitectura

```
   PROVEEDORES                SERVER A                    SERVER B (esta app)
                          84.46.251.249                   185.182.8.62
                                                          Contabo, 8 cores, 24 GB
   CEGALD por SINLI  ──>  n8n: lee correo    ──┐
   (11 proveedores)       y adjuntos           │
                                               ├──>  Postgres  <──  FastAPI + Playwright
   Ficheros a mano   ──>  n8n: ingesta       ──┘   213.165.85.117        │
   (Logista, Machado,     desde Drive                                    │
    Penguin)                                                             ├──> Odoo (JSON-RPC)
                                                                         │    javier-vela.odoo.com
   AZETA: CSV HTTP  ────────────────────────────────────────────────────>│
                                                                         ├──> Shopify (GraphQL/REST)
                                                                         │    kalamobooks.myshopify.com
                                                                         │
                                                                         ├──> Casa del Libro (scraping)
                                                                         └──> Google Books (API)
```

**Reparto de responsabilidades**

| | Server A | Server B (esta app) |
|---|---|---|
| Recibe ficheros de proveedores | ✅ n8n | ❌ |
| Escribe `libros_proveedor` | ✅ | solo AZETA |
| Sincroniza a Odoo | ❌ | ✅ |
| Enriquece fichas | ❌ | ✅ |
| Publica en Shopify | ❌ | ✅ |
| Manda los correos | ✅ | manda el webhook |

**Anti-colisión con AZETA:** AZETA va 100% por el scraper (Server B lee su
CSV). El sync SINLI la excluye explícitamente y nunca escribe en la ubicación
14 (AZE01).

## 2. Stack

- **Python 3.11** + FastAPI + uvicorn
- **Playwright Chromium** para el scraping pesado; `aiohttp` para el ligero
- **Postgres 15.8** (Supabase autoalojado en Server A)
- **Docker**, desplegado con Dokploy
- Sin ORM: SQL directo con `psycopg2`
- Sin cola externa: los trabajos largos van en hilos con su propio event loop

## 3. Módulos

| Fichero | Qué hace |
|---|---|
| `app.py` | FastAPI: 143 endpoints y el panel web |
| `db.py` | Conexión. Traduce `?` a `%s` según el motor |
| `odoo_client.py` | Cliente JSON-RPC de Odoo, con reintentos |
| `sync_stock_sinli.py` | Sube stock y precios a Odoo (todos menos AZETA) |
| `azeta_stock.py` | Descarga el CSV de stock de AZETA |
| `azeta_push_odoo.py` | Sube el stock de AZETA a AZE01 |
| `azeta_catalog.py` | Catálogo completo de AZETA al mirror |
| `proveedores_admin.py` | Alta, pausa, conciliación y reparación de catálogo |
| `pricing_engine.py` | Regla API-15 de precios |
| `auto_scrape.py` | Ciclo diario de libros nuevos |
| `odoo_mirror.py` | Espejo local de Odoo |
| `odoo_tags.py` | Clasificación Completo/Web/Foto/Stock |
| `enrichment.py` | Enriquecimiento desde Casa del Libro |
| `cdl_http_client.py` | Cliente HTTP de Casa del Libro |
| `google_books.py` | Cliente de Google Books |
| `manual_fill.py` | Relleno manual de fichas |
| `shopify_api.py` | Cliente de Shopify |
| `shopify_ficha.py` | Generador de fichas con DeepSeek |
| `shopify_pub.py` | Auditoría, generación por lotes, publicación y XLSX |
| `audit_log.py` | Registro de eventos y peticiones |
| `notify.py` | Telegram |

## 4. Base de datos

**Propiedad de las tablas.** `proveedores`, `libros_proveedor`,
`proveedor_almacen_odoo` y `cegald_isbns_v2` son de `supabase_admin`: el rol
`postgres` puede leer y escribir pero **no puede hacer `ALTER`**. Cualquier
estado nuevo va en tabla propia.

### Las que importan

| Tabla | Filas | Dueño | Contenido |
|---|---|---|---|
| `odoo_books_mirror` | 1.169.714 | nosotros | Espejo de Odoo + todo lo enriquecido |
| `shopify_productos` | 746.943 | nosotros | Las 23 columnas Matrixify de lo publicado |
| `libros_proveedor` | 708.515 | Server A | Stock y precio por proveedor |
| `cdl_isbn_index` | 683.112 | nosotros | Índice de ISBNs de Casa del Libro |
| `books` | 326.380 | nosotros | Fichas scrapeadas |
| `distributor_books` | 208.767 | nosotros | Catálogos XLSX de distribuidores |
| `cegald_isbns_v2` | 2.007.834 | Server A | Foto de cada CEGALD recibido |
| `sinli_auditoria` | 1.820 | Server A | Registro de cada fichero SINLI |
| `proveedor_almacen_odoo` | 14 | Server A | Mapeo proveedor → almacén |
| `proveedor_pausa` | — | nosotros | Estado de pausa y `crear_nuevos` |
| `sync_state` | 2 | nosotros | Marcapáginas y lock de los syncs |
| `event_log` | 1.980 | nosotros | Qué hizo cada job |

### Campos clave de `odoo_books_mirror`

- `barcode` — el ISBN, la clave de todo
- `odoo_id` — id del `product.template`
- `list_price` — precio web (ya con suplemento)
- `pvp_base` — PVP crudo del proveedor, para idempotencia
- `cdl_*` — lo scrapeado de Casa del Libro
- `gbooks_*` — lo de Google Books
- `azeta_*` — lo del catálogo de AZETA
- `nuevo_creado_en` — marca los creados por nuestro pipeline
- `inferred_categories` — categoría del distribuidor, sin normalizar

### El marcapáginas

`sync_state` guarda, por proceso, hasta qué `actualizado_en` se ha procesado.
El sync solo mira filas posteriores.

**Consecuencia importante:** la ingesta solo mueve `actualizado_en` cuando el
stock **cambia**. Un libro que entra con stock y nunca varía se queda fuera del
radar para siempre. De ahí `forzar_resync()` y `conciliar()`.

**Lock.** `sync_state.lock_activo` evita dos ejecuciones. Los locks huérfanos
(proceso muerto en un deploy) se roban a los 30 minutos.

## 5. API

Todo bajo autenticación HTTP Basic o sesión de navegador. 143 endpoints;
aquí van los que se usan. Documentación viva en `/docs`.

### Shopify

| Método | Ruta | Parámetros | Qué hace |
|---|---|---|---|
| GET | `/api/v1/shopify/estado` | `con_tienda` | Contadores y estado del job |
| POST | `/api/v1/shopify/auditar` | `escribir` | Exporta la tienda y anota lo publicado |
| POST | `/api/v1/shopify/generar` | `limite`, `concurrencia` | Genera fichas con IA |
| POST | `/api/v1/shopify/publicar` | `limite`, `dry_run` | Sube a la tienda |
| POST | `/api/v1/shopify/exportar` | `estado` | Escribe el XLSX Matrixify |
| GET | `/api/v1/shopify/ficheros` | | Lista los XLSX generados |
| GET | `/api/v1/shopify/descargar/{fichero}` | | Descarga un XLSX |
| POST | `/api/v1/shopify/stop` | | Detiene el job |

### Proveedores

| Método | Ruta | Parámetros | Qué hace |
|---|---|---|---|
| GET | `/api/v1/proveedores` | `con_stats`, `con_odoo` | Tabla completa |
| POST | `/api/v1/proveedores/pausar` | `email`, `dry_run`, `motivo` | Pausa y pone su stock a 0 |
| POST | `/api/v1/proveedores/reactivar` | `email`, `empujar` | Quita la pausa y re-empuja |
| POST | `/api/v1/proveedores/alta` | `email`, `nombre`, `warehouse_code` | Crea almacén y mapeo |
| POST | `/api/v1/proveedores/empujar-ahora` | `email` | Marca sus libros para re-empuje |
| POST | `/api/v1/proveedores/conciliar` | `email`, `dry_run` | Detecta stock sin subir |
| POST | `/api/v1/proveedores/reparar-catalogo` | `email`, `dry_run` | Arregla los que no admiten stock |
| POST | `/api/v1/proveedores/crear-nuevos` | `email`, `valor` | Interruptor de creación automática |

### Sync de stock

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/api/v1/sync-stock/run-once` | Un lote de 2.000 |
| POST | `/api/v1/sync-stock/run-backlog` | Bucle hasta vaciar |
| GET | `/api/v1/sync-stock/status` | Estado, marcapáginas y errores |
| POST | `/api/v1/sync-stock/cron/{start,stop}` | Cron horario |
| POST | `/api/v1/sync-stock/cegald-replace` | Apagado por ausencia |

### AZETA

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/api/v1/azeta/stock-cycle` | Ciclo completo: descarga + push + apagado |
| POST | `/api/v1/azeta/stock-sync` | Solo descargar el CSV |
| POST | `/api/v1/azeta/stock-push-only` | Solo subir a AZE01 |
| POST | `/api/v1/azeta/stock-cron/{start,stop}` | Cron horario |

### Libros nuevos

| Método | Ruta | Parámetros | Qué hace |
|---|---|---|---|
| POST | `/api/v1/auto-scrape/run` | `max_new`, `test_sample` | Ciclo completo |
| POST | `/api/v1/auto-scrape/cron/{start,stop}` | | Cron diario |
| GET | `/api/v1/reportes/{id}` | | Excel del reporte |

### Auditoría y diagnóstico

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/api/v1/audit/summary` | Resumen de N días |
| GET | `/api/v1/audit/events` | Log de eventos |
| GET | `/api/v1/audit/cegalds` | CEGALDs por proveedor |
| GET | `/api/v1/audit/stock-proveedor` | Encendidos por almacén |
| GET | `/api/v1/admin/stock-debug` | Radiografía de un ISBN |
| GET | `/api/v1/admin/schema-info` | Columnas de una tabla |
| GET | `/api/v1/admin/odoo-warehouses` | Almacenes reales de Odoo |

## 6. Variables de entorno

### Imprescindibles

```
APP_USERNAME / APP_PASSWORD        auth del panel
DATABASE_URL                       postgres
ODOO_URL / ODOO_DB / ODOO_LOGIN / ODOO_API_KEY
```

### Crones (sin esto no arrancan tras un reinicio)

```
AZETA_STOCK_CRON_ENABLED=1         stock AZETA cada hora
SYNC_STOCK_CRON_ENABLED=1          sync SINLI cada hora
AUTO_SCRAPE_CRON_ENABLED=1         libros nuevos, diario
```

### Shopify y DeepSeek

```
SHOPIFY_TIENDA=kalamobooks.myshopify.com
SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET
SHOPIFY_TOPE_DIARIO=900            tope de altas al día
DEEPSEEK_API_KEYS                  varias separadas por coma, con relevo
```

### Ajustes con valor por defecto razonable

```
SHOPIFY_PESO_DEFECTO=350           gramos cuando no se sabe
SHOPIFY_ANIO_NOVEDAD=2025          desde qué año es novedad
SHOPIFY_DESC_MINIMA=120            descripción mínima para publicar
SYNC_STOCK_LOCK_TTL_MIN=30         cuándo se roba un lock huérfano
CEGALD_UMBRAL_PCT=50               salvaguarda del apagado por ausencia
SCRAPE_WEBHOOK_TIMEOUT_S=180       Server A tarda en responder
```

## 7. Trampas conocidas

Cosas que costaron horas de encontrar. Antes de tocar algo relacionado,
leer esto.

### Odoo

**`is_storable` obligatorio.** En Odoo 19 los tipos son `consu`/`service`/
`combo`; lo almacenable es el booleano `is_storable`. Crear con `type=consu`
sin él da un producto que **rechaza cualquier stock**: *"No se pueden crear
cuantos para consumibles o servicios"*. Afectó a 97.848 libros.

**Las variantes no se desarchivan solas.** Al archivar una plantilla, Odoo
archiva sus variantes; al desarchivarla, **no las devuelve**. Queda plantilla
activa con variante archivada: el sync no la encuentra y nunca le escribe
stock. Afectó a 22.074. Por eso `pricing_engine.reactivar_variantes()` se
llama en los tres sitios que escriben `active=True`.

**`action_apply_inventory` lanza excepción pero funciona.** En Odoo 19 SaaS
devuelve un Fault al serializar; hay que envolverlo en try/except y verificar
releyendo `quantity`.

### Postgres

**Sin `ALTER` en las tablas de Supabase.** El rol `postgres` no es
superusuario. Estado nuevo, en tabla propia.

**Índices del catálogo corruptos.** El 31/07 había filas duplicadas en
`pg_type` de sesiones caídas, y **fallaba crear tablas con ciertos nombres**.
Síntoma: `heap tid from index tuple ... points to unused heap page item`.
Se arregla borrando los tipos huérfanos y reindexando (hace falta superusuario).

**`%` en los `LIKE`.** `db.execute_query` traduce `?` a `%s`; si la consulta
lleva `LIKE 'cat:%'` y se le pasan parámetros, psycopg2 toma el `%` por un
marcador. Usar `cur.execute` sin parámetros.

### openpyxl

**Las filas vienen recortadas.** En modo `read_only`, `iter_rows` devuelve
tuplas más cortas que la cabecera cuando las últimas celdas van vacías.
Indexar con red o revienta.

**Escribir en `write_only`.** Para ficheros de cientos de miles de filas es
obligatorio; en modo normal se carga todo en memoria.

**Caracteres de control.** Un `.xlsx` es XML: un carácter de control en una
sinopsis corrompe el fichero. Todo texto pasa por `limpiar_xml()`.

### Shopify

**Tope de 1.000 variantes nuevas al día** por encima de 50.000 en la tienda.
Por eso la carga grande va por XLSX y solo el goteo por API.

**El token caduca a las 24 h.** La app del Dev Dashboard no da token fijo: se
pide con `client_credentials`. Se guardan Client ID y Secret, no el token.

**Los permisos no se aplican solos.** Al cambiar los alcances hay que publicar
una versión nueva **y reinstalar la app**, o el token sale sin permisos.

**Leer el catálogo entero:** con operación masiva de GraphQL, no paginando.
746.925 productos en 76 segundos frente a ~3.000 peticiones.

## 8. Jobs y concurrencia

Los trabajos largos van en un hilo con su propio event loop
(`asyncio.new_event_loop()`), no en el del servidor. Cada módulo tiene una
variable global con el estado del job y un `get_status()` que consulta el panel.

Solo puede haber **un job por familia** a la vez: se devuelve 409 si ya hay uno.

**Cuidado con el estado compartido.** Escribir el job global antes de coger el
lock hacía que el cron horario pisara el estado de un backlog en curso y
pareciera fallido. El lock va primero.

## 9. Despliegue

1. `git push origin main`
2. **Redeployar a mano en Dokploy** — no es automático
3. Comprobar llamando a un endpoint nuevo: si da 404, no ha cogido el código

**No hace falta rebuild** salvo que cambie `requirements.txt` o el
`Dockerfile`.

**Los crones se levantan solos** al arrancar si están sus variables.

**Un deploy a mitad de un job lo mata.** Deja el lock cogido; se libera solo a
los 30 minutos. Antes de desplegar, mirar que no haya nada corriendo.

## 10. Diagnóstico rápido

**Un libro no aparece en la web:**
```
GET /api/v1/admin/stock-debug?isbn=9788412345678
```
Devuelve su estado en la base de datos, en el espejo, en Odoo (con todos sus
quants por almacén) y los errores recientes.

**Un proveedor no sincroniza:** mirar `Último CEGALD` en la tabla de
proveedores. Si está viejo, el problema es de entrada, no nuestro.

**Stock que no sube:** `POST /api/v1/proveedores/conciliar?dry_run=true`
compara la base de datos con Odoo y dice cuántos faltan.

**Productos que no admiten stock:**
`POST /api/v1/proveedores/reparar-catalogo?dry_run=true`

**El sync parece fallido:** comprobar `pending_count` y `lock_activo` en
`/api/v1/sync-stock/status`. Si los pendientes son 0 y el lock está libre,
terminó bien.

## 11. Reglas de negocio en código

**Precio (API-15)** — `pricing_engine.web_price()`:

| PVP crudo | Precio web |
|---|---|
| < 2,90 | No publicar (producto apagado) |
| 2,90–4,99 | +2,00 |
| 5,00–6,00 | +1,50 |
| 6,01–7,50 | +1,00 |
| > 7,50 | Sin suplemento |

Idempotente: el precio web se calcula **siempre** desde `pvp_base`, nunca
sobre uno ya suplementado.

**Origen del precio, en cascada:** `list_price` → `pvp_base` →
`libros_proveedor.precio_con_iva`.

**Etiquetas de completitud** — `odoo_tags._classify()`:

| Etiqueta | Requiere |
|---|---|
| Completo | imagen + descripción + peso y medidas |
| Web | imagen + descripción |
| Foto | descripción, sin imagen |
| Stock | ni imagen ni descripción |

**Publicable en Shopify:** ISBN `978`/`979`, título propio, portada,
descripción de 120 caracteres o más, precio ≥ 2,90 y stock.
