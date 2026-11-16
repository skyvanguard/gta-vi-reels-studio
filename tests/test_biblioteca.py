import io
import random
from pathlib import Path

import pytest
from PIL import Image

from vstudio import biblioteca, fuentes
from vstudio.modelos import Fuente
from vstudio.planificador import _partes_plan

NOTA = "Texto de la nota sobre GTA VI. " * 30  # mas que fuentes.MIN_TEXTO_NOTA


def _imagen_distinta(semilla: int) -> bytes:
    """Bloques al azar: cada semilla da una huella distinta (no se descartan como duplicadas)."""
    azar = random.Random(semilla)
    chica = Image.new("L", (8, 8))
    chica.putdata([azar.choice((10, 240)) for _ in range(64)])
    buf = io.BytesIO()
    chica.resize((1350, 900), Image.NEAREST).convert("RGB").save(buf, format="JPEG")
    return buf.getvalue()


ARCHIVOS = [
    "GTAVI_Screenshots/People/Lucia Caminos/Lucia_Caminos_01.jpg",
    "GTAVI_Screenshots/People/Lucia Caminos/Lucia_Caminos_02.jpg",
    "GTAVI_Screenshots/People/Lucia Caminos/Lucia_Caminos_03.jpg",
    "GTAVI_Screenshots/Places/Vice City/Vice_City_01.jpg",
    "GTAVI_Screenshots/Places/Vice City/Vice_City_02.jpg",
    "GTAVI_Screenshots/Places/Leonida Keys/Leonida_Keys_01.jpg",
    "GTAVI_Artwork_Wallpapers/Jason_and_Lucia_Beach/Jason_and_Lucia_Beach_portrait.jpg",
    "GTAVI_Artwork_Wallpapers/Jason_and_Lucia_Beach/Jason_and_Lucia_Beach_landscape.jpg",
    "GTAVI_Artwork_Wallpapers/Jason_and_Lucia_Beach_With_Logos/Jason_and_Lucia_Beach_portrait.jpg",
    "GTAVI_Ultimate_Edition_Benefits/ULTIMATE_EDITION_GROTTI_CHEETAH_01.jpg",
    "GTAVI_Ultimate_Edition_Benefits/ULTIMATE_EDITION_GROTTI_CHEETAH_02.jpg",
    "GTAVI_Ultimate_Edition_Benefits/ULTIMATE_EDITION_SQUALO_01.jpg",
    "_zips/GTAVI_Screenshots/no_va.jpg",
]


@pytest.fixture
def carpeta(tmp_path) -> Path:
    raiz = tmp_path / "biblioteca"
    for i, relativa in enumerate(ARCHIVOS):
        ruta = raiz / relativa
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(_imagen_distinta(i))
    return raiz


def test_escanea_solo_lo_usable_con_etiquetas(carpeta):
    imagenes = {i.relativa: i for i in biblioteca.escanear(carpeta)}
    assert len(imagenes) == 10  # sin la version horizontal del arte, sin "With Logos" y sin _zips
    assert imagenes["GTAVI_Screenshots/People/Lucia Caminos/Lucia_Caminos_02.jpg"].etiqueta == "Lucia Caminos"
    arte = imagenes["GTAVI_Artwork_Wallpapers/Jason_and_Lucia_Beach/Jason_and_Lucia_Beach_portrait.jpg"]
    assert arte.etiqueta == "Jason And Lucia Beach"
    cheetah = imagenes["GTAVI_Ultimate_Edition_Benefits/ULTIMATE_EDITION_GROTTI_CHEETAH_01.jpg"]
    assert cheetah.etiqueta == "Ultimate Edition Grotti Cheetah"
    assert biblioteca.escanear(carpeta / "no_existe") == []


def test_elige_lo_que_nombra_la_nota_hasta_la_mitad(carpeta):
    nota = "Rockstar mostró a Lucía Caminos manejando el Grotti Cheetah por Vice City."
    todas = biblioteca.escanear(carpeta)
    for semilla in range(5):
        elegidas = biblioteca.elegir(todas, nota, 6, {}, random.Random(semilla))
        etiquetas = [e.etiqueta for e in elegidas]
        assert len(elegidas) == 6
        # las 3 primeras son de lo que la nota nombra entero (Lucía o Vice City); despues rota
        assert set(etiquetas[:3]) <= {"Lucia Caminos", "Vice City"}
        temas = [biblioteca.tema(e) for e in elegidas]
        assert max(temas.count(t) for t in temas) <= biblioteca.MAX_POR_TEMA


def test_jason_y_lucia_son_un_solo_tema(carpeta):
    """Un plan tenia 5 de 6 escenas de la pareja (Jason, Lucía y "Jason and Lucia" eran 3 temas)."""
    todas = biblioteca.escanear(carpeta)
    pareja = [i for i in todas if biblioteca.tema(i) == "protagonistas"]
    assert {i.etiqueta for i in pareja} == {"Lucia Caminos", "Jason And Lucia Beach"}
    elegidas = biblioteca.elegir(todas, "Jason y Lucía Caminos en la playa (Jason and Lucia Beach)", 8, {})
    assert sum(biblioteca.tema(e) == "protagonistas" for e in elegidas) == 2


def test_rota_los_temas_que_hace_mas_que_no_salen(carpeta, tmp_path):
    usos = biblioteca.Usos(tmp_path / "usos.json")
    todas = biblioteca.escanear(carpeta)
    primera = biblioteca.elegir(todas, "una nota sin nombres", 4, usos.leer(), random.Random(3))
    usos.marcar([i.relativa for i in primera], ahora=100.0)
    segunda = biblioteca.elegir(todas, "una nota sin nombres", 4, usos.leer(), random.Random(3))
    temas_primera = {biblioteca.tema(i) for i in primera}
    temas_todos = {biblioteca.tema(i) for i in todas}
    nuevos = temas_todos - temas_primera
    # la segunda arranca por los temas que no salieron en la primera
    assert {biblioteca.tema(i) for i in segunda[:len(nuevos)]} <= nuevos


def test_rota_las_que_hace_mas_que_no_se_usan(carpeta, tmp_path):
    usos = biblioteca.Usos(tmp_path / "usos.json")
    todas = biblioteca.escanear(carpeta)
    primera = biblioteca.elegir(todas, "una nota sin nombres", 4, usos.leer())
    usos.marcar([i.relativa for i in primera], ahora=100.0)
    segunda = biblioteca.elegir(todas, "una nota sin nombres", 4, usos.leer())
    assert not {i.relativa for i in primera} & {i.relativa for i in segunda}  # no repite fotos seguidas
    assert usos.leer()[primera[0].relativa] == 100.0


def test_usos_tolera_un_archivo_roto(tmp_path):
    archivo = tmp_path / "usos.json"
    archivo.write_text("{roto", encoding="utf-8")
    assert biblioteca.Usos(archivo).leer() == {}


def test_la_fuente_suma_oficiales_con_su_descripcion(carpeta, tmp_path, monkeypatch):
    def sin_busqueda(*a, **k):
        raise AssertionError("con biblioteca no hace falta buscar en la web")
    monkeypatch.setattr(fuentes, "buscar_imagenes", sin_busqueda)
    usos = biblioteca.Usos(tmp_path / "usos.json")
    f = fuentes.preparar_fuente("Lucía Caminos en Vice City", [], tmp_path / "p",
                                minimo_imagenes=4, maximo_imagenes=4, buscador_url="http://127.0.0.1:1",
                                biblioteca_dir=carpeta, usos=usos)
    assert len(f.imagenes) == 4 and all(n.startswith("oficial_") for n in f.imagenes)
    assert all(f.descripciones)  # todas llevan lo que muestran
    # las 2 primeras son de lo que la nota nombra (hasta la mitad); el resto rota por temas y puede volver a caer
    # en uno nombrado si es el que menos salio (por eso no se cuenta el total: el sorteo lo hacia intermitente)
    assert set(f.descripciones[:2]) <= {"Lucia Caminos", "Vice City"}
    assert f.descripciones.count("Lucia Caminos") <= 2 and f.descripciones.count("Vice City") <= 2
    assert len(usos.leer()) == 4  # se recuerda cuales se ofrecieron


def test_con_fotos_del_editor_salen_solo_esas(carpeta, tmp_path, monkeypatch):
    """Mando una foto para animarla y el plan la mezclo con 9 oficiales."""
    monkeypatch.setattr(fuentes, "leer_articulo", lambda url: ("Nota", "2026-11-09", NOTA, ["u"] * 5))

    def no_bajar(*a, **k):
        raise AssertionError("con fotos del editor no se bajan las de la nota")
    monkeypatch.setattr(fuentes, "_bajar_imagenes", no_bajar)
    usos = biblioteca.Usos(tmp_path / "usos.json")
    f = fuentes.preparar_fuente("https://n.com/a solo con esta", [_imagen_distinta(99)], tmp_path / "p",
                                minimo_imagenes=4, maximo_imagenes=10, buscador_url="http://127.0.0.1:1",
                                biblioteca_dir=carpeta, usos=usos)
    assert f.imagenes == ("usuario_1.jpg",) and f.texto == NOTA  # la nota se lee igual
    assert usos.leer() == {}


def test_la_nota_deja_lugar_para_las_oficiales(carpeta, tmp_path, monkeypatch):
    monkeypatch.setattr(fuentes, "leer_articulo", lambda url: ("Nota", "2026-11-09", NOTA, ["u"] * 20))

    def bajar_muchas(urls, coleccion, prefijo):  # la pagina trae fotos de sobra (de otras notas)
        semilla = 200
        while not coleccion.lleno():
            coleccion.agregar(_imagen_distinta(semilla), prefijo)
            semilla += 1
    monkeypatch.setattr(fuentes, "_bajar_imagenes", bajar_muchas)
    f = fuentes.preparar_fuente("https://n.com/a", [], tmp_path / "p", maximo_imagenes=10, biblioteca_dir=carpeta,
                                usos=biblioteca.Usos(tmp_path / "usos.json"))
    assert len(f.imagenes) == 10
    assert sum(n.startswith("nota_") for n in f.imagenes) == 5 and sum(n.startswith("oficial_") for n in f.imagenes) == 5
    sin = fuentes.preparar_fuente("https://n.com/a", [], tmp_path / "q", maximo_imagenes=10)
    assert len(sin.imagenes) == 10 and all(n.startswith("nota_") for n in sin.imagenes)  # sin biblioteca, todo


def test_el_plan_le_dice_al_modelo_quien_es_quien(tmp_path):
    imgs = [Image.new("RGB", (64, 64)), Image.new("RGB", (64, 64))]
    f = Fuente(texto="nota", imagenes=("nota_1.jpg", "oficial_2.jpg"), descripciones=("", "Lucia Caminos"))
    textos = [p["text"] for p in _partes_plan(f, imgs) if p["type"] == "text"]
    assert "IMAGEN 0:" in textos and "IMAGEN 1 (imagen oficial de Rockstar: Lucia Caminos):" in textos
