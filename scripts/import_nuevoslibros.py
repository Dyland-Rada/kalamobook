"""
Helper local para subir los 4 XLSX de nuevoslibros/ al endpoint del server.

Uso (desde la raiz del repo):
    python scripts/import_nuevoslibros.py \
        --base-url https://tu-dominio-B \
        --user KalamoAdminstgre \
        --password tu-password

Sube los archivos uno por uno en serie (el server solo permite 1 import
a la vez). Muestra el progreso de cada uno hasta que termine.
"""
import argparse
import os
import sys
import time

import requests
from requests.auth import HTTPBasicAuth


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True,
                        help="ej: https://kalamob.reinventaconia.com")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--folder", default="nuevoslibros",
                        help="carpeta con los xlsx (default: nuevoslibros)")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    auth = HTTPBasicAuth(args.user, args.password)
    base = args.base_url.rstrip("/")

    if not os.path.isdir(args.folder):
        print(f"[ERROR] No existe la carpeta: {args.folder}", file=sys.stderr)
        sys.exit(1)

    files = sorted(
        f for f in os.listdir(args.folder)
        if f.endswith(".xlsx") and not f.startswith("~")
    )
    if not files:
        print(f"[WARN] Sin XLSX en {args.folder}", file=sys.stderr)
        sys.exit(0)

    print(f"Voy a subir {len(files)} archivos a {base}")
    for fname in files:
        path = os.path.join(args.folder, fname)
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"\n→ Subiendo {fname} ({size_mb:.1f} MB)...")

        with open(path, "rb") as fp:
            r = requests.post(
                f"{base}/api/v1/distributors/import",
                params={"batch_size": args.batch_size},
                files={"file": (fname, fp, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                auth=auth,
                timeout=600,
            )
        if r.status_code != 200:
            print(f"  [ERROR] {r.status_code}: {r.text[:300]}")
            sys.exit(1)
        print(f"  OK: {r.json()}")

        # Poll hasta que termine
        while True:
            time.sleep(5)
            s = requests.get(f"{base}/api/v1/distributors/status", auth=auth, timeout=30)
            data = s.json()
            status = data.get("status")
            processed = data.get("processed", 0)
            inserted = data.get("inserted", 0)
            print(f"  status={status} processed={processed} inserted={inserted}", end="\r")
            if status in ("completed", "error", "idle"):
                print()
                if status == "error":
                    print(f"  ERRORES: {data.get('errors')}", file=sys.stderr)
                break

    print("\nTodos los archivos procesados.")
    stats = requests.get(f"{base}/api/v1/distributors/stats", auth=auth, timeout=30).json()
    print(f"\nResumen final: {stats}")


if __name__ == "__main__":
    main()
