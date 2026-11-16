"""Descargas HTTP seguras para URLs que llegan de afuera (mensajes de Telegram y el HTML de las notas).

- Solo http/https a los puertos 80/443, sin credenciales en la URL y sin proxies del entorno.
- Bloquea destinos internos (loopback, privados, link-local, etc.) en cada salto de redireccion, y despues
  de conectar verifica la IP real del servidor: cierra el DNS rebinding (validar una IP y conectar a otra).
- Corta por tamano maximo y por plazo total de descarga.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) ReelsStudio/1.0"
PUERTOS_PERMITIDOS = {80, 443}
MAX_REDIRECCIONES = 5
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
PLAZO_TOTAL_S = 45


class UrlRechazada(ValueError):
    pass


@dataclass(frozen=True)
class Respuesta:
    url_final: str
    tipo: str
    contenido: bytes
    charset: str = ""  # el que declara el servidor en Content-Type (vacio si no declara)


def _ip_permitida(ip: str) -> bool:
    direccion = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(direccion, ipaddress.IPv6Address):
        if direccion.ipv4_mapped:
            direccion = direccion.ipv4_mapped
        elif direccion in ipaddress.ip_network("64:ff9b::/96"):  # NAT64: valida la IPv4 embebida
            direccion = ipaddress.IPv4Address(int(direccion) & 0xFFFFFFFF)
    return direccion.is_global and not direccion.is_multicast


def validar_url(url: str, resolver=socket.getaddrinfo) -> str:
    """Devuelve la URL si es segura para descargar; si no, levanta UrlRechazada."""
    partes = urlsplit(url.strip())
    if partes.scheme not in ("http", "https"):
        raise UrlRechazada(f"esquema no permitido: {partes.scheme or '(vacio)'}")
    if partes.username or partes.password:
        raise UrlRechazada("la URL no puede llevar usuario/clave")
    host = partes.hostname
    if not host:
        raise UrlRechazada("URL sin host")
    puerto = partes.port if partes.port is not None else (443 if partes.scheme == "https" else 80)
    if puerto not in PUERTOS_PERMITIDOS:
        raise UrlRechazada(f"puerto no permitido: {puerto}")
    try:
        infos = resolver(host, puerto, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UrlRechazada(f"no se pudo resolver {host}") from exc
    ips = {info[4][0] for info in infos}
    if not ips or not all(_ip_permitida(ip) for ip in ips):
        raise UrlRechazada(f"{host} apunta a una direccion interna o no publica")
    return url.strip()


def _verificar_par(resp: httpx.Response) -> None:
    """La IP a la que realmente se conecto tiene que ser publica (si el transporte la expone)."""
    stream = resp.extensions.get("network_stream")
    if stream is None:
        return  # transporte sin socket (tests con MockTransport)
    direccion = stream.get_extra_info("server_addr")
    if direccion and not _ip_permitida(str(direccion[0])):
        raise UrlRechazada("la conexion termino en una direccion interna (DNS rebinding)")


def _leer(resp: httpx.Response, max_bytes: int, limite: float) -> bytes:
    declarado = int(resp.headers.get("content-length") or 0)
    if declarado > max_bytes:
        raise UrlRechazada(f"archivo demasiado grande ({declarado} bytes)")
    partes, total = [], 0
    for trozo in resp.iter_bytes():
        total += len(trozo)
        if total > max_bytes:
            raise UrlRechazada(f"archivo supera {max_bytes} bytes")
        if time.monotonic() > limite:
            raise UrlRechazada(f"la descarga supero {PLAZO_TOTAL_S} s")
        partes.append(trozo)
    return b"".join(partes)


def descargar(url: str, max_bytes: int, tipos_permitidos: tuple[str, ...],
              resolver=socket.getaddrinfo, cliente: httpx.Client | None = None) -> Respuesta:
    """GET siguiendo redirecciones a mano (validando cada salto), con tope de tamano y de tiempo."""
    propio = cliente is None
    cliente = cliente or httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False,
                                      headers={"User-Agent": USER_AGENT})
    limite = time.monotonic() + PLAZO_TOTAL_S
    try:
        actual = url
        for _ in range(MAX_REDIRECCIONES + 1):
            validar_url(actual, resolver)
            with cliente.stream("GET", actual) as resp:
                _verificar_par(resp)
                if resp.is_redirect:
                    destino = resp.headers.get("location", "")
                    if not destino:
                        raise UrlRechazada("redireccion sin destino")
                    actual = urljoin(actual, destino)
                    continue
                resp.raise_for_status()
                tipo = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if not any(tipo.startswith(t) for t in tipos_permitidos):
                    raise UrlRechazada(f"tipo de contenido no permitido: {tipo or '(vacio)'}")
                return Respuesta(url_final=actual, tipo=tipo, contenido=_leer(resp, max_bytes, limite),
                                 charset=resp.charset_encoding or "")
        raise UrlRechazada("demasiadas redirecciones")
    finally:
        if propio:
            cliente.close()
