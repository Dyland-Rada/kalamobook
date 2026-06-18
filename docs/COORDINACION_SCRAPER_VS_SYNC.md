# Coordinación scraper ↔ sync (Supabase → Odoo)

Documento que captura el reparto definitivo de competencias entre:

- **Scraper** (este repo, `kalamobook`): enriquece Odoo con descripción, categorías,
  dimensiones desde Casa del Libro.
- **Sync SINLI → Odoo** (otro Claude, repo de n8n): lleva precio/stock de
  proveedores desde Supabase a Odoo, cron horario.

Ambos viven en `Server B` (Contabo Dokploy, 24 GB RAM) y comparten el mismo
Postgres (`84.46.251.249:5432`, db `postgres`).

## 1. Reparto de campos en Odoo (NO ROMPER)

| Campo Odoo | Quién escribe | El otro NO toca |
|---|---|---|
| `list_price` | **sync** | scraper jamás |
| `stock.quant` | **sync** | scraper jamás |
| `name` (al crear) | **sync** | scraper jamás (solo al CREAR un producto nuevo) |
| `barcode` | carga inicial | ambos solo leen |
| `description` (HTML) | **scraper** | sync jamás |
| `categ_id` / `public_categ_ids` | **scraper** | sync jamás |
| `weight`, `volume`, dimensiones | **scraper** (futuro) | sync jamás |
| `seller_ids` / `supplierinfo` | nadie por ahora | (futuro: sync) |

## 2. Tablas en Postgres compartido — owner por tabla

| Tabla | Owner | Otro proceso |
|---|---|---|
| `odoo_books_mirror` | **scraper escribe**, sync **lee** | sync lo usa como cache de Odoo para evitar XML-RPC |
| `enrichment_queue` (legacy) | **scraper exclusivo** | sync NO toca |
| `books`, `cdl_isbn_index`, `distributor_books`, `notfound_books` | scraper exclusivo | sync NO toca |
| `proveedores`, `libros_proveedor` | **SINLI/n8n** (vía Supabase) | scraper NO toca (solo lee si necesita) |
| `sync_state`, `sync_errores` | **sync exclusivo** | scraper NO toca |
| `sync_sin_ficha` | **sync exclusivo** | scraper la puede consumir como cola de prioridad (futuro) |
| `proveedor_almacen_odoo` | **sync exclusivo** | scraper puede leer si quiere mostrar en CSV (futuro) |

## 3. Responsabilidades del scraper relativas al sync

### 3.1 Mantener `odoo_books_mirror` fresco
El sync depende del mirror para clasificar A/B/C (existe en Odoo / hay que crear /
sin ficha). Si el mirror se queda viejo:
- `list_price` viejo → el sync compara con un valor obsoleto y decide actualizar
  cuando no hace falta (no es grave, idempotente).
- Producto creado por el sync NO está aún en mi mirror → la siguiente vez que
  el sync lo procese, va a verlo como "no existe" y lo va a intentar crear de
  nuevo. **Riesgo de duplicado.**

Mitigación: **re-correr el mirror periódicamente** (sugerido: 1× por día o
después de cada ejecución del sync). Idempotente, ON CONFLICT DO UPDATE.

### 3.2 NO escribir en Odoo los campos del sync
Mi código actualmente:
- `enrichment.py run_enrichment_job`: escribe `description` HTML. ✅ OK.
- `odoo_mirror.assign_books_to_odoo_categories`: escribe `categ_id`. ✅ OK
  (es campo del scraper).
- `odoo_mirror.sync_suppliers_from_odoo`: solo LEE supplierinfo. ✅ OK.
- `_upsert_batch` (mirror): solo lee `list_price`, no escribe. ✅ OK.

**Verificado: el scraper NO escribe `list_price`, `stock`, `supplierinfo`.**

## 4. Coordinación operacional (futuro)

### Cuando el sync esté activo
- El sync corre `:00` cada hora.
- Si mi push de `description` corre al mismo tiempo, podemos saturar Odoo SaaS.
- Mitigación 1: lock cooperativo via `sync_state.lock_activo` (el otro Claude lo
  implementó). El scraper puede chequearlo antes de pushar.
- Mitigación 2: rate-limit en mi `OdooClient` (max 5 writes/seg). 1h trabajo.

### Cola de prioridad para el scraper
Cuando el sync detecte un ISBN sin ficha y lo encole en `sync_sin_ficha`, el
scraper podría consumirlo con prioridad. Esto NO está implementado en mi
código todavía. Implementación futura cuando sea relevante.

## 5. Para el otro Claude — utilidades del scraper que le pueden servir

- **OdooClient en `odoo_client.py`**: cliente JSON-RPC async. Pero el sync es
  sync con cron, mejor usar XML-RPC sync clásico. Compatible a nivel de Odoo.
- **Endpoint `/api/v1/admin/odoo-warehouses`** (a añadir): lista los warehouses
  reales de Odoo con su code/name/lot_stock_id para verificar
  `proveedor_almacen_odoo`.
- **Endpoint `/api/v1/admin/odoo-modules`**: ya existente. Comprueba qué módulos
  están instalados (website_sale, stock, sale, etc.).
- **Endpoint `/api/v1/admin/schema-info?table=odoo_books_mirror`**: ya existente.
  Lista columnas reales del mirror.

## 6. Estado actual (2026-06-18)

- Scraper: ~70k libros con `cdl_fetched_at`, velocidad ~25-37 libros/min,
  proyección 3-4 semanas para 1M libros.
- Sync: en diseño/implementación inicial, paso 1-3 del plan del otro Claude.
- Mirror: 1.068.869 libros espejados. 1.068.856 con vendor sincronizado.
  132k+ con categoría inferida.

## Cambios futuros que requieren coordinación

Cualquier cambio a las tablas compartidas (`odoo_books_mirror`, `enrichment_queue`)
o a los campos de Odoo en el reparto debe acordarse aquí antes de implementarse.