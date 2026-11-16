"""Mide la pronunciacion de cada variante de voz: transcribe con faster-whisper large-v3 y calcula el WER
(porcentaje de palabras mal entendidas) contra el texto original. Corre en el venv de tts-cb.

  python evaluar_voces.py --whisper DIR --textos textos.json --salida informe.json VARIANTE_DIR [VARIANTE_DIR ...]

Cada VARIANTE_DIR tiene 1.wav, 2.wav, ... en el mismo orden que textos.json.
El WER mide inteligibilidad, no naturalidad: la eleccion final es de oido.
"""
import argparse
import json
import re
import unicodedata
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
from faster_whisper import WhisperModel


def audio_16k(ruta: Path) -> np.ndarray:
    """Decodifica sin PyAV (la version instalada no acepta los argumentos que pasa faster-whisper)."""
    datos, sr = sf.read(str(ruta), dtype="float32", always_2d=True)
    return librosa.resample(datos.mean(axis=1), orig_sr=sr, target_sr=16000).astype(np.float32)


def normalizar(texto: str) -> list[str]:
    sin_tildes = unicodedata.normalize("NFKD", texto.lower())
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9ñ ]+", " ", sin_tildes).split()


def wer(referencia: list[str], hipotesis: list[str]) -> float:
    """Distancia de edicion por palabras / largo de la referencia."""
    filas = list(range(len(hipotesis) + 1))
    for i, palabra in enumerate(referencia, start=1):
        previa, filas[0] = filas[0], i
        for j, otra in enumerate(hipotesis, start=1):
            actual = min(filas[j] + 1, filas[j - 1] + 1, previa + (palabra != otra))
            previa, filas[j] = filas[j], actual
    return filas[-1] / max(len(referencia), 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--whisper", required=True)
    ap.add_argument("--textos", required=True)
    ap.add_argument("--salida", required=True)
    ap.add_argument("variantes", nargs="+")
    args = ap.parse_args()
    textos = json.loads(Path(args.textos).read_text(encoding="utf-8"))
    modelo = WhisperModel(args.whisper, device="cpu", compute_type="int8", cpu_threads=16)
    informe = {}
    for carpeta in map(Path, args.variantes):
        detalle, errores, palabras = [], 0.0, 0
        for i, texto in enumerate(textos, start=1):
            segmentos, _ = modelo.transcribe(audio_16k(carpeta / f"{i}.wav"), language="es", beam_size=5)
            oido = " ".join(s.text.strip() for s in segmentos)
            ref = normalizar(texto)
            w = wer(ref, normalizar(oido))
            errores += w * len(ref)
            palabras += len(ref)
            detalle.append({"texto": texto, "oido": oido, "wer": round(w, 3)})
        informe[carpeta.name] = {"wer": round(errores / max(palabras, 1), 3), "frases": detalle}
        print(f"{carpeta.name:<28} WER {informe[carpeta.name]['wer'] * 100:5.1f} %")
    Path(args.salida).write_text(json.dumps(informe, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
