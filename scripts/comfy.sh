#!/usr/bin/env bash
# Levanta/baja un ComfyUI por GPU, solo en loopback, atado al nodo NUMA de su GPU.
#   comfy.sh start [0|1|all]   comfy.sh stop [0|1|all]   comfy.sh status
# stop espera a que el proceso termine (escala a SIGKILL) y a que la GPU libere la VRAM,
# para que el Qwen pueda volver a cargar sus 2x24 GB sin OOM.
set -euo pipefail
VS="$HOME/video-studio"; CU="$VS/ComfyUI"
ESPERA_TERM_S=25
VRAM_LIBRE_MIB=20000
puerto() { echo $((8188 + $1)); }
start_one() {
  local g=$1 p; p=$(puerto "$g")
  if curl -s -m 2 "http://127.0.0.1:$p/system_stats" >/dev/null; then echo "GPU$g ya corre en :$p"; return; fi
  mkdir -p "$CU/user/gpu$g" "$VS/logs"
  CUDA_DEVICE_ORDER=PCI_BUS_ID nohup numactl --cpunodebind="$g" --preferred="$g" \
    "$CU/.venv/bin/python" "$CU/main.py" --listen 127.0.0.1 --port "$p" --cuda-device "$g" \
    --user-directory "$CU/user/gpu$g" --disable-auto-launch \
    > "$VS/logs/comfy-gpu$g.log" 2>&1 &
  echo $! > "$VS/logs/comfy-gpu$g.pid"
  for _ in $(seq 1 90); do
    if curl -s -m 2 "http://127.0.0.1:$p/system_stats" >/dev/null; then echo "GPU$g listo en 127.0.0.1:$p"; return; fi
    sleep 2
  done
  echo "GPU$g NO arranco, ver $VS/logs/comfy-gpu$g.log" >&2; exit 1
}
pids_de() {  # pid del pidfile + cualquier ComfyUI huerfano de ese puerto
  local g=$1 p; p=$(puerto "$g")
  { [ -f "$VS/logs/comfy-gpu$g.pid" ] && cat "$VS/logs/comfy-gpu$g.pid"; pgrep -f "[C]omfyUI/main.py --listen 127.0.0.1 --port $p" || true; } | sort -u
}
stop_one() {
  local g=$1 pids; pids=$(pids_de "$g")
  for pid in $pids; do kill "$pid" 2>/dev/null || true; done
  for _ in $(seq 1 $ESPERA_TERM_S); do
    vivos=""; for pid in $pids; do kill -0 "$pid" 2>/dev/null && vivos="$vivos $pid"; done
    [ -z "$vivos" ] && break; sleep 1
  done
  for pid in $pids; do kill -0 "$pid" 2>/dev/null && { echo "GPU$g: SIGKILL a $pid"; kill -9 "$pid" 2>/dev/null || true; }; done
  rm -f "$VS/logs/comfy-gpu$g.pid"
  for _ in $(seq 1 30); do  # la VRAM tarda unos segundos en liberarse tras salir el proceso
    usada=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i "$g" | tr -d ' ')
    total=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits -i "$g" | tr -d ' ')
    [ $((total - usada)) -ge $VRAM_LIBRE_MIB ] && { echo "GPU$g detenido (${usada} MiB en uso)"; return; }
    sleep 1
  done
  echo "GPU$g: la VRAM no se libero (${usada} MiB en uso)" >&2; return 1
}
gpus() { case "${1:-all}" in all) echo 0 1;; 0|1) echo "$1";; *) echo "gpu invalida: $1" >&2; exit 2;; esac; }
case "${1:-status}" in
  start) for g in $(gpus "${2:-all}"); do start_one "$g"; done;;
  stop)  rc=0; for g in $(gpus "${2:-all}"); do stop_one "$g" || rc=1; done; exit $rc;;
  status) for g in 0 1; do p=$(puerto $g); curl -s -m 2 "http://127.0.0.1:$p/system_stats" >/dev/null && echo "GPU$g :$p arriba" || echo "GPU$g :$p abajo"; done;;
  *) echo "uso: comfy.sh start|stop|status [0|1|all]" >&2; exit 2;;
esac
