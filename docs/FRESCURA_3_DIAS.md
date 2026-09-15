# La guarda de frescura baja de 7 a 3 días

15 de septiembre de 2026.

En el `CASE` de los dos feeds —`CdL - FEED nuevo` (`weNR6gDvZNpZjg0Q`) y
`Fnac - FEED nuevo` (`DJYbT4YurjeNrCCx`)— la línea que apaga los datos rancios pasa
de 7 a 3 días:

```sql
-- antes
WHEN c.confirmado_en < now() - interval '7 days' THEN 0
-- ahora
WHEN c.confirmado_en < now() - interval '3 days' THEN 0
```

Sigue siendo una guarda del `CASE`, no del `WHERE`: produce **cantidad 0**, no
excluye la fila. Si excluyera la fila, la oferta no se podría apagar nunca.

---

## Corrección de la versión anterior de este documento

La primera redacción decía que la medición cubría **del 15 de julio al 8 de
septiembre**, «cinco semanas y media de operación normal». **Eso era falso.**

`cegald_isbns_v2` **solo guarda desde el 5 de septiembre de 2026**:

```
global_mas_antiguo  2026-09-05 01:01:19
global_mas_nuevo    2026-09-15 17:32:06
filas_totales       4.488.540
```

La consulta pedía un rango que empezaba en julio, pero no había nada que leer antes
del día 5. El rango real era **del 5 al 8 de septiembre: tres días**, más lo poco
recuperado desde el 14. La ventana de observación efectiva es de unos **cinco días
útiles**, no de cinco semanas y media.

También decía que PODIPRINT «no tiene cadencia medible, manda muy de tarde en
tarde». **También falso**, y por el mismo motivo: el corte superior de la consulta
(`AND '2026-09-08 12:00'`) dejaba fuera su carga de las 17:32 de ese día, con lo que
quedaba un solo timestamp y ningún hueco que medir. PODIPRINT manda **a diario**,
siempre sobre las 17:31:

```
15/09 17:32 | 14/09 17:31 | 08/09 17:32 | 07/09 17:32
```

Los huecos del 08 al 14 son el parón de la ingesta, no suyos.

---

## Lo que sí sostiene el corte de 3 días

Con la ventana real, el peor hueco observado entre ficheros de un mismo proveedor:

| proveedor | mediana | peor hueco | veces > 3 d |
|---|---|---|---|
| DISBOOK BCN | 6,0 h | 38,0 h | 0 |
| AKAL | 11,9 h | 36,2 h | 0 |
| LES PUNXES | 6,0 h | 36,0 h | 0 |
| ÍCARO | 4,5 h | 24,1 h | 0 |
| ANAYA | 24,0 h | 24,0 h | 0 |
| MACHADO | 24,0 h | 24,0 h | 0 |
| ARCOBALENO | 24,0 h | 24,0 h | 0 |
| PENGUIN (SINLI) | 24,0 h | 24,0 h | 0 |
| PODIPRINT | 24,0 h | 24,0 h | 0 |
| ALFA OMEGA | 10,0 h | 14,0 h | 0 |
| LOGISTA | 6,0 h | 14,0 h | 0 |
| DISTRIFORMA | 6,1 h | 12,0 h | 0 |
| DISTRIFER | 6,0 h | 11,0 h | 0 |

**Peor hueco de cualquier proveedor: 38 horas.** El corte de 3 días son 72, así que
queda un margen de 1,9× sobre el peor caso observado. Ninguno lo ha rozado.

**Pero el margen está medido sobre cinco días de datos, no sobre cinco semanas.**
Siete de los quince proveedores mandan una vez al día a hora fija; a ésos, 72 horas
les dan margen para saltarse **dos días seguidos** antes de apagarse. Es un margen
razonable, no holgado: un puente o una incidencia de dos días en el proveedor apaga
su catálogo. Con 7 días el margen eran seis días saltados.

La contrapartida es la buscada: un proveedor que se queda mudo deja de ofertarse
cuatro días antes que con la regla anterior.

## Qué apaga hoy, en concreto

Nada nuevo. A 15/09, sobre 500.900 libros vendibles:

| corte | libros que apaga | de ellos vivos en CdL |
|---|---|---|
| 7 días | 4.728 | 0 |
| 4 días | 4.728 | 0 |
| **3 días** | **4.728** | **0** |

Los tres cortes apagan exactamente el mismo conjunto porque la distribución quedó
bimodal tras el parón: 496.303 libros con menos de dos días y 4.728 clavados en
siete. No hay nada en medio.

Y esos 4.728 **son casi exactamente los 4.723 de PENGUIN (Excel)**, que llevaba sin
mandar fichero desde el 8 de septiembre. Es decir: bajar el corte y desactivar
Penguin son la misma acción sobre los mismos libros.

## Recomendación pendiente: persistir el histórico de cargas

Que `cegald_isbns_v2` solo guarde diez días tiene dos consecuencias:

1. No se puede medir la cadencia real de un proveedor con perspectiva, que es justo
   lo que hace falta para elegir un corte con criterio.
2. El informe al cliente con «las últimas 6 llegadas» se queda corto para los
   proveedores diarios en cuanto pasan unos días.

Propuesta: una tabla `proveedor_carga` de una fila por (proveedor, fichero) —
proveedor, instante, nº de ISBN— que no se pode. Son unas 40 filas al día; en un año
no llega a 15.000. Con eso la cadencia se mide sobre meses en vez de sobre días.

---

## Dónde se aplica

El corte vive **solo en el SQL de los dos feeds de n8n**. No hay ninguna guarda de
frescura en el repositorio: `catalogo_publicable` calcula `confirmado_en` y no juzga
si es fresco. Por eso el cambio no necesita despliegue.
