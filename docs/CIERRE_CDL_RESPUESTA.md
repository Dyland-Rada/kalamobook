# Cierre de la tienda en Casa del Libro: respuesta del backend

Respuesta al informe del 15 de septiembre de 2026.
Todo lo que sigue está medido en producción ese día, después de leer el código.

---

## Resumen

El informe acierta en tres cosas importantes y se equivoca en la que señala como
causa raíz. En corto:

| hallazgo | veredicto |
|---|---|
| 1. Tienda cerrada | **Confirmado**, y nuestro sistema no lo detectó |
| 2. «Stock pegado»: el catálogo no caduca | **No se sostiene.** El apagado por ausencia existe y funciona |
| 3. Publicamos a ciegas, no leemos de vuelta | **Correcto, y es el fallo de diseño real** |
| 4. 41 % del catálogo con datos rancios | **La cifra es reproducible, pero mide el campo equivocado** |
| 5. Fnac apaga todo stock = 1 | **Confirmado**, y hace falta una decisión |

**La causa del cierre no es un fallo de diseño del catálogo. Es que la ingesta de
proveedores estuvo parada seis días**, del 8 al 14 de septiembre, y durante ese
tiempo nada se pudo apagar. Los pedidos rechazados se acumularon justo en esa
ventana, y el cierre llegó el 14 a las 22:00, el mismo día que volvió la ingesta.

---

## 1. El apagado por ausencia sí existe, y funciona

El informe dice: «cuando un proveedor deja de incluir un ISBN, el sistema NO pone
ese stock a 0 — mantiene el último valor conocido indefinidamente».

Eso está implementado desde hace tiempo. Es `run_cegald_replacement`, en
`sync_stock_sinli.py`, y hace exactamente lo contrario:

```python
# _apagar_en_bd_cegald
UPDATE libros_proveedor
SET stock_disponible = 0, actualizado_en = NOW()
WHERE proveedor_email = ? AND stock_disponible > 0
  AND isbn = ANY(?)          # los ausentes del ultimo CEGALD
```

Y los apagados llegan a Odoo, porque el sync selecciona por `actualizado_en`, que
es justo el campo que esa consulta actualiza.

Se ve funcionando en la bitácora de hoy:

```
CEGALD sinli.disbookbcn: 7.910 presentes, 7.802 con stock en DIS01, 8 a apagar, 8 apagados
CEGALD sinli.distrifer: 23.012 presentes, 21.838 con stock en DIS02, 0 a apagar
CEGALD fandite: 35.979 presentes, 35.261 con stock en DIS03, 0 a apagar
```

### La comprobación

Sobre los **500.031 libros vendibles** del catálogo, ahora mismo:

| | libros |
|---|---|
| Que **ningún** proveedor declara con stock | **115** (0,02 %) |
| Con `confirmado_en` de más de **30 días** | **0** |
| Con `confirmado_en` de más de 7 días | 4.688 |

Y por proveedor, los que declaran stock sin aparecer en su último fichero:

| proveedor | con stock | ausentes de su fichero |
|---|---|---|
| Logista | 61.458 | **5** |
| `sinli@*` (Punxes, Akal, Alfaomega, Podiprint, PRH) | 159.245 | **2** |
| Disbook, Distrifer, Machado, Ícaro, Anaya, Distriforma, Arcobaleno | 138.129 | **0** |
| PENGUIN (Excel) | 23.604 | 23.604 — lleva 7 días mudo |
| AZETA | 256.768 | no aplica: no manda CEGALD, va por CSV |

**Salvo Penguin, que es un caso conocido, el apagado por ausencia no deja nada
pegado.**

## 2. El 41 % mide un campo que no significa lo que parece

La consulta del informe es reproducible — sale **134.469**, prácticamente su cifra.
Pero usa `libros_proveedor.actualizado_en`, y ese campo **no es un indicador de
frescura**: se mueve cuando algo *cambia*, no cuando el proveedor *menciona* el
libro.

Un libro que Distriforma lista todos los días con la misma cantidad **nunca
cambia**, así que su `actualizado_en` se queda con la fecha del último cambio real.
Puede tener meses y estar confirmado esta mañana.

Por eso la cifra sale igual de alta aunque la ingesta esté sana: no está midiendo
lo que se quiere medir.

### El campo correcto ya existe

**`catalogo_publicable.confirmado_en`**, y por debajo la tabla `cegald_isbns_v2`.

`cegald_isbns_v2` guarda una fila por (ISBN, proveedor, fichero) cada vez que un
proveedor menciona un libro. Eso **es** «última vez que el proveedor mencionó este
ISBN», que es justo lo que el informe pide en su pregunta 2. Ya está construido y
alimenta a `confirmado_en`:

```sql
GREATEST(lp.stock_actualizado_en, ce.visto)   -- ce.visto = max(registrado_en)
```

Con ese campo, el catálogo de hoy tiene **0 libros** con más de 30 días y **4.688**
con más de 7, no 134.469.

Y sí, `confirmado_en` se congela cuando el libro desaparece del fichero. **Eso es
correcto y deliberado**: congelarse es la señal. A los 4 días la guarda de frescura
lo apaga —corte bajado de 7 a 4 el 15/09/2026, ver `docs/FRESCURA_4_DIAS.md`— y
antes de eso el apagado por ausencia ya lo ha puesto a 0.

---

## 3. Lo que sí es un fallo de diseño, y tienen toda la razón

**Nunca leemos de vuelta el estado real del marketplace.** Eso es exacto y es el
problema de fondo.

`mirakl_offer_state` guarda **lo que enviamos**, no lo que Casa del Libro aplicó. Y
eso tiene una consecuencia mecánica peor de lo que parece, por el delta:

1. Enviamos cantidad 0 para un libro y apuntamos «enviado 0».
2. Nadie comprueba si se aplicó.
3. El catálogo dice 0 y nuestra tabla dice 0: **coinciden, así que el delta no lo
   vuelve a mandar nunca.**

Si ese envío no se aplicó, la oferta queda viva en Casa del Libro **y es
inalcanzable para siempre**. No hay reintento posible, porque para nosotros ya está
hecho.

### Y hay evidencia de que está pasando

`cdl_import_log` registra cada envío. La columna de resultado lleva vacía desde el
8 de septiembre:

| día | imports | líneas | **revisados** |
|---|---|---|---|
| 15/09 | 34 | 19.239 | **0** |
| 14/09 | 40 | 36.856 | **0** |
| 13/09 | 25 | 3.486 | **0** |
| 12/09 | 26 | 5.140 | **0** |
| 11/09 | 23 | 15.092 | **0** |
| 10/09 | 28 | 15.208 | **0** |
| 09/09 | 31 | 40.378 | **0** |
| 08/09 | 53 | 2.466.334 | 39 |
| 07/09 | 29 | 567.650 | 29 |

**207 envíos sin verificar.** Y las dos últimas tandas que sí se revisaron, el 7 y
el 8, dan **261.335 y 508.491 líneas con error**.

La causa es concreta: el workflow **`CdL - Revisar imports Mirakl`**
(`qbdkiSlzmqz6x6ZB`) está **inactivo**. Es de solo lectura: consulta a Mirakl el
resultado de cada import, rellena `lineas_ok`/`lineas_error`, apunta los SKU
rechazados en `cdl_sku_rechazado` y avisa si el ratio de error pasa del 5 %.

### Y la otra mitad del bucle también existe y está apagada

`cdl_barrido` y `fnac_barrido` guardan **el export real de ofertas** de cada
marketplace: lo que ellos dicen tener publicado. Entre las dos hay 681.202 filas de
un barrido que se hizo **una sola vez, el 20 de agosto**.

Esa es la tabla que habría detectado el cierre de la tienda. No hay que construir
nada: hay que encenderlo.

---

## 4. Entonces, ¿por qué se cerró la tienda?

La cadena, con fechas:

**8 de septiembre, ~12:20.** Al desactivar los 48 workflows para vaciar los
catálogos, se apagó también la ingesta de stock. No fue intencionado: vaciar los
feeds sí lo era, parar la entrada de ficheros no.

**Del 8 al 14.** Ningún proveedor salvo AZETA entra. Sin ficheros no hay apagado
por ausencia, así que **nada se puede apagar** aunque el proveedor ya no lo tenga.
El catálogo publica, correctamente, la foto del día 8.

**Durante esa ventana** entran los pedidos que luego se rechazan. Los cinco casos
testigo del informe caen todos ahí: L000095 el 12, L000103 el 13, L000116 y L000117
el 14.

**14 de septiembre, 22:00.** Casa del Libro cierra la tienda.

**14 de septiembre, ~17:00.** La ingesta vuelve. Hoy 14 de 15 proveedores están
frescos y el catálogo ha retirado cientos de libros por vuelta.

Los REFUSED no vienen de un fallo de diseño del catálogo. Vienen de **seis días
publicando una foto congelada**, más la imposibilidad de saber si los apagados que
sí mandamos llegaron a aplicarse.

---

## 5. Respuestas a las cinco preguntas

### 1. ¿Cómo caducar el stock de un ISBN que el proveedor dejó de listar?

**Ya se hace.** `run_cegald_replacement` lo pone a 0 en cuanto llega el fichero
siguiente sin ese ISBN. Lo que falló no fue la regla: fue que durante seis días no
llegó ningún fichero que disparara la comparación.

No hace falta añadir nada. Lo que hace falta es **que la ingesta no vuelva a
pararse sin que nadie se entere** — ver el apartado 6.

### 2. ¿Cuál es el campo de frescura correcto?

**`catalogo_publicable.confirmado_en`.** Es `GREATEST(último cambio, última vez que
el ISBN vino en un fichero de ese proveedor)`, y esa segunda mitad sale de
`cegald_isbns_v2`, que es exactamente el registro de menciones que piden.

No usen `libros_proveedor.actualizado_en` ni `stock_actualizado_en` para esto: el
primero mide cambios, el segundo también, y ninguno se mueve cuando el proveedor
reconfirma el mismo número.

Si necesitan consultarlo directamente:

```sql
SELECT max(registrado_en) FROM cegald_isbns_v2
WHERE isbn = ? AND proveedor_email = ?
```

**Salvedad:** AZETA no manda CEGALD, va por CSV propio cada 36 minutos. Para AZETA
el campo válido es `libros_proveedor.stock_actualizado_en`, que su push sí sella.

### 3. ¿Quién monta el proceso de leer de vuelta?

Las dos piezas ya existen y son de n8n, así que les toca a ustedes encenderlas:

- **`CdL - Revisar imports Mirakl`** (`qbdkiSlzmqz6x6ZB`), hoy inactivo. Es lo más
  urgente de todo este informe: sin él seguimos enviando a ciegas.
- **El barrido de ofertas** a `cdl_barrido` / `fnac_barrido`, semanal. Es lo que
  habría detectado el cierre de la tienda el mismo día.

Nosotros añadimos una tercera pieza que falta y sí es de backend: **cuando un
import falle, hay que borrar la fila correspondiente de `mirakl_offer_state`.** Sin
eso, el delta da el envío por bueno y no lo reintenta jamás. Podemos dejar un
endpoint para que el workflow de revisión lo llame con los SKU rechazados.

### 4. ¿El `WHEN cantidad = 1 THEN 0` de Fnac es intencional?

No lo escribimos nosotros y no está en el repositorio: vive en el SQL del feed. Lo
señalamos el 8 de septiembre como algo a confirmar y sigue sin confirmarse.

Sobre el criterio, que es lo que piden: **apagar todo lo que tenga una unidad es
demasiado grueso**. El 57 % del catálogo tiene exactamente 1, y de esas ofertas el
95 % son de AZETA, que actualiza cada 36 minutos con cantidades reales. Su «1» es
un último ejemplar honesto.

El riesgo real está en un subconjunto pequeño: **stock exactamente 1 cuya línea de
proveedor lleva 7 días o más sin cambiar**, y ahí sí conviene no ofertar en
marketplace. Son del orden de 1.800 ofertas, un 0,8 %, frente a las 130.317 que
apaga la regla actual.

Recomendación: cambiar `cantidad = 1 → 0` por `cantidad = 1 Y línea rancia → 0`.

### 5. ¿Qué limpiar antes de reabrir el 30 de septiembre?

En este orden:

1. **Encender la revisión de imports** y dejar pasar unos días. Antes de reabrir
   hay que saber qué porcentaje de nuestras líneas rechaza Mirakl. Si es alto,
   reabrir sin arreglarlo lleva al mismo sitio.
2. **Hacer un barrido completo** de las ofertas reales de CdL y compararlo con
   `catalogo_publicable`. Todo lo que esté vivo allí y a 0 aquí es una oferta que
   no pudimos apagar: hay que forzar su envío borrando su fila del espejo.
3. **Aplicar el corte del «1 rancio»** en los dos feeds.
4. **Resolver el Penguin duplicado.** Es el único proveedor mudo que queda y sus
   23.604 libros se apoyan en una foto del 8 de septiembre.
5. **Vigilar la ingesta.** Ver abajo.

---

## 6. Lo que proponemos para que no se repita

El cierre no lo causó una regla mal escrita. Lo causó que **la entrada de datos se
paró y el sistema siguió publicando con normalidad durante seis días sin que nada
avisara**.

Tres avisos, ninguno caro:

**Un proveedor sin fichero en el doble de su cadencia habitual.** Cada proveedor
tiene un ritmo conocido: AZETA 36 minutos, Distrifer 3 horas, Distriforma 6,
Podiprint diario. Si uno pasa del doble, hay que avisar. Hoy existe un watchdog
pero avisa a las 24 horas por igual para todos, y con eso Penguin llevaba una
semana caído sin que saltara nada.

**El ratio de error de los imports.** Ya lo hace el workflow de revisión cuando
está encendido, con umbral del 5 %.

**La diferencia entre lo enviado y lo publicado.** Semanal, contra el barrido real.
Un salto brusco significa que el marketplace no está aplicando lo que mandamos, o
que ha pasado algo del lado de ellos — como un cierre de tienda.

---

## 7. Lo que ya hemos cambiado por nuestro lado

Además de reactivar la ingesta el 14 por la tarde:

- **Registro de libros vendidos sin stock** (`libro_fallido`): un libro que falla
  deja de ofertarse hasta que su proveedor **cambie** la cantidad. Reafirmar no
  cuenta. Hay 90 vetos abiertos.
- **Veto del proveedor desmentido**: un proveedor sin almacén no puede servir, pero
  su «no lo tengo» ahora veta. 259 libros retirados en la primera vuelta.
- **Reserva de corta vida**: al vender se retiene la unidad hasta que el proveedor
  vuelva a mandar fichero.
- **El catálogo ya no acredita stock a un proveedor sin almacén en Odoo.**

Lo que pedían de nosotros en el punto 6 de su informe —«caducar stock viejo»— ya
estaba hecho. Lo que faltaba, y falta, es el bucle de retroalimentación del
apartado 3.
