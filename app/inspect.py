"""Revisión puntual de una URL pública ("Pruébalo"), protegida contra SSRF.

Un visitante puede pedirle al servidor que haga una petición HTTP. Sin
protección, eso permitiría usar VIGÍA para explorar la red interna
(localhost, 10.x, 169.254.169.254 = metadatos de la nube...). Defensas:

1. Solo http/https, sin credenciales en la URL y solo puertos web.
2. El dominio se resuelve una vez y TODAS sus IPs deben ser públicas.
3. La conexión se hace a la IP ya validada (no se vuelve a resolver), así
   un DNS que cambia de respuesta (DNS rebinding) no sirve para colarse.
4. Las redirecciones se siguen a mano y cada salto se valida igual.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

ALLOWED_PORTS = {80, 443, 8080, 8443}
MAX_REDIRECTS = 3
Resolver = Callable[[str, int], Awaitable[list[str]]]


class Blocked(ValueError):
    """La URL no se revisa. El mensaje se muestra tal cual al visitante."""


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def normalize(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise Blocked("Escribe una URL")
    if len(raw) > 2048:
        raise Blocked("La URL es demasiado larga")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise Blocked("Solo se permiten URLs http o https")
    if parts.username or parts.password:
        raise Blocked("La URL no puede llevar usuario ni contraseña")
    if not parts.hostname:
        raise Blocked("A la URL le falta el dominio")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise Blocked("Puerto inválido") from None
    if port not in ALLOWED_PORTS:
        raise Blocked(f"Puerto {port} no permitido (solo 80, 443, 8080 y 8443)")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def check_ip(ip: str) -> None:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if not addr.is_global or addr.is_multicast:
        raise Blocked(f"{addr} es una dirección privada o reservada · bloqueada por protección SSRF")


async def resolve_public(host: str, port: int, resolver: Resolver) -> tuple[list[str], float]:
    t0 = time.perf_counter()
    try:
        ips = await asyncio.wait_for(resolver(host, port), 3)
    except (OSError, asyncio.TimeoutError):
        raise Blocked(f"No se pudo resolver el dominio {host}") from None
    if not ips:
        raise Blocked(f"El dominio {host} no tiene direcciones")
    for ip in ips:
        check_ip(ip)
    return ips, round((time.perf_counter() - t0) * 1000, 1)


def _pinned(parts, ip: str) -> str:
    host = f"[{ip}]" if ":" in ip else ip
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


async def _connect_probe(ip: str, port: int, host: str, https: bool, timeout: float) -> tuple[float, dict | None]:
    """Mide TCP (+ TLS) y lee el certificado. Devuelve (ms, info_tls)."""
    ctx = ssl.create_default_context() if https else None
    t0 = time.perf_counter()
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(ip, port, ssl=ctx, server_hostname=host if https else None), timeout)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    info = None
    if https:
        sslobj = writer.get_extra_info("ssl_object")
        cert = sslobj.getpeercert()
        expires = ssl.cert_time_to_seconds(cert["notAfter"])
        issuer = dict(x[0] for x in cert.get("issuer", ()))
        info = {
            "version": sslobj.version(),
            "issuer": issuer.get("organizationName") or issuer.get("commonName"),
            "expires": time.strftime("%Y-%m-%d", time.gmtime(expires)),
            "days_left": int((expires - time.time()) // 86400),
        }
    writer.close()
    try:
        await writer.wait_closed()
    except (OSError, ssl.SSLError):
        pass
    return ms, info


async def inspect(raw: str, client: httpx.AsyncClient, resolver: Resolver = system_resolve,
                  timeout: float = 6.0, measure_connect: bool = True) -> dict:
    url = normalize(raw)
    result: dict = {"url": url, "final_url": url, "ok": False, "status": None, "reason": None, "ip": None,
                    "ms": None, "timings": {}, "server": None, "content_type": None,
                    "redirects": [], "tls": None, "error": None}
    for hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        https = parts.scheme == "https"
        port = parts.port or (443 if https else 80)
        ips, dns_ms = await resolve_public(parts.hostname, port, resolver)
        ip = ips[0]
        if hop == 0:
            result["ip"] = ip
            result["timings"]["dns"] = dns_ms
            if measure_connect:
                try:
                    result["timings"]["connect"], result["tls"] = await _connect_probe(
                        ip, port, parts.hostname, https, timeout)
                except ssl.SSLCertVerificationError as e:
                    result["error"] = f"Certificado TLS inválido: {e.verify_message}"
                    return result
                except (OSError, asyncio.TimeoutError, ssl.SSLError) as e:
                    result["error"] = f"No se pudo conectar: {type(e).__name__}"
                    return result

        extensions = {"sni_hostname": parts.hostname} if https else {}
        t0 = time.perf_counter()
        try:
            async with client.stream("GET", _pinned(parts, ip), headers={"host": parts.netloc},
                                     extensions=extensions) as r:
                ms = round((time.perf_counter() - t0) * 1000, 1)
                location = r.headers.get("location")
                if r.is_redirect and location:
                    result["redirects"].append({"url": url, "status": r.status_code})
                    url = normalize(urljoin(url, location))
                    continue
                result.update(
                    final_url=url, ok=r.status_code < 400, status=r.status_code, reason=r.reason_phrase,
                    ms=ms, server=r.headers.get("server"),
                    content_type=(r.headers.get("content-type") or "").split(";")[0] or None,
                )
                result["timings"]["http"] = ms
                return result
        except httpx.HTTPError as e:
            result["error"] = f"La petición falló: {type(e).__name__}"
            return result
    result["error"] = f"Demasiadas redirecciones (más de {MAX_REDIRECTS})"
    return result


class RateLimiter:
    """Ventana deslizante en memoria: `limit` peticiones cada `per` segundos por clave."""

    def __init__(self, limit: int, per: float):
        self.limit, self.per = limit, per
        self.hits: dict[str, list[float]] = {}

    def check(self, key: str, now: float | None = None) -> float:
        """0 si se permite; si no, los segundos que faltan para poder reintentar."""
        now = time.monotonic() if now is None else now
        if len(self.hits) > 10_000:   # que un ataque con muchas IPs no llene la memoria
            self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < self.per}
        q = [t for t in self.hits.get(key, []) if now - t < self.per]
        if len(q) >= self.limit:
            self.hits[key] = q
            return self.per - (now - q[0])
        q.append(now)
        self.hits[key] = q
        return 0.0
