"""Chatterbox Multilingual V3 - espanol latino (MIT, Resemble AI). Corre dentro del venv de ~/video-studio/tts-cb,
que trae el codigo de inferencia de la demo oficial (chatterbox/src/chatterbox) y los pesos en models/.

Dice cada texto de una lista JSON con la voz de un wav de referencia (o la voz integrada del modelo si no hay
referencia) y escribe 1.wav, 2.wav, ... Imprime una linea RESULTADO_JSON con archivos y duraciones.
"""
import argparse
import json
import sys
from pathlib import Path

import soundfile as sf
import torch

T3 = "t3_es_mx_latam.safetensors"
S3GEN = "s3gen_v3.pt"
MAX_CARACTERES = 300  # el mismo tope que usa la demo oficial


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raiz", required=True, help="carpeta tts-cb (codigo vendored + models/)")
    ap.add_argument("--textos-json", required=True)
    ap.add_argument("--salida-dir", required=True)
    ap.add_argument("--ref", default="", help="wav de referencia; vacio = voz integrada")
    ap.add_argument("--dispositivo", default="cpu")
    ap.add_argument("--exageracion", type=float, default=0.5)
    ap.add_argument("--cfg", type=float, default=0.5)
    ap.add_argument("--temperatura", type=float, default=0.8)
    ap.add_argument("--semilla", type=int, default=0)
    args = ap.parse_args()

    raiz = Path(args.raiz)
    sys.path.insert(0, str(raiz))
    from chatterbox.src.chatterbox.tts import ChatterboxTTS  # noqa: E402 - depende del sys.path

    textos = json.loads(Path(args.textos_json).read_text(encoding="utf-8"))
    if not isinstance(textos, list) or not all(isinstance(t, str) and t.strip() for t in textos):
        raise SystemExit("textos_json debe ser una lista de textos no vacios")
    modelo = ChatterboxTTS.from_local(raiz / "models", args.dispositivo, t3_filename=T3, s3gen_filename=S3GEN)
    salida = Path(args.salida_dir)
    salida.mkdir(parents=True, exist_ok=True)
    resultado = []
    for i, texto in enumerate(textos, start=1):
        if args.semilla:
            torch.manual_seed(args.semilla + i)
        wav = modelo.generate(texto[:MAX_CARACTERES], audio_prompt_path=args.ref or None, language_id="es",
                              exaggeration=args.exageracion, cfg_weight=args.cfg, temperature=args.temperatura)
        datos = wav.squeeze(0).cpu().numpy()
        destino = salida / f"{i}.wav"
        sf.write(destino, datos, modelo.sr)
        resultado.append({"archivo": str(destino), "segundos": round(len(datos) / modelo.sr, 3)})
    print("RESULTADO_JSON " + json.dumps(resultado, ensure_ascii=False))


if __name__ == "__main__":
    main()
