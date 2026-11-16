"""Textos del reel dibujados como PNG con tipografia real (nunca pasan por el modelo de video)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W = 1080
MARGEN = 70
AMARILLO = (255, 212, 0, 255)
BLANCO = (255, 255, 255, 255)
NEGRO = (0, 0, 0, 255)
ALTO_BANDA_TITULAR = 560
Y_TITULAR = 150
TAM_TITULAR, TAM_MIN_TITULAR, PASO_TAM_TITULAR = 160, 70, 10
MAX_LINEAS_TITULAR = 3
BORDE_TITULAR = 9
AIRE_INFERIOR_TITULAR = 24  # la ultima linea no toca el borde de la banda


def _fuente(ruta: Path, tam: int, variacion: bytes | None = None) -> ImageFont.FreeTypeFont:
    fuente = ImageFont.truetype(str(ruta), tam)
    if variacion:
        try:
            fuente.set_variation_by_name(variacion)
        except (OSError, ValueError):
            pass  # fuente sin ejes variables: se usa el peso por defecto
    return fuente


def partir(dibujo: ImageDraw.ImageDraw, texto: str, fuente, ancho_max: int) -> list[str]:
    lineas, actual = [], ""
    for palabra in texto.split():
        prueba = f"{actual} {palabra}".strip()
        if dibujo.textlength(prueba, font=fuente) <= ancho_max or not actual:
            actual = prueba
        else:
            lineas.append(actual)
            actual = palabra
    return lineas + ([actual] if actual else [])


def titular(texto: str, fuente_ruta: Path, destino: Path) -> Path:
    """Banda superior con degradado y titular grande (gancho de los primeros segundos)."""
    img = Image.new("RGBA", (W, ALTO_BANDA_TITULAR), (0, 0, 0, 0))
    dibujo = ImageDraw.Draw(img)
    for y in range(ALTO_BANDA_TITULAR):
        dibujo.line([(0, y), (W, y)], fill=(0, 0, 0, int(210 * (1 - y / ALTO_BANDA_TITULAR))))
    fuente, lineas, paso = _ajustar_titular(dibujo, texto.upper(), fuente_ruta)
    y = Y_TITULAR
    for linea in lineas:
        x = (W - dibujo.textlength(linea, font=fuente)) / 2
        dibujo.text((x, y), linea, font=fuente, fill=AMARILLO, stroke_width=BORDE_TITULAR, stroke_fill=NEGRO)
        y += paso
    img.save(destino)
    return destino


def _ajustar_titular(dibujo: ImageDraw.ImageDraw, texto: str, fuente_ruta: Path):
    """El tamano mas grande con el que el titular entra en 3 lineas Y dentro de la banda (antes solo se miraba la
    cantidad de lineas: 3 lineas a 160 px bajaban hasta ~680 px y la ultima salia cortada)."""
    tam = TAM_TITULAR
    while True:
        fuente = _fuente(fuente_ruta, tam)
        lineas = partir(dibujo, texto, fuente, W - 2 * MARGEN)
        paso = int(tam * 1.06)
        if tam <= TAM_MIN_TITULAR or (len(lineas) <= MAX_LINEAS_TITULAR
                                      and _fondo(dibujo, lineas, fuente, paso) <= ALTO_BANDA_TITULAR
                                      - AIRE_INFERIOR_TITULAR):
            return fuente, lineas, paso
        tam -= PASO_TAM_TITULAR


def _fondo(dibujo: ImageDraw.ImageDraw, lineas: list[str], fuente, paso: int) -> int:
    """Hasta donde baja el dibujo (con el borde negro) si la primera linea arranca en Y_TITULAR."""
    if not lineas:
        return Y_TITULAR
    y_ultima = Y_TITULAR + paso * (len(lineas) - 1)
    return dibujo.textbbox((0, y_ultima), lineas[-1], font=fuente, stroke_width=BORDE_TITULAR)[3]


def subtitulo(texto: str, fuente_ruta: Path, destino: Path) -> Path:
    """Caja semitransparente con texto blanco (subtitulo de la escena)."""
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    fuente = _fuente(fuente_ruta, 68, b"ExtraBold")
    lineas = partir(tmp, texto, fuente, W - 4 * MARGEN)
    alto_linea = 86
    alto = len(lineas) * alto_linea + 64
    img = Image.new("RGBA", (W, alto), (0, 0, 0, 0))
    dibujo = ImageDraw.Draw(img)
    dibujo.rounded_rectangle([MARGEN, 0, W - MARGEN, alto - 1], radius=30, fill=(0, 0, 0, 175))
    y = 32
    for linea in lineas:
        x = (W - dibujo.textlength(linea, font=fuente)) / 2
        dibujo.text((x, y), linea, font=fuente, fill=BLANCO, stroke_width=2, stroke_fill=NEGRO)
        y += alto_linea
    img.save(destino)
    return destino


def sello_fuente(medio: str, fuente_ruta: Path, destino: Path) -> Path:
    """"FUENTE: IGN" en una pastilla amarilla: de donde sale la noticia (confianza, y es aporte propio)."""
    texto = f"FUENTE: {medio.upper()}"
    fuente = _fuente(fuente_ruta, 34, b"ExtraBold")
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    ancho = int(tmp.textlength(texto, font=fuente)) + 44
    img = Image.new("RGBA", (ancho, 60), (0, 0, 0, 0))
    dibujo = ImageDraw.Draw(img)
    dibujo.rounded_rectangle([0, 0, ancho - 1, 59], radius=14, fill=AMARILLO)
    dibujo.text((22, 9), texto, font=fuente, fill=NEGRO)
    img.save(destino)
    return destino


def marca(texto: str, fuente_ruta: Path, destino: Path) -> Path:
    """Firma chica de la pagina, arriba a la izquierda."""
    fuente = _fuente(fuente_ruta, 40, b"Bold")
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    ancho = int(tmp.textlength(texto, font=fuente)) + 48
    img = Image.new("RGBA", (ancho, 68), (0, 0, 0, 0))
    dibujo = ImageDraw.Draw(img)
    dibujo.rounded_rectangle([0, 0, ancho - 1, 67], radius=18, fill=(0, 0, 0, 150))
    dibujo.text((24, 10), texto, font=fuente, fill=BLANCO)
    img.save(destino)
    return destino
