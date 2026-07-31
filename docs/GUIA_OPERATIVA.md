# Guía operativa — Kalamo

Para quien opera el sistema día a día. No hace falta saber programar.

Última revisión: 31 de julio de 2026.

---

## 1. Qué hace esto, en una frase

Recoge el stock y los precios que mandan los distribuidores, mantiene el
catálogo de Odoo al día, y publica en la tienda de Shopify los libros que
están listos para venderse.

## 2. El circuito completo

```
  DISTRIBUIDORES              SERVER A            NOSOTROS (Server B)         ODOO            SHOPIFY
  (14 proveedores)
        |                        |                        |                    |                 |
   mandan ficheros  ──────>  los lee y      ──────>   sincroniza      ──────>  stock      ──>  producto
   CEGALD por SINLI          guarda en BD             a Odoo cada 1h          por almacén      publicado
        |                        |                        |                    |                 |
   AZETA: CSV por web      libros_proveedor        enriquece con CDL      almacén por        ficha con IA
   Logista/Machado:                                 y Google Books        proveedor
   ficheros a mano                                       |
                                                   crea los nuevos
                                                   y les hace ficha
```

**Lo importante:** cada proveedor tiene **su propio almacén** en Odoo. El
stock de ÍCARO va a ICA01, el de AZETA a AZE01, el de PODIPRINT a POD01.
Nunca se mezclan. Así, si un proveedor deja de servir, solo se apaga lo suyo.

## 3. Los proveedores

| Proveedor | Almacén | Cómo manda el stock |
|---|---|---|
| AZETA | AZE01 | CSV por web, cada hora |
| LOGISTA/PLANETA | LOG01 | Ficheros subidos a mano (Drive) |
| ÍCARO | ICA01 | CEGALD por SINLI |
| DISTRIFORMA | DIS03 | CEGALD por SINLI |
| LES PUNXES | LES01 | CEGALD por SINLI |
| MACHADO | MAC01 | Ficheros a mano |
| PENGUIN | PEN01 | Ficheros a mano |
| Grupo Anaya | GRU01 | CEGALD por SINLI |
| DISTRIFER | DIS02 | CEGALD por SINLI |
| DISBOOK | DIS01 | CEGALD por SINLI |
| ALFA OMEGA | ALF01 | CEGALD por SINLI |
| Ediciones Akal | EDI01 | CEGALD por SINLI |
| PODIPRINT | POD01 | CEGALD por SINLI (impresión bajo demanda) |
| UDL | UDL01 | Aún no manda nada |

**Un CEGALD es una foto de lo disponible.** El proveedor solo lista lo que
tiene. Lo que no aparece, no lo tiene.

## 4. Qué pasa solo, sin que nadie toque nada

| Cuándo | Qué hace |
|---|---|
| Cada hora | Descarga el stock de AZETA y lo sube a su almacén |
| Cada hora | Sube a Odoo el stock de los demás proveedores que haya cambiado |
| Una vez al día | Busca libros nuevos con stock, los busca en Casa del Libro, los crea en Odoo y manda un correo con el resumen |

**Nada se publica solo en Shopify.** Eso es siempre manual.

## 5. El panel, pestaña por pestaña

Se entra en `https://kalamob.reinventaconia.com` con usuario y contraseña.

### Auditoría

La pantalla de control. Lo primero que hay que mirar cada mañana.

**Stock por proveedor** — cuántos libros tiene encendidos cada uno en Odoo.
Si un proveedor baja de golpe, algo pasó.

**Proveedores — pausar y reactivar** — la tabla más importante:

| Columna | Qué significa |
|---|---|
| Catálogo | Todo lo que vende ese proveedor |
| Reportados | Lo que manda en sus ficheros de stock |
| Con stock | De esos, los disponibles ahora |
| En Odoo | Cuántos existen como producto |
| Encendidos | Cuántos tienen stock de verdad en la tienda |
| Último CEGALD | Cuándo llegó su última foto de stock y cuántos libros traía |
| Último fichero | El último envío suyo de cualquier tipo |

Si **Último CEGALD** lleva días en ámbar, ese proveedor ha dejado de mandar.

**Libros nuevos — ciclo diario** — cuántos se detectaron, cuántos se
encontraron en Casa del Libro y cuántos no. Los que no aparecen van al correo
y a la pestaña de relleno manual.

### Shopify

Tres pasos, en orden y todos manuales:

1. **Auditar** — lee la tienda y comprueba qué está publicado ya. Hay que
   hacerlo antes de generar; si no, se pagaría IA por fichas de libros que ya
   están. Solo lee, no toca nada.
2. **Generar fichas** — la IA escribe la ficha de los libros pendientes.
   Se guardan sin publicar, se pueden revisar. Se puede parar y seguir.
3. **Subir** — dos opciones:
   - *Publicar por API*: primero avisa de cuántos y cuáles va a subir, y solo
     después de confirmar toca la tienda. Máximo 900 al día.
   - *Generar XLSX*: crea el fichero para subirlo con Matrixify. Sin tope.

### Relleno manual

Los libros que se crearon sin ficha porque no se encontraron en ninguna
fuente. Se rellenan a mano y quedan publicables.

### Catálogo Odoo

Buscador para consultar cualquier libro y ver qué datos tenemos.

### Odoo Sync

Herramientas de mantenimiento. Uso puntual, no diario.

## 6. Tareas frecuentes

### Un proveedor cierra por vacaciones

Auditoría → *Proveedores* → **Pausar** en su fila.

Primero dice cuántos libros va a poner a 0. Al confirmar, apaga su stock y
deja de sincronizarlo. **Los demás proveedores no se tocan**: si un libro lo
sirven dos, sigue disponible por el otro.

### Vuelve de vacaciones

**Reactivar** en su fila. Además de quitar la pausa, marca sus libros para
que el stock vuelva a subir en la siguiente pasada.

> No basta con esperar a su fichero: el sistema solo mira los libros cuya
> cantidad *cambia*, y si vuelve con el mismo stock que tenía, no se enteraría.
> Por eso reactivar re-empuja.

### Un proveedor nuevo

Auditoría → *Proveedores* → **Dar de alta**: email SINLI, nombre y código de
almacén (tres letras y dos números, por ejemplo `POD01`). Crea el almacén en
Odoo y lo deja mapeado.

Después, **Empujar** en su fila para que suba el stock que ya tuviera.

### Publicar libros nuevos en la tienda

Pestaña *Shopify*: **Auditar** → **Generar fichas** → **Publicar** o
**Generar XLSX**. Sin prisa: se puede generar hoy y publicar mañana.

### Un proveedor tiene menos stock del que debería

Auditoría → *Proveedores* → **Conciliar stock**. Compara libro a libro lo que
dice la base de datos con lo que hay en Odoo y sube lo que falte.

## 7. Las reglas del negocio

**Precio.** El que manda el proveedor, más un suplemento si es bajo:

| PVP del proveedor | Se vende a |
|---|---|
| Menos de 2,90 € | No se publica |
| De 2,90 a 4,99 € | +2,00 € |
| De 5,00 a 6,00 € | +1,50 € |
| De 6,01 a 7,50 € | +1,00 € |
| Más de 7,50 € | Sin suplemento |

**Peso.** El del libro; si no se sabe, **350 gramos**.

**Novedad.** Los editados en 2025 o después llevan la etiqueta de novedades.

**Idioma.** Los que no están en castellano van a la categoría de otros idiomas.

**Para publicar en Shopify** un libro necesita: ISBN de verdad, título propio,
portada, descripción con cuerpo, precio y stock. Si le falta algo, no sale.

## 8. Cuando algo va mal

| Lo que ves | Qué significa | Qué hacer |
|---|---|---|
| Un proveedor con "Último CEGALD" de hace días | Ha dejado de mandar ficheros | Avisar a Server A o al proveedor |
| "Encendidos" muy por debajo de "Con stock" | Stock sin subir | *Conciliar stock* en su fila |
| El sync marca error nada más empezar | Dos procesos a la vez | Normal, se resuelve solo en 30 min |
| Muchos libros nuevos sin ficha | Casa del Libro no los tiene | Normal en libros extranjeros; van al relleno manual |
| El correo diario no llega | Falló el aviso a Server A | Mirar la tarjeta del ciclo diario, dice el motivo |
| Un libro con stock no aparece en la web | Puede estar apagado por precio bajo | Buscarlo en *Catálogo Odoo* y mirar su precio |

**Regla de oro:** ninguna acción del panel borra datos. Lo peor que puede
pasar es poner un stock a 0, y eso se recupera volviendo a empujar.

## 9. Lo que conviene mirar cada día

1. **Auditoría → Stock por proveedor.** ¿Alguno se ha desplomado?
2. **Proveedores → Último CEGALD.** ¿Alguno lleva más de 3 días sin mandar?
3. **Ciclo diario.** ¿Corrió anoche? ¿Cuántos libros nuevos?
4. **El correo del resumen.** ¿Llegó?

Con eso está cubierto el 90% de lo que puede salir mal.

## 10. Lo que NO hace el sistema

- No manda pedidos a los proveedores
- No gestiona devoluciones ni facturas
- No toca precios en Odoo salvo por la regla del suplemento
- No borra productos: los apaga, nunca los elimina
- No publica en Shopify sin que alguien le dé al botón
