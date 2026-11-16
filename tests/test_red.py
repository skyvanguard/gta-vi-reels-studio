import httpx
import pytest

from tests.conftest import resolver_falso
from vstudio import red

PUBLICA = "93.184.216.34"
RESOLVER = resolver_falso({
    "noticias.com": PUBLICA, "local.test": "127.0.0.1", "lan.test": "192.168.1.50", "privada.test": "10.0.0.5",
    "metadata.test": "169.254.169.254", "v6local.test": "::1", "mapeada.test": "::ffff:127.0.0.1",
    "cero.test": "0.0.0.0", "nat64.test": "64:ff9b::7f00:1",
})


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://noticias.com/a", "http://usuario:clave@noticias.com/", "http://noticias.com:22/",
    "http://local.test/", "http://lan.test:8080/v1/chat", "http://privada.test/", "http://metadata.test/latest",
    "http://v6local.test/", "http://mapeada.test/", "http://cero.test/", "http://noexiste.test/", "http:///sinhost",
    "http://nat64.test/", "https://noticias.com:8443/", "http://noticias.com:0/",
])
def test_rechaza_urls_peligrosas(url):
    with pytest.raises(red.UrlRechazada):
        red.validar_url(url, RESOLVER)


def test_acepta_url_publica():
    assert red.validar_url("https://noticias.com/nota?x=1", RESOLVER) == "https://noticias.com/nota?x=1"


def _cliente(manejador):
    return httpx.Client(transport=httpx.MockTransport(manejador), follow_redirects=False)


def test_redireccion_a_red_interna_se_bloquea():
    def manejador(req):
        return httpx.Response(302, headers={"location": "http://local.test/admin"})
    with pytest.raises(red.UrlRechazada, match="interna"):
        red.descargar("https://noticias.com/a", 1000, ("text/html",), RESOLVER, _cliente(manejador))


def test_sigue_redireccion_publica_y_devuelve_contenido():
    def manejador(req):
        if req.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=b"<html>ok</html>")
    r = red.descargar("https://noticias.com/a", 1000, ("text/html",), RESOLVER, _cliente(manejador))
    assert r.url_final == "https://noticias.com/b" and r.contenido == b"<html>ok</html>"


def test_corta_por_tamano_y_por_tipo():
    grande = _cliente(lambda req: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"x" * 5000))
    with pytest.raises(red.UrlRechazada, match="supera|grande"):
        red.descargar("https://noticias.com/i.jpg", 1000, ("image/",), RESOLVER, grande)
    html = _cliente(lambda req: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html/>"))
    with pytest.raises(red.UrlRechazada, match="tipo"):
        red.descargar("https://noticias.com/i.jpg", 1000, ("image/",), RESOLVER, html)


def test_demasiadas_redirecciones():
    bucle = _cliente(lambda req: httpx.Response(302, headers={"location": "https://noticias.com/otra"}))
    with pytest.raises(red.UrlRechazada, match="redirecciones"):
        red.descargar("https://noticias.com/a", 1000, ("text/html",), RESOLVER, bucle)


class _StreamFalso:
    def __init__(self, ip):
        self.ip = ip

    def get_extra_info(self, clave):
        return (self.ip, 80) if clave == "server_addr" else None


def test_rebinding_se_detecta_por_la_ip_real():
    def manejador(req):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html/>",
                              extensions={"network_stream": _StreamFalso("127.0.0.1")})
    with pytest.raises(red.UrlRechazada, match="rebinding"):
        red.descargar("https://noticias.com/a", 1000, ("text/html",), RESOLVER, _cliente(manejador))
