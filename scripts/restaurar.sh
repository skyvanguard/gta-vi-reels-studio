#!/usr/bin/env bash
# ExecStopPost del servicio: si el proceso murio con un turno de GPU abierto, deja el Qwen como estaba.
# No actua si el proceso dueno del turno sigue vivo, ni prende el Qwen si estaba apagado antes del turno.
set -uo pipefail
VS="$HOME/video-studio"
MARCA="$VS/data/turno_gpu.json"
[ -f "$MARCA" ] || exit 0
leer() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get(sys.argv[2], ''))" "$MARCA" "$1" 2>/dev/null; }
PID=$(leer pid)
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  echo "el turno sigue en curso (pid $PID): no se toca"; exit 0
fi
echo "turno de GPU abierto: bajando ComfyUI"
"$VS/app/scripts/comfy.sh" stop all || true
if [ "$(leer qwen_arriba)" = "False" ]; then
  echo "el Qwen estaba apagado antes del turno: se deja apagado"; rm -f "$MARCA"; exit 0
fi
for intento in 1 2 3; do
  if "$VS/app/scripts/qwen.sh" start; then rm -f "$MARCA"; exit 0; fi
  echo "el Qwen no volvio (intento $intento)"; sleep 10
done
exit 1
