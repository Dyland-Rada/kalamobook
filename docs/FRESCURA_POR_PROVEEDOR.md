# La guarda de frescura pasa a ser por proveedor

28 de septiembre de 2026. Sustituye al corte único de 3 días del 15/09, que a su
vez había sustituido al de 7.

| proveedor | días sin fichero antes de dejar de venderse |
|---|---|
| **PODIPRINT** (`sinli@podiprint.com`) | **7** |
| **DISTRIFORMA** (`fandite@distriforma.es`) | **7** |
| **el resto** | **5** |

El mismo número marca las dos cosas: cuándo el libro deja de ofertarse y cuándo el
proveedor se marca **CAÍDO** en los partes. Antes iban por separado —el stock se
apagaba a los 3 días y el rojo salía al cuádruple de la cadencia propia— y eso
dejaba el aviso llegando después del apagado.

## Por qué Podiprint y Distriforma tienen más margen

**Podiprint es impresión bajo demanda.** Su stock no se agota porque no hay
existencias que agotar: se imprime cuando se vende. Un dato suyo de hace una semana
sigue siendo cierto, así que aplicarle el mismo corte que a un distribuidor con
almacén es castigar un catálogo de 100.000 títulos por una regla pensada para otro
caso.

Distriforma comparte el umbral por decisión de operaciones.

## Dónde se aplica

**1. El `CASE` de los dos feeds** —`CdL - FEED nuevo` (`weNR6gDvZNpZjg0Q`) y
`Fnac - FEED nuevo` (`DJYbT4YurjeNrCCx`)—, que es lo que de verdad apaga la oferta:

```sql
WHEN c.confirmado_en < now() - (CASE
       WHEN c.proveedor IN ('sinli@podiprint.com','fandite@distriforma.es')
            THEN INTERVAL '7 days'
       ELSE INTERVAL '5 days'
     END)                                   THEN 0
```

Sigue siendo una guarda del `CASE`, no del `WHERE`: produce **cantidad 0**, no
excluye la fila. Si excluyera la fila la oferta no se podría apagar nunca.

**2. El estado CAÍDO** en `Parte de proveedores (cada 2h)` (`h51iSCU8r569WOGe`) y
`Estado de catalogos de proveedor (diario)` (`mrAfg8IcR3Gjy1eR`).

**RETRASO no cambia**: sigue siendo el doble de la cadencia propia de cada
proveedor. Sin eso no quedaría ningún aviso temprano, porque el rojo ya no llega
hasta los 5 o 7 días. La escalera queda ámbar pronto, rojo el día acordado.

**La alerta de proveedor mudo** (`RRwGtbRBqj7sS6Ta`) tampoco cambia. Salta al doble
de la cadencia propia y es lo único que avisa a tiempo: con 5 días no habría
detectado ni a Distriforma ni a Logista.

## Lo que sostiene el 5 para el resto

Medido sobre `cegald_isbns_v2` en operación normal, el **peor hueco entre ficheros
de cualquier proveedor son 38 horas** (Disbook), seguido de Akal con 36,2 y Punxes
con 36,0. Cinco días son 120 horas: más de tres veces el peor caso observado.

El corte anterior de 3 días ya daba 2,5× de margen y en once días de vigencia no
apagó a nadie por error. El de 5 es más holgado todavía, así que el riesgo de
apagar un proveedor sano es menor que antes.

**El coste va en la otra dirección**, y conviene tenerlo escrito: un proveedor que
se queda mudo ahora sigue publicándose **dos días más** que con la regla anterior, y
cuatro más en el caso de Podiprint y Distriforma. Con Distriforma eso son 35.494
libros vendiéndose con datos de hasta una semana, y Distriforma sí tiene almacén y
sí se le agotan las existencias. Es la parte de este cambio que hay que vigilar.

## Recordatorio del límite de la medición

`cegald_isbns_v2` **solo guarda unos diez días**, así que cualquier medición de
cadencia tiene ese recorrido y no más. La propuesta sigue en pie: una tabla
`proveedor_carga` con una fila por carga —unas 40 al día, 15.000 al año— que no se
pode, para poder decidir estos umbrales sobre meses en vez de sobre días.
