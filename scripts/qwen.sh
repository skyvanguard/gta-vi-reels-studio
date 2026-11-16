#!/usr/bin/env bash
# Intercambio de GPU: baja o sube el llm-server (el Qwen) sin tocar su configuracion.
#   qwen.sh status  -> codigo 0 si el contenedor esta corriendo, 3 si no
#   qwen.sh stop    -> detiene el contenedor
#   qwen.sh start   -> lo vuelve a arrancar tal cual y espera /health = 200
set -euo pipefail
case "${1:-}" in
  status)
    [ "$(docker inspect -f '{{.State.Running}}' llm-server 2>/dev/null)" = "true" ] && exit 0 || exit 3;;
  stop)
    docker stop llm-server >/dev/null && echo "llm-server detenido"
    nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader;;
  start)
    docker start llm-server >/dev/null
    for _ in $(seq 1 120); do
      [ "$(curl -s -o /dev/null -w "%{http_code}" -m 3 http://127.0.0.1:8080/health)" = 200 ] && { echo "llm-server healthy"; exit 0; }
      sleep 3
    done
    echo "llm-server no respondio /health en 6 min" >&2; exit 1;;
  *) echo "uso: qwen.sh status|stop|start" >&2; exit 2;;
esac
