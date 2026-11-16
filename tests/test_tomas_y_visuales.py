"""Video real en los reels: la biblioteca de tomas, el corte de un video en tomas, los
clips que no usan GPU y el montaje con varias tomas por escena."""
import io
import json
import random
import subprocess

import pytest
from PIL import Image

from tests.conftest import hay_ffmpeg, imagen_bytes
from vstudio import ingesta, montaje, tomas, visuales
from vstudio.biblioteca import Usos

con_ffmpeg = pytest.mark.skipif(not hay_ffmpeg(), reason="hace falta ffmpeg")


PATRONES = ("testsrc2", "smptebars", "rgbtestsrc", "pal75bars")  # con dibujo: su huella no es la de un color liso


def _video(destino, colores=("red", "blue", "green"), segundos=(2.0, 2.0, 2.0), tamano="640x360"):
    """Un video con un plano por color (o por patron de prueba): ffmpeg detecta el cambio entre uno y otro."""
    entradas, etiquetas = [], ""
    for i, (color, dura) in enumerate(zip(colores, segundos)):
        fuente = f"{color}=s={tamano}:r=30:d={dura}" if color in PATRONES else f"color=c={color}:s={tamano}:r=30:d={dura}"
        entradas += ["-f", "lavfi", "-i", fuente]
        etiquetas += f"[{i}:v]"
    subprocess.run(["ffmpeg", "-v", "error", "-y", *entradas, "-filter_complex",
                    f"{etiquetas}concat=n={len(colores)}:v=1:a=0", "-pix_fmt", "yuv420p", str(destino)], check=True)
    return destino


def cuadro_distinto(semilla: int, tamano: tuple[int, int] = (1280, 720)) -> bytes:
    """Un cuadro 16:9 con manchas al azar: la huella de cada uno es distinta (las imagenes lisas se dan por
    repetidas)."""
    azar = random.Random(semilla)
    img = Image.new("RGB", tamano)
    paso_x, paso_y = tamano[0] // 8, tamano[1] // 8
    for x in range(8):
        for y in range(8):
            img.paste(tuple(azar.randrange(256) for _ in range(3)),
                      (x * paso_x, y * paso_y, (x + 1) * paso_x, (y + 1) * paso_y))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _biblioteca(raiz, tomas_por_fuente):
    """{fuente: [(sujeto, etiquetas, segundos, energia)]} -> carpeta con indices y archivos vacios."""
    for fuente, lista in tomas_por_fuente.items():
        carpeta = raiz / fuente
        carpeta.mkdir(parents=True)
        indice = []
        for i, (sujeto, etiquetas, segundos, energia) in enumerate(lista, start=1):
            archivo = f"toma_{i:04d}.mp4"
            (carpeta / archivo).write_bytes(b"video")
            (carpeta / archivo).with_suffix(".jpg").write_bytes(cuadro_distinto(len(fuente) * 100 + i))
            indice.append({"archivo": archivo, "segundos": segundos, "sujeto": sujeto, "etiquetas": etiquetas,
                           "energia": energia, "centro_x": 0.4})
        tomas.guardar_indice(carpeta, indice)
    return raiz


# ---------- la biblioteca ----------
def test_escanea_las_tomas_y_saltea_lo_que_no_es(tmp_path):
    raiz = _biblioteca(tmp_path / "tomas", {"launch_trailer": [("Jason corre", ["jason"], 2.4, 3)],
                                             "_originales": [("no es una toma", [], 90.0, 0)]})
    indice = tomas.leer_indice(raiz / "launch_trailer")
    tomas.guardar_indice(raiz / "launch_trailer", [*indice, {"archivo": "no_existe.mp4", "segundos": 2},
                                                   {"sin": "archivo"}])
    todas = tomas.escanear(raiz)
    assert [t.relativa for t in todas] == ["launch_trailer/toma_0001.mp4"]
    assert todas[0].fuente == "launch_trailer" and todas[0].energia == 3 and todas[0].centro_x == 0.4
    assert tomas.escanear(tmp_path / "no_hay") == [] and tomas.escanear(None) == []
    # un registro raro (un numero infinito, una ruta en vez de un nombre) se saltea: no frena los planes
    tomas.guardar_indice(raiz / "launch_trailer", [*indice, {"archivo": "toma_0001.mp4", "segundos": 1e999},
                                                   {"archivo": "../launch_trailer/toma_0001.mp4", "segundos": 2},
                                                   {"archivo": "toma_0001.mp4", "segundos": 2, "energia": float("inf")}])
    assert [t.relativa for t in tomas.escanear(raiz)] == ["launch_trailer/toma_0001.mp4"]
    assert tomas.resumen(raiz) == {"launch_trailer": (1, 2.4)}


def test_ofrece_primero_lo_que_la_nota_nombra_y_reparte_entre_videos(tmp_path):
    raiz = _biblioteca(tmp_path / "t", {
        "trailer_1": [("Lucía maneja una lancha", ["lucia", "lancha"], 2.0, 2)] + [
            (f"paisaje {i}", ["paisaje"], 2.0, 0) for i in range(5)],
        "trailer_2": [("Persecución con la policía", ["persecucion", "policia"], 2.5, 3)]})
    todas = tomas.escanear(raiz)
    elegidas = tomas.elegir(todas, "Rockstar mostró la lancha de Lucía", 5, {}, random.Random(1))
    assert elegidas[0].sujeto == "Lucía maneja una lancha"  # lo que la nota nombra
    assert elegidas[1].sujeto == "Persecución con la policía"  # despues, la de mas accion
    assert sum(t.fuente == "trailer_1" for t in elegidas) == tomas.MAX_POR_FUENTE  # no todas del mismo video
    usada = {elegidas[1].relativa: 100.0}
    assert tomas.elegir(todas, "sin nombres", 1, usada, random.Random(1))[0].relativa != elegidas[1].relativa


def test_las_tomas_que_acompanan_no_se_repiten_y_cubren_la_escena(tmp_path):
    raiz = _biblioteca(tmp_path / "t", {"trailer": [("Jason dispara", ["jason", "disparos"], 1.5, 3),
                                                     ("Autos en la autopista", ["autos"], 2.0, 2),
                                                     ("Playa de Vice City", ["playa"], 6.0, 0)]})
    todas = tomas.escanear(raiz)
    extras = tomas.acompanar(todas, "Los disparos de Jason", {todas[1].relativa}, {}, faltan_s=4.0, maximo=3,
                             azar=random.Random(2))
    assert [t.sujeto for t in extras] == ["Jason dispara", "Playa de Vice City"]  # la excluida no vuelve
    assert tomas.acompanar(todas, "x", set(), {}, faltan_s=0.0, maximo=3) == []
    assert len(tomas.acompanar(todas, "x", set(), {}, faltan_s=99.0, maximo=1)) == 1


def test_el_proyecto_recuerda_que_toma_es_cada_imagen(tmp_path):
    raiz = _biblioteca(tmp_path / "t", {"trailer": [("Jason", ["jason"], 2.0, 1)]})
    todas = tomas.escanear(raiz)
    tomas.anotar_en_proyecto(tmp_path, {"toma_3.jpg": todas[0].relativa, "toma_4.jpg": "borrada/toma_0009.mp4"})
    assert tomas.del_proyecto(tmp_path, todas) == {"toma_3.jpg": todas[0]}
    assert tomas.del_proyecto(tmp_path / "otro", todas) == {}
    catalogo = tomas.Catalogo(tuple(todas), Usos(tmp_path / "usos.json"))
    catalogo.marcar([todas[0].relativa])
    assert list(catalogo.usadas()) == [todas[0].relativa] and tomas.Catalogo().usadas() == {}


# ---------- cortar un video en tomas ----------
def test_tramos_sin_las_cortas_y_con_las_largas_partidas():
    tramos = ingesta.tramos([1.0, 4.0, 4.5, 14.5], 16.0)
    # 0-1 (corta, afuera), 1-4 (entra), 4-4.5 (corta), 4.5-14.5 (10 s: 4 trozos parejos), 14.5-16 (entra)
    assert len(tramos) == 6 and tramos[0] == pytest.approx((1.07, 3.93))
    largos = [fin - ini for ini, fin in tramos[1:5]]
    assert all(largo == pytest.approx(2.465) for largo in largos)
    assert all(ingesta.MIN_TOMA_S <= fin - ini <= ingesta.MAX_TOMA_S for ini, fin in tramos)
    assert ingesta.tramos([], 0.5) == []
    assert ingesta.tramos([], 1.0) == [(0.0, 1.0)]  # un bucle corto de un solo plano entra entero
    assert ingesta.tramos([], 3.0) == [(0.0, 3.0)]  # el principio y el final del archivo no son transiciones


def test_anotacion_descarta_lo_que_no_sirve_y_acota_los_valores():
    assert ingesta.anotacion({"usable": False, "sujeto": "logo de Rockstar"}, "t.mp4", 2.0) is None
    assert ingesta.anotacion({"usable": True, "sujeto": " "}, "t.mp4", 2.0) is None
    a = ingesta.anotacion({"usable": True, "sujeto": "Lucía  corre", "etiquetas": ["Lucía", "Persecución", "lucia"],
                           "energia": 9, "centro_x": "raro"}, "t.mp4", 2.345, tema="Swat Policía")
    assert a == {"archivo": "t.mp4", "segundos": 2.35, "sujeto": "Lucía corre",
                 "etiquetas": ["swat policia", "lucia", "persecucion"], "energia": 3, "centro_x": 0.5}
    assert ingesta.anotacion({"usable": "false", "sujeto": "un logo"}, "t.mp4", 1.0) is None  # no es true
    raro = ingesta.anotacion({"usable": True, "sujeto": "x", "energia": float("nan"), "centro_x": float("inf"),
                              "etiquetas": [None, 7, "auto"]}, "t.mp4", 1.0)
    assert (raro["energia"], raro["centro_x"], raro["etiquetas"]) == (1, 0.5, ["auto"])
    assert ingesta.nombre_de_fuente("Launch Trailer (4K)") == "launch_trailer_4k"


@con_ffmpeg
def test_ingresar_corta_guarda_y_descarta(tmp_path):
    video = _video(tmp_path / "v.mp4", colores=("testsrc2", "black", "smptebars"), segundos=(2.0, 2.0, 2.0))
    vistas = []

    def mirar(cuadro):
        vistas.append(cuadro.name)
        return {"usable": len(vistas) == 1, "sujeto": "plano blanco", "etiquetas": ["blanco"], "energia": 2}
    carpeta = tmp_path / "tomas" / "prueba"
    r = ingesta.ingresar(video, carpeta, mirar, tema="Prueba")
    # el negro no se le muestra al Qwen; de los otros dos, uno "no sirve"
    assert (r.guardadas, r.descartadas, len(vistas)) == (1, 2, 2) and r.segundos == pytest.approx(1.86, abs=0.1)
    (toma,) = tomas.escanear(tmp_path / "tomas")
    assert toma.sujeto == "plano blanco" and toma.etiquetas == ("prueba", "blanco") and toma.cuadro.exists()
    assert visuales.duracion(toma.ruta) == pytest.approx(1.86, abs=0.1)
    assert sorted(p.name for p in carpeta.iterdir()) == ["indice.json", "toma_0001.jpg", "toma_0001.mp4", "vistos.json"]
    # repetir el mismo video no duplica ni vuelve a preguntar: lo que entro y lo que no, ya esta anotado
    def nunca(cuadro):
        raise AssertionError("ya se habia mirado")
    otra = ingesta.ingresar(video, carpeta, nunca)
    indice = json.loads((carpeta / "indice.json").read_text(encoding="utf-8"))
    assert (otra.guardadas, otra.descartadas, otra.sin_mirar) == (0, 0, 0) and len(indice) == 1
    assert indice[0]["origen"] == "v.mp4" and len(json.loads((carpeta / "vistos.json").read_text())) == 2
    # un indice de la version anterior (el primer tramo empezaba 0,07 s despues, sin huella) tampoco se duplica
    viejo = [{k: v for k, v in indice[0].items() if k != "huella"} | {"inicio": 0.07}]
    tomas.guardar_indice(carpeta, viejo)
    assert ingesta.ingresar(video, carpeta, nunca).guardadas == 0
    # si el video da mas tomas que el tope, entran repartidas a lo largo (no las primeras)
    assert ingesta.tramos([], 30.0, maximo=3) == [(0.0, 3.0), (12.0, 15.0), (27.0, 30.0)]


@con_ffmpeg
def test_si_el_qwen_no_responde_no_se_descarta_nada(tmp_path):
    """Antes "no pude mirar" contaba como "no sirve": el clip quedaba como procesado con 0 tomas."""
    video = _video(tmp_path / "v.mp4", colores=("smptebars", "rgbtestsrc") * 3, segundos=(1.5,) * 6)
    miradas = []

    def caido(cuadro):
        miradas.append(cuadro.name)
        return None
    r = ingesta.ingresar(video, tmp_path / "t", caido)
    assert (r.guardadas, r.descartadas) == (0, 0) and r.sin_mirar == 6
    assert len(miradas) == ingesta.MAX_SIN_MIRAR_SEGUIDAS  # no sigue cortando si el Qwen no esta
    assert list((tmp_path / "t").glob("toma_*")) == []
    despues = ingesta.ingresar(video, tmp_path / "t", lambda c: {"usable": True, "sujeto": "ya responde"})
    # los seis planos son dos imagenes que se alternan: entra una toma de cada una
    assert (despues.guardadas, despues.descartadas, despues.sin_mirar) == (2, 4, 0)


@con_ffmpeg
def test_un_plano_quieto_y_largo_deja_una_sola_toma_y_un_bucle_corto_entra_entero(tmp_path):
    """La portada animada oficial dura 32 s y casi no cambia: partida en trozos daba diez tomas iguales. Los
    clips de personajes duran 1 s: antes quedaban afuera por cortos."""
    quieto = _video(tmp_path / "portada.mp4", colores=("smptebars",), segundos=(9.0,))
    miradas = []

    def mirar(cuadro):
        miradas.append(cuadro.name)
        return {"usable": True, "sujeto": "la portada"}
    r = ingesta.ingresar(quieto, tmp_path / "t" / "portada", mirar)
    assert (r.guardadas, r.descartadas, len(miradas)) == (1, 2, 1)  # las repetidas no se le muestran al Qwen
    # dos planos de un solo color no son "la misma toma": la huella de un cuadro liso no dice nada
    lisos = _video(tmp_path / "lisos.mp4", colores=("white", "blue", "white"), segundos=(2.0, 2.0, 2.0))
    assert ingesta.ingresar(lisos, tmp_path / "t" / "lisos", mirar).guardadas == 3
    assert not ingesta.repetida(2**64 - 1, [2**64 - 1]) and ingesta.repetida(0b1011, [0b1000])
    assert not ingesta.repetida(0b1011, [0b1000, 1 << 40, 1 << 41, 1 << 42])  # solo contra las ultimas
    bucle = _video(tmp_path / "jason.mp4", colores=("testsrc2",), segundos=(1.0,))
    r = ingesta.ingresar(bucle, tmp_path / "t" / "jason", mirar, tema="Jason Duval")
    (toma,) = [t for t in tomas.escanear(tmp_path / "t") if t.fuente == "jason"]
    assert r.guardadas == 1 and toma.segundos == pytest.approx(1.0, abs=0.05) and "jason duval" in toma.etiquetas


@con_ffmpeg
def test_solo_se_cortan_videos_de_verdad_y_de_tamano_razonable(tmp_path):
    lista = tmp_path / "lista.mp4"  # una lista de reproduccion con nombre de video
    lista.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n/etc/passwd\n#EXT-X-ENDLIST\n")
    with pytest.raises(ingesta.IngestaError, match="no parece un video"):
        ingesta.sondear(lista)
    largo = _video(tmp_path / "largo.mp4", colores=("white",), segundos=(3.0,))
    assert ingesta.sondear(largo) == (pytest.approx(3.0, abs=0.1), 640, 360)
    with pytest.raises(ingesta.IngestaError, match="máximo 0 min"):
        ingesta.sondear(largo, max_segundos=2.0)
    ancho = _video(tmp_path / "ancho.mp4", colores=("white",), segundos=(1.0,), tamano="4200x64")
    with pytest.raises(ingesta.IngestaError, match="4200x64"):
        ingesta.sondear(ancho)
    assert len(ingesta.tramos([], 30.0, maximo=4)) == 4  # tope de tomas por video


@con_ffmpeg
def test_las_franjas_negras_de_un_trailer_no_llegan_a_la_toma(tmp_path):
    """Los trailers vienen en formato de cine dentro de un cuadro 16:9: recortada en vertical, la toma salia con
    una banda negra arriba y otra abajo."""
    cine = tmp_path / "cine.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x268:r=30:d=2", "-vf",
                    "pad=640:360:0:46:black", "-pix_fmt", "yuv420p", str(cine)], check=True)
    assert ingesta.franjas(cine, 0.0, 2.0, 640, 360) == "crop=640:268:0:46,"
    r = ingesta.ingresar(cine, tmp_path / "t", lambda c: {"usable": True, "sujeto": "una toma de cine"})
    (toma,) = tomas.escanear(tmp_path)
    assert r.guardadas == 1 and _tamano(toma.ruta) == (640, 268)
    with Image.open(toma.cuadro) as cuadro:
        assert cuadro.size == (640, 268)
    sin_franjas = _video(tmp_path / "pleno.mp4", colores=("white",), segundos=(2.0,))
    assert ingesta.franjas(sin_franjas, 0.0, 2.0, 640, 360) == ""
    negro = _video(tmp_path / "negro.mp4", colores=("black",), segundos=(2.0,))
    assert ingesta.franjas(negro, 0.0, 2.0, 640, 360) == ""  # todo oscuro: no se recorta (se descarta despues)


@con_ffmpeg
def test_un_archivo_que_no_es_video_avisa(tmp_path):
    (tmp_path / "nota.mp4").write_text("esto no es un video")
    with pytest.raises(ingesta.IngestaError, match="no parece un video"):
        ingesta.ingresar(tmp_path / "nota.mp4", tmp_path / "t", lambda c: None)


# ---------- clips sin GPU ----------
def test_cada_clip_ocupa_su_parte_con_tope():
    assert visuales.repartir([5.0, 2.0], 5.0) == pytest.approx([3.0, 2.0])  # el clip largo no se queda con todo
    assert visuales.repartir([2.0, 2.0, 2.0], 5.4) == pytest.approx([1.8, 1.8, 1.8])
    with pytest.raises(visuales.VisualError):
        visuales.repartir([0.0], 5.0)


def _tamano(ruta):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(ruta)], capture_output=True, text=True, check=True)
    return tuple(int(v) for v in out.stdout.strip().split(","))


@con_ffmpeg
def test_recorta_una_toma_en_vertical_y_anima_una_foto(tmp_path):
    toma = _video(tmp_path / "toma.mp4", colores=("red",), segundos=(2.0,), tamano="1280x720")
    clip = visuales.recortar_toma(toma, tmp_path / "clip.mp4", (0.25, 0.0, 0.25 + 9 / 16 * 720 / 1280, 1.0))
    assert _tamano(clip) == (1080, 1920) and visuales.duracion(clip) == pytest.approx(2.0, abs=0.1)
    (tmp_path / "foto.png").write_bytes(imagen_bytes(1080, 1920, formato="PNG"))
    paneo = visuales.foto_animada(tmp_path / "foto.png", tmp_path / "paneo.mp4", 3.0)
    assert _tamano(paneo) == (1080, 1920) and visuales.duracion(paneo) == pytest.approx(3.0, abs=0.1)
    with pytest.raises(visuales.VisualError):
        visuales.recortar_toma(tmp_path / "no_existe.mp4", tmp_path / "x.mp4", (0, 0, 1, 1))


@con_ffmpeg
def test_une_los_clips_de_una_escena_al_largo_pedido(tmp_path):
    clips = [_video(tmp_path / f"c{i}.mp4", colores=(color,), segundos=(dura,), tamano="360x640")
             for i, (color, dura) in enumerate((("red", 5.0), ("blue", 2.0), ("green", 1.5)))]
    cambios = visuales.componer(clips, 6.5, tmp_path / "visual.mp4")
    assert cambios == pytest.approx([3.0, 5.0])  # 3 + 2 + 1,5 s: cada uno su parte
    assert visuales.duracion(tmp_path / "visual.mp4") == pytest.approx(6.5, abs=0.1)
    # la escena salio corta (la voz dijo poco): la toma que quedaria como un parpadeo no entra
    assert visuales.componer(clips, 3.0, tmp_path / "corta.mp4") == pytest.approx([1.8])
    # tomas cortas en una escena larga: se repite la serie en vez de congelar el ultimo cuadro
    assert visuales.componer(clips[1:], 9.0, tmp_path / "larga.mp4") == pytest.approx([2.571, 4.5, 7.071], abs=0.01)
    assert visuales.duracion(tmp_path / "larga.mp4") == pytest.approx(9.0, abs=0.1)


def test_la_serie_de_clips_se_ajusta_al_largo_real_de_la_escena(tmp_path):
    a, b, c = tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "c.mp4"
    assert visuales.secuencia([a, b, c], [5.0, 2.0, 2.0], 6.0) == [a, b, c]
    assert visuales.secuencia([a, b, c], [5.0, 2.0, 2.0], 2.2) == [a]  # 0,7 s por clip: un parpadeo
    assert visuales.secuencia([b], [1.5], 5.0) == [b, b, b]  # 1,5 s x 1,35 no llega a 5 s
    assert visuales.secuencia([a], [5.0], 6.5) == [a]  # un clip de IA se estira, no se repite
    # antes de pasar a camara lenta, un clip largo muestra mas de si
    assert visuales.repartir([5.0, 2.0], 6.0) == pytest.approx([4.0, 2.0])
    with pytest.raises(visuales.VisualError):
        visuales.repartir([0.0, 2.0], 5.0)
    with pytest.raises(visuales.VisualError):
        visuales.duracion(tmp_path / "no_existe.mp4")


# ---------- montaje ----------
def test_una_escena_con_tomas_extra_tiene_mas_para_mostrar():
    assert montaje.segundos_de_imagen(5.06, []) == 5.06
    assert montaje.segundos_de_imagen(5.06, [2.0, 1.5]) == pytest.approx(6.5)  # 3 (tope) + 2 + 1,5


@con_ffmpeg
def test_arma_un_reel_con_tomas_extra(tmp_path, fuentes_dir):
    rojo = _video(tmp_path / "clip_1.mp4", colores=("red",), segundos=(5.0,), tamano="720x1280")
    azul = _video(tmp_path / "extra_1_1.mp4", colores=("blue",), segundos=(2.0,), tamano="1080x1920")
    verde = _video(tmp_path / "clip_2.mp4", colores=("green",), segundos=(5.0,), tamano="720x1280")
    escenas = [montaje.EscenaMontaje(clip=rojo, texto="UNO", extras=(azul,)),
               montaje.EscenaMontaje(clip=verde, texto="DOS")]
    info = montaje.armar_reel(escenas, "TITULAR", tmp_path, tmp_path / "reel.mp4", fuentes_dir, cortes_rapidos=True,
                              efectos_sonido=True)
    # sin voz: la escena 1 dura lo que tiene para mostrar (3 + 2) y la 2, su clip; menos el fundido
    assert info["duracion_s"] == pytest.approx(5.0 + 5.0 - montaje.FUNDIDO_S, abs=0.2)
    assert (tmp_path / "visual_1.mp4").exists() and not (tmp_path / "visual_2.mp4").exists()
    assert visuales.duracion(tmp_path / "visual_1.mp4") == pytest.approx(5.0, abs=0.1)
    # a los 4 s del reel se ve la toma extra (azul), no el clip rojo
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "4.0", "-i", str(tmp_path / "reel.mp4"), "-frames:v", "1",
                    "-vf", "crop=200:200:440:800", str(tmp_path / "cuadro.png")], check=True)
    with Image.open(tmp_path / "cuadro.png") as cuadro:
        r, g, b = cuadro.convert("RGB").resize((1, 1)).getpixel((0, 0))
    assert b > r + 80 and b > g + 80  # azul: la toma extra


@con_ffmpeg
def test_una_toma_corta_sola_se_repite_en_vez_de_congelarse(tmp_path, fuentes_dir):
    """Una toma de 1,5 s en una escena de 5 (la voz manda) quedaba 3 s quieta en su ultimo cuadro."""
    toma = _video(tmp_path / "clip_1.mp4", colores=("red",), segundos=(1.5,), tamano="1080x1920")
    ia = _video(tmp_path / "clip_2.mp4", colores=("green",), segundos=(5.0,), tamano="720x1280")
    voces = []
    for i in (1, 2):
        voces.append(tmp_path / f"voz_{i}.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=300:duration=4",
                        str(voces[-1])], check=True)
    escenas = [montaje.EscenaMontaje(clip=toma, texto="UNO", audio=voces[0]),
               montaje.EscenaMontaje(clip=ia, texto="DOS", audio=voces[1])]
    montaje.armar_reel(escenas, "TITULAR", tmp_path, tmp_path / "reel.mp4", fuentes_dir, cortes_rapidos=True)
    assert visuales.duracion(tmp_path / "visual_1.mp4") == pytest.approx(4.65, abs=0.15)  # voz + pausa
    assert not (tmp_path / "visual_2.mp4").exists()  # el clip de IA alcanza: sigue como antes
