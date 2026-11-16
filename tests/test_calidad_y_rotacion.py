"""Texto para publicar solo, "otras imágenes" sin rehacer el guion, "solo un video",
ortografia, verificacion de datos y fotos del editor en la biblioteca."""
from dataclasses import replace
from datetime import date

import pytest

from tests.conftest import imagen_bytes
from tests.test_almacen_servicio_acceso import _proyecto
from tests.test_bot_trabajador import entorno  # noqa: F401 - fixture
from tests.test_planificador_llm import LlmFalso
from vstudio import biblioteca, ortografia, pedido, verificador
from vstudio import trabajador as trab_mod
from vstudio.llm import LlmError
from vstudio.modelos import Estado

HOY = date(2026, 11, 12)

PALABRAS = ["descargas", "anticipadas", "ya", "disponibles", "comentar", "contar", "seguir", "segur", "mañana",
            "sale", "precio"]


@pytest.fixture
def diccionario(tmp_path, monkeypatch):
    """Un diccionario hunspell minimo (formato .aff/.dic real) con las palabras de PALABRAS."""
    base = tmp_path / "es_ES"
    base.with_suffix(".aff").write_text("SET UTF-8\n", encoding="utf-8")
    base.with_suffix(".dic").write_text(f"{len(PALABRAS)}\n" + "\n".join(PALABRAS) + "\n", encoding="utf-8")
    monkeypatch.setattr(ortografia, "_DICCIONARIO", base)
    return base


# ---------- entrega ----------
def test_el_texto_para_publicar_va_solo_y_al_final(entorno, monkeypatch):  # noqa: F811
    tg, servicio, trabajador, _, _ = entorno
    carpeta = servicio.cfg.proyectos / "abc"
    carpeta.mkdir(parents=True)
    p = _proyecto(estado=Estado.GENERANDO, chat=70)
    for e in p.plan.escenas:
        (carpeta / e.clip).write_bytes(b"mp4")
    servicio.almacen.guardar(p.con(plan=replace(p.plan, texto_post="Se viene GTA VI. ¿Lo precargás? #GTA6",
                                                advertencia="la fecha es de una filtración")))
    monkeypatch.setattr(trab_mod.montaje, "armar_reel", lambda *a, **k: {"duracion_s": 30.0, "bytes": 4})
    trabajador._montar_y_entregar("abc")
    tipos = [(tipo, texto) for tipo, _, texto in tg.enviados]
    assert tipos[0][0] == "video" and "abc" in tipos[0][1]
    assert tipos[1] == ("mensaje", "⚠️ abc · revisar antes de publicar: la fecha es de una filtración")
    assert tipos[-1] == ("mensaje", "Se viene GTA VI. ¿Lo precargás? #GTA6")  # nada mas: se copia entero


# ---------- "solo un video" ----------
@pytest.mark.parametrize("texto", ["solo un video", "Quiero solo una animación con esto", "sólo el video"])
def test_solo_un_video_es_solo_animacion(texto):
    assert pedido.leer(texto).solo_animacion


# ---------- ortografia ----------
def test_marca_lo_mal_escrito_y_no_el_voseo(diccionario):
    assert ortografia.dudosas(["DESCARGAS ANTECIDAS YA DISPONIBLES"]) == ["ANTECIDAS"]  # el titular del 10/11
    assert ortografia.dudosas(["Comentá, contanos y seguinos"]) == []  # voseo: infinitivo sin la r
    assert ortografia.dudosas(["SEGUNOS"]) == ["SEGUNOS"]  # "segur" existe, pero "segu" no es un verbo
    assert ortografia.dudosas(["Mañana sale GTA VI con joinSession #CuentaRegresiva"]) == []  # juego, código, hashtag
    assert ortografia.dudosas(["El precio de Leonida"], conocidas="Leonida") == []  # lo que nombra la nota


def test_sin_diccionario_no_frena_nada(monkeypatch, tmp_path):
    monkeypatch.setattr(ortografia, "_DICCIONARIO", tmp_path / "no_existe")
    assert ortografia.dudosas(["cualquier cosa mal escrita: antecidas"]) == []


def test_el_guionista_corrige_la_ortografia(diccionario):
    from datetime import date

    from tests.test_planificador_llm import guion_json
    from vstudio import guionista
    from vstudio.modelos import Fuente
    narraciones = ["Doscientos doce gigas: eso es lo que te va a pedir GTA seis.",
                   "Y lo más fuerte no es el tamaño, es la fecha en que arranca la precarga.",
                   "¿Tú lo vas a precargar el primer día o esperas las reseñas?"]
    malo, bueno = guion_json(narraciones), guion_json(narraciones)
    malo["titular"] = "DESCARGAS ANTECIDAS"
    falso = LlmFalso([malo, bueno])
    # el diccionario de prueba es chico: el resto de las palabras van como "del material"
    material = " ".join(narraciones + [bueno["titular"], bueno["texto_post"]] + [e["texto"] for e in bueno["escenas"]])
    g = guionista.escribir(falso, Fuente(nota_usuario=material), ["a", "b", "c"], date(2026, 11, 11), 15)
    assert "ANTECIDAS" in falso.partes[1]  # el segundo intento lleva la correccion
    assert g.titular == bueno["titular"] and not g.problemas


# ---------- verificacion de datos ----------
def test_verificador_lista_lo_que_no_tiene_respaldo():
    falso = LlmFalso([{"sin_respaldo": ["el motel Starlet — la nota no lo nombra", "  ", "x" * 300]}])
    dudas = verificador.verificar(falso, _proyecto().fuente, _proyecto().plan, HOY)
    assert dudas[0] == "el motel Starlet — la nota no lo nombra" and len(dudas) == 2 and len(dudas[1]) == 160
    assert "GUION:" in falso.partes[0] and "MATERIAL:" in falso.partes[0]


@pytest.mark.parametrize("respuesta", [LlmError("sin respuesta"), {"otra": "cosa"}, IndexError("raro")])
def test_verificador_que_falla_no_frena_pero_queda_anotado(respuesta):
    """El plan sigue, pero "no se pudo verificar" no es "todo bien": ese reel no sale solo a Facebook."""
    assert verificador.verificar(LlmFalso([respuesta]), _proyecto().fuente, _proyecto().plan, HOY) is None
    adv = verificador.con_dudas("nota vieja", None)
    assert adv == "No se pudo verificar los datos del guion. | nota vieja"
    assert verificador.con_dudas(adv, []) == "nota vieja" and verificador.dudas_de(adv) == ()


def test_las_dudas_van_primero_y_no_se_acumulan():
    adv = verificador.con_dudas("nota vieja | Guion para revisar: muletilla", ["kayak — no está"])
    assert adv == "Datos que la nota no respalda (revisalos): kayak — no está | nota vieja | Guion para revisar: muletilla"
    otra = verificador.con_dudas(adv, ["Starlet — no está"])
    assert otra.count("Datos que la nota") == 1 and "kayak" not in otra and otra.endswith("muletilla")
    assert verificador.con_dudas(adv, []) == "nota vieja | Guion para revisar: muletilla"


# ---------- biblioteca del editor ----------
def test_suma_fotos_del_editor_a_la_rotacion(tmp_path):
    raiz = tmp_path / "biblioteca"
    primera = biblioteca.agregar(raiz, "swat (policía)", imagen_bytes(1350, 900, color=(10, 200, 30)))
    assert primera == raiz / "editor" / "Swat_Policía" / "Swat_Policía_01.jpg" and primera.exists()
    assert biblioteca.agregar(raiz, "Swat Policía", imagen_bytes(1350, 900, color=(10, 200, 30))) is None  # repetida
    with pytest.raises(ValueError, match="falta el tema"):
        biblioteca.agregar(raiz, "  !!  ", imagen_bytes(1350, 900))
    assert biblioteca.temas_del_editor(raiz) == {"Swat Policía": 1}
    imagen = biblioteca.escanear(raiz)[0]
    assert imagen.etiqueta == "Swat Policía" and biblioteca.tema(imagen) == "swat policia"
    assert biblioteca.elegir([imagen], "llegó el SWAT policía", 1, {}) == [imagen]  # la nota lo nombra


def test_nombre_de_tema_no_sale_de_la_carpeta():
    assert biblioteca.nombre_de_tema("../../etc/passwd") == "Etc Passwd"
    assert biblioteca.nombre_de_tema("x" * 80) == "X" + "x" * 39


def test_bot_guarda_las_fotos_con_biblioteca(entorno, tmp_path):  # noqa: F811
    tg, servicio, _, acceso, bot = entorno
    acceso.vincular(7, acceso.nuevo_codigo())
    servicio.cfg = replace(servicio.cfg, biblioteca_dir=tmp_path / "biblioteca")
    tg.bajar_archivo = lambda file_id: imagen_bytes(1350, 900, color=(200, 10, 10))
    bot._manejar({"message": {"chat": {"id": 70, "type": "private"}, "from": {"id": 7}, "caption": "/biblioteca SWAT",
                              "photo": [{"file_id": "f1", "file_size": 10}]}})
    assert biblioteca.temas_del_editor(tmp_path / "biblioteca") == {"Swat": 1}
    assert "«Swat»: guardé 1 foto" in tg.enviados[-1][2]
    assert servicio.almacen.listar() == []  # no armo un reel con la foto


def test_nombre_del_medio_para_el_sello():
    from vstudio.fuentes import nombre_del_medio
    from vstudio.modelos import Fuente
    assert nombre_del_medio(Fuente(medio="Xataka México on MSN")) == "Xataka México"
    assert nombre_del_medio(Fuente(medio="Rockstar Games (oficial)")) == "Rockstar Games"
    assert nombre_del_medio(Fuente(url="https://www.ign.com/articles/x")) == "IGN"
    assert nombre_del_medio(Fuente(url="https://m.elinformador.com.mx/nota")) == "elinformador.com.mx"
    assert nombre_del_medio(Fuente(nota_usuario="texto pegado")) == ""
