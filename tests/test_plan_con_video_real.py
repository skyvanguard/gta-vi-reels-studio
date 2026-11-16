"""El plan usa video real y fotos de la noticia, la imagen cambia mas seguido, los clips
del editor entran a la biblioteca y hay formatos ademas de la noticia."""
import os
import subprocess
import time
from datetime import date
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from tests.conftest import ZONA_PRUEBA, hay_ffmpeg, imagen_bytes
from tests.test_almacen_servicio_acceso import _proyecto, servicio  # noqa: F401 - fixture
from tests.test_automatico import _nota
from tests.test_bot_trabajador import entorno  # noqa: F401 - fixture
from tests.test_guion_y_monetizable import GUION_OK
from tests.test_planificador_llm import LlmFalso, guion_json
from tests.test_tomas_y_visuales import _biblioteca, _video, cuadro_distinto
from vstudio import (armado, automatico, bot as bot_mod, formatos, fuentes, guionista, ilustracion, ingesta, pedido,
                     tomas)
from vstudio.biblioteca import Usos
from vstudio.llm import LlmError
from vstudio.modelos import Escena, Estado, Fuente, Plan, Proyecto, proyecto_a_dict, proyecto_desde_dict
from vstudio.planificador import PROMPT_MOVIMIENTO, planificar
from vstudio.servicio import CARPETA_ENTRADA, CARPETA_PROCESADOS, CARPETA_RECHAZADOS, EdicionInvalida

con_ffmpeg = pytest.mark.skipif(not hay_ffmpeg(), reason="hace falta ffmpeg")
HOY = date(2026, 11, 15)
NOTA = "Rockstar mostró la lancha de Lucía en el nuevo tráiler de GTA VI. " * 12
MOVIMIENTO = {"motion_prompt_en": "the car speeds forward, tracking shot", "negative_extra_en": "warping"}


def _catalogo(raiz, datos=None):
    datos = datos or {"trailer": [("Lucía maneja una lancha", ["lucia", "lancha"], 2.0, 3),
                                  ("Jason corre por la playa", ["jason", "playa"], 2.5, 2),
                                  ("Autos en la autopista", ["autos"], 2.0, 1),
                                  ("Fiesta en un yate", ["fiesta", "yate"], 3.0, 1)]}
    _biblioteca(raiz / "tomas", datos)
    return tomas.Catalogo.abrir(raiz / "tomas", raiz / "tomas_usos.json")


# ---------- el material: tomas de video y fotos de la noticia ----------
def test_el_material_ofrece_tomas_de_video_y_anota_cual_es_cual(tmp_path, monkeypatch):
    monkeypatch.setattr(fuentes, "leer_articulo", lambda url: ("Nota", "2026-11-15", NOTA, []))
    catalogo = _catalogo(tmp_path)
    f = fuentes.preparar_fuente("https://n.com/a", [], tmp_path / "p", maximo_imagenes=8, tomas_cat=catalogo)
    de_video = [n for n in f.imagenes if n.startswith("toma_")]
    assert len(de_video) == fuentes.cupo_de_tomas(catalogo, 8) == 4
    # lo que nombra la nota va primero, y el planificador sabe cuanta accion tiene cada toma
    assert f.descripciones[0] == "TOMA DE VIDEO de 2.0 s, accion 3 de 3: Lucía maneja una lancha"
    mapa = tomas.del_proyecto(tmp_path / "p", list(catalogo.tomas))
    assert set(mapa) == set(de_video) and mapa[de_video[0]].sujeto == "Lucía maneja una lancha"
    assert catalogo.usadas() == {}  # ofrecer no es usar: se marca cuando el plan la usa


def test_otras_imagenes_no_vuelve_a_ofrecer_las_tomas_del_plan_anterior(tmp_path, monkeypatch):
    monkeypatch.setattr(fuentes, "leer_articulo", lambda url: ("Nota", "2026-11-15", NOTA, []))
    catalogo = _catalogo(tmp_path)
    primero = fuentes.preparar_fuente("https://n.com/a", [], tmp_path / "p", maximo_imagenes=6, tomas_cat=catalogo)
    antes = set(tomas.mapa_del_proyecto(tmp_path / "p").values())
    excluir = tuple(tmp_path / "p" / n for n in primero.imagenes)
    fuentes.preparar_fuente("https://n.com/a", [], tmp_path / "q", maximo_imagenes=6, tomas_cat=catalogo,
                            excluir=excluir)
    despues = set(tomas.mapa_del_proyecto(tmp_path / "q").values())
    assert len(antes) == 3 and len(despues) == 1 and not antes & despues  # hay 4 tomas: queda la que no salio


def test_con_fotos_del_editor_no_hay_tomas_ni_busqueda(tmp_path):
    catalogo = _catalogo(tmp_path)

    def nunca(*a):
        raise AssertionError("con fotos del editor no se busca nada")
    f = fuentes.preparar_fuente("animá esta foto", [imagen_bytes(1350, 900)], tmp_path / "p", tomas_cat=catalogo,
                                buscador_url="http://127.0.0.1:1", consultas=nunca, filtro=nunca)
    assert f.imagenes == ("usuario_1.jpg",)
    assert fuentes.cupo_de_tomas(None, 10) == 0 and fuentes.cupo_de_tomas(tomas.Catalogo(), 10) == 0


def test_busca_fotos_de_lo_que_la_nota_nombra_y_deja_las_que_sirven(tmp_path, monkeypatch):
    monkeypatch.setattr(fuentes, "leer_articulo", lambda url: ("Un Cheetah real a la venta", "2026-11-15", NOTA, []))
    buscadas, mostradas = [], []

    def buscar(url, consultas_, por_consulta=10):
        buscadas.append(consultas_[0])
        return [f"https://img.com/{consultas_[0][:3]}/{i}.jpg" for i in range(por_consulta)]

    def descargar(url, *a, **k):
        return fuentes.red.Respuesta(url, "image/jpeg", cuadro_distinto(sum(map(ord, url))))
    monkeypatch.setattr(fuentes, "buscar_imagenes", buscar)
    monkeypatch.setattr(fuentes.red, "descargar", descargar)

    def filtro(titulo, candidatas):
        mostradas.append((titulo, len(candidatas)))
        return [i in (0, 3) for i in range(len(candidatas))]
    f = fuentes.preparar_fuente("https://n.com/a", [], tmp_path / "p", maximo_imagenes=10,
                                buscador_url="http://127.0.0.1:1", filtro=filtro,
                                consultas=lambda titulo, texto: ("Grotti Cheetah Selfridges", "Ferrari Testarossa"))
    assert buscadas == ["Grotti Cheetah Selfridges", "Ferrari Testarossa"]
    assert mostradas == [("Un Cheetah real a la venta", fuentes.MAX_CANDIDATAS_NOTICIA)]
    fotos = [n for n in f.imagenes if n.startswith("foto_")]
    assert len(fotos) == 2 and f.descripciones[f.imagenes.index(fotos[0])] == "FOTO DE LA NOTICIA"


def test_las_busquedas_y_el_filtro_fallan_sin_romper_nada():
    assert ilustracion.consultas_de(LlmFalso([{"busquedas": [" Grotti  Cheetah ", "", None, {"x": 1}, "x" * 200,
                                                             "a", "b"]}]), "t", "nota") == ("Grotti Cheetah", "x" * 80, "a")
    assert ilustracion.consultas_de(LlmFalso([LlmError("caído")]), "t", "nota") == ()
    assert ilustracion.consultas_de(LlmFalso([]), "", "") == ()  # sin nota no se pregunta
    imgs = [Image.new("RGB", (800, 600))] * 3
    assert ilustracion.sirven(LlmFalso([{"sirven": [2, "0", 9, "x", "²", None, True]}]), "t", imgs) == [True, False,
                                                                                                         True]
    assert ilustracion.sirven(LlmFalso([LlmError("caído")]), "t", imgs) == [False] * 3  # mejor sin foto
    assert ilustracion.sirven(LlmFalso([]), "t", []) == []


# ---------- el plan ----------
def _fuente_mixta(carpeta, catalogo):
    """Una toma de video, una foto de la noticia y una imagen oficial, como las arma preparar_fuente."""
    carpeta.mkdir(parents=True, exist_ok=True)
    toma = catalogo.tomas[0]
    (carpeta / "toma_1.jpg").write_bytes(toma.cuadro.read_bytes())
    (carpeta / "foto_2.jpg").write_bytes(cuadro_distinto(2))
    (carpeta / "oficial_3.jpg").write_bytes(cuadro_distinto(3, (3840, 2160)))  # las oficiales vienen en 4K
    tomas.anotar_en_proyecto(carpeta, {"toma_1.jpg": toma.relativa})
    return Fuente(url="https://n.com/a", titulo="Nota", fecha="2026-11-15", texto=NOTA,
                  imagenes=("toma_1.jpg", "foto_2.jpg", "oficial_3.jpg"),
                  descripciones=(f"TOMA DE VIDEO de 2.0 s: {toma.sujeto}", "FOTO DE LA NOTICIA", "Vice City"))


PLAN = {"titular": "LA LANCHA DE LUCÍA", "texto_post": "Post. #GTA6", "advertencia": "", "escenas": [
    {"imagen": 0, "centro_x": 0.5, "zoom": 1.6, "sujeto": "Lucía en la lancha", "texto": "LA LANCHA",
     "narracion": "Lucía maneja una lancha en el nuevo tráiler de GTA seis."},
    {"imagen": 1, "centro_x": 0.5, "sujeto": "la lancha real", "texto": "ES REAL", "narracion": "Y existe de verdad."},
    {"imagen": 2, "centro_x": 0.5, "sujeto": "Vice City", "texto": "VICE CITY", "narracion": "¿Te subirías?"}]}


@con_ffmpeg
def test_el_plan_usa_el_video_real_y_solo_la_ia_necesita_gpu(tmp_path):
    catalogo = _catalogo(tmp_path)
    for t in catalogo.tomas:  # las tomas de prueba tienen que ser videos de verdad
        _video(t.ruta, colores=("white",), segundos=(t.segundos,), tamano="1280x720")
    fuente = _fuente_mixta(tmp_path / "p", catalogo)
    falso = LlmFalso([PLAN, MOVIMIENTO, guion_json(GUION_OK), {"sin_respaldo": []}])
    plan = planificar(falso, tmp_path / "p", fuente, HOY, 3, 7, 720, 1280, catalogo=catalogo)
    assert [e.medio for e in plan.escenas] == ["toma", "foto", "ia"]
    assert "TOMA DE VIDEO son el cuadro del medio" in falso.llamadas[0] and "FOTO DE LA NOTICIA" in falso.llamadas[0]
    assert falso.llamadas.count(PROMPT_MOVIMIENTO) == 1  # solo la escena de IA lleva prompt de movimiento
    toma, foto, ia = plan.escenas
    assert toma.clip == "clip_1.mp4" and toma.revisada and toma.zoom == armado.ZOOM_MAX_TOMA and not toma.motion_prompt
    assert foto.clip == "clip_2.mp4" and foto.revisada and (tmp_path / "p" / "clip_2.mp4").exists()
    assert ia.clip == "" and ia.motion_prompt and not ia.revisada
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(tmp_path / "p" / "clip_1.mp4")], capture_output=True, text=True)
    assert out.stdout.strip() == "1080,1920"
    # cada escena lleva tomas que siguen a su clip, sin repetir ninguna en el reel
    extras = [x for e in plan.escenas for x in e.extras]
    assert all(e.extras for e in plan.escenas) and len(extras) == len(set(extras)) == 3
    assert all((tmp_path / "p" / x).exists() for x in extras)
    assert len(catalogo.usadas()) == 4  # la principal y las tres que acompanan quedan marcadas para rotar
    from vstudio.trabajador import escenas_pendientes
    p = Proyecto(id="x", chat_id=1, estado=Estado.EN_COLA, fuente=fuente, plan=plan)
    assert [e.n for e in escenas_pendientes(p, tmp_path / "p")] == [3]


def test_sin_extras_o_con_fotos_del_editor_no_se_suman_tomas(tmp_path, monkeypatch):
    catalogo = _catalogo(tmp_path)
    monkeypatch.setattr(armado.visuales, "recortar_toma", lambda origen, destino, caja: destino.write_bytes(b"v"))
    monkeypatch.setattr(armado.visuales, "foto_animada",
                        lambda img, destino, s, acercar=True: destino.write_bytes(b"v"))
    fuente = _fuente_mixta(tmp_path / "p", catalogo)
    respuestas = [PLAN, MOVIMIENTO, guion_json(GUION_OK), {"sin_respaldo": []}]
    plan = planificar(LlmFalso(list(respuestas)), tmp_path / "p", fuente, HOY, 3, 7, 720, 1280, catalogo=catalogo,
                      extras=False)
    assert [e.medio for e in plan.escenas] == ["toma", "foto", "ia"] and not any(e.extras for e in plan.escenas)
    (tmp_path / "q").mkdir()
    (tmp_path / "q" / "usuario_1.jpg").write_bytes(imagen_bytes(1350, 900))
    propia = Fuente(nota_usuario="animá esta foto", imagenes=("usuario_1.jpg",))
    solo = {**PLAN, "escenas": [{**e, "imagen": 0, "centro_x": 0.1 + 0.4 * i} for i, e in enumerate(PLAN["escenas"])]}
    plan = planificar(LlmFalso([solo, MOVIMIENTO, MOVIMIENTO, MOVIMIENTO]), tmp_path / "q", propia, HOY, 3, 7, 720,
                      1280, solo_animacion=True, catalogo=catalogo)
    assert all(e.medio == "ia" and not e.extras for e in plan.escenas) and catalogo.usadas() != {}


def test_si_el_clip_sin_gpu_falla_la_escena_la_anima_la_ia(tmp_path, monkeypatch):
    catalogo = _catalogo(tmp_path)

    def roto(*a, **k):
        raise armado.visuales.VisualError("ffmpeg no pudo")
    monkeypatch.setattr(armado.visuales, "recortar_toma", roto)
    monkeypatch.setattr(armado.visuales, "foto_animada", roto)
    falso = LlmFalso([PLAN, MOVIMIENTO, MOVIMIENTO, MOVIMIENTO, guion_json(GUION_OK), {"sin_respaldo": []}])
    plan = planificar(falso, tmp_path / "p", _fuente_mixta(tmp_path / "p", catalogo), HOY, 3, 7, 720, 1280,
                      catalogo=catalogo)
    assert all(e.medio == "ia" and e.motion_prompt and not e.clip and not e.extras for e in plan.escenas)


def test_el_prompt_de_movimiento_pide_accion():
    assert "ACCION" in PROMPT_MOVIMIENTO and '"static"' in PROMPT_MOVIMIENTO  # lo que no tiene que decir


# ---------- modelo, servicio y bot ----------
def test_el_proyecto_guarda_el_medio_las_tomas_extra_y_el_formato():
    escena = Escena(n=1, imagen="escena_1.png", origen="toma_1.jpg", centro_x=0.5, texto="t", narracion="n",
                    clip="clip_1.mp4", medio="toma", extras=("extra_1_1.mp4",), revisada=True)
    p = Proyecto(id="a", chat_id=1, estado=Estado.EN_COLA, fuente=Fuente(), plan=Plan("T", (escena,)),
                 formato="ranking")
    assert proyecto_desde_dict(proyecto_a_dict(p)) == p
    viejo = proyecto_a_dict(_proyecto())  # los proyectos de antes no traen estos campos
    for e in viejo["plan"]["escenas"]:
        del e["medio"], e["extras"]
    del viejo["formato"]
    leido = proyecto_desde_dict(viejo)
    assert leido.formato == "noticia" and leido.plan.escenas[0].medio == "ia" and leido.plan.escenas[0].extras == ()


def test_una_escena_de_video_real_no_se_regenera_con_ia(servicio):  # noqa: F811
    p = _proyecto(estado=Estado.ENTREGADO)
    real = tuple(e if e.n != 2 else Escena(**{**e.__dict__, "medio": "toma"}) for e in p.plan.escenas)
    servicio.almacen.guardar(Proyecto(**{**p.__dict__, "plan": Plan("T", real)}))
    with pytest.raises(EdicionInvalida, match="video real"):
        servicio.regenerar_escena("abc", 2)
    assert servicio.regenerar_escena("abc", 1).estado == Estado.EN_COLA


@con_ffmpeg
def test_los_clips_del_editor_entran_a_la_biblioteca(servicio, monkeypatch):  # noqa: F811
    monkeypatch.setattr(servicio, "con_llm", lambda fn, cuadro, tema: {
        "usable": True, "sujeto": f"clip de {tema}", "etiquetas": ["auto"], "energia": 3})
    entrada = servicio.cfg.tomas_dir / CARPETA_ENTRADA
    (entrada / "Persecucion").mkdir(parents=True)
    _video(entrada / "Persecucion" / "ps5.mp4", colores=("testsrc2", "smptebars"), segundos=(2.0, 2.0))
    _video(entrada / "suelto.mp4", colores=("white",), segundos=(2.0,))
    (entrada / "roto.mp4").write_text("no es un video")
    assert servicio.sumar_entrada() == [] and servicio.clips_en_entrada() == ([], 3)  # se estan copiando todavia
    hace_rato = time.time() - 300
    for archivo in entrada.rglob("*.mp4"):
        os.utime(archivo, (hace_rato, hace_rato))
    hechos = dict(servicio.sumar_entrada())
    assert hechos["ps5.mp4"].guardadas == 2 and hechos["suelto.mp4"].fuente == "Gameplay"
    assert "no parece un video" in hechos["roto.mp4"]
    catalogo = servicio.catalogo()
    assert sorted({t.fuente for t in catalogo.tomas}) == ["Gameplay", "Persecucion"]
    assert all(t.sujeto.startswith("clip de") for t in catalogo.tomas)
    # todo sale de la entrada: lo cortado a _procesados y lo que no es un video a _rechazados (no se reintenta)
    assert list(entrada.rglob("*.mp4")) == [] and servicio.sumar_entrada() == []
    assert len(list((servicio.cfg.tomas_dir / CARPETA_PROCESADOS).iterdir())) == 2
    assert [p.name.split("_", 2)[2] for p in (servicio.cfg.tomas_dir / CARPETA_RECHAZADOS).iterdir()] == ["roto.mp4"]
    assert tomas.escanear(servicio.cfg.tomas_dir) == list(catalogo.tomas)  # _entrada y _procesados no son tomas
    with pytest.raises(ValueError, match="falta el tema"):
        servicio.sumar_clip(entrada / "roto.mp4", "!!")


@con_ffmpeg
def test_un_clip_que_el_qwen_no_llego_a_mirar_queda_en_la_entrada(servicio, monkeypatch):  # noqa: F811
    monkeypatch.setattr(servicio, "con_llm", lambda fn, cuadro, tema: None)  # el Qwen no responde
    carpeta = servicio.cfg.tomas_dir / CARPETA_ENTRADA / "Persecucion"
    carpeta.mkdir(parents=True)
    clip = _video(carpeta / "tg_1.mp4", colores=("testsrc2", "smptebars"), segundos=(2.0, 2.0))
    resultado = servicio.sumar_archivo(clip, "Persecución")
    assert resultado.guardadas == 0 and resultado.sin_mirar == 2 and clip.exists()
    monkeypatch.setattr(servicio, "con_llm", lambda fn, cuadro, tema: {"usable": True, "sujeto": "ahora sí"})
    assert servicio.sumar_archivo(clip, "Persecución").guardadas == 2 and not clip.exists()


def test_un_album_de_videos_usa_el_texto_del_primero(entorno, monkeypatch):  # noqa: F811
    tg, servicio_, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    recibidos = []
    monkeypatch.setattr(bot, "_clip_a_biblioteca", lambda chat, texto, video: recibidos.append((texto, video["file_id"])))
    base = {"chat": {"id": 70, "type": "private"}, "from": {"id": 7}, "media_group_id": "g1"}
    bot._mensaje({**base, "video": {"file_id": "v1"}, "caption": "/biblioteca Persecución"})
    bot._mensaje({**base, "video": {"file_id": "v2"}})
    assert recibidos == [("/biblioteca Persecución", "v1"), ("/biblioteca Persecución", "v2")]
    bot._albumes["g1"]["t"] -= 10
    bot._vaciar_albumes()
    assert bot._albumes == {} and servicio_.almacen.listar() == []  # el album no se toma como un pedido de reel


def test_el_bot_pide_el_tema_para_un_video_y_muestra_las_tomas(entorno, tmp_path):  # noqa: F811
    tg, servicio_, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    mensaje = {"chat": {"id": 70, "type": "private"}, "from": {"id": 7}, "video": {"file_id": "v1"}}
    bot._mensaje({**mensaje, "caption": "mirá este clip"})
    assert "/biblioteca TEMA" in tg.enviados[-1][2] and servicio_.almacen.listar() == []  # no arma un reel
    _biblioteca(servicio_.cfg.tomas_dir, {"launch_trailer": [("Jason", ["jason"], 2.0, 1)] * 3})
    bot._comando(70, "/tomas")
    assert "Tomas de video real: 3 (6 s)" in tg.enviados[-1][2] and "launch trailer: 3" in tg.enviados[-1][2]


def test_el_plan_muestra_que_escenas_son_video_real():
    p = _proyecto()
    escenas = (Escena(**{**p.plan.escenas[0].__dict__, "medio": "toma", "motion_prompt": "",
                         "extras": ("extra_1_1.mp4", "extra_1_2.mp4")}), *p.plan.escenas[1:])
    texto = bot_mod.resumen_plan(Proyecto(**{**p.__dict__, "plan": Plan("T", escenas), "formato": "sabias"}))
    assert "🎥 video real del juego + 2 tomas de video real" in texto and "Formato: ¿Sabías que…?" in texto
    assert "con video real" in bot_mod.acuse(0, 30, False)
    assert "«Ranking»" in bot_mod.acuse(0, 30, False, False, "ranking")


# ---------- formatos ----------
@pytest.mark.parametrize("texto, esperado", [
    ("hacé un sabías que con esto", "sabias"), ("¿Sabías que…? de la nota", "sabias"),
    ("un ranking con las canciones", "ranking"), ("top 3 de autos", "ranking"), ("Top cinco", "ranking"),
    ("antes y ahora", "comparacion"), ("comparalo con GTA V", "comparacion"),
    ("que dure 30 segundos", ""), ("el topo sabe", ""),
])
def test_el_formato_se_pide_en_el_mensaje(texto, esperado):
    assert pedido.leer(texto).formato == esperado


def test_un_pedido_largo_es_una_nota_no_un_pedido_de_formato():
    assert pedido.leer("El ranking de ventas dice que " + "GTA VI vende mucho. " * 20).formato == ""


def test_el_guionista_recibe_las_instrucciones_del_formato():
    falso = LlmFalso([guion_json(GUION_OK), guion_json(GUION_OK)])
    vistas = ["un auto", "una playa", "un helicóptero"]
    guionista.escribir(falso, Fuente(texto="nota"), vistas, HOY, 15, formato="sabias")
    guionista.escribir(falso, Fuente(texto="nota"), vistas, HOY, 15)
    assert "FORMATO DE ESTE REEL" in falso.llamadas[0] and "¿Sabías que" in falso.llamadas[0]
    assert "FORMATO DE ESTE REEL" not in falso.llamadas[1]
    assert formatos.formato("INVENTADO").clave == "noticia" and formatos.formato(" Ranking ").clave == "ranking"


def test_a_lo_sumo_la_mitad_de_la_tanda_sale_en_otro_formato():
    assert formatos.repartir(["sabias", "ranking", "raro", "comparacion"], 2) == ["sabias", "ranking", "noticia",
                                                                                  "noticia"]
    assert formatos.repartir(["sabias", "ranking"], 0) == ["noticia", "noticia"]
    notas = [_nota(n) for n in range(1, 5)]
    respuesta = {"elegidas": [{"fuente": n, "titulo": f"Título {n}", "formato": f}
                              for n, f in ((1, "ranking"), (2, "sabias"), (3, "comparacion"), (4, "noticia"))]}
    falso = LlmFalso([respuesta, respuesta])
    zona = ZoneInfo(ZONA_PRUEBA)
    elegidas = automatico.elegir(falso, notas, [], HOY, 8, zona)
    assert [e.formato for e in elegidas] == ["ranking", "sabias", "noticia", "noticia"]
    assert '"sabias": un solo dato curioso' in falso.llamadas[0]  # el editor sabe que formatos hay
    assert {e.formato for e in automatico.elegir(falso, notas, [], HOY, 8, zona, con_formatos=False)} == {"noticia"}


def test_el_automatico_pide_cada_reel_con_su_formato_y_su_largo(entorno, monkeypatch):  # noqa: F811
    tg, servicio_, _, acceso, _ = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    notas = [_nota(1), _nota(2)]
    monkeypatch.setattr(automatico, "elegir", lambda *a, **k: [
        automatico.Elegida("Dato curioso", "x", (notas[0],), formato="sabias"),
        automatico.Elegida("Novedad", "x", (notas[1],))])
    pedidos = []

    def crear(chat, texto, fotos, segundos=None, formato="noticia", **k):
        pedidos.append((segundos, formato))
        return servicio_.almacen.guardar(_proyecto(chat=chat, id_=f"p{len(pedidos)}"))
    monkeypatch.setattr(servicio_, "crear", crear)
    monkeypatch.setattr(servicio_, "aprobar", lambda id_: None)
    automatico.Automatico(servicio_, acceso, buscar=lambda *a, **k: notas).ejecutar()
    assert pedidos == [(15, "sabias"), (30, "noticia")]
    textos = [t for _, _, t in tg.enviados]
    assert any("Dato curioso (Medio 1) · ¿Sabías que…?" in t for t in textos)
    assert any("6 clip(s) de IA" in t for t in textos)  # los clips de prueba no existen: todos pendientes
    assert ingesta.EXTENSIONES  # el modulo se importa donde corre el bot
