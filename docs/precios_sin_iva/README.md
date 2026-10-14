# Once proveedores nos mandan el precio sin IVA

14 de octubre de 2026.

Sandra detecta que en los marketplaces se cobra por debajo del PVP que marca
el editor. Lleva días viéndolo y anota diez ejemplos. Uno de ellos acaba en
un pedido anulado por una diferencia de 3,57 €.

La causa no es el céntimo de los marketplaces ni un error del proveedor:
**estamos leyendo como "precio con IVA" un campo que para once de los quince
proveedores viene sin él.**

## El dato que lo prueba

Nueve de los diez ejemplos de Sandra cuadran al céntimo con dividir el PVP
entre 1,04:

| ISBN | PVP del editor | ÷ 1,04 | Lo que nos manda | Paga el cliente |
|---|---|---|---|---|
| 9788492470167 | 22,00 | **21,15** | ALFAOMEGA e ICARO: 21,15 | 21,14 |
| 9788492843800 | 36,00 | **34,62** | ICARO: 34,62 | 34,10 |
| 9788490550335 | 16,00 | **15,38** | ICARO: 15,38 | 15,37 |
| 9788415906070 | 15,00 | **14,42** | DISTRIFER: 14,42 | 14,41 |
| 9788466359177 | 12,95 | **12,45** | PENGUIN: 12,45 | 12,44 |
| 9788494586835 | 17,00 | **16,35** | ALFAOMEGA e ICARO: 16,35 | 16,34 |
| 9788498414448 | 27,90 | **26,83** | LES PUNXES: 26,83 | 26,82 |
| 9788408311782 | 13,90 | **13,37** | LOGISTA: 13,37 | 13,21 |

En todos ellos **AZETA manda el precio correcto, con IVA**, y el otro
proveedor manda la base imponible.

## El alcance

Medido contra el feed de descuentos de AZETA, que es la única fuente con PVP
y tipo de IVA en columnas separadas y por tanto la única que permite saber
cuál es el precio de verdad:

| Proveedor | Comparables | Con IVA | **Sin IVA** | % |
|---|---|---|---|---|
| AZETA | 276.107 | 275.781 | 24 | 0,0 |
| MACHADO | 25.432 | 24.746 | 24 | 0,1 |
| GRUPO ANAYA | 18.935 | 18.824 | 5 | 0,0 |
| DISBOOK | 7.996 | 0 | **7.961** | 99,6 |
| PENGUIN | 27.056 | 0 | **26.887** | 99,4 |
| AKAL | 3.523 | 1 | **3.485** | 98,9 |
| LES PUNXES | 25.357 | 20 | **24.935** | 98,3 |
| DISTRIFORMA | 44.053 | 43 | **42.749** | 97,0 |
| ALFA OMEGA | 4.651 | 14 | **4.506** | 96,9 |
| DISTRIFER | 22.526 | 24 | **21.734** | 96,5 |
| ICARO | 42.267 | 70 | **39.656** | 93,8 |
| DISAL | 8.833 | 85 | **8.242** | 93,3 |
| LOGISTA/PLANETA | 40.353 | 21 | **37.298** | 92,4 |
| ARCOBALENO | 2.479 | 100 | **1.542** | 62,2 |
| PODIPRINT | 3.595 | 257 | **1.053** | 29,3 |

**220.048 libros confirmados**, y eso solo entre los que AZETA también
tiene. Contando los que cada proveedor lleva en exclusiva, el total ronda
los 295.000.

## Por qué el efecto se multiplica

El catálogo elige para cada libro **al proveedor más barato**. Un proveedor
que manda la base imponible parece siempre un 3,85 % más barato que AZETA,
así que **gana siempre**. En cuanto uno de esos once tiene el libro, el
precio que sale a los marketplaces es el suyo.

Por eso Sandra lo ve en todas las editoriales: no depende de la editorial,
depende de qué distribuidor sirva el libro.

Y por eso el céntimo parece mayor de lo que es. El céntimo se quita bien;
se quita sobre un precio que ya venía un 4 % por debajo.

## Lo que no es esto

**9788865278444** (20,50 € → 16,93 €), el del pedido anulado, es otra cosa.
ALFAOMEGA lo manda a 16,94 € y sin IVA serían 19,71 €. Ese precio está mal
en origen y hay que reclamárselo aparte.

**9788494704376** (18,00 € → 17,10 €) tampoco encaja: solo lo tiene AZETA,
con el precio correcto, y la diferencia es del 5 % exacto. Sin diagnosticar.

## Los ficheros

Dos informes, en el panel y por API:

**Ejemplos** — `GET /api/v1/informes/precios-ejemplos`

Unas filas por proveedor de cada caso, con el ISBN, lo que nos manda, el PVP
real, el IVA, el PVP sin IVA y el veredicto. Sirve para que cada proveedor lo
compruebe contra su propio fichero sin bucear en medio millón de líneas.

**Catálogo con PVPs** — `GET /api/v1/informes/precios-catalogo`

Una fila por ISBN con el PVP de **cada** proveedor en su columna, el PVP de
referencia, lo que cobramos hoy, la diferencia en euros y en porcentaje, y
el caso. Es el fichero para sumar el impacto económico y para cruzar con los
pedidos de Odoo.

## Lo que falta decidir

El arreglo parece obvio —multiplicar por 1,04 al cargar— y por eso conviene
no correr:

1. **El IVA no siempre es el 4 %.** Los libros llevan superreducido, pero el
   material que no es libro va al 21 %. Una regla global se equivocaría en
   esos.
2. **Podiprint está mezclado** (29 %). Ahí no vale una regla única.
3. **Hay que confirmarlo con cada proveedor.** Si el campo es la base
   imponible por especificación, lo corregimos al cargar. Si ellos creen que
   mandan con IVA, el error está en su fichero y hay que decírselo.

Que once proveedores cometan el mismo error por separado no es creíble. Lo
más probable es que el campo sea la base imponible por norma y que nuestra
ingesta lo etiquete mal. Los tres que salen bien son justo los que no entran
por la misma vía: AZETA por su CSV propio con columnas separadas, y Machado
por Drive.
