"""El pedido del mensaje (largo, "solo video") y el modo solo animacion: sin voz, sin guion y sin textos."""
from dataclasses import replace
from datetime import date

import pytest

from tests.conftest import imagen_bytes
from tests.test_almacen_servicio_acceso import _proyecto
from tests.test_planificador_llm import LlmFalso
from vstudio import montaje, pedido
from vstudio.bot import LARGOS, acuse, resumen_plan
from vstudio.modelos import Fuente, proyecto_a_dict, proyecto_desde_dict
from vstudio.planificador import planificar

PEDIDO_REAL = "Genera un video con esto, solo video, agrégale la animación y que diré al menos 1min,"


def test_lee_el_pedido_real_del_editor():
    p = pedido.leer(PEDIDO_REAL)
    assert p == pedido.Pedido(segundos=60, solo_animacion=True)
    assert pedido.largo_para(p.segundos, LARGOS) == 75  # el primero que pasa del minuto


@pytest.mark.parametrize("texto, segundos, solo", [
    ("hacelo de 30 segundos", 30, False),
    ("un reel de 90 s sin voz", 90, True),
    ("que dure un minuto", 60, False),
    ("1,5 min, solo animación", 90, True),
    ("medio minuto", 30, False),
    ("GTA 6 sale en PS5 y cuesta 80 dólares", None, False),  # "6 s..." y "PS5" no son largos
    ("solo el video por favor", None, True),
    ("", None, False),
])
def test_lee_largo_y_solo_animacion(texto, segundos, solo):
    assert pedido.leer(texto) == pedido.Pedido(segundos=segundos, solo_animacion=solo)


def test_una_nota_pegada_no_es_un_pedido():
    nota = "Rockstar confirmó que el tráiler dura 2 minutos y que habrá solo video del gameplay. " * 5
    assert pedido.leer(nota) == pedido.Pedido()


def test_largo_para_ajusta_a_los_que_se_ofrecen():
    assert pedido.largo_para(20, LARGOS) == 30
    assert pedido.largo_para(10, LARGOS) == 10
    assert pedido.largo_para(300, LARGOS) == 90  # mas que todos: el mayor


def test_el_acuse_dice_lo_que_entendio():
    assert "solo con tu foto, sin voz ni textos" in acuse(1, 75, True) and "~75 s" in acuse(1, 75, True)
    assert "solo con tus 3 fotos, con voz" in acuse(3, 30, False)
    assert "oficiales" in acuse(0, 30, False)


def test_solo_animacion_no_pasa_por_el_guionista(tmp_path):
    (tmp_path / "usuario_1.jpg").write_bytes(imagen_bytes(1280, 720))
    fuente = Fuente(nota_usuario=PEDIDO_REAL, imagenes=("usuario_1.jpg",))
    escenas = [{"imagen": 0, "centro_x": x, "zoom": z, "sujeto": s, "texto": "t", "narracion": "n"}
               for x, z, s in ((0.35, 1.5, "Lucía de cerca"), (0.6, 1.3, "Jason con el vaso"), (0.5, 1.0, "el auto"))]
    movimiento = {"motion_prompt_en": "wind in her hair", "negative_extra_en": ""}
    falso = LlmFalso([{"titular": "JASON Y LUCÍA", "escenas": escenas, "texto_post": "#GTA6"}] + [movimiento] * 3)
    plan = planificar(falso, tmp_path, fuente, date(2026, 11, 10), 3, 7, 720, 1280, solo_animacion=True)
    assert len(plan.escenas) == 3 and {e.origen for e in plan.escenas} == {"usuario_1.jpg"}
    assert len(falso.llamadas) == 4  # plan + 3 movimientos: ninguna llamada al guionista
    assert "SOLO ANIMACION" in falso.llamadas[0]


def test_el_resumen_de_una_animacion_no_muestra_guion():
    p = replace(_proyecto(), con_voz=False, solo_animacion=True)
    texto = resumen_plan(p, 70.0)
    assert "Solo animación" in texto and "🎙️" not in texto and "📺" not in texto and "TITULAR" not in texto
    assert "🎙️" in resumen_plan(_proyecto(), 70.0)


def test_solo_animacion_se_guarda_y_los_viejos_cargan_sin_el_campo():
    p = replace(_proyecto(), solo_animacion=True)
    assert proyecto_desde_dict(proyecto_a_dict(p)).solo_animacion
    viejo = proyecto_a_dict(_proyecto())
    del viejo["solo_animacion"]
    assert proyecto_desde_dict(viejo).solo_animacion is False


def test_grafo_sin_titular_ni_textos():
    t = montaje.calcular_tiempos([5.0, 5.0], [0.0, 0.0])
    video, salida = montaje.grafo_video(t, None, [], 2)
    assert "[tit]" not in video and "enable='gte(t,0.000)'" in video and salida == "om"
    video, salida = montaje.grafo_video(t, None, [], None)
    assert salida == "x1" and "overlay" not in video


# el plan real (foto 1280x720, 15 escenas): 2, 9 y 10 eran el mismo plano; 1, 8 y 15 la misma cara
ENCUADRES_REALES = [(0.45, 0.35, 1.5), (0.5, 0.5, 1.0), (0.4, 0.78, 1.5), (0.65, 0.32, 1.3), (0.73, 0.45, 1.5),
                    (0.15, 0.15, 1.1), (0.82, 0.55, 1.3), (0.42, 0.3, 1.5), (0.5, 0.55, 1.2), (0.5, 0.33, 1.0),
                    (0.35, 0.88, 1.5), (0.62, 0.3, 1.4), (0.25, 0.1, 1.1), (0.88, 0.62, 1.4), (0.45, 0.4, 1.5)]


def _plan_de(encuadres):
    return {"titular": "JASON Y LUCÍA", "texto_post": "#GTA6",
            "escenas": [{"imagen": 0, "centro_x": x, "centro_y": y, "zoom": z, "texto": "t", "narracion": "n"}
                        for x, y, z in encuadres]}


def test_detecta_los_encuadres_repetidos_del_plan_real():
    from vstudio.planificador import PlanInvalido, encuadres_repetidos, validar_plan
    plan = validar_plan(_plan_de(ENCUADRES_REALES), 1, 15)  # sin aspectos: como antes
    pares = encuadres_repetidos(plan["escenas"], [1280 / 720])
    assert {(2, 9), (2, 10), (9, 10), (1, 8), (1, 15), (8, 15)} <= set(pares)
    with pytest.raises(PlanInvalido, match="mismo encuadre"):
        validar_plan(_plan_de(ENCUADRES_REALES), 1, 15, [1280 / 720])
    flojo = validar_plan(_plan_de(ENCUADRES_REALES), 1, 15, [1280 / 720], estricto=False)
    assert flojo["advertencia"].startswith("Escenas con casi el mismo encuadre: ") and "2 y 10" in flojo["advertencia"]


def test_encuadres_distintos_de_la_misma_foto_pasan():
    from vstudio.planificador import validar_plan
    distintos = [(x, y, 1.5) for x in (0.1, 0.3, 0.5, 0.7, 0.9) for y in (0.0, 1.0)] + [(0.2, 0.5, 1.0),
                                                                                        (0.8, 0.5, 1.0)]
    assert len(validar_plan(_plan_de(distintos), 1, 12, [1280 / 720])["escenas"]) == 12


def test_caja_relativa_coincide_con_el_recorte():
    """El color de cada pixel dice donde esta (rojo = x, verde = y): las esquinas del recorte real tienen que caer
    donde dice caja_relativa."""
    from PIL import Image

    from vstudio import imagenes
    from vstudio.planificador import caja_relativa
    for tam, (cx, cy, z) in (((1280, 720), (0.7, 0.2, 1.4)), ((600, 1200), (0.3, 0.8, 1.2))):
        ancho, alto = tam
        img = Image.new("RGB", tam)
        img.putdata([(round(255 * x / (ancho - 1)), round(255 * y / (alto - 1)), 0)
                     for y in range(alto) for x in range(ancho)])
        recorte = imagenes.recorte_vertical(img, cx, 90, 160, z, cy)
        izq, arriba, der, abajo = caja_relativa(ancho / alto, cx, cy, z)
        r0, g0, _ = recorte.getpixel((0, 0))
        r1, g1, _ = recorte.getpixel((89, 159))
        assert (r0 / 255, g0 / 255, r1 / 255, g1 / 255) == pytest.approx((izq, arriba, der, abajo), abs=0.03)
