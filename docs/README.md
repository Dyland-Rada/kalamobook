# Documentación de Kalamo

Sistema que recoge el stock de 14 distribuidores de libros, mantiene el
catálogo de Odoo al día y publica en Shopify los libros listos para vender.

Última revisión: 31 de julio de 2026.

---

## Por dónde empezar

**Si vas a usar el sistema** → [Guía operativa](GUIA_OPERATIVA.md)
Sin tecnicismos: qué hace, cada pestaña del panel, las tareas del día a día y
qué hacer cuando algo va mal.

**Si vas a tocar el código** → [Manual técnico](MANUAL_TECNICO.md)
Arquitectura, módulos, despliegue y —lo más valioso— el apartado de **trampas
conocidas**, con lo que costó horas descubrir.

---

## Los documentos

| Documento | Qué contiene |
|---|---|
| [Guía operativa](GUIA_OPERATIVA.md) | Uso diario, tareas frecuentes, diagnóstico sin tecnicismos |
| [Manual técnico](MANUAL_TECNICO.md) | Arquitectura, módulos, entorno, despliegue, trampas |
| [Flujos](FLUJOS.md) | Cada proceso paso a paso, con sus salvaguardas |
| [Modelo de datos](MODELO_DATOS.md) | Las 41 tablas y sus 478 columnas |
| [Referencia de la API](REFERENCIA_API.md) | Los 144 endpoints |
| [Coordinación con el scraper](COORDINACION_SCRAPER_VS_SYNC.md) | Reparto de escrituras entre procesos |
| [Fetcher de AZETA](AZETA_FETCHER.md) | Detalle del caso AZETA |
| [Cola de enriquecimiento](COORDINACION_ENRICHMENT_QUEUE.md) | Cómo se reparte el trabajo entre workers |
| [Especificación de Shopify](superpowers/specs/2026-07-31-shopify-publicacion-design.md) | Diseño de la publicación automática |

---

## El sistema en cinco líneas

1. Los distribuidores mandan su stock por SINLI, por ficheros o por web
2. Server A lo recibe y lo guarda en `libros_proveedor`
3. Esta app lo sincroniza a Odoo, **cada proveedor a su propio almacén**
4. Enriquece las fichas con Casa del Libro, Google Books y los catálogos
5. Publica en Shopify los libros que están completos

---

## Las cifras, a 31 de julio de 2026

| | |
|---|---|
| Proveedores | 14 |
| Libros en Odoo | 1.259.523 |
| Con stock disponible | 552.343 |
| Productos en Shopify | 746.925 |
| Pendientes de publicar | 26.502 |
| Endpoints | 144 |
| Tablas | 41 |

---

## Reglas que conviene saber de memoria

**Cada proveedor tiene su almacén.** ICA01, AZE01, POD01… Nunca se mezclan.
Si uno deja de servir, solo se apaga lo suyo.

**Un CEGALD es una foto de lo disponible.** Lo que no viene, no lo tiene.

**Precio:** el del proveedor más un suplemento si es bajo. Por debajo de
2,90 € no se publica.

**Peso:** el del libro; si no se sabe, 350 gramos.

**`actualizado_en` solo se mueve cuando el stock cambia.** De aquí salen la
mitad de las sorpresas del sistema: un libro que entra con stock y nunca varía
se queda fuera del radar del sync para siempre. Por eso existen *Empujar* y
*Conciliar*.

**Nada se publica en Shopify sin que alguien le dé al botón.**

---

## Estado del desarrollo

**Terminado y en producción**

- Sincronización de stock de los 14 proveedores
- Motor de precios API-15
- Gestión de proveedores: alta, pausa, reactivación, conciliación, reparación
- Ciclo diario de libros nuevos con informe por correo
- Integración completa con Shopify: auditoría, generación con IA y publicación

**Pendiente**

- Generar y publicar los 26.502 libros que faltan (unas 8 horas de IA)
- Aclarar qué hace la app *Odoo Integration*, que también escribe en la tienda
- Automatizar el goteo diario de publicación (hoy es manual a propósito)
- Conciliación semanal automática
- Enriquecer los 13.966 libros españoles del rango 979-13

**Deudas conocidas**

- 6.124 libros sin título y 5.994 sin precio: no se pueden publicar
- LOGISTA lleva días sin mandar fichero de stock
- UDL está dado de alta y nunca ha enviado nada
