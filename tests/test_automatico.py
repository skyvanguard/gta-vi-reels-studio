"""Reels automaticos con las noticias del dia."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import ZONA_PRUEBA
from tests.test_almacen_servicio_acceso import _proyecto
from tests.test_bot_trabajador import entorno  # noqa: F401 - fixture
from tests.test_planificador_llm import LlmFalso
from vstudio import automatico, noticias
from vstudio.llm import LlmError
from vstudio.modelos import Estado

ZONA = ZoneInfo(ZONA_PRUEBA)
AHORA = datetime(2026, 11, 12, 5, 0, tzinfo=ZONA)

RSS_BING = """<?xml version="1.0" encoding="utf-8" ?><rss version="2.0" xmlns:News="https://www.bing.com/news">
<channel><title>Bing</title>
<item><title>GTA VI: preventas superan las expectativas</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;url=https%3a%2f%2fwww.infobae.com%2fgta-preventas%3futm_source%3dbing%26id%3d7&amp;c=1</link>
<description>&lt;b&gt;Take-Two&lt;/b&gt; dijo que GTA VI vendió mucho</description>
<pubDate>Thu, 12 Nov 2026 06:00:00 GMT</pubDate><News:Source>Infobae</News:Source></item>
<item><title>Vieja: GTA 6 tráiler</title><link>https://x.com/vieja</link>
<pubDate>Sun, 01 Nov 2026 06:00:00 GMT</pubDate></item>
<item><title>Otro juego sin nada que ver</title><link>https://x.com/otro</link>
<pubDate>Thu, 12 Nov 2026 06:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>VGC</title>
<entry><title>GTA 6 gets a Japanese rating</title><link rel="alternate" href="https://vgc.com/gta6-japon"/>
<published>2026-11-12T04:30:00Z</published><summary type="html">&lt;p&gt;Rating news&lt;/p&gt;</summary></entry>
<entry><title>GTA VI: PREVENTAS superan las expectativas!</title><link href="https://otro.com/copia"/>
<updated>2026-11-12T05:00:00Z</updated></entry>
</feed>"""


def _nota(n: int, titulo: str = "", horas: float = 1.0) -> noticias.Candidata:
    return noticias.Candidata(titulo=titulo or f"Noticia {n} de GTA VI", url=f"https://medio.com/{n}",
                              medio=f"Medio {n}", fecha=AHORA - timedelta(hours=horas), resumen=f"resumen {n}")


# ---------- noticias ----------
def test_lee_rss_de_bing_con_el_link_real_y_el_medio():
    notas = noticias.leer_feed(RSS_BING.encode(), "Bing Noticias")
    assert len(notas) == 3
    infobae = notas[0]
    assert infobae.url == "https://www.infobae.com/gta-preventas?id=7"  # sin apiclick ni utm_
    assert infobae.medio == "Infobae" and infobae.resumen == "Take-Two dijo que GTA VI vendió mucho"
    assert infobae.fecha == datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)


def test_lee_atom():
    notas = noticias.leer_feed(ATOM.encode(), "VGC")
    assert [n.url for n in notas] == ["https://vgc.com/gta6-japon", "https://otro.com/copia"]
    assert notas[0].resumen == "Rating news" and notas[0].medio == "VGC"


def test_filtra_por_gta_fecha_y_repetidas():
    notas = noticias.leer_feed(RSS_BING.encode(), "Bing") + noticias.leer_feed(ATOM.encode(), "VGC")
    quedan = noticias.filtrar(notas, datetime(2026, 11, 12, 7, 0, tzinfo=timezone.utc), 30)
    assert [n.titulo for n in quedan] == ["GTA VI: preventas superan las expectativas", "GTA 6 gets a Japanese rating"]
    # la vieja (11 dias), la que no es de GTA y la misma con otro link y mayusculas quedan afuera


def test_xml_malicioso_o_roto_no_rompe():
    xxe = b"""<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<rss><channel><item><title>GTA 6 &x;</title><link>https://a.com/1</link>
<pubDate>Thu, 12 Nov 2026 06:00:00 GMT</pubDate></item></channel></rss>"""
    notas = noticias.leer_feed(xxe, "x")
    assert all("root:" not in n.titulo for n in notas)
    assert noticias.leer_feed(b"no es xml", "x") == []


def test_buscar_sigue_si_una_fuente_falla():
    def descargar(url, maximo, tipos):
        if "rota" in url:
            raise OSError("caida")
        return SimpleNamespace(contenido=ATOM.encode())
    notas = noticias.buscar(datetime(2026, 11, 12, 7, 0, tzinfo=timezone.utc), 30,
                            fuentes=(("Rota", "https://rota.com"), ("VGC", "https://vgc.com/feed")),
                            descargar=descargar)
    assert len(notas) == 2


# ---------- eleccion ----------
def test_elige_agrupa_y_descarta_lo_invalido():
    candidatas = [_nota(n) for n in range(1, 6)]
    falso = LlmFalso([{"elegidas": [
        {"fuente": 2, "otras": [4, 99], "titulo": "Preventas récord", "por_que": "dato nuevo"},
        {"fuente": 4, "otras": [], "titulo": "Repite la 4 (ya usada)"},
        {"fuente": 9, "titulo": "no existe"},
        {"fuente": 1, "otras": ["x"], "titulo": ""},
        {"fuente": 5, "titulo": "Censura en Japón"},
    ]}])
    elegidas = automatico.elegir(falso, candidatas, ["Ya publicado: la fecha"], date(2026, 11, 12), 8, ZONA)
    assert [e.titulo for e in elegidas] == ["Preventas récord", "Censura en Japón"]
    assert [n.url for n in elegidas[0].notas] == ["https://medio.com/2", "https://medio.com/4"]
    assert "Ya publicado: la fecha" in falso.partes[0] and "1. [Medio 1" in falso.partes[0]


def test_elige_respeta_el_maximo_y_sin_respuesta_avisa():
    candidatas = [_nota(n) for n in range(1, 6)]
    falso = LlmFalso([{"elegidas": [{"fuente": n, "titulo": f"t{n}"} for n in range(1, 6)]}])
    assert len(automatico.elegir(falso, candidatas, [], date(2026, 11, 12), 3, ZONA)) == 3
    with pytest.raises(LlmError):
        automatico.elegir(LlmFalso([{"otra": 1}]), candidatas, [], date(2026, 11, 12), 3, ZONA)
    assert automatico.elegir(LlmFalso([]), [], [], date(2026, 11, 12), 3, ZONA) == []  # sin notas no pregunta


# ---------- el automatico ----------
def _automatico(servicio, acceso, notas):
    return automatico.Automatico(servicio, acceso, buscar=lambda ahora, horas: notas)


def test_toca_una_vez_por_dia_a_la_hora(entorno):  # noqa: F811
    _, servicio, _, acceso, _ = entorno
    a = _automatico(servicio, acceso, [])
    assert not a.toca(AHORA.replace(hour=4, minute=59))
    assert a.toca(AHORA)
    a._guardar({"ultima": "2026-11-12"})
    assert not a.toca(AHORA.replace(hour=9))  # hoy ya corrio
    assert a.toca(AHORA + timedelta(days=1))
    a.activar(False)
    assert not a.toca(AHORA + timedelta(days=1))  # /auto off


def test_la_primera_vez_no_arranca_en_el_momento(entorno, monkeypatch):  # noqa: F811
    _, servicio, _, acceso, _ = entorno
    a = _automatico(servicio, acceso, [])
    a._parar.set()  # que el ciclo no espere
    a.run()
    hoy = datetime.now(ZoneInfo(servicio.cfg.zona_horaria)).date().isoformat()
    assert a.leer()["ultima"] == hoy and not a.toca(datetime.now(ZoneInfo(servicio.cfg.zona_horaria)))


def test_ejecutar_planifica_todo_y_despues_aprueba_junto(entorno, monkeypatch):  # noqa: F811
    tg, servicio, _, acceso, _ = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    notas = [_nota(n) for n in range(1, 4)]
    monkeypatch.setattr(automatico, "elegir", lambda *a, **k: [
        automatico.Elegida("Preventas récord", "dato", (notas[0], notas[1])),
        automatico.Elegida("Censura en Japón", "dato", (notas[2],))])
    pedidos, aprobados = [], []

    def crear(chat, texto, fotos, segundos=None, **k):
        pedidos.append((chat, texto.split("\n")[0], segundos))
        id_ = f"p{len(pedidos)}"
        estado = Estado.ERROR if texto.startswith("https://medio.com/1") else Estado.ESPERANDO_APROBACION
        return servicio.almacen.guardar(replace(_proyecto(chat=chat, id_=id_), estado=estado, error="403"))
    monkeypatch.setattr(servicio, "crear", crear)
    monkeypatch.setattr(servicio, "aprobar", lambda id_: aprobados.append((id_, len(pedidos))))
    a = _automatico(servicio, acceso, notas)
    a.ejecutar()
    # la nota 1 no se pudo leer: se probo con la 2, que cuenta lo mismo; despues la 3
    assert pedidos == [(7, "https://medio.com/1", 30), (7, "https://medio.com/2", 30), (7, "https://medio.com/3", 30)]
    assert aprobados == [("p2", 3), ("p3", 3)]  # se aprueban juntos, despues de planificar todo
    assert [p["titulo"] for p in a.leer()["publicadas"]] == ["Preventas récord", "Censura en Japón"]
    textos = [t for _, _, t in tg.enviados]
    assert any("elegí 2 de 3 notas" in t for t in textos) and any("2 reeles en cola" in t for t in textos)
    assert "Preventas récord" in a.ya_publicado(AHORA.date())  # mañana no se repite


def test_sin_noticias_nuevas_avisa_y_no_planifica(entorno, monkeypatch):  # noqa: F811
    tg, servicio, _, acceso, _ = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    monkeypatch.setattr(automatico, "elegir", lambda *a, **k: [])
    monkeypatch.setattr(servicio, "crear", lambda *a, **k: pytest.fail("no tenia que planificar"))
    _automatico(servicio, acceso, [_nota(1)]).ejecutar()
    assert "no hay noticias nuevas" in tg.enviados[-1][2]


def test_bot_auto(entorno):  # noqa: F811
    tg, servicio, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    bot.automatico = _automatico(servicio, acceso, [])
    bot._manejar({"message": {"chat": {"id": 70, "type": "private"}, "from": {"id": 7}, "text": "/auto off"}})
    assert not bot.automatico.activo and "apagados" in tg.enviados[-1][2]
    bot._manejar({"message": {"chat": {"id": 70, "type": "private"}, "from": {"id": 7}, "text": "/auto on"}})
    assert bot.automatico.activo and "05:00" in tg.enviados[-1][2]
