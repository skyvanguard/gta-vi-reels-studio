"""Operaciones de imagen: normalizar entradas, recortar a 9:16, miniaturas y storyboard."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

Image.MAX_IMAGE_PIXELS = 20_000_000  # Pillow corta con error al doble (40 Mpx): "bombas de descompresion"
ASPECTO_VERTICAL = 9 / 16
LADO_MIN = 400
LADO_MAX = 4096  # lo que se guarda; alcanza de sobra para recortes de 720x1280
PIXELES_MIN = 300_000


class ImagenInvalida(ValueError):
    pass


def normalizar(datos: bytes) -> Image.Image:
    """Abre bytes de imagen, valida tamano y devuelve RGB con la orientacion EXIF aplicada."""
    try:
        with Image.open(io.BytesIO(datos)) as probe:
            probe.verify()
        img = Image.open(io.BytesIO(datos))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise ImagenInvalida(f"imagen ilegible: {exc}") from exc
    if min(img.size) < LADO_MIN or img.width * img.height < PIXELES_MIN:
        raise ImagenInvalida(f"imagen demasiado chica: {img.size}")
    if max(img.size) > LADO_MAX:
        img.thumbnail((LADO_MAX, LADO_MAX), Image.LANCZOS)
    return img


def guardar_jpeg(img: Image.Image, destino: Path, calidad: int = 92) -> Path:
    """Re-codifica a JPEG (descarta metadatos y cualquier contenido extra del archivo original)."""
    img.save(destino, format="JPEG", quality=calidad)
    return destino


def huella(img: Image.Image) -> int:
    """Average-hash de 8x8 para detectar imagenes repetidas."""
    chica = img.convert("L").resize((8, 8), Image.LANCZOS)
    pixeles = list(chica.tobytes())
    media = sum(pixeles) / len(pixeles)
    return sum(1 << i for i, v in enumerate(pixeles) if v >= media)


def distancia_huella(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


ZOOM_MAX = 1.6


def recorte_vertical(img: Image.Image, centro_x: float, ancho: int, alto: int, zoom: float = 1.0,
                     centro_y: float = 0.5) -> Image.Image:
    """Recorte 9:16 centrado en (centro_x, centro_y), ambos 0-1. zoom > 1 achica la ventana (primer plano).

    La ventana mas grande posible es toda la altura (o todo el ancho si la imagen es mas angosta que 9:16);
    el zoom la divide y nunca se sale de la imagen.
    """
    cx = min(max(float(centro_x), 0.0), 1.0)
    cy = min(max(float(centro_y), 0.0), 1.0)
    z = min(max(float(zoom), 1.0), ZOOM_MAX)
    if img.width / img.height >= ASPECTO_VERTICAL:
        h = img.height / z
        w = h * ASPECTO_VERTICAL
    else:
        w = img.width / z
        h = w / ASPECTO_VERTICAL
    izq = min(max(cx * img.width - w / 2, 0), img.width - w)
    arriba = min(max(cy * img.height - h / 2, 0), img.height - h)
    caja = (round(izq), round(arriba), round(izq + w), round(arriba + h))
    return img.crop(caja).resize((ancho, alto), Image.LANCZOS)


Caja = tuple[float, float, float, float]  # izq, arriba, der, abajo, en fracciones de la imagen


def caja_relativa(aspecto: float, centro_x: float, centro_y: float, zoom: float) -> Caja:
    """La ventana de recorte_vertical en fracciones de la imagen (izq, arriba, der, abajo)."""
    if aspecto >= ASPECTO_VERTICAL:
        alto = 1 / zoom
        ancho = ASPECTO_VERTICAL / (zoom * aspecto)
    else:
        ancho = 1 / zoom
        alto = aspecto / (ASPECTO_VERTICAL * zoom)
    izq = min(max(centro_x - ancho / 2, 0.0), 1 - ancho)
    arriba = min(max(centro_y - alto / 2, 0.0), 1 - alto)
    return izq, arriba, izq + ancho, arriba + alto


# Wan no inventa detalle: con la misma escena y la imagen de entrada borrosa, el clip salio con la mitad de
# nitidez. Y en produccion la escena 1 solia ser la foto de la nota (1200x630) con un
# recorte cerrado: habia que estirarla 3 veces para darsela a Wan
MAX_AMPLIACION = 1.25  # cuanto se puede estirar un recorte para llegar al tamano que pide el modelo
_TOLERANCIA = 1e-6  # el zoom que devuelve zoom_nitido da justo el maximo, mas o menos el redondeo


def ampliacion(tamano: tuple[int, int], zoom: float, ancho: int, alto: int) -> float:
    """Cuantas veces hay que estirar el recorte vertical de una imagen de `tamano` para llegar a ancho x alto
    (1.0 = sobra resolucion; 2.0 = cada pixel se estira al doble). El zoom se acota como en recorte_vertical."""
    ancho_img, alto_img = tamano
    if ancho_img <= 0 or alto_img <= 0:
        return float("inf")
    z = min(max(float(zoom), 1.0), ZOOM_MAX)
    if ancho_img / alto_img >= ASPECTO_VERTICAL:
        return alto * z / alto_img
    return ancho * z / ancho_img


def estirada(tamano: tuple[int, int], zoom: float, ancho: int, alto: int) -> bool:
    """Si con ese zoom el recorte se estira mas de lo que se acepta."""
    return ampliacion(tamano, zoom, ancho, alto) > MAX_AMPLIACION + _TOLERANCIA


def zoom_nitido(tamano: tuple[int, int], zoom: float, ancho: int, alto: int) -> float:
    """El zoom pedido, o el mas cerrado que la imagen aguanta sin estirarse mas de MAX_AMPLIACION (nunca menos
    de 1.0: una imagen chica igual se usa entera)."""
    z = min(max(float(zoom), 1.0), ZOOM_MAX)
    entera = ampliacion(tamano, 1.0, ancho, alto)  # con zoom 1.0; la ampliacion crece en proporcion al zoom
    return max(1.0, min(z, MAX_AMPLIACION / entera)) if entera > 0 else 1.0


def jpeg_para_llm(img: Image.Image, lado_max: int = 1024) -> bytes:
    copia = img.copy()
    copia.thumbnail((lado_max, lado_max), Image.LANCZOS)
    buf = io.BytesIO()
    copia.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def storyboard(rutas: list[Path], etiquetas: list[str], fuente: Path | None, alto: int = 640) -> Image.Image:
    """Una tira con los recortes de cada escena numerados, para aprobar desde Telegram."""
    if not rutas:
        raise ValueError("storyboard sin imagenes")
    cuadros = []
    for ruta in rutas:
        with Image.open(ruta) as im:
            cuadro = im.convert("RGB")
            cuadro.thumbnail((alto, alto), Image.LANCZOS)
            cuadros.append(cuadro)
    margen = 12
    ancho_total = sum(c.width for c in cuadros) + margen * (len(cuadros) + 1)
    lienzo = Image.new("RGB", (ancho_total, alto + 2 * margen), (18, 18, 18))
    dibujo = ImageDraw.Draw(lienzo)
    letra = ImageFont.truetype(str(fuente), 44) if fuente and fuente.exists() else ImageFont.load_default()
    x = margen
    for cuadro, etiqueta in zip(cuadros, etiquetas):
        lienzo.paste(cuadro, (x, margen))
        dibujo.rectangle([x, margen, x + 70, margen + 62], fill=(255, 212, 0))
        dibujo.text((x + 18, margen + 4), etiqueta, font=letra, fill=(0, 0, 0))
        x += cuadro.width + margen
    return lienzo
