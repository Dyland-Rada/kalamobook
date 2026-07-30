# Coordinación scraper ↔ sync (Supabase → Odoo)

Documento que captura el reparto definitivo de competencias entre:

- **Scraper** (este repo, `kalamobook`): enriquece Odoo con descripción, categorías,
  dimensiones desde Casa del Libro **+ TODO lo de AZETA** (stock, precio, todo).
- **Sync SINLI → Odoo** (otro Claude, repo de n8n): lleva precio/stock de
  proveedores SINLI a Odoo, cron horario. **NO toca AZETA**.

Ambos viven en `Server B` (Contabo Dokploy, 24 GB RAM) y comparten el mismo
Postgres (`84.46.251.249:5432`, db `postgres`).

## 1. Reparto de campos en Odoo — POR PROVEEDOR

**Cambio importante (2026-06-23)**: AZETA pasa 100% al scraper por dos motivos:
1. AZETA tiene su catálogo COMPLETO disponible en un CSV HTTP (1M libros con precio
   EUR, peso, dimensiones, descripción, categorías). Más rápido que SINLI email.
2. Sin colisión posible: AZETA va al almacén AZE01 (lot_stock_id 14). Los demás
   proveedores escriben en almacenes distintos (ICA01/DIS03/LES01/EDI01/ALF01).

### Por proveedor

| Proveedor | Almacén | Stock+Precio en Odoo | Description/Dimensiones |
|---|---|---|---|
| **AZETA** | AZE01 (14) | **scraper (yo)** | scraper (yo) |
| ÍCARO | ICA01 (50) | sync | scraper |
| DISTRIFORMA | DIS03 (32) | sync | scraper |
| PUNXES | LES01 (56) | sync | scraper |
| ALFA OMEGA | ALF01 (?) | sync | scraper |
| AKAL | EDI01 (38) | sync | scraper |
| Otros SINLI | varios | sync | scraper |

### Por campo de Odoo (regla general)

| Campo Odoo | Quién escribe |
|---|---|
| `description` (HTML) | **scraper** (todos los proveedores) |
| `categ_id` / `public_categ_ids` | **scraper** (todos) |
| `weight`, `volume`, dimensiones | **scraper** (todos) |
| `list_price` (excepto AZETA) | **sync** |
| `list_price` para AZETA | **scraper** |
| `stock.quant` en ICA01/DIS03/LES01/EDI01/ALF01 | **sync** |
| `stock.quant` en AZE01 | **scraper** |
| `name` al crear producto nuevo | **sync** (cuando active Grupo B) |
| `barcode` | carga inicial |
| `seller_ids` / `supplierinfo` | nadie por ahora |

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

### 2.1 Pausa de proveedores (tabla `proveedor_pausa`, 2026-07-30)

| Tabla | Owner | Otro proceso |
|---|---|---|
| `proveedor_pausa` | **sync/scraper (rol postgres)** | n8n no la necesita |

Tabla propia: `proveedor_email` (PK), `activo` (default true), `pausado_en`,
`pausado_motivo`, `reactivado_en`. La crea `proveedores_admin.ensure_schema()`
al arrancar la app (CREATE TABLE IF NOT EXISTS, idempotente).

Va en tabla aparte y no en columnas de `proveedor_almacen_odoo` porque esa
tabla (como `proveedores` y `libros_proveedor`) es de `supabase_admin`: el rol
`postgres` puede INSERT/UPDATE pero **no ALTER**.

**`activo = false` significa: ese proveedor NO se sincroniza a Odoo y su
almacén ya está a 0.** Al pausar se apagan todos sus quants con stock; el
sync SINLI salta sus libros (`skipped_pausados`) y los push de AZETA se
cancelan si AZETA está pausado. Un proveedor sin fila en `proveedor_pausa`
está activo.

Quien ingiere ficheros (n8n de Server A) **puede seguir escribiendo en
`libros_proveedor` con normalidad** — no hace falta que mire esta columna.
El filtro está en la escritura a Odoo, no en la ingesta. Al reactivar, el
stock entra con el siguiente fichero del proveedor (el marcapáginas del
sync avanzó durante la pausa, así que las filas viejas no se reprocesan).

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

## 6. Estado actual (2026-06-23)

- Scraper:
  - CDL HTTP fill validado (~2000 libros/min capacidad)
  - Fetcher AZETA stock validado contra endpoint real (262k ISBNs)
  - Catálogo AZETA descubierto: 1.038.024 ISBNs con TODOS los campos
  - Decisión: AZETA pasa 100% al scraper (este lado)
- Sync:
  - Validó stock.quant + action_apply_inventory en Odoo v19 (workaround Fault aplicado)
  - Va a empezar con Grupo A (actualizar stock+precio de libros que ya existen
    en Odoo) para los proveedores SINLI no-AZETA.
  - Grupo B (crear productos nuevos) en pausa. No requiere refresh del mirror.
- Mirror: 1.068.869 libros espejados. 1.068.856 con vendor sincronizado.
  132k+ con categoría inferida.

## 7. Carga AZETA (responsabilidad del scraper)

Fases:
- **Fase 1**: descargar catálogo CSV de AZETA, cargar al mirror (todos los campos
  descriptivos). Sin tocar Odoo todavía.
- **Fase 2**: push a Odoo (description, weight, dimensions, list_price para AZETA,
  stock.quant en AZE01 lot 14).
- **Mantenimiento**: cron horario para refrescar stock (endpoint stock.php).
  Catálogo completo (descripción + precio) cada 24h o on-demand.

## Cambios futuros que requieren coordinación

Cualquier cambio a las tablas compartidas (`odoo_books_mirror`, `enrichment_queue`)
o a los campos de Odoo en el reparto debe acordarse aquí antes de implementarse.