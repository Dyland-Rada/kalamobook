# La guarda de frescura baja de 7 a 4 días

15 de septiembre de 2026.

En el `CASE` de los dos feeds —`CdL - FEED nuevo` (`weNR6gDvZNpZjg0Q`) y
`Fnac - FEED nuevo` (`DJYbT4YurjeNrCCx`)— la línea que apaga los datos rancios pasa
de 7 a 4 días:

```sql
-- antes
WHEN c.confirmado_en < now() - interval '7 days' THEN 0
-- ahora
WHEN c.confirmado_en < now() - interval '4 days' THEN 0
```

Sigue siendo una guarda del `CASE`, no del `WHERE`: produce **cantidad 0**, no
excluye la fila. Si excluyera la fila, la oferta no se podría apagar nunca.

---

## Por qué 4 días es seguro

Un corte de frescura solo hace daño si es **más corto que la cadencia normal de un
proveedor**: entonces apagaría libros que sí existen, simplemente porque el
proveedor todavía no ha mandado su fichero de esta semana.

Así que la pregunta a responder era: **¿algún proveedor ha tardado alguna vez más de
96 horas entre fichero y fichero, en operación normal?**

Medido sobre `cegald_isbns_v2` del **15 de julio al 8 de septiembre** —se excluye a
propósito la ventana del 8 al 14, en la que la ingesta estuvo parada y todos los
huecos son artificiales— agrupando por `date_trunc('minute', registrado_en)` para no
repetir el error de agrupar por `archivo_nombre`, que colapsa las cargas de los
proveedores que siempre mandan el mismo nombre de fichero:

| proveedor | mediana | p95 | **peor hueco** | veces > 4 d |
|---|---|---|---|---|
| DISBOOK BCN | 6,0 h | 32,0 h | **38,0 h** | 0 |
| AKAL | 11,9 h | 33,7 h | **36,2 h** | 0 |
| LES PUNXES | 6,0 h | 30,0 h | **36,0 h** | 0 |
| ÍCARO | 4,5 h | 19,1 h | 24,1 h | 0 |
| ANAYA | 24,0 h | 24,0 h | 24,0 h | 0 |
| MACHADO | 24,0 h | 24,0 h | 24,0 h | 0 |
| ARCOBALENO | 24,0 h | 24,0 h | 24,0 h | 0 |
| PENGUIN (SINLI) | 24,0 h | 24,0 h | 24,0 h | 0 |
| ALFA OMEGA | 10,0 h | 14,0 h | 14,0 h | 0 |
| LOGISTA | 6,0 h | 13,2 h | 14,0 h | 0 |
| DISTRIFORMA | 6,1 h | 12,0 h | 12,0 h | 0 |
| DISTRIFER | 6,0 h | 10,7 h | 11,0 h | 0 |
| **PODIPRINT** | — | — | — | sin huecos medibles |

**El peor hueco de todo el periodo son 38 horas.** El corte de 4 días son 96. Queda
un margen de 2,5× sobre el peor caso observado, y ningún proveedor lo ha rozado
nunca en cinco semanas y media de operación normal.

## Qué apaga hoy, en concreto

Nada. Distribución de `confirmado_en` sobre los 501.026 libros vendibles:

| antigüedad | libros | vivos en CdL |
|---|---|---|
| menos de 1 día | 496.259 | 325.845 |
| 1 a 2 días | 44 | 31 |
| 2 a 7 días | **0** | 0 |
| 7 a 8 días | 4.722 | 0 |

La franja de 4 a 7 días está vacía, así que el cambio apaga **0 libros adicionales
hoy**. Eso es una casualidad del calendario, no una prueba de que dé igual: la
distribución es bimodal porque la ingesta volvió el 14 y lo que no volvió se quedó
clavado en el 8. En operación normal el corte sí morderá antes, que es justo lo que
se busca.

## La excepción: PODIPRINT

PODIPRINT no tiene cadencia medible en el periodo: **manda fichero muy de tarde en
tarde**, y son 100.000 libros con stock declarado, casi todos en el catálogo.

Es print-on-demand: su stock no se agota, se imprime. Aplicarle un corte de frescura
—de 4 días o de 7— es conceptualmente equivocado, porque el dato no caduca. Con 7
días ya estaba expuesto; con 4 lo está antes.

**Pendiente de decidir:** o se le exime de la guarda de frescura, o se le pone un
umbral propio. Mientras tanto, si PODIPRINT pasa 96 horas sin mandar, sus 98.288
libros del catálogo se apagarán de golpe en los dos marketplaces. El parte de
proveedores de cada dos horas lo hará visible antes de que ocurra.

---

## Dónde se aplica

El corte vive **solo en el SQL de los dos feeds de n8n**. No hay ninguna guarda de 7
ni de 4 días en el repositorio: `catalogo_publicable` calcula `confirmado_en` y no
juzga si es fresco. Por eso el cambio no necesita despliegue.

Documentos actualizados: `ALIMENTAR_MARKETPLACES.md`, `CATALOGO_PUBLICABLE.md`,
`CIERRE_CDL_RESPUESTA.md`.
