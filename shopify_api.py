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


def graphql(consulta: str, variables: dict | None = None,
            reintentos: int = 6) -> dict:
    """
    GraphQL con reintento ante THROTTLED.

    Ojo con esto: cuando Shopify corta por limite de coste NO responde 429,
    responde 200 con un error dentro. _peticion no lo ve -para el la peticion
    fue bien- asi que sin este bucle una corrida larga muere a mitad. Se noto
    preparando el empuje de 225.000 productos, que son ~900 llamadas seguidas.
    """
    url = f"https://{TIENDA}/admin/api/{VERSION}/graphql.json"
    cuerpo = {"query": consulta}
    if variables:
        cuerpo["variables"] = variables
    espera = 2.0
    for intento in range(reintentos):
        r = _peticion(url, json.dumps(cuerpo).encode())
        errores = r.get("errors")
        if not errores:
            return r["data"]
        texto = json.dumps(errores)
        if "THROTTLED" in texto or "Throttled" in texto:
            time.sleep(espera)
            espera = min(espera * 2, 60)
            continue
        raise ShopifyError(f"GraphQL: {texto[:300]}")
    raise ShopifyError(f"Shopify sigue limitando tras {reintentos} intentos")


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


# ─── Escritura masiva ────────────────────────────────────────────────
# Cambiar un precio es productVariantsBulkUpdate, y esa mutacion pide el
# producto: como cada libro tiene una sola variante, serian 700.000
# llamadas. La via buena es la misma que para leer, pero al reves: se sube
# un JSONL con una linea por cambio y Shopify lo aplica en su lado.
_SUBIDA = """
mutation($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}
"""

_MUTACION_MASIVA = """
mutation($mutacion: String!, $ruta: String!) {
  bulkOperationRunMutation(mutation: $mutacion, stagedUploadPath: $ruta) {
    bulkOperation { id status }
    userErrors { field message }
  }
}
"""


def _multipart(campos: list[tuple[str, str]], nombre_fichero: str,
               contenido: bytes) -> tuple[bytes, str]:
    """Arma el cuerpo multipart a mano; aqui no hay requests."""
    sep = "----kalamo" + str(int(time.time() * 1000))
    trozos = []
    for k, v in campos:
        trozos.append(
            f"--{sep}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
            f"{v}\r\n".encode())
    trozos.append(
        f"--{sep}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{nombre_fichero}\"\r\n"
        f"Content-Type: text/jsonl\r\n\r\n".encode())
    trozos.append(contenido)
    trozos.append(f"\r\n--{sep}--\r\n".encode())
    return b"".join(trozos), f"multipart/form-data; boundary={sep}"


def subir_jsonl(lineas: list[str], nombre: str = "kalamo.jsonl") -> str:
    """
    Deja un JSONL en el almacen temporal de Shopify y devuelve su ruta,
    que es lo que pide despues bulkOperationRunMutation.
    """
    d = graphql(_SUBIDA, {"input": [{
        "resource": "BULK_MUTATION_VARIABLES", "filename": nombre,
        "mimeType": "text/jsonl", "httpMethod": "POST"}]})
    res = d["stagedUploadsCreate"]
    if res["userErrors"]:
        raise ShopifyError(f"subida preparada: {res['userErrors']}")
    destino = res["stagedTargets"][0]
    campos = [(p["name"], p["value"]) for p in destino["parameters"]]
    cuerpo, tipo = _multipart(campos, nombre,
                              ("\n".join(lineas) + "\n").encode("utf-8"))
    req = urllib.request.Request(destino["url"], data=cuerpo,
                                 headers={"Content-Type": tipo})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise ShopifyError(
            f"subiendo el JSONL: HTTP {e.code} "
            f"{e.read()[:300].decode('utf-8', 'replace')}")
    ruta = dict(campos).get("key")
    if not ruta:
        raise ShopifyError("la subida no devolvio 'key'")
    return ruta


def mutacion_masiva(mutacion: str, ruta_jsonl: str, espera_s: int = 15,
                    maximo_min: int = 90) -> dict:
    """
    Lanza la mutacion sobre el JSONL subido y espera a que acabe.

    Devuelve el resumen de la operacion. Si Shopify deja un fichero de
    errores, su URL viene en 'url_errores': ahi es donde se ve linea a
    linea que no se pudo aplicar.
    """
    d = graphql(_MUTACION_MASIVA, {"mutacion": mutacion, "ruta": ruta_jsonl})
    res = d["bulkOperationRunMutation"]
    if res["userErrors"]:
        raise ShopifyError(f"no arranco la mutacion masiva: {res['userErrors']}")
    limite = time.time() + maximo_min * 60
    while time.time() < limite:
        time.sleep(espera_s)
        est = graphql("{ currentBulkOperation(type: MUTATION) "
                      "{ status objectCount errorCode url partialDataUrl } }")
        op = est.get("currentBulkOperation") or {}
        if op.get("status") == "COMPLETED":
            return {"status": "completed",
                    "objetos": op.get("objectCount"),
                    "url_errores": op.get("partialDataUrl"),
                    "url": op.get("url")}
        if op.get("status") in ("FAILED", "CANCELED"):
            raise ShopifyError(f"mutacion masiva {op.get('status')}: "
                               f"{op.get('errorCode')}")
        print(f"[Shopify] aplicando... {op.get('objectCount') or 0} objetos",
              flush=True)
    raise ShopifyError("la mutacion masiva no termino a tiempo")


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
