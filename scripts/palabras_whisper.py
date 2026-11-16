"""Cuando se dice cada palabra: faster-whisper con marcas por palabra. Corre en el venv de ~/video-studio/tts-cb.

Recibe un JSON [{"audio": ruta, "texto": narracion}, ...] y devuelve, en una linea RESULTADO_JSON, una lista por
audio de {"texto", "inicio", "fin"} (segundos desde el comienzo del archivo). El texto va como initial_prompt:
el guion ya se conoce, Whisper solo tiene que ubicarlo en el tiempo.
"""
import argparse
import json
from pathlib import Path

import librosa
from faster_whisper import WhisperModel

MUESTREO = 16000  # lo que espera Whisper


def audio_16k(ruta: str):
    """El audio ya decodificado: el decode de faster-whisper usa PyAV y la version del venv no es compatible
    (open() got an unexpected keyword argument 'metadata_errors')."""
    datos, _ = librosa.load(ruta, sr=MUESTREO, mono=True)
    return datos


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entrada", required=True, help="JSON con la lista de audios y textos")
    ap.add_argument("--modelo", default="small")
    ap.add_argument("--hilos", type=int, default=16)
    ap.add_argument("--dispositivo", default="cpu", choices=("cpu", "cuda"))
    ap.add_argument("--sin-pista", action="store_true",
                    help="sin el guion como pista: para oir lo que la voz dice de verdad (control de pronunciacion)")
    args = ap.parse_args()

    pedidos = json.loads(Path(args.entrada).read_text(encoding="utf-8"))
    if not isinstance(pedidos, list) or not all(isinstance(p, dict) and p.get("audio") for p in pedidos):
        raise SystemExit("la entrada debe ser una lista de {audio, texto}")
    if args.dispositivo == "cuda":
        import torch  # noqa: F401 - carga las librerias de CUDA/cuDNN del venv que usa ctranslate2
        modelo = WhisperModel(args.modelo, device="cuda", compute_type="float16")
    else:
        modelo = WhisperModel(args.modelo, device="cpu", compute_type="int8", cpu_threads=args.hilos)
    resultado = []
    for pedido in pedidos:
        pista = None if args.sin_pista else (pedido.get("texto") or None)
        segmentos, _ = modelo.transcribe(audio_16k(pedido["audio"]), language="es", word_timestamps=True,
                                         beam_size=5 if args.sin_pista else 1, initial_prompt=pista,
                                         condition_on_previous_text=False, vad_filter=False)
        resultado.append([{"texto": w.word.strip(), "inicio": round(w.start, 3), "fin": round(w.end, 3)}
                          for s in segmentos for w in (s.words or []) if w.word.strip()])
    print("RESULTADO_JSON " + json.dumps(resultado, ensure_ascii=False))


if __name__ == "__main__":
    main()
