# `penguin@kalamo.local`: qué es, y por qué excluirlo no arreglaría el problema

Respuesta a la investigación del 14 de septiembre de 2026 sobre el pedido L000111.
Verificado en producción antes de contestar.

---

## 1. Lo primero: la investigación está bien hecha

La cronología es correcta, el alcance está bien medido y el caso L000111 está bien
reconstruido. Lo confirmamos dato por dato:

| | |
|---|---|
| `penguin@kalamo.local` declara 1 el 08/09 12:21, a 12,95 € | ✅ |
| `sinli@penguinrandomhouse.com` declara 0 desde el 04/09 18:11 | ✅ |
| El libro se vendió con el «1» del segundo origen | ✅ |
| 23.604 libros con stock de ese origen | ✅ |
| El 100 % de sus positivos es exactamente «1» | ✅ |

Y la conclusión de fondo es la correcta: **este es un caso (b)**, un libro que no
tenía stock real y aun así se vendió. Hay que arreglarlo de raíz.

Donde discrepamos es en **qué es la raíz**.

## 2. Qué es `penguin@kalamo.local`

**No es ficticio, y no es un placeholder.** Es el fichero de disponibilidad de
Penguin Random House, que llega **por correo** —lo manda Javier Vela en un Excel—
en lugar de por SINLI. Lo creamos nosotros el 3 de septiembre al montar su ingesta.

El «1» no es un valor inventado ni un defecto hardcodeado. Es una **traducción**:
el Excel de Penguin no da cantidades, da palabras.

| lo que dice el Excel | lo que guardamos |
|---|---|
| `Con stock` | 1 |
| `Poco stock` | 1 |
| `Sin stock` | 0 |
| `Proxima novedad` | se descarta |

El dominio `.local` es una dirección sintética: ese canal no tiene buzón SINLI, y
`libros_proveedor` necesita un email como clave. **Fue una decisión nuestra y está
mal elegida** — invita exactamente a la lectura que hizo la investigación. Un
nombre como `penguin.excel@kalamo.local`, o simplemente documentarlo, habría
ahorrado el susto. Eso lo asumimos.

## 3. Por qué «solo declara 1» no prueba que sea falso

La investigación razona: «un proveedor real tiene cantidades variadas, que el 100 %
sea 1 apunta a stock inventado». **Ese razonamiento no se sostiene con los datos.**

Proveedores que declaran **exclusivamente** «1» (medido el 14/09 sobre
`libros_proveedor`):

| proveedor | libros con stock | valores positivos distintos |
|---|---|---|
| LOGISTA | **61.441** | 1 |
| ICARO | 45.152 | 1 |
| DISTRIFORMA | 36.469 | 1 |
| MACHADO | 24.178 | 1 |
| **PENGUIN (`.local`)** | **23.604** | 1 |
| DISTRIFER | 19.220 | 1 |
| ANAYA | 18.307 | 1 |
| DISBOOK | 7.888 | 1 |

Solo tres de los once mandan cantidades de verdad: AZETA (55 valores distintos,
máximo 628), el grupo `sinli@*` (99 valores, máximo 99) y ARCOBALENO (111 valores,
máximo 1.582).

**Penguin no es la excepción: es el quinto de ocho.** Son **236.259 libros** con la
misma huella, y LOGISTA tiene casi el triple que Penguin.

El «1» no significa «tengo una unidad». Significa **«disponible»**. Es un semáforo,
no un inventario, y así funciona la mayoría del catálogo.

## 4. Consecuencia: excluir `%.local` arregla 68 libros de 212.655

Si se excluyen los proveedores `%.local` del cálculo de stock:

- se apagan los **68** libros que dependen solo de Penguin `.local`,
- se pierden **23.604** libros de disponibilidad real de Penguin,
- y quedan **212.655** libros de otros siete proveedores con exactamente el mismo
  riesgo, intacto.

El filtro por dominio confunde *cómo se llama el origen* con *cuánto se puede fiar
uno de su dato*. No es lo mismo.

## 5. Cuál es la raíz, entonces

Son tres cosas, y la tercera es la que disparó este pedido.

### 5.1 Penguin está dado de alta dos veces — ya avisado el 8/09

`penguin@kalamo.local` (Excel, 26.868 libros, almacén PEN01) y
`sinli@penguinrandomhouse.com` (SINLI, 27.053 libros, **sin almacén asignado**)
comparten **26.764 ISBN**: es el mismo editor por dos vías, y en 319 títulos los dos
registros se contradicen.

El L000111 es uno de esos 319. Y el desenlace estaba escrito: **el origen SINLI no
tiene almacén en Odoo, así que su «0» no llega a ninguna parte.** Odoo solo ve el
«1» de PEN01. El catálogo publica lo que ve Odoo.

Esto se documentó el 8 de septiembre con la advertencia de decidir cuál es la fuente
buena antes de tocar nada. Sigue sin decidirse. **Es la causa estructural.**

### 5.2 El «1» rancio, que es el problema general

Ya está medido y documentado: **111.976 libros** (22,4 % del catálogo) se apoyan en
una declaración que no se mueve desde hace más de dos meses, y **todos** son
exactamente un ejemplar.

La regla que sí cubre esto —y cubre también los 68 de Penguin— es:

> No ofertar en marketplace un libro cuyo stock sea exactamente 1 **y** cuya línea
> de proveedor lleve 7 días o más sin cambiar, salvo AZETA.

Techo del recorte: ~1.800 ofertas, un 0,8 % de las vivas. Frente a las 23.604 que se
perderían filtrando por dominio.

### 5.3 La causa inmediata: la ingesta lleva parada desde el 8 de septiembre

**Ningún proveedor salvo AZETA ha entrado desde el 8 de septiembre.** Medido el
14/09:

| proveedor | último fichero | días |
|---|---|---|
| AZETA | 14/09 16:29 | 0 |
| todos los demás | 07–08/09 | 6 |

Los workflows de ingesta (`Reloj - Sync stock a Odoo` y `Stock manual (Logista +
Machado + Penguin)`) están **desactivados** desde el apagado masivo del 8, el que se
hizo para vaciar los catálogos de marketplace. Vaciar los feeds era intencionado;
parar la entrada de stock casi seguro que no.

**Por eso los 61 fantasmas siguen vivos:** nada ha refrescado el dato en seis días.
Si la ingesta hubiera seguido, el Excel siguiente de Penguin habría actualizado ese
libro, y la regla de frescura de 7 días lo habría apagado igualmente mañana.

Esto también explica lo que la investigación observa como «solo 3 fechas de
actualización, no es una integración viva». Es una integración viva **que está
apagada**. Cosa distinta, y más fácil de arreglar.

## 6. Qué hacer

**Ahora, y estamos de acuerdo:** apagar los 61 libros vivos que se apoyan solo en
Penguin `.local` con el real en 0. Es reversible y evita 61 sobreventas. Adelante.

**Hoy, y es lo más urgente de todo:** reactivar la ingesta de stock. Sin eso,
mañana la regla de frescura apaga **202.565 libros** de golpe al cumplirse los 7
días — no por un fallo, sino porque el sistema deja de fiarse, correctamente, de un
dato que nadie ha renovado en una semana.

**Esta semana:** decidir cuál de los dos Penguin es la fuente buena. Si es el
SINLI, hay que darle almacén en Odoo y retirar el Excel. Si es el Excel, hay que
retirar el registro SINLI. Mantener los dos es lo que produce contradicciones como
la del L000111.

**Y la regla general:** el corte del «1 rancio» del apartado 5.2, que es lo que
convierte esto de una lista de excepciones en una defensa.

## 7. Lo que NO recomendamos

**Excluir `%.local`.** Apaga 23.604 libros de disponibilidad real de Penguin,
arregla 68, y deja 212.655 con el mismo riesgo. El criterio útil no es el dominio
del email: es **si el dato se ha renovado y si la cantidad es un semáforo o un
inventario**.

Si aun así se quiere una defensa por origen mientras se decide lo de Penguin, la
correcta es más estrecha: **cuando dos orígenes del mismo editor se contradicen,
que gane el «0»**. Un proveedor que dice «no lo tengo» es más fiable que uno que
dice «disponible» con un semáforo. Eso cubre los 319 títulos en conflicto sin tocar
los otros 26.445.
