"""Revision de la primera corrida automatica: las tildes de Vandal (ISO-8859-1) llegaban
perdidas y las notas sin texto (MSN, el recuadro de suscripcion de GamesRadar) pasaban como leidas."""
import httpx
import pytest

from tests.conftest import imagen_bytes
from tests.test_almacen_servicio_acceso import servicio  # noqa: F401 - fixture
from vstudio import fuentes, red
from vstudio.modelos import Estado

PARRAFOS = ("<p>Microsoft y Take-Two Interactive, la compañía propietaria de Rockstar Games, han anunciado una nueva "
            "alianza estratégica que se extenderá durante los próximos diez años.</p>"
            "<p>La primera iniciativa confirmada es la llegada de GTA 6 a Xbox Cloud Gaming desde el mismo día de su "
            "lanzamiento, lo que permitirá jugarlo en Xbox One, una consola que no recibirá versión nativa.</p>"
            "<p>El servicio también estará en Xbox Series X/S con los ajustes gráficos de Series S; para jugarlo "
            "será necesario comprar el juego, porque no estará incluido en Game Pass.</p>"
            "<p>Además, Grand Theft Auto V y Red Dead Redemption 2 se sumarán a la nube en los próximos meses, según "
            "confirmó la compañía en un comunicado difundido el martes por la tarde.</p>")
HTML = f"<html><head><title>Alianza de 10 años</title></head><body><article>{PARRAFOS}</article></body></html>"
LARGO = "Texto de la nota sobre GTA VI. " * 30


# ---------- 1. la pagina en ISO-8859-1 (Vandal) ----------
def test_respeta_el_charset_que_declara_el_servidor():
    texto = fuentes.decodificar_html("Alianza de 10 años: compañía".encode("iso-8859-1"), "ISO-8859-1")
    assert texto == "Alianza de 10 años: compañía"


def test_latin1_se_lee_como_windows_1252_como_en_el_navegador():
    assert fuentes.decodificar_html(b"\x93GTA 6\x94 \x96 a\xf1os", "iso-8859-1") == "“GTA 6” – años"


def test_sin_cabecera_usa_el_meta_charset_aunque_este_lejos():
    relleno = b"<!-- " + b"x" * 5000 + b" -->"
    html = (b"<html><head>" + relleno + b'<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">'
            + "<title>años</title>".encode("iso-8859-1"))
    assert "<title>años</title>" in fuentes.decodificar_html(html)


@pytest.mark.parametrize("declarado", ["", "x-inventado"])
def test_sin_declaracion_valida_prueba_utf8_y_despues_windows_1252(declarado):
    assert fuentes.decodificar_html("compañía".encode("utf-8"), declarado) == "compañía"
    assert fuentes.decodificar_html("compañía".encode("cp1252"), declarado) == "compañía"


def test_la_cabecera_gana_al_meta_y_el_bom_a_los_dos():
    html = '<meta charset="utf-8"><p>año</p>'.encode("iso-8859-1")
    assert "año" in fuentes.decodificar_html(html, "iso-8859-1")
    assert fuentes.decodificar_html(b"\xef\xbb\xbf" + "año".encode("utf-8"), "iso-8859-1") == "año"


def test_leer_articulo_de_vandal_llega_con_tildes(monkeypatch):
    def descargar(url, *a, **k):
        return red.Respuesta(url_final=url, tipo="text/html", contenido=HTML.encode("iso-8859-1"),
                             charset="ISO-8859-1")
    monkeypatch.setattr(fuentes.red, "descargar", descargar)
    _, _, texto, _ = fuentes.leer_articulo("https://vandal.elespanol.com/noticia/1/alianza")
    assert "compañía" in texto and "próximos diez años" in texto and "�" not in texto


def test_red_guarda_el_charset_de_la_cabecera():
    transporte = httpx.MockTransport(lambda req: httpx.Response(
        200, headers={"content-type": "text/html; charset=ISO-8859-1"}, content="año".encode("iso-8859-1")))
    with httpx.Client(transport=transporte) as cliente:
        resp = red.descargar("https://93.184.216.34/nota", 1000, ("text/html",), cliente=cliente,
                             resolver=lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 443))])
    assert resp.charset.lower() == "iso-8859-1"


# ---------- 2. la pagina se baja pero no trae la nota ----------
def test_si_el_modo_estricto_saca_poco_prueba_el_amplio(monkeypatch):
    """GamesRadar: el estricto devolvia el recuadro de suscripcion (261 caracteres); el amplio, la nota."""
    def extract(html, favor_precision=False, favor_recall=False, **k):
        return "Weekly digests. Join the club" if favor_precision else "Japanese gamers are concerned. " * 40
    monkeypatch.setattr(fuentes.trafilatura, "extract", extract)
    assert fuentes.extraer_texto("<html></html>", "https://www.gamesradar.com/x").startswith("Japanese gamers")


def _nota_corta(monkeypatch, texto="Weekly digests. Join the club"):
    monkeypatch.setattr(fuentes, "leer_articulo", lambda url: ("MSN", "", texto, []))


def test_link_sin_texto_y_sin_nada_pegado_falla_con_un_motivo_claro(tmp_path, monkeypatch):
    _nota_corta(monkeypatch)
    with pytest.raises(fuentes.NotaSinTexto, match="no deja leer la nota"):
        fuentes.preparar_fuente("https://www.msn.com/es-es/x", [], tmp_path / "p")


def test_link_sin_texto_con_la_nota_pegada_usa_lo_pegado(tmp_path, monkeypatch):
    _nota_corta(monkeypatch)
    pegado = "Filtran la edición física de GTA VI: la caja no trae disco, solo un código. " * 4
    f = fuentes.preparar_fuente(f"https://www.msn.com/es-es/x {pegado}", [], tmp_path / "p")
    assert f.texto == "" and "no deja leer la nota" in f.aviso and f.nota_usuario.startswith("Filtran")


def test_con_fotos_del_editor_la_nota_no_hace_falta(tmp_path, monkeypatch):
    """Una foto con un link de X para animarla: la pagina no deja leer nada y el pedido no tiene que caerse."""
    _nota_corta(monkeypatch, "")
    f = fuentes.preparar_fuente("https://x.com/rockstar/status/1 solo video", [imagen_bytes(1350, 900)],
                                tmp_path / "p")
    assert f.imagenes == ("usuario_1.jpg",) and f.texto == "" and "tus fotos" in f.aviso


def test_una_nota_normal_pasa(tmp_path, monkeypatch):
    _nota_corta(monkeypatch, LARGO)
    assert fuentes.preparar_fuente("https://vandal.elespanol.com/x", [], tmp_path / "p").texto == LARGO


@pytest.mark.parametrize("falla", ["corta", "403"])
def test_en_automatico_sin_la_nota_no_se_arma_aunque_haya_resumen(tmp_path, monkeypatch, falla):
    """Lo "pegado" del automatico es el titulo y el resumen del buscador: con eso solo salian reels cebo."""
    if falla == "corta":
        _nota_corta(monkeypatch)
    else:
        def prohibido(url):
            raise httpx.HTTPStatusError("403", request=httpx.Request("GET", url), response=httpx.Response(403))
        monkeypatch.setattr(fuentes, "leer_articulo", prohibido)
    resumen = "Filtran la supuesta edición física de GTA 6, la abren y confirman una mala noticia. " * 4
    with pytest.raises((fuentes.NotaSinTexto, httpx.HTTPStatusError)):
        fuentes.preparar_fuente(f"https://www.msn.com/es-es/x\n\n{resumen}", [], tmp_path / "p", exigir_nota=True)


def test_el_servicio_exige_la_nota_solo_en_los_automaticos(servicio, monkeypatch):  # noqa: F811
    pedidos = []

    def preparar(*a, exigir_nota=False, **k):
        pedidos.append(exigir_nota)
        raise fuentes.NotaSinTexto("la página no deja leer la nota (solo 0 caracteres)")
    monkeypatch.setattr(fuentes, "preparar_fuente", preparar)
    auto = servicio.crear(1, "https://www.msn.com/x", [], automatico=True)
    manual = servicio.crear(1, "https://www.msn.com/x", [])
    assert pedidos == [True, False]
    assert auto.estado == Estado.ERROR and "no deja leer la nota" in auto.error and manual.estado == Estado.ERROR
