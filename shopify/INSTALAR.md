# Burbuja de chat en Shopify (tema Warehouse)

El agente ya responde en producción:
`POST https://kalamobooks-n8n-fad1ef-213-165-85-117.sslip.io/webhook/kalamo-web-chat`
con `{ "session_id": "...", "message": "...", "user_name": "..." }`
y devuelve `{ "respuesta": "...", "session_id": "..." }`.

Falta solo pegar dos archivos en el tema. El conector de Shopify que uso no permite
escribir en el tema **publicado** (bloquea `themeFilesUpsert` sobre el tema live), así
que estos dos pasos van a mano. Son 2 minutos.

## Paso 1 — crear la sección

Shopify admin → **Tienda online → Temas → … → Editar código** → carpeta `sections`
→ **Agregar una nueva sección** → nombre `kalamo-chat` (Shopify creará
`sections/kalamo-chat.liquid`) → borrar todo lo que venga por defecto y pegar el
contenido íntegro de `shopify/kalamo-chat.liquid` de este repo → **Guardar**.

## Paso 2 — registrar la sección

En el mismo editor de código, abrir `sections/overlay-group.json` y sustituir todo su
contenido por el de `shopify/overlay-group.json` de este repo → **Guardar**.

El único cambio real es añadir `kalamo_chat` a `sections` y a `order`. Ese grupo ya lo
renderiza `layout/theme.liquid` en todas las páginas, así que no hay que tocar
`theme.liquid`.

## Resultado

- Burbuja azul abajo a la derecha en toda la web (no en el checkout; ahí Shopify no
  carga el tema).
- La conversación y el `session_id` se guardan en `localStorage`, así que el hilo
  sobrevive al navegar entre páginas.
- Todo es configurable sin código en **Personalizar → Overlay group → Chat asistente**:
  activar/desactivar, color, títulos, mensaje de bienvenida, email de soporte y la URL
  del webhook.

## Para quitarlo

Desmarcar "Mostrar la burbuja de chat" en Personalizar. Para quitarlo del todo, dejar
`sections/overlay-group.json` como estaba (sin `kalamo_chat`).
