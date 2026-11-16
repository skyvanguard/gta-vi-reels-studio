"""Experimento: lo que entra blando a Wan sale blando (misma escena, imagen de entrada borrosa:
la mitad de nitidez). En produccion la escena 1 solia ser la foto de la nota (1200x630) con un recorte cerrado:
habia que estirarla 3 veces. Ahora el recorte no se cierra mas de lo que la imagen aguanta y una imagen chica va
como foto con paneo, sin gastar GPU."""
import random
from datetime import date

import pytest
from PIL import Image

from tests.conftest import imagen_bytes
from tests.test_guion_y_monetizable import GUION_OK
from tests.test_planificador_llm import LlmFalso, guion_json
from vstudio import armado, imagenes
from vstudio.modelos import Fuente
from vstudio.planificador import PROMPT_MOVIMIENTO, PlanInvalido, planificar, topes_de_zoom, validar_plan

HOY = date(2026, 11, 16)
MOVIMIENTO = {"motion_prompt_en": "the car speeds forward, tracking shot", "negative_extra_en": "warping"}
WAN = (720, 1280)


@pytest.mark.parametrize("tamano, zoom, esperado", [
    ((3840, 2160), 1.6, 0.95),  # una oficial en 4K aguanta el primer plano mas cerrado
    ((1920, 1080), 1.0, 1.19),  # una foto de nota en 1080p, entera: justo
    ((1920, 1080), 1.5, 1.78),
    ((1200, 630), 1.5, 3.05),  # lo que pasaba en la escena 1: tres veces estirada
    ((1080, 1920), 1.6, 1.07),  # arte vertical
    ((600, 1600), 1.0, 1.2),  # mas angosta que 9:16: manda el ancho
])
def test_cuanto_hay_que_estirar_un_recorte(tamano, zoom, esperado):
    assert imagenes.ampliacion(tamano, zoom, *WAN) == pytest.approx(esperado, abs=0.01)


@pytest.mark.parametrize("tamano, pedido, queda", [
    ((3840, 2160), 1.6, 1.6),  # sobra resolucion: el zoom pedido
    ((1920, 1080), 1.5, 1.05),  # se abre hasta donde aguanta
    ((1200, 630), 1.5, 1.0),  # chica: entera (y aun asi no alcanza)
    ((2160, 3840), 1.6, 1.6),
])
def test_el_recorte_no_se_cierra_mas_de_lo_que_la_imagen_aguanta(tamano, pedido, queda):
    zoom = imagenes.zoom_nitido(tamano, pedido, *WAN)
    assert zoom == pytest.approx(queda, abs=0.01)
    assert zoom == 1.0 or not imagenes.estirada(tamano, zoom, *WAN)
    assert imagenes.zoom_nitido(tamano, zoom, *WAN) == pytest.approx(zoom)  # volver a aplicarlo no cambia nada


def test_el_redondeo_no_manda_a_paneo_una_imagen_que_cumple():
    """El zoom ajustado da justo el maximo de estirado, mas o menos el redondeo: 1.560 de 200.000 casos al azar
    quedaban apenas por encima y pasaban de IA a foto sin motivo."""
    azar = random.Random(7)
    for _ in range(20000):
        tamano = (azar.randint(200, 4000), azar.randint(200, 4000))
        zoom = imagenes.zoom_nitido(tamano, azar.uniform(1.0, 1.6), *WAN)
        assert zoom == 1.0 or not imagenes.estirada(tamano, zoom, *WAN), (tamano, zoom)


def test_valores_raros_no_rompen_la_cuenta():
    assert imagenes.ampliacion((720, 1280), 2.0, *WAN) == imagenes.ampliacion((720, 1280), 1.6, *WAN)  # como el recorte
    assert imagenes.zoom_nitido((1920, 1080), 0, *WAN) == 1.0 and imagenes.zoom_nitido((0, 0), 1.5, *WAN) == 1.0
    assert imagenes.estirada((0, 100), 1.0, *WAN)


def _plan(imagenes_zoom):
    return {"titular": "TITULAR DE PRUEBA", "texto_post": "Post. #GTA6", "advertencia": "", "escenas": [
        {"imagen": i, "centro_x": 0.5, "zoom": z, "sujeto": f"sujeto {i}", "texto": f"TEXTO {i}",
         "narracion": f"Narración de la escena {i}."} for i, z in imagenes_zoom]}


def test_la_foto_chica_de_la_nota_va_con_paneo_y_la_grande_se_anima(tmp_path, monkeypatch):
    paneos = []
    monkeypatch.setattr(armado.visuales, "foto_animada",
                        lambda img, destino, s, acercar=True: (paneos.append(img.name), destino.write_bytes(b"v")))
    (tmp_path / "nota_1.jpg").write_bytes(imagen_bytes(1200, 630, color=(200, 30, 30)))
    (tmp_path / "nota_2.jpg").write_bytes(imagen_bytes(1920, 1080, color=(30, 200, 30)))
    (tmp_path / "oficial_3.jpg").write_bytes(imagen_bytes(3840, 2160, color=(30, 30, 200)))
    fuente = Fuente(url="https://n.com/a", titulo="Nota", fecha="2026-11-16", texto="GTA VI sale el 19/11.",
                    imagenes=("nota_1.jpg", "nota_2.jpg", "oficial_3.jpg"), descripciones=("", "", "Vice City"))
    falso = LlmFalso([_plan([(0, 1.5), (1, 1.5), (2, 1.6)]), MOVIMIENTO, MOVIMIENTO, guion_json(GUION_OK),
                      {"sin_respaldo": []}])
    chica, mediana, grande = planificar(falso, tmp_path, fuente, HOY, 3, 7, *WAN).escenas
    # la de 1200x630 no se le da a Wan: paneo, entera, sin prompt de movimiento y sin pasar por la GPU
    assert (chica.medio, chica.zoom, chica.clip, chica.motion_prompt) == ("foto", 1.0, "clip_1.mp4", "")
    assert paneos == ["escena_1_hd.png"] and chica.revisada
    # la de 1920x1080 se anima, pero con el recorte abierto hasta donde aguanta
    assert mediana.medio == "ia" and mediana.zoom == pytest.approx(1.05, abs=0.01) and mediana.motion_prompt
    # la oficial en 4K, como se pidio
    assert (grande.medio, grande.zoom) == ("ia", 1.6)
    assert falso.llamadas.count(PROMPT_MOVIMIENTO) == 2
    # y el planificador sabe cual es chica antes de elegir el encuadre
    material = falso.partes[0]
    assert "IMAGEN 0 — imagen chica: va como foto fija con paneo, usala entera (zoom 1.0):" in material
    assert "IMAGEN 1 — resolución media: usala con zoom 1.0, no en primer plano:" in material
    assert "IMAGEN 2 (imagen oficial de Rockstar: Vice City):" in material  # la de 4K, sin aviso


def test_el_editor_se_entera_de_las_fotos_fijas(tmp_path, monkeypatch):
    """Sin aviso, un reel podia salir entero con fotos fijas (sin biblioteca de tomas) y nadie lo sabia."""
    monkeypatch.setattr(armado.visuales, "foto_animada", lambda img, destino, s, acercar=True: destino.write_bytes(b"v"))
    for i in (1, 2, 3):
        (tmp_path / f"nota_{i}.jpg").write_bytes(imagen_bytes(1200, 630, color=(60 * i, 30, 30)))
    (tmp_path / "oficial_4.jpg").write_bytes(imagen_bytes(3840, 2160))
    nombres = ("nota_1.jpg", "nota_2.jpg", "nota_3.jpg", "oficial_4.jpg")
    fuente = Fuente(url="https://n.com/a", titulo="Nota", texto="GTA VI sale el 19/11.", imagenes=nombres)
    fin = [guion_json(GUION_OK), {"sin_respaldo": []}]
    quieto = planificar(LlmFalso([_plan([(0, 1.0), (1, 1.0), (2, 1.0)]), *fin]), tmp_path, fuente, HOY, 3, 7, *WAN)
    assert all(e.medio == "foto" for e in quieto.escenas)
    assert quieto.advertencia.startswith("Todo el reel sale con fotos fijas")
    mixto = planificar(LlmFalso([_plan([(0, 1.0), (3, 1.0), (1, 1.0)]), MOVIMIENTO, *fin]), tmp_path, fuente, HOY, 3,
                       7, *WAN)
    assert [e.medio for e in mixto.escenas] == ["foto", "ia", "foto"]
    assert mixto.advertencia.startswith("Imagen chica en las escenas 1, 3: va como foto con paneo")


def test_con_solo_video_la_imagen_chica_se_anima_igual(tmp_path):
    """El editor pidio la imagen animada ("solo video" con un link): no se la cambia por una foto fija."""
    (tmp_path / "nota_1.jpg").write_bytes(imagen_bytes(1200, 630))
    fuente = Fuente(url="https://n.com/a", titulo="Nota", texto="GTA VI sale el 19/11.", imagenes=("nota_1.jpg",))
    plan = _plan([(0, 1.0), (0, 1.0)])
    plan["escenas"][1]["centro_x"] = 0.05
    escenas = planificar(LlmFalso([plan, MOVIMIENTO, MOVIMIENTO]), tmp_path, fuente, HOY, 2, 7, *WAN,
                         solo_animacion=True).escenas
    assert [e.medio for e in escenas] == ["ia", "ia"] and all(e.motion_prompt for e in escenas)


def test_dos_escenas_de_una_imagen_chica_no_pasan_como_encuadres_distintos(tmp_path):
    """El zoom se ajusta antes de mirar los encuadres: con una imagen de 1200x630 todo zoom termina en 1.0, y
    dos escenas con "zooms distintos" salian con el mismo recorte."""
    plan = _plan([(0, 1.0), (0, 1.6)])
    with pytest.raises(PlanInvalido, match="casi el mismo encuadre"):
        validar_plan(plan, 1, 2, [1200 / 630], zoom_maximo=[1.0])
    assert [e["zoom"] for e in validar_plan(plan, 1, 2, [1200 / 630])["escenas"]] == [1.0, 1.6]  # sin tope, como antes
    (tmp_path / "nota_1.jpg").write_bytes(imagen_bytes(1200, 630))
    (tmp_path / "toma_2.jpg").write_bytes(imagen_bytes(3840, 1920))
    (tmp_path / "usuario_3.jpg").write_bytes(imagen_bytes(1200, 630))
    nombres = ("nota_1.jpg", "toma_2.jpg", "usuario_3.jpg")
    imgs = [Image.open(tmp_path / n) for n in nombres]
    assert topes_de_zoom(imgs, nombres, *WAN) == [1.0, armado.ZOOM_MAX_TOMA, imagenes.ZOOM_MAX]


def test_con_las_fotos_del_editor_se_hace_lo_que_pidio(tmp_path):
    """Una foto del editor se anima con tomas distintas, sea como sea la foto."""
    (tmp_path / "usuario_1.jpg").write_bytes(imagen_bytes(1200, 630))
    fuente = Fuente(nota_usuario="animá esta foto", imagenes=("usuario_1.jpg",))
    plan = {**_plan([(0, 1.0), (0, 1.6), (0, 1.3)])}
    for i, e in enumerate(plan["escenas"]):
        e["centro_x"] = 0.1 + 0.4 * i
    falso = LlmFalso([plan, MOVIMIENTO, MOVIMIENTO, MOVIMIENTO])
    escenas = planificar(falso, tmp_path, fuente, HOY, 3, 7, *WAN, solo_animacion=True).escenas
    assert [(e.medio, e.zoom) for e in escenas] == [("ia", 1.0), ("ia", 1.6), ("ia", 1.3)]
    assert "imagen chica" not in falso.partes[0]
