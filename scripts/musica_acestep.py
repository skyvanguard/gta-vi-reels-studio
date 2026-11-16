"""Musica de fondo instrumental con ACE-Step 1.5 (MIT; segun su ficha, la musica generada se puede usar
comercialmente). Corre dentro del venv de ~/video-studio/acestep (git clone + uv sync) y usa solo el DiT turbo
(sin el LM: thinking=False).

Lee una lista JSON de pedidos {caption, segundos, semilla, destino} y escribe cada tema en su destino (WAV).
Imprime una linea RESULTADO_JSON con lo que salio (o el error de cada pedido).
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raiz", required=True, help="carpeta del repo de ACE-Step (con checkpoints/)")
    ap.add_argument("--pedidos", required=True, help="JSON: [{caption, segundos, semilla, destino}]")
    ap.add_argument("--dispositivo", default="cuda")
    args = ap.parse_args()

    raiz = Path(args.raiz)
    os.chdir(raiz)
    sys.path.insert(0, str(raiz))
    from acestep.handler import AceStepHandler  # noqa: E402 - depende del sys.path
    from acestep.inference import GenerationConfig, GenerationParams, generate_music  # noqa: E402

    pedidos = json.loads(Path(args.pedidos).read_text(encoding="utf-8"))
    dit = AceStepHandler()
    mensaje, ok = dit.initialize_service(project_root=str(raiz), config_path="acestep-v15-turbo",
                                         device=args.dispositivo)
    if not ok:
        raise SystemExit(f"ACE-Step no arranco: {mensaje}")
    resultado = []
    for pedido in pedidos:
        params = GenerationParams(caption=pedido["caption"], lyrics="[Instrumental]", instrumental=True,
                                  duration=float(pedido["segundos"]), inference_steps=8, shift=3.0,
                                  seed=int(pedido["semilla"]), thinking=False, use_cot_caption=False,
                                  use_cot_language=False, use_cot_metas=False)
        config = GenerationConfig(batch_size=1, use_random_seed=False, audio_format="wav")
        with tempfile.TemporaryDirectory() as tmp:
            r = generate_music(dit, None, params, config, save_dir=tmp)
            if not r.success or not r.audios:
                resultado.append({"destino": pedido["destino"], "error": str(r.error)[:300]})
                continue
            destino = Path(pedido["destino"])
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(r.audios[0]["path"], destino)
            resultado.append({"destino": str(destino)})
    print("RESULTADO_JSON " + json.dumps(resultado, ensure_ascii=False))


if __name__ == "__main__":
    main()
