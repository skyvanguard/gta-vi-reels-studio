from datetime import date

import pytest
from PIL import Image

from tests.conftest import imagen_bytes
from vstudio import fuentes, imagenes

HTML = """<html><head>
<meta property="og:image" content="https://cdn.noticias.com/portada.jpg">
</head><body>
<img src="/uploads/1.jpg">
<img srcset="/uploads/2-480.jpg 480w, /uploads/2-1600.jpg 1600w" src="/uploads/2-480.jpg">
<img data-src="https://cdn.noticias.com/3.jpg" src="data:image/gif;base64,AAAA">
<img src="/img/logo.png"><img src="/img/icono.svg"><img src="/uploads/1.jpg">
</body></html>"""


def test_extraer_url_y_texto():
    t = "Mirá esto: https://gtavicentral.com/news/nota-1). Hacelo corto"
    assert fuentes.extraer_url(t) == "https://gtavicentral.com/news/nota-1"
    limpio = fuentes.sin_url(t)
    assert "gtavicentral" not in limpio and limpio.startswith("Mirá esto:") and limpio.endswith("Hacelo corto")
    assert fuentes.extraer_url("sin link") is None


def test_urls_de_imagenes_prioriza_og_y_descarta_logos():
    urls = fuentes.urls_de_imagenes(HTML, "https://noticias.com/nota")
    assert urls == ["https://cdn.noticias.com/portada.jpg", "https://noticias.com/uploads/1.jpg",
                    "https://noticias.com/uploads/2-1600.jpg", "https://cdn.noticias.com/3.jpg"]


def test_dias_de_antiguedad():
    assert fuentes.dias_de_antiguedad("2026-05-19", date(2026, 11, 4)) == 169
    assert fuentes.dias_de_antiguedad("no-es-fecha", date(2026, 11, 4)) is None


def test_coleccion_descarta_chicas_repetidas_e_invalidas(tmp_path):
    col = fuentes._Coleccion(tmp_path)
    assert col.agregar(imagen_bytes(1350, 900), "nota")
    assert not col.agregar(imagen_bytes(1350, 900), "nota")  # repetida
    assert not col.agregar(imagen_bytes(200, 150), "nota")  # chica
    assert not col.agregar(b"esto no es una imagen", "nota")
    assert col.archivos == ["nota_1.jpg"] and (tmp_path / "nota_1.jpg").exists()


def test_recorte_vertical_de_imagen_ancha_respeta_el_centro():
    img = imagenes.normalizar(imagen_bytes(1350, 900))
    izquierda = imagenes.recorte_vertical(img, 0.0, 720, 1280)
    derecha = imagenes.recorte_vertical(img, 1.0, 720, 1280)
    assert izquierda.size == derecha.size == (720, 1280)
    assert izquierda.getpixel((360, 640))[2] > 150  # mitad izquierda es azul
    assert derecha.getpixel((360, 640))[0] > 150  # mitad derecha es roja


def test_recorte_de_imagen_alta_y_clamp():
    alta = Image.new("RGB", (900, 2400), (10, 200, 10))
    assert imagenes.recorte_vertical(alta, 7.0, 720, 1280).size == (720, 1280)


def test_normalizar_rechaza_basura_y_chicas():
    with pytest.raises(imagenes.ImagenInvalida):
        imagenes.normalizar(b"\x89PNG basura")
    with pytest.raises(imagenes.ImagenInvalida):
        imagenes.normalizar(imagen_bytes(300, 300))


def test_storyboard(tmp_path, fuentes_dir):
    rutas = []
    for i in range(3):
        ruta = tmp_path / f"e{i}.png"
        Image.new("RGB", (720, 1280), (i * 80, 40, 40)).save(ruta)
        rutas.append(ruta)
    sb = imagenes.storyboard(rutas, ["1", "2", "3"], fuentes_dir / "Anton-Regular.ttf", alto=320)
    assert sb.height == 320 + 24 and sb.width > 3 * 180
