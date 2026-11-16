#!/usr/bin/env bash
# Compara motores/voces en espanol: mismas frases, tiempo de sintesis (CPU) y WER con Whisper large-v3.
# Deja una muestra por variante en $OUT/muestras/ para elegir de oido.
#   comparar_voces.sh [OUT]
set -euo pipefail
VS="$HOME/video-studio"; APP="$VS/app"; OUT="${1:-/tmp/eval-voces}"
CB="$VS/tts-cb"; QW="$VS/tts"; VOZ="$VS/assets/voz"
mkdir -p "$OUT/refs" "$OUT/muestras"
cat > "$OUT/textos.json" <<'EOF'
["¡Atención, gamers! GTA seis ya tiene fecha de precarga: arranca el diez de noviembre.",
 "Rockstar confirmó que el juego va a ocupar doscientos doce gigas en tu consola.",
 "Jason y Lucía recorren Vice City, en el estado ficticio de Leonida.",
 "Si tenés la PlayStation cinco o la Xbox Series, andá liberando espacio desde ahora.",
 "¿Ya lo reservaste? Contanos en los comentarios y seguí la página para no perderte nada."]
EOF
# referencias de la demo oficial de Resemble: SOLO para comparar (no tienen licencia clara para uso comercial)
curl -sfL -m 60 -o "$OUT/refs/demo_mx_mujer.wav" https://storage.googleapis.com/chatterbox-demo-samples/mtl-v3-single-language-prompts/es-latam/es_mx_f1.wav || true
curl -sfL -m 60 -o "$OUT/refs/demo_hombre.flac" https://storage.googleapis.com/chatterbox-demo-samples/mtl_prompts/es_m1.flac || true

medir() {  # nombre comando...
  local nombre=$1; shift
  local t0; t0=$(date +%s)
  if "$@" > "$OUT/$nombre.log" 2>&1; then
    printf "%-26s %4d s en CPU para 5 frases\n" "$nombre" "$(( $(date +%s) - t0 ))"
  else
    echo "$nombre FALLO (ver $OUT/$nombre.log)"; return 0
  fi
}
cb() {  # nombre ref
  medir "cb_$1" "$CB/.venv/bin/python" "$APP/scripts/tts_chatterbox.py" --raiz "$CB" --textos-json "$OUT/textos.json" \
    --salida-dir "$OUT/cb_$1" --ref "$2" --semilla 7
}
qw() {  # nombre
  medir "qwen3_$1" "$QW/.venv/bin/python" "$APP/scripts/tts_qwen.py" clonar --ref "$VOZ/$1.wav" \
    --ref-texto "$(cat "$VOZ/$1.txt")" --textos-json "$OUT/textos.json" --salida-dir "$OUT/qwen3_$1" --modelos "$QW/models"
}
qw locutor
qw locutora
cb integrada ""
cb locutor "$VOZ/locutor.wav"
cb locutora "$VOZ/locutora.wav"
[ -s "$OUT/refs/demo_mx_mujer.wav" ] && cb demo_mx_mujer "$OUT/refs/demo_mx_mujer.wav"
[ -s "$OUT/refs/demo_hombre.flac" ] && cb demo_hombre "$OUT/refs/demo_hombre.flac"

variantes=()
for d in "$OUT"/qwen3_* "$OUT"/cb_*; do
  [ -d "$d" ] && [ -s "$d/5.wav" ] || continue
  variantes+=("$d")
  n=$(basename "$d")
  ffmpeg -v error -y $(for i in 1 2 3 4 5; do echo -i "$d/$i.wav"; done) -filter_complex \
    "$(for i in 0 1 2 3 4; do printf "[$i]aresample=24000,apad=pad_dur=0.4[a$i];"; done)[a0][a1][a2][a3][a4]concat=n=5:v=0:a=1" \
    "$OUT/muestras/$n.wav"
done
"$CB/.venv/bin/python" "$APP/scripts/evaluar_voces.py" --whisper "$CB/whisper" --textos "$OUT/textos.json" \
  --salida "$OUT/informe.json" "${variantes[@]}"
