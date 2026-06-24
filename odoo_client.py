"""
Async Odoo JSON-RPC client.

Odoo exposes /jsonrpc which accepts {service, method, args} payloads.
We use this instead of XML-RPC because aiohttp is already a dependency
and JSON is easier to debug.

Auth flow:
- authenticate() → returns uid (cached on the client instance)
- execute_kw() → generic call to any model/method (search, read, write, etc.)
"""
import asyncio
import os
from typing import Any

import aiohttp


class OdooError(Exception):
    """Raised when Odoo returns an error in the JSON-RPC response."""


class OdooClient:
    """Lightweight async Odoo JSON-RPC client."""

    def __init__(self, url: str | None = None, db: str | None = None,
                 login: str | None = None, api_key: str | None = None,
                 timeout: int = 60):
        self.url = (url or os.environ.get("ODOO_URL", "")).rstrip("/")
        self.db = db or os.environ.get("ODOO_DB", "")
        self.login = login or os.environ.get("ODOO_LOGIN", "")
        self.api_key = api_key or os.environ.get("ODOO_API_KEY", "")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.uid: int | None = None
        self._session: aiohttp.ClientSession | None = None

        if not all([self.url, self.db, self.login, self.api_key]):
            missing = [k for k, v in {
                "ODOO_URL": self.url, "ODOO_DB": self.db,
                "ODOO_LOGIN": self.login, "ODOO_API_KEY": self.api_key,
            }.items() if not v]
            raise OdooError(f"Missing Odoo env vars: {', '.join(missing)}")

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=self.timeout)
        await self.authenticate()
        return self

    async def __aexit__(self, *_):
        if self._session:
            await self._session.close()
            self._session = None

    async def _rpc(self, service: str, method: str, args: list) -> Any:
        """
        Low-level JSON-RPC call con retry automatico para 429/502/503/504.
        Backoff exponencial: 1s, 2s, 4s. Total 3 reintentos.
        """
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
        }
        retry_status = {429, 502, 503, 504}
        last_err: Exception | None = None
        for attempt in range(4):  # 1 intento + 3 retries
            try:
                async with self._session.post(f"{self.url}/jsonrpc",
                                              json=payload) as resp:
                    if resp.status in retry_status and attempt < 3:
                        wait = 2 ** attempt  # 1, 2, 4
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    body = await resp.json()
                if "error" in body:
                    err = body["error"]
                    msg = err.get("data", {}).get("message") or err.get("message", "Unknown")
                    raise OdooError(f"Odoo: {msg}")
                return body.get("result")
            except aiohttp.ClientResponseError as e:
                last_err = e
                if e.status in retry_status and attempt < 3:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # Errores de red transientes: reintenta con backoff
                last_err = e
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        # Si por algún motivo agotamos retries sin raise
        if last_err:
            raise last_err
        raise OdooError("RPC: agotados los retries sin error capturado")

    async def authenticate(self) -> int:
        """Login and cache the user id."""
        self.uid = await self._rpc("common", "authenticate",
                                   [self.db, self.login, self.api_key, {}])
        if not self.uid:
            raise OdooError("Authentication failed: invalid credentials")
        return self.uid

    async def execute_kw(self, model: str, method: str,
                         args: list, kwargs: dict | None = None) -> Any:
        """
        Generic call wrapper.
        Equivalent to Odoo's models.execute_kw(db, uid, password, model, method, args, kwargs).
        """
        if self.uid is None:
            await self.authenticate()
        return await self._rpc(
            "object", "execute_kw",
            [self.db, self.uid, self.api_key, model, method, args, kwargs or {}]
        )

    # ── Convenience methods ────────────────────────────────────────────
    async def search_count(self, model: str, domain: list) -> int:
        return await self.execute_kw(model, "search_count", [domain])

    async def search_read(self, model: str, domain: list,
                          fields: list[str], offset: int = 0,
                          limit: int = 0, order: str = "") -> list[dict]:
        kwargs = {"fields": fields}
        if offset:
            kwargs["offset"] = offset
        if limit:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return await self.execute_kw(model, "search_read", [domain], kwargs)

    async def write(self, model: str, ids: list[int], values: dict) -> bool:
        return await self.execute_kw(model, "write", [ids, values])

    async def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict]:
        return await self.execute_kw(model, "read", [ids], {"fields": fields})


# ── Module-level smoke test (run: python odoo_client.py) ──────────────
async def _smoke_test():
    async with OdooClient() as o:
        total = await o.search_count("product.template", [])
        with_isbn = await o.search_count("product.template", [["barcode", "!=", False]])
        sample = await o.search_read(
            "product.template",
            [["barcode", "!=", False]],
            ["id", "name", "barcode", "description_sale"],
            limit=2,
        )
        print(f"UID: {o.uid}")
        print(f"Total productos: {total}")
        print(f"Con barcode:     {with_isbn}")
        print(f"Muestra:         {sample}")


if __name__ == "__main__":
    asyncio.run(_smoke_test())
