import io
import shutil
import socket
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from vstudio import ortografia
from vstudio.config import Config

ZONA_PRUEBA = "UTC"  # fija: las horas de los tests no dependen de la zona del servidor


def imagen_bytes(w: int, h: int, color=(200, 50, 50), formato="JPEG") -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (w, h), color)
    # mitad izquierda distinta para que los recortes y las huellas se puedan verificar
    img.paste((20, 20, 220), (0, 0, w // 2, h))
    img.save(buf, format=formato)
    return buf.getvalue()


def resolver_falso(mapa: dict[str, str]):
    def resolver(host, puerto, type=0):
        if host not in mapa:
            raise socket.gaierror("no existe")
        familia = socket.AF_INET6 if ":" in mapa[host] else socket.AF_INET
        return [(familia, socket.SOCK_STREAM, 6, "", (mapa[host], puerto))]
    return resolver


def _fuente_sistema() -> Path | None:
    candidatas = [Path("C:/Windows/Fonts/arial.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                  Path.home() / "video-studio/assets/fonts/Anton-Regular.ttf"]
    return next((c for c in candidatas if c.exists()), None)


@pytest.fixture
def fuentes_dir(tmp_path) -> Path:
    origen = _fuente_sistema()
    if origen is None:
        pytest.skip("no hay una fuente TTF en el sistema")
    carpeta = tmp_path / "fonts"
    carpeta.mkdir()
    for nombre in ("Anton-Regular.ttf", "Montserrat[wght].ttf"):
        shutil.copy(origen, carpeta / nombre)
    return carpeta


@pytest.fixture
def cfg(tmp_path, fuentes_dir) -> Config:
    base = Config()
    return replace(base, datos=tmp_path / "data", proyectos=tmp_path / "proyectos", zona_horaria=ZONA_PRUEBA,
                   diccionario=tmp_path / "sin_diccionario" / "es_ES",  # en el servidor el de verdad esta en la ruta
                   # en el servidor existen la biblioteca de tomas y el buscador: los tests no los tocan
                   tomas_dir=tmp_path / "tomas", fotos_de_la_noticia=False,
                   montaje=replace(base.montaje, fuentes_dir=fuentes_dir, musica_dir=tmp_path / "musica"),
                   voz=replace(base.voz, motor="ninguna", voces_dir=tmp_path / "voz"),
                   gpu=replace(base.gpu, sesiones_app=tmp_path / "sesiones"),
                   # los clips de prueba no son videos; y en el servidor el modelo de interpolacion esta bajado: que
                   # los tests den lo mismo en las dos maquinas
                   video=replace(base.video, revisar_deformes=False, interpolar=False),
                   musica=replace(base.musica, activo=False))  # en el servidor ACE-Step existe: aca no corre


@pytest.fixture(autouse=True)
def sin_ortografia(monkeypatch):
    """La ortografia es global del modulo (la fija el Servicio): cada test arranca sin diccionario, y los que la
    prueban usan uno chico propio. Si no, en el servidor (donde el diccionario real esta en la ruta por defecto) los
    guiones de prueba se "corrigen" y los LLM falsos se quedan sin respuestas."""
    monkeypatch.setattr(ortografia, "_DICCIONARIO", None)


def hay_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
