# 📚 Buscador de Libros — API REST

API para buscar información de libros en **La Casa del Libro España** (precios en EUR).

---

## Inicio Rápido

```bash
# 1. Instalar dependencias
pip install -r requirements.txt
playwright install chromium

# 2. (Opcional) Configurar proxy europeo si estás fuera de España
#    Esto fuerza que el sitio sirva la versión española con precios en EUR
export PROXY_URL="http://usuario:contraseña@proxy-europeo:puerto"
#    En Windows PowerShell:
#    $env:PROXY_URL = "http://usuario:contraseña@proxy-europeo:puerto"

# 3. Iniciar el servidor
python -m uvicorn app:app --port 8000

# El servidor estará disponible en http://127.0.0.1:8000
```

---

## Endpoints

### `GET /api/v1/books?q={query}`

Buscar un libro por **ISBN** o **nombre**.

| Parámetro | Tipo   | Requerido | Descripción |
|-----------|--------|-----------|-------------|
| `q`       | string | Sí        | ISBN o nombre del libro |

#### Ejemplos con `curl`

```bash
# Buscar por ISBN
curl "http://127.0.0.1:8000/api/v1/books?q=9788499899619"

# Buscar por nombre
curl "http://127.0.0.1:8000/api/v1/books?q=Dune"

# Buscar con espacios (URL-encoded)
curl "http://127.0.0.1:8000/api/v1/books?q=Cien%20a%C3%B1os%20de%20soledad"
```

#### Respuesta Exitosa (`200`)

```json
{
  "status": "success",
  "data": {
    "title": "EL TEMOR DE UN HOMBRE SABIO (SAGA CRONICA DEL ASESINO DE REYES 2)",
    "author": "Patrick Rothfuss",
    "editorial": "Debolsillo",
    "isbn": "9788499899619",
    "price": "$ 92.000 COP",
    "original_price": "",
    "discount": "",
    "description": "Llega El temor de un hombre sabio...",
    "translator": "Gemma Rovira Ortega",
    "illustrator": "",
    "language": "Castellano",
    "pages": "1200",
    "reading_time": "",
    "binding": "Tapa blanda bolsillo",
    "release_date": "03/01/2013",
    "edition_year": "",
    "edition_place": "",
    "collection": "Best Seller",
    "dimensions": {
      "height": "19.0",
      "width": "12.8",
      "weight": "775.0 gr"
    },
    "origin": "Colombia",
    "url": "https://www.casadellibro.com.co/libro-...",
    "image_url": "https://imagessl9.casadellibro.com/a/l/t1/19/9788499899619.jpg"
  }
}
```

#### Libro no encontrado (`404`)

```json
{
  "status": "error",
  "message": "No se encontraron resultados para: xyz"
}
```

#### Error interno (`500`)

```json
{
  "status": "error",
  "message": "Ocurrió un error interno: ..."
}
```

---

## Campos de Respuesta

| Campo | Descripción |
|-------|-------------|
| `title` | Título del libro |
| `author` | Autor(es) |
| `editorial` | Editorial / sello editorial |
| `isbn` | Código ISBN |
| `price` | Precio actual (formato: `XX,XX €`) |
| `original_price` | Precio original (si hay descuento) |
| `discount` | Porcentaje de descuento |
| `description` | Sinopsis del libro |
| `translator` | Traductor |
| `illustrator` | Ilustrador |
| `language` | Idioma |
| `pages` | Número de páginas |
| `reading_time` | Tiempo estimado de lectura |
| `binding` | Tipo de encuadernación |
| `release_date` | Fecha de lanzamiento |
| `edition_year` | Año de edición |
| `edition_place` | Plaza de edición |
| `collection` | Colección / serie |
| `dimensions.height` | Alto (cm) |
| `dimensions.width` | Ancho (cm) |
| `dimensions.weight` | Peso (gr) |
| `origin` | País de origen de la tienda |
| `url` | URL del libro en La Casa del Libro |
| `image_url` | URL de la portada del libro |

---

## Documentación Interactiva (Swagger)

FastAPI genera documentación interactiva automáticamente:

- **Swagger UI**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc**: [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)

---

## Interfaz Web

Además de la API, hay una interfaz web disponible en [http://127.0.0.1:8000](http://127.0.0.1:8000) para búsquedas manuales con resultado visual.

---

## Notas

- La **primera búsqueda** de un libro puede tardar ~15-30 segundos (scraping en tiempo real).
- Las búsquedas posteriores del mismo libro se sirven desde la **base de datos local** (instantáneo).
- La fuente de datos es [La Casa del Libro España](https://www.casadellibro.com).
- Los precios se muestran en **EUR** (euros).
- Si ejecutas desde fuera de Europa, configura `PROXY_URL` para evitar redirección geográfica.
