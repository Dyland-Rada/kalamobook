# Coordinación: tabla `enrichment_queue` compartida

Documento para coordinar el schema de la tabla `enrichment_queue` entre:

- **Sistema scraper** (Claude scraping, repo `kalamobook`)
- **Sistema sync** (Claude n8n / sync Supabase → Odoo, en diseño)

Ambos sistemas viven en el mismo Postgres (`84.46.251.249:5432`, db `postgres`).
Para evitar choque, **unificamos en una sola tabla con schema híbrido**.

## Schema actual del scraper (legacy, ya en producción)

```sql
CREATE TABLE enrichment_queue (
    odoo_id           INTEGER PRIMARY KEY,
    isbn              TEXT,
    name              TEXT,
    status            TEXT DEFAULT 'pending',  -- pending/scraping/scraped/pushing/done/notfound
    retries           INTEGER DEFAULT 0,
    scraped_data      JSONB,
    last_attempt      TIMESTAMP,
    started_at        TIMESTAMP,
    push_attempt_at   TIMESTAMP,
    added_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Usado por**: `enrichment.py` (`run_enrichment_job`) para hacer scrape CDL y
push HTML a Odoo `description`. Hoy tiene ~7M filas históricas.

## Schema requerido por el sync SINLI

```sql
isbn              TEXT
motivo            TEXT          -- 'sinli_sin_ficha', etc.
proveedor_email   TEXT
prioridad         INTEGER       -- mayor = más urgente
encolado_en       TIMESTAMPTZ
procesado         BOOLEAN DEFAULT false
```

**Usado por**: el sync, cuando recibe un libro de SINLI sin ficha en la tabla
`books`. Lo encola aquí para que el scraper lo procese con prioridad.

## Propuesta: schema híbrido (unión)

```sql
ALTER TABLE enrichment_queue
    ADD COLUMN IF NOT EXISTS motivo           TEXT DEFAULT 'legacy_mirror',
    ADD COLUMN IF NOT EXISTS proveedor_email  TEXT,
    ADD COLUMN IF NOT EXISTS prioridad        INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS encolado_en      TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS procesado        BOOLEAN DEFAULT FALSE;

-- Hacer odoo_id nullable: el sync no siempre lo tiene
-- (libro nuevo en SINLI antes de crearse en Odoo)
ALTER TABLE enrichment_queue ALTER COLUMN odoo_id DROP NOT NULL;

-- Si odoo_id era PRIMARY KEY, hay que quitar la restricción y crear una nueva
ALTER TABLE enrichment_queue DROP CONSTRAINT enrichment_queue_pkey;
ALTER TABLE enrichment_queue ADD COLUMN IF NOT EXISTS id SERIAL PRIMARY KEY;

-- Índices clave para evitar duplicados y acelerar queries
CREATE UNIQUE INDEX IF NOT EXISTS uq_enrichment_isbn_motivo
    ON enrichment_queue (isbn, motivo)
    WHERE isbn IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_enrichment_odoo_id ON enrichment_queue (odoo_id);
CREATE INDEX IF NOT EXISTS idx_enrichment_pending
    ON enrichment_queue (procesado, prioridad DESC)
    WHERE procesado = FALSE;
CREATE INDEX IF NOT EXISTS idx_enrichment_status
    ON enrichment_queue (status);
```

## Convenciones de uso

### Quién escribe qué columna

| Columna | Scraper | Sync SINLI |
|---|---|---|
| `id` (PK) | auto | auto |
| `odoo_id` | sí (cuando viene del mirror) | sí (tras crear el producto en Odoo) |
| `isbn` | sí | sí |
| `name` | sí (del mirror) | sí (de SINLI `books.title`) |
| `status` | sí (pending/scraping/...) | no toca |
| `retries` | sí | no toca |
| `scraped_data` | sí (JSON del scrape) | no toca |
| `last_attempt` | sí | no toca |
| `started_at` | sí | no toca |
| `push_attempt_at` | sí | no toca |
| `added_at` | sí | no toca |
| `motivo` | `'legacy_mirror'` por default | `'sinli_sin_ficha'` |
| `proveedor_email` | NULL | sí (email del proveedor que ofreció el libro) |
| `prioridad` | 0 (default) | 10+ para libros urgentes de SINLI |
| `encolado_en` | NOW() al insertar | NOW() al insertar |
| `procesado` | sí: TRUE cuando status='done' | lectura, marca FALSE al re-encolar |

### Reglas para encolar (sync SINLI)

Cuando el sync recibe un ISBN sin ficha en `books`:

```sql
INSERT INTO enrichment_queue (isbn, name, motivo, proveedor_email, prioridad, encolado_en)
VALUES (?, NULL, 'sinli_sin_ficha', ?, 10, NOW())
ON CONFLICT (isbn, motivo) DO UPDATE SET
    proveedor_email = EXCLUDED.proveedor_email,
    encolado_en = NOW(),
    prioridad = GREATEST(enrichment_queue.prioridad, 10);
```

### Reglas para consumir (scraper)

El scraper procesa con prioridad:

```sql
SELECT isbn, odoo_id, name
FROM enrichment_queue
WHERE procesado = FALSE
  AND (status IS NULL OR status = 'pending')
ORDER BY prioridad DESC, encolado_en ASC
FOR UPDATE SKIP LOCKED
LIMIT 50;
```

Cuando termina:
```sql
UPDATE enrichment_queue
SET status = 'done',
    procesado = TRUE,
    scraped_data = ?,
    last_attempt = NOW()
WHERE id = ?;
```

### Reglas para no pisarse

- El sync **nunca escribe** `status`, `scraped_data`, `retries`, `started_at`,
  `push_attempt_at`. Esos son del scraper.
- El scraper **nunca escribe** `proveedor_email`, `motivo`. Esos son del sync.
- Ambos pueden leer todo. Ambos pueden escribir `isbn`, `odoo_id`, `name`,
  `encolado_en`, `prioridad` (con cuidado).

## Migración (un solo ALTER, idempotente)

El scraper puede correr este SQL en su `init_db()` o en un endpoint
`/admin/run-migrations` (ya existe). Es seguro re-ejecutar.

```sql
DO $$
BEGIN
    -- 1. Añadir columnas nuevas si no existen
    ALTER TABLE enrichment_queue ADD COLUMN IF NOT EXISTS motivo TEXT DEFAULT 'legacy_mirror';
    ALTER TABLE enrichment_queue ADD COLUMN IF NOT EXISTS proveedor_email TEXT;
    ALTER TABLE enrichment_queue ADD COLUMN IF NOT EXISTS prioridad INTEGER DEFAULT 0;
    ALTER TABLE enrichment_queue ADD COLUMN IF NOT EXISTS encolado_en TIMESTAMPTZ DEFAULT NOW();
    ALTER TABLE enrichment_queue ADD COLUMN IF NOT EXISTS procesado BOOLEAN DEFAULT FALSE;
    ALTER TABLE enrichment_queue ADD COLUMN IF NOT EXISTS id SERIAL;

    -- 2. Quitar la PK antigua si aún es odoo_id, y añadir id como PK
    IF EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'enrichment_queue' AND constraint_type = 'PRIMARY KEY'
          AND constraint_name = 'enrichment_queue_pkey'
    ) THEN
        ALTER TABLE enrichment_queue DROP CONSTRAINT enrichment_queue_pkey;
        ALTER TABLE enrichment_queue ALTER COLUMN odoo_id DROP NOT NULL;
        ALTER TABLE enrichment_queue ADD PRIMARY KEY (id);
    END IF;

    -- 3. Índices
    CREATE UNIQUE INDEX IF NOT EXISTS uq_enrichment_isbn_motivo
        ON enrichment_queue (isbn, motivo) WHERE isbn IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_enrichment_odoo_id ON enrichment_queue (odoo_id);
    CREATE INDEX IF NOT EXISTS idx_enrichment_pending
        ON enrichment_queue (procesado, prioridad DESC)
        WHERE procesado = FALSE;
END $$;
```

## Próximos pasos

1. El otro Claude (sync) **confirma** que este schema híbrido le funciona.
2. El scraper aplica el ALTER (en `/admin/run-migrations`).
3. El scraper adapta sus queries para usar `id` como PK en vez de `odoo_id`.
4. El scraper adapta su consumo para respetar `ORDER BY prioridad DESC` cuando
   haya filas del sync.
5. Cuando arranque el sync, ambos sistemas escriben en la misma tabla sin
   pisarse.

## Contacto

Cualquier cambio a este schema debe acordarse previamente. Si necesitas añadir
columnas, hazlo via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (idempotente,
no rompe).