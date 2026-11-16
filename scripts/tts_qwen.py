"""Qwen3-TTS (Apache-2.0). Corre dentro del venv de TTS, separado del resto por sus dependencias fijas.

  disenar: crea una voz desde una descripcion (modelo VoiceDesign) -> wav de referencia.
  clonar:  dice varios textos con la voz de un wav de referencia (modelo Base) -> 1.wav, 2.wav, ...

Imprime un JSON con los archivos y sus duraciones.
"""
import argparse
import json
from pathlib import Path

import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

IDIOMA = "Spanish"


def cargar(ruta: Path, dispositivo: str) -> Qwen3TTSModel:
    dtype = torch.bfloat16 if dispositivo.startswith("cuda") else torch.float32
    return Qwen3TTSModel.from_pretrained(str(ruta), device_map=dispositivo, dtype=dtype, attn_implementation="sdpa")


def disenar(args) -> list[dict]:
    modelo = cargar(Path(args.modelos) / "VoiceDesign", args.dispositivo)
    wavs, sr = modelo.generate_voice_design(text=args.texto, language=IDIOMA, instruct=args.instruct)
    sf.write(args.salida, wavs[0], sr)
    return [{"archivo": args.salida, "segundos": round(len(wavs[0]) / sr, 3)}]


def clonar(args) -> list[dict]:
    textos = json.loads(Path(args.textos_json).read_text(encoding="utf-8"))
    if not isinstance(textos, list) or not all(isinstance(t, str) and t.strip() for t in textos):
        raise SystemExit("textos_json debe ser una lista de textos no vacios")
    modelo = cargar(Path(args.modelos) / "Base", args.dispositivo)
    prompt = modelo.create_voice_clone_prompt(ref_audio=args.ref, ref_text=args.ref_texto)
    salida_dir = Path(args.salida_dir)
    salida_dir.mkdir(parents=True, exist_ok=True)
    resultado = []
    for i, texto in enumerate(textos, start=1):
        wavs, sr = modelo.generate_voice_clone(text=texto, language=IDIOMA, voice_clone_prompt=prompt)
        destino = salida_dir / f"{i}.wav"
        sf.write(destino, wavs[0], sr)
        resultado.append({"archivo": str(destino), "segundos": round(len(wavs[0]) / sr, 3)})
    return resultado


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="modo", required=True)
    d = sub.add_parser("disenar")
    d.add_argument("--instruct", required=True)
    d.add_argument("--texto", required=True)
    d.add_argument("--salida", required=True)
    c = sub.add_parser("clonar")
    c.add_argument("--ref", required=True)
    c.add_argument("--ref-texto", required=True)
    c.add_argument("--textos-json", required=True)
    c.add_argument("--salida-dir", required=True)
    for p in (d, c):
        p.add_argument("--modelos", required=True)
        p.add_argument("--dispositivo", default="cpu")
    args = ap.parse_args()
    resultado = disenar(args) if args.modo == "disenar" else clonar(args)
    print("RESULTADO_JSON " + json.dumps(resultado, ensure_ascii=False))


if __name__ == "__main__":
    main()
