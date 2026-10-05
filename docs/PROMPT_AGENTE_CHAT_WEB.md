# El agente del chat web divaga: por que, y que cambia

5 de octubre de 2026. Workflow **"Agente kalamo - Chat Web"** (`l19zeojn1Ams2FH0`),
nodo **KalamoBotAI Web**, bloque `TONO Y ESTILO` del `systemMessage`.

## El limite que habia no limitaba nada

El prompt ya decia:

> `- Respuestas cortas: maximo 4-5 lineas salvo que se pida un detalle.`

Y el agente lo cumplia. Medido en vivo el 05/10 sobre seis preguntas reales, **todas las
respuestas ocupaban UNA linea** — porque salen en un unico parrafo de texto plano, que es
justo lo que el propio prompt exige dos lineas mas abajo ("Texto plano, no uses markdown").

Un parrafo de 75 palabras es una linea. Cumple el tope y divaga igual. El limite estaba
escrito en la unidad equivocada.

## Lo que se midio

| pregunta | palabras | que fallaba |
|---|---|---|
| cuanto tarda un envio a madrid | **65** | mete PostLibri, Paquete Economico, que no tiene el plazo, una pregunta Y el email |
| quiero devolver un libro | **75** | vuelca la seccion 4 entera: plazo, email, direccion postal de Burriana y quien paga |
| teneis el quijote en arabe | 43 | correcto |
| a que hora abris | 25 | correcto |
| hola | 11 | correcto |
| puedo pagar con paypal | 35 | "Si necesitas mas ayuda escribenos a..." de relleno |

Media: 42 palabras. El problema no es el promedio, son los dos casos de arriba — y son
precisamente los dos temas que mas se preguntan.

El email de Kalamo aparecia en **cinco de las seis** respuestas, incluida una donde no
pintaba nada. Se habia convertido en firma.

## Los cuatro huecos reales

El prompt no tenia ninguna regla que impidiera:

1. **Apilar temas.** Nada decia "contesta lo que te preguntan y para".
2. **Firmar con el email.** Nada distinguia "no tengo el dato" de "por si acaso, ahi lo
   tienes".
3. **Dar dos salidas a la vez.** El caso del envio termina con una pregunta *y* con el
   email: el cliente no sabe cual de las dos seguir.
4. **Preambular.** Nada exigia que la primera frase llevara ya el dato.

Lo de "una sola pregunta por mensaje" si estaba, y se respeta.

## El cambio

Se sustituye la linea del tope por una medida en palabras y se anaden las cuatro reglas que
faltaban. El bloque nuevo esta en `docs/prompt_chat_web_NUEVO.txt`, que es el
`systemMessage` completo listo para pegar en el nodo.

No se toca nada mas del prompt: ni las reglas duras, ni la taxonomia de intenciones, ni el
formato JSON de salida, ni el escalado.

## Por que no va en el documento de reglas

`REGLAS_ASISTENTE.md` y su original de Drive son la **base de conocimiento**: lo que el
agente sabe, y lo mantiene el equipo de Kalamo. El estilo de respuesta es del **prompt**.
Si se escriben las normas de formato en los dos sitios acaban contradiciendose, y gana el
que nadie recuerde haber editado.

## Pendiente

Aplicarlo. La API publica de n8n lo acepta por
`PUT /api/v1/workflows/l19zeojn1Ams2FH0`, pero **ojo**: el esquema de esa API rechaza
`availableInMCP` y `binaryMode` dentro de `settings`, asi que un PUT los pierde. El workflow
usa Google Drive + Extract from File, que es ruta binaria. Mas seguro pegar el prompt a mano
en el editor.

Copia de seguridad del workflow entero, antes de tocarlo, en el scratchpad de la sesion
(`wfchat_BACKUP_05oct.json`).
