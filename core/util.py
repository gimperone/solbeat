"""Shared helpers: keyless HTTP/JSON-RPC with retry, time utils. Stdlib only."""
import json
import time
import urllib.error
import urllib.request

USER_AGENT = "solbeat/1.0 (Solana ecosystem autonomous reporter)"


def utcnow() -> float:
    return time.time()


def http_request(url: str, data: bytes | None = None, headers: dict | None = None,
                 timeout: int = 25, retries: int = 2) -> bytes:
    """GET/POST with retry and backoff. Raises last exception after retries."""
    hdrs = {"User-Agent": USER_AGENT}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def http_json(url: str, params: dict | None = None, **kw) -> dict | list:
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    return json.loads(http_request(url, **kw).decode("utf-8", "replace"))


class RpcClient:
    """Minimal Solana JSON-RPC client over a list of failover endpoints."""

    def __init__(self, endpoints: list[str]):
        self.endpoints = endpoints or ["https://api.mainnet-beta.solana.com"]
        self._idx = 0

    def call(self, method: str, params: list | None = None):
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or [],
        }).encode()
        errors = []
        for _ in range(len(self.endpoints)):
            ep = self.endpoints[self._idx % len(self.endpoints)]
            self._idx += 1
            try:
                body = http_request(ep, data=payload, timeout=25)
                out = json.loads(body.decode("utf-8", "replace"))
                if "error" in out:
                    raise RuntimeError(f"RPC error: {out['error']}")
                return out.get("result")
            except Exception as exc:  # try next endpoint
                errors.append(f"{ep}: {exc}")
        raise RuntimeError("all endpoints failed: " + " | ".join(errors))


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0):
        return None
    return round((new - old) / abs(old) * 100.0, 3)
