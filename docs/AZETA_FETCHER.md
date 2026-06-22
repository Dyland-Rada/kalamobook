# AZETA — caso especial dual

Resumen para el otro Claude (sync Supabase → Odoo). AZETA es un proveedor
que NO sigue el patrón estándar de SINLI por email para stock.

## El problema

Los 11 otros proveedores mandan TODO por SINLI email:
- Precios, stock, situación, albaranes → al buzón SINLI → n8n parsea → `libros_proveedor`

AZETA divide en 2 canales:

| Tipo de dato | Canal | Cómo llega a `libros_proveedor` |
|---|---|---|
| **Precios, altas, situación, albaranes** | SINLI email (`sinli@azetadistribuciones.es`, código `LIB01192`) | n8n lo procesa igual que los demás → llena precio, situación |
| **Stock** | CSV HTTP cada hora | **NADIE lo llena hoy** → la columna `stock_disponible` para AZETA está vacía |
| **Altas exclusivas** | SINLI email 2 (`sinli2@azetadistribuciones.es`, código `L0002703`) | n8n lo procesa (es solo altas de novedades) |

Resultado: si tu sync procesa `libros_proveedor` filtrando por
`stock_disponible > 0` para AZETA, no encuentra nada porque no hay quien
populé esa columna para AZETA.

## El endpoint HTTP de AZETA

```
GET http://www.azetadistribuciones.es/servicios_web/stock.php?fr_usuario=120153&fr_clave=jalta4b
```

Devuelve CSV simple, separador `;`:
```
9788476002032;2
9788446002314;1
9788446023517;1
9788446027492;1
```

Caveats:
- **Cap a 50**: si hay más de 50 en stock, devuelve 50. No es el stock real
  cuando es alto. Si el negocio necesita el número exacto, llamar a AZETA.
- **HTTP plano** (no HTTPS). Las credenciales viajan en query string en claro.
- **Actualización cada hora** (mismo intervalo que tu cron horario, encaja).

## Solución propuesta — fetcher AZETA en tu sync

Añade al script de sync un fetcher periódico para AZETA. Opciones de
arquitectura, en orden de simplicidad:

### Opción A (recomendada) — escribir a `libros_proveedor` como cualquier proveedor

Cada hora, antes de la rutina principal del sync:

```python
def fetch_azeta_stock():
    url = "http://www.azetadistribuciones.es/servicios_web/stock.php"
    params = {"fr_usuario": "120153", "fr_clave": "jalta4b"}
    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    rows = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line or ";" not in line:
            continue
        ean, qty = line.split(";", 1)
        ean = ean.strip()
        try:
            qty = int(qty.strip())
        except ValueError:
            continue
        if not ean.isdigit() or len(ean) not in (10, 13):
            continue
        rows.append((ean, qty))
    return rows

def upsert_azeta_to_libros_proveedor(rows):
    # ON CONFLICT lo importante: marcar actualizado_en SOLO si cambió
    # (mismo patrón IS DISTINCT FROM que usa SINLI v3.1)
    sql = """
        INSERT INTO libros_proveedor
            (isbn, proveedor_email, stock_disponible, stock_actualizado_en, actualizado_en)
        VALUES (%s, 'info@azetadistribuciones.es', %s, NOW(), NOW())
        ON CONFLICT (isbn, proveedor_email) DO UPDATE
        SET stock_disponible = EXCLUDED.stock_disponible,
            stock_actualizado_en = NOW(),
            actualizado_en = CASE
                WHEN libros_proveedor.stock_disponible IS DISTINCT FROM EXCLUDED.stock_disponible
                THEN NOW()
                ELSE libros_proveedor.actualizado_en
            END
    """
    cur.executemany(sql, rows)
```

Después tu sync horario procesa los cambios como cualquier otro proveedor.
**Cero cambios en la rutina principal**. AZETA queda transparente.

Almacén destino para AZETA (ya verificado):
- `proveedor_email = info@azetadistribuciones.es`
- `warehouse_code = AZE01`
- `lot_stock_id = 14`

### Opción B — fetcher independiente que escribe directo a `stock.quant`

Saltarse `libros_proveedor`. Más simple si AZETA es realmente caso único,
pero rompe la consistencia arquitectónica (todos los demás proveedores
pasan por `libros_proveedor` antes de Odoo).

**No recomendado** salvo que descubras más proveedores con CSV-HTTP-style.

## Caveat — campos que NO trae AZETA por este canal

El CSV solo trae `EAN;cantidad`. NO trae:
- Precio (eso sí lo manda por SINLI email)
- Situación (`A`/`B`/etc.)
- Fecha de última actualización por libro (solo "actualización general cada hora")

Cuando combine con `precio_con_iva` de la tabla, ese vendrá del SINLI email
de AZETA cuando llegue (que sí manda precios). Mientras tanto el precio
puede no estar para libros sin SINLI previo.

## Resumen para el sync

1. Crea un fetcher horario AZETA (cron desfasado 5 min del sync principal:
   `5 * * * *` para no chocar exactamente con `0 * * * *`).
2. Llena `libros_proveedor` con AZETA stock.
3. El sync principal recoge esos cambios y los lleva a Odoo en AZE01.
4. Cap a 50 documentado en logs para que el negocio sepa.

## Si quieres que el scraper te lo entregue como endpoint

Si prefieres no añadir un fetcher HTTP a tu script Python, el scraper puede
exponer:

```
POST /api/v1/azeta/sync-stock
```

Que internamente baja el CSV y escribe a `libros_proveedor`. Lo programa tu
cron via `curl` o desde n8n. Avísame y lo monto en 30 min. Pero **la lógica
de stock es tuya** por contrato — yo lo expondría solo como utilidad de
descarga.