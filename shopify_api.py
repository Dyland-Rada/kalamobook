"""
Cliente de la API de Shopify.

La app es del Dev Dashboard nuevo, asi que no hay token fijo: se pide uno con
client_credentials y caduca a las 24 h. Aqui se cachea en memoria y se renueva
solo cuando le quedan menos de 5 minutos.

Para leer el catalogo entero (746.925 productos) NO se pagina: se usa una
operacion masiva de GraphQL, que Shopify resuelve en su lado y devuelve como
un JSONL descargable. Paginar serian ~3.000 peticiones y media hora.

Limite que condiciona todo lo demas: por encima de 50.000 variantes, Shopify
solo admite 1.000 variantes nuevas al dia.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

TIENDA = os.environ.get("SHOPIFY_TIENDA", "kalamobooks.myshopify.com")
CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
TIMEOUT_S = int(os.environ.get("SHOPIFY_TIMEOUT_S", "120"))

# Tope diario de altas. Shopify corta en 1.000 variantes nuevas al dia cuando
# la tienda pasa de 50.000; se deja margen por debajo.
TOPE_DIARIO = int(os.environ.get("SHOPIFY_TOPE_DIARIO", "900"))

_token: str | None = None
_token_expira: float = 0


class ShopifyError(RuntimeError):
    pass


def token(forzar: bool = False) -> str:
    """Token de la app. Se renueva solo; caduca a las 24 h."""
    global _token, _token_expira
    if _token and not forzar and time.time() < _token_expira - 300:
        return _token
    if not (CLIENT_ID and CLIENT_SECRET):
        raise ShopifyError("Faltan SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET")
    datos = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        f"https://{TIENDA}/admin/oauth/access_token", data=datos,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        cuerpo = e.read()[:200].decode("utf-8", "replace")
        if "app_not_installed" in cuerpo:
            raise ShopifyError("La app no esta instalada en la tienda")
        raise ShopifyError(f"HTTP {e.code} pidiendo el token: {cuerpo}")
    _token = d["access_token"]
    _token_expira = time.time() + int(d.get("expires_in", 86400))
    if not d.get("scope"):
        print("[Shopify] AVISO: el token no trae permisos. Hay que publicar "
              "una version con los alcances y REINSTALAR la app.")
    return _token


def _peticion(url: str, datos: bytes | None = None,
              cabeceras: dict | None = None, reintentos: int = 4):
    """GET/POST con reintento ante 429 (limite de peticiones) y 5xx."""
    espera = 1.0
    for intento in range(reintentos):
        req = urllib.request.Request(url, data=datos, headers={
            "X-Shopify-Access-Token": token(),
            "Content-Type": "application/json", **(cabeceras or {})})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            cuerpo = e.read()[:400].decode("utf-8", "replace")
            if e.code == 401:
                token(forzar=True)
            elif e.code in (429, 500, 502, 503, 504):
                time.sleep(espera)
                espera *= 2
            else:
                raise ShopifyError(f"HTTP {e.code}: {cuerpo}")
        except Exception:
            time.sleep(espera)
            espera *= 2
    raise ShopifyError(f"sin respuesta tras {reintentos} intentos: {url}")


def rest(ruta: str, datos: dict | None = None):
    url = f"https://{TIENDA}/admin/api/{VERSION}/{ruta}"
    return _peticion(url, json.dumps(datos).encode() if datos else None)


def graphql(consulta: str, variables: dict | None = None) -> dict:
    url = f"https://{TIENDA}/admin/api/{VERSION}/graphql.json"
    cuerpo = {"query": consulta}
    if variables:
        cuerpo["variables"] = variables
    r = _peticion(url, json.dumps(cuerpo).encode())
    if r.get("errors"):
        raise ShopifyError(f"GraphQL: {json.dumps(r['errors'])[:300]}")
    return r["data"]


# ─── Lecturas ────────────────────────────────────────────────────────
def info_tienda() -> dict:
    s = rest("shop.json")["shop"]
    return {"nombre": s.get("name"), "dominio": s.get("myshopify_domain"),
            "moneda": s.get("currency"), "plan": s.get("plan_name")}


def contar_productos() -> int:
    return int(rest("products/count.json")["count"])


def canales() -> list[dict]:
    return [{"id": p["id"], "nombre": p["name"]}
            for p in rest("publications.json").get("publications", [])]


_BULK_HANDLES = """
mutation {
  bulkOperationRunQuery(
    query: \"\"\"
    { products { edges { node { id handle status createdAt } } } }
    \"\"\"
  ) { bulkOperation { id status } userErrors { field message } }
}
"""


def exportar_handles(espera_s: int = 10, maximo_min: int = 40) -> list[dict]:
    """
    Descarga TODOS los productos (id, handle, estado) con una operacion
    masiva. Shopify la resuelve en su lado y deja un JSONL; paginar por REST
    serian ~3.000 peticiones.
    """
    d = graphql(_BULK_HANDLES)
    errores = d["bulkOperationRunQuery"]["userErrors"]
    if errores:
        raise ShopifyError(f"no arranco la operacion masiva: {errores}")
    limite = time.time() + maximo_min * 60
    url = None
    while time.time() < limite:
        time.sleep(espera_s)
        est = graphql("{ currentBulkOperation { status objectCount url errorCode } }")
        op = est["currentBulkOperation"] or {}
        if op.get("status") == "COMPLETED":
            url = op.get("url")
            break
        if op.get("status") in ("FAILED", "CANCELED"):
            raise ShopifyError(f"operacion masiva {op.get('status')}: {op.get('errorCode')}")
        print(f"[Shopify] exportando... {op.get('objectCount') or 0} objetos",
              flush=True)
    if not url:
        raise ShopifyError("la operacion masiva no termino a tiempo")
    salida = []
    with urllib.request.urlopen(url, timeout=600) as r:
        for linea in r:
            linea = linea.strip()
            if linea:
                salida.append(json.loads(linea))
    return salida


# ─── Escritura ───────────────────────────────────────────────────────
def crear_producto(fila: dict, canal_ids: list[str] | None = None) -> dict:
    """
    Crea un producto a partir de una fila Matrixify (las 23 columnas).
    Devuelve {id, handle}. NO toca stock: el inventario va por su camino.
    """
    tags = [t.strip() for t in str(fila.get("Tags") or "").split(",") if t.strip()]
    entrada = {
        "title": fila["Title"],
        "handle": fila["Handle"],
        "descriptionHtml": fila.get("Body HTML") or "",
        "vendor": fila.get("Vendor") or "",
        "productType": fila.get("Type") or "Libro",
        "tags": tags,
        "status": "ACTIVE" if str(fila.get("Status", "active")).lower() == "active"
                  else "DRAFT",
        "seo": {"title": fila.get("SEO Title") or "",
                "description": fila.get("SEO Description") or ""},
    }
    if canal_ids:
        entrada["productPublications"] = [{"publicationId": c} for c in canal_ids]
    d = graphql("""
        mutation crear($input: ProductInput!) {
          productCreate(input: $input) {
            product { id handle }
            userErrors { field message }
          }
        }""", {"input": entrada})
    res = d["productCreate"]
    if res["userErrors"]:
        raise ShopifyError(f"{fila['Handle']}: {res['userErrors']}")
    return {"id": res["product"]["id"], "handle": res["product"]["handle"]}


if __name__ == "__main__":
    import sys
    accion = sys.argv[1] if len(sys.argv) > 1 else "info"
    if accion == "info":
        print("tienda   :", info_tienda())
        print("productos:", f"{contar_productos():,}")
        print("canales  :", canales())
    elif accion == "handles":
        h = exportar_handles()
        print(f"{len(h):,} productos exportados")
        print("muestra:", h[:3])
