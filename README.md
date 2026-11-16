# GTA VI Reels Studio

Un bot privado de Telegram que convierte una noticia de GTA VI en un reel vertical listo para publicar, generado
**entero en tu propio servidor**: un LLM local escribe el guion, Wan 2.2 anima las imágenes, Chatterbox pone la
voz y FFmpeg hace el montaje. No usa ninguna API paga.

*A private Telegram bot that turns a GTA VI news link into a ready-to-post vertical reel, fully self-hosted:
local LLM (Qwen) for planning and script, Wan 2.2 image-to-video, Chatterbox TTS and FFmpeg. Code, comments and
bot messages are in Spanish.*

> **Qué es y qué no es.** Es el código de un sistema que funciona todos los días en un servidor con 2 × RTX 3090.
> No es un producto llave en mano: no trae instalador, las rutas suponen `~/video-studio/` y vas a tener que
> adaptar los scripts de `scripts/` a tu forma de correr el LLM y ComfyUI. Se publica porque el flujo, los
> chequeos y lo que se midió le pueden servir a alguien más.
>
> **No trae material del juego.** Ni imágenes, ni tráilers, ni música. Proyecto de fans, sin relación con
> Rockstar Games ni Take-Two Interactive.

## Qué hace

```
Telegram (link / texto / fotos / video)
  → nota: título, fecha, texto e imágenes (red.py valida cada URL contra SSRF)
  → material: tomas de video real de tu biblioteca, imágenes oficiales, fotos de la noticia
  → Qwen local con visión: 2 a 18 escenas según el largo (qué imagen, qué recorte 9:16, qué se ve)
  → guionista (el mismo Qwen, solo texto): titular, guion hablado con gancho, textos y post
  → verificador: marca lo que el guion afirma y la nota no dice
  → aprobás o editás desde Telegram
  → voz (Chatterbox Multilingual, en CPU, antes del turno de GPU)
  → turno de GPU: se apaga el LLM, un ComfyUI por placa anima con Wan 2.2 I2V las escenas que lo necesitan
  → el LLM vuelve (siempre, aunque algo falle)
  → FFmpeg: 1080×1920, titular, subtítulos palabra por palabra, música y efectos, -14 LUFS
  → el reel y el texto para el post llegan por Telegram
```

Cada escena sale de una de tres fuentes (`vstudio/armado.py`):

| Medio | De dónde sale | Usa GPU |
|---|---|---|
| `toma` | un tramo de video real de tu biblioteca (`assets/tomas/`), recortado a 9:16 | no |
| `foto` | una foto con paneo (las de la noticia, o una imagen chica para animar) | no |
| `ia` | una imagen animada por Wan 2.2 (5 s) | sí, ~8 min por clip |

Además: formatos de reel (noticia, «¿sabías que…?», ranking, antes y ahora), reels automáticos con las noticias
del día, agenda de entrega repartida en el día, y publicación programada en Facebook (opcional).

## Qué necesitás

- **Linux** con `systemd` de usuario, **Python 3.12+** y **FFmpeg**.
- **GPU NVIDIA de 24 GB** (una o más). Los números de abajo son con 2 × RTX 3090: un ComfyUI por placa.
- **Un LLM con visión servido por `llama-server`** (llama.cpp) en `127.0.0.1:8080`, con API compatible con OpenAI
  y clave en un archivo. Acá se usa un Qwen de 27B; `scripts/qwen.sh` lo apaga y lo prende con
  `docker stop|start llm-server`: si lo corrés de otra forma, ese script es lo que hay que cambiar.
- **ComfyUI 0.44** con Wan 2.2 I2V 14B (fp8, alto y bajo ruido), el LoRA LightX2V de 4 pasos, el codificador
  `umt5_xxl` y el VAE de Wan 2.1 (los nombres exactos están en `workflows/wan/wan22_i2v_14b.api.json`).
  Opcional: `rife_v4.25.safetensors` en `models/frame_interpolation` para los cuadros intermedios.
- **Chatterbox Multilingual** en su propio venv (`~/video-studio/tts-cb`), con `faster-whisper` para los
  subtítulos y el control de pronunciación.
- **Un bot de Telegram** (token de @BotFather).
- Opcionales: **SearXNG** local (`127.0.0.1:8888`) para buscar fotos de la noticia, **ACE-Step 1.5** para la
  música de fondo, **Qwen3-TTS** como voz alternativa y el diccionario `hunspell-es` para la ortografía. Sin
  ellos, esa parte no corre y lo demás sigue.

## Estructura en el servidor

| Ruta | Qué es |
|---|---|
| `~/video-studio/app/` | este repo (`vstudio/`, `scripts/`, `workflows/`) + `.venv` |
| `~/video-studio/config.toml` | tu configuración (ver `config.example.toml`); todo es opcional |
| `~/video-studio/ComfyUI/` | ComfyUI + modelos de Wan 2.2 |
| `~/video-studio/tts-cb/` | Chatterbox + faster-whisper en su venv |
| `~/video-studio/assets/` | `fonts/`, `voz/`, `musica/`, `biblioteca/` (imágenes), `tomas/` (video real), `diccionario/` |
| `~/video-studio/proyectos/<id>/` | imágenes, recortes, clips, voz, reel y logs de cada proyecto |
| `~/video-studio/data/` | `studio.db` (SQLite), marca del turno de GPU, estado de la agenda |
| `~/video-studio/secrets/` | token del bot, usuarios vinculados, código de vinculación (700/600) |

## Puesta en marcha

```bash
git clone https://github.com/skyvanguard/gta-vi-reels-studio ~/video-studio/app
cd ~/video-studio/app
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q                      # los tests no necesitan GPU ni modelos
cp config.example.toml ~/video-studio/config.toml   # y poné tu zona_horaria
```

1. En Telegram, hablale a **@BotFather**: `/newbot`. Después `/setjoingroups` → Disable (el bot es solo para
   chats privados).
2. Guardá el token directo en el servidor (no lo pegues en ningún chat):
   ```bash
   mkdir -p ~/video-studio/secrets && chmod 700 ~/video-studio/secrets
   (umask 077; read -rsp "token: " T && printf '%s' "$T" > ~/video-studio/secrets/telegram_token)
   ```
3. Instalá y arrancá el servicio:
   ```bash
   mkdir -p ~/.config/systemd/user
   cp ~/video-studio/app/deploy/video-studio.service ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now video-studio
   ```
4. Vinculá tu cuenta: `cat ~/video-studio/secrets/codigo_vinculacion` y mandale al bot `/vincular CODIGO`. El
   código sirve una sola vez y vence a las 24 h. Para sumar a otra persona: `python -m vstudio vincular`.
5. Voces: `python -m vstudio voces` prepara las voces; por Telegram, `/voces` y `/voz NOMBRE`.

## El material del juego (lo ponés vos)

El repo no trae nada de Rockstar. Lo que uses lo bajás vos y lo usás bajo tu responsabilidad.

- **Imágenes** (`assets/biblioteca/rockstar/`): los paquetes que Rockstar publica en
  <https://www.rockstargames.com/VI/media>. `vstudio/biblioteca.py` etiqueta cada imagen por su carpeta y nombre
  y se lo dice al LLM, que solo no reconoce a los personajes; rota por temas para no repetir siempre lo mismo.
- **Video real** (`assets/tomas/`): `python -m vstudio tomas VIDEO --fuente NOMBRE [--tema TEMA]` corta un video
  en tomas (detecta los cortes y las franjas negras, descarta lo oscuro y lo repetido) y el LLM anota qué se ve
  en cada una. Por Telegram: un video de hasta 20 MB con `/biblioteca TEMA`, o archivos en
  `assets/tomas/_entrada/TEMA/` y después `/tomas`.
- **Tus fotos**: `/biblioteca TEMA` con una o varias fotos las guarda (re-codificadas, sin metadatos) y entran en
  la rotación.

Ojo con la monetización: la política de Rockstar sobre su material está pensada para el uso de fans no comercial,
y una plataforma puede reconocer y reclamar el material de un tráiler.

## Uso desde Telegram

- Mandá un **link**, un texto y/o **fotos** (también como álbum). Llega el storyboard y el plan con botones:
  ✅ Generar · 🔁 Otro plan · 🔊 Voz · 💬 Subtítulos · 🔥 Titular · 🖼️ Otras imágenes · ✍️ Otro guion ·
  ❌ Cancelar. El reel entregado trae los mismos botones: voz, subtítulos y titular se rearman **sin GPU**.
- Con fotos, el reel usa **solo esas**. «Solo video» en el mensaje = la foto animada, sin voz ni textos. El largo
  se puede pedir en el mensaje («que dure 1 min»).
- **Cambios con palabras**: un mensaje corto sin link («sin subtítulos», «sin voz», «otras imágenes», «otro
  guion», «que dure 30 s») cambia el último plan o reel del chat, o el del mensaje al que se responde.
- **Una escena sola**: «cambiá la escena 3» o `/escena 3` le pone otra toma de video real, sin tocar el resto.
- **Formatos**: «sabías que…» (un dato, 15 s), «ranking» o «antes y ahora» en el mensaje.
- **Guion propio**: si el texto trae secciones o tiempos («[INTRO – 0:00 a 0:10]»), la voz lo lee tal cual y el
  reel dura lo que marcan los tiempos.
- Edición: `/gancho N`, `/reescribir`, `/titular TEXTO`, `/texto N TEXTO`, `/narracion N TEXTO`, `/post TEXTO`.
- Largo: `/largo 10|15|30|75|90` (75 y 90 pasan del minuto).
- Cola y GPU: `/estado`, `/ya` (apaga el LLM y genera ahora), `/politica inactivo|inmediato`.
- Después: `/regenerar ID N` (rehace con otra semilla una escena animada), `/revoz ID`, `/reescribir ID`,
  `/cancelar ID`.
- Voz: `/voces`, `/voz NOMBRE`, y una nota de voz de 10-20 s con `/voz_nueva NOMBRE` para una voz propia (solo
  la tuya o la de alguien que te dé permiso).

## Reels automáticos y agenda

Todos los días a la hora configurada (`[automatico] hora`, 05:00 por defecto, en tu `zona_horaria`) el servicio:

1. junta las notas de GTA VI de las últimas 30 h (`vstudio/noticias.py`): Bing Noticias en español e inglés,
   3DJuegos, Vandal, Eurogamer.es, IGN, GamesRadar y VGC;
2. el LLM agrupa las que cuentan lo mismo, descarta lo ya publicado en los últimos 10 días y lo que no es de
   GTA VI, y ordena por interés;
3. arma todos los planes (si una nota no se puede leer, prueba otro medio que cuente lo mismo; sin el texto de
   la nota no arma nada) y los genera en un solo turno de GPU;
4. entrega cada reel con su texto para publicar.

Hasta la mitad de la tanda sale en otro formato, para que no tengan todos el mismo molde. Comandos: `/auto`,
`/auto ya`, `/auto off`, `/auto on`.

**Agenda** (`vstudio/agenda.py`): en lugar de llegar todos juntos, los automáticos llegan de a uno en los
horarios de `[agenda] horarios`. `/agenda`, `/agenda ya`, `/agenda off`, `/agenda on`.

## Publicación en Facebook (opcional)

`vstudio/facebook.py` usa la Reels Publishing API de Meta: sube el reel y lo deja programado, marcado como hecho
con IA. Hace falta una app de Meta con los permisos `pages_show_list`, `pages_read_engagement` y
`pages_manage_posts`, en modo Live. Si Meta no te da ese acceso, el sistema funciona igual: los reels llegan por
Telegram y se publican a mano. `python -m vstudio facebook` guarda el token de la página (el de usuario no queda
en ningún lado).

`vstudio/publicador.py` solo programa los reels automáticos que pasan el control de `calidad.py`. Lo frenan:
datos sin respaldo o sin verificar, ortografía, pronunciación dudosa, imágenes o encuadres repetidos y lo que el
LLM ve deforme en un cuadro de cada clip. Si un control no pudo correr, también frena.

## Turnos de GPU

El LLM y Wan no entran juntos en la memoria de las placas, así que se turnan (`vstudio/gpu.py`):

- Política **`inactivo`** (por defecto): se genera cuando el LLM lleva `inactividad_min` sin uso. Se miran las
  métricas de `llama-server` y, si configurás `[gpu] sesiones_app`, la carpeta donde otra aplicación que usa el
  mismo LLM guarda sus sesiones. Política **`inmediato`**: se genera apenas hay algo aprobado.
- Durante el turno se escribe la marca `data/turno_gpu.json`. Si el proceso muere, el próximo ciclo y el
  `ExecStopPost` del servicio (`scripts/restaurar.sh`) devuelven el LLM. Si estaba apagado antes del turno, se
  deja apagado.
- A mano: `python -m vstudio restaurar` (no actúa si el turno sigue vivo).

## Números medidos (2 × RTX 3090)

| Paso | Tiempo |
|---|---|
| Plan (bajar la nota + LLM con imágenes) | ~2 min |
| Guionista (15 escenas) | 1,5-4 min por intento (casi todo es razonamiento) |
| Clip de 720×1280, 5 s, Wan 2.2 14B + LoRA de 4 pasos | 435-486 s, pico de 21-23 GB de VRAM |
| El mismo clip con cuadros intermedios (RIFE, 161 cuadros a 30 por segundo) | ~18 s más |
| 15 clips (reel de 76 s) con las 2 placas | 58,5 min |
| Escena de video real o de foto | segundos, sin GPU |
| El LLM vuelve a estar listo | ~10 s |

## Lo que aprendimos

- **Mostrar video real cambia más que mejorar la IA.** Con todas las escenas animadas a partir de las mismas
  fotos, los reels se veían como una presentación de diapositivas. Mezclar tomas reales, fotos de la noticia y
  alguna animación, y cambiar de imagen cada ~3 s, fue el cambio más grande.
- **Lo que entra blando a Wan sale blando.** Estirar una foto chica antes de animarla da un clip borroso y
  cuesta lo mismo que uno bueno. `vstudio/imagenes.py` limita el estirado a 1,25×: abre el recorte hasta donde
  la imagen aguanta y, si ni entera alcanza, la escena va como foto con paneo.
- **Subir la resolución de Wan ayuda poco para lo que cuesta.** En una prueba controlada con dos escenas,
  864×1536 dio +41 % de nitidez en un primer plano y +7 % en un plano general, con 1,7× de tiempo y la VRAM al
  límite de las placas de 24 GB.
- **Más pasos no dieron más nitidez.** El LoRA de 4 pasos con 8 pasos dio lo mismo; sin LoRA (20 pasos) tardó
  9 veces más y salió peor.
- **El LLM cuenta mal los días.** La cuenta regresiva se calcula en código (`vstudio/fechas.py`) y se le pasa
  hecha; el guion se revisa contra ese número.
- **Los TTS en español latino pronuncian mal el voseo.** El guion va en tú, y Whisper escucha cada escena sin
  ver el texto: si una palabra con tilde o eñe no se oye bien, la escena se vuelve a generar con otra semilla.
- **Los ejemplos del prompt se copian.** Un prompt con «pero ojo» de ejemplo dio guiones con «ojo» tres veces.
  Las reglas de `vstudio/guion.py` se chequean en código y el guionista corrige con el motivo.

## Criterio de guion

El guion lo escribe un paso aparte (`vstudio/guionista.py`): el plan con imágenes reparte las escenas y el
guionista, sin imágenes y con razonamiento, escribe lo que se dice y lo que se lee.

**Cómo suena**: un fan contándole la noticia a un amigo, no una lista de datos. Un hilo, pocos datos bien
contados con su consecuencia para el que mira, frases completas con ritmo variado, palabras de todos los días.

**Estructura**: gancho (escena 1, hasta 12 palabras, sin saludos) → promesa de algo concreto que se cumple al
final → desarrollo con un giro cada 3-4 escenas → remate → una sola pregunta de cierre, sin cebo de interacción.

**Se mide en cada guion** (si falla, el guionista lo corrige con el motivo, hasta 3 intentos): gancho largo o con
saludo, escenas fuera de 9-21 palabras, cierre sin pregunta, escenas de relleno, cebo, muletillas repetidas, la
misma frase en 3 escenas o más, anglicismos y jerga de nota de prensa.

**Verificación** (`vstudio/verificador.py`): una pasada aparte compara el guion con la nota y lista lo inventado.
No cambia el guion: lo pone primero en la advertencia del plan.

## Operación

```bash
cd ~/video-studio/app
systemctl --user status video-studio          # estado del servicio
journalctl --user -u video-studio -f          # logs del servicio
tail -f ~/video-studio/logs/vstudio.log       # log propio (rotativo)
.venv/bin/python -m vstudio estado            # proyectos activos
.venv/bin/python -m vstudio planificar --texto "https://..."   # plan sin Telegram
.venv/bin/python -m vstudio aprobar ID && .venv/bin/python -m vstudio procesar --esperar
.venv/bin/python -m vstudio tomas             # resumen de la biblioteca de video real
.venv/bin/python -m pytest -q                 # tests
```

## Seguridad

- Sin puertos nuevos: Telegram va por long polling, y ComfyUI y el LLM escuchan solo en 127.0.0.1.
- El bot solo atiende chats privados de usuarios vinculados; a los demás no les responde.
- `vstudio/red.py`: solo http/https a 80/443 y sin proxies del entorno. Bloquea IPs internas en cada salto y
  verifica la IP real después de conectar (DNS rebinding). Tiene topes de tamaño y de tiempo.
- Los videos que entran se abren con FFmpeg limitado a archivos locales y a contenedores conocidos.
- El token nunca se loguea y se borra de los mensajes de error.
- A tener en cuenta: si el usuario del servicio puede manejar Docker (para apagar y prender el LLM), una falla
  en una librería que procesa una nota maliciosa equivale a root. Conviene darle solo
  `docker stop/start llm-server` por sudoers.

## Licencia y avisos

- El **código** de este repo se publica bajo licencia **MIT** (ver `LICENSE`).
- **Grand Theft Auto**, **GTA VI** y **Rockstar Games** son marcas de Take-Two Interactive. Este proyecto no
  tiene relación con ellas y no incluye material suyo.
- Cada modelo tiene su licencia (Wan 2.2, Chatterbox, Whisper, ACE-Step, Qwen): revisala antes de usarlo, sobre
  todo para un uso comercial. Las muestras de voz de las demos no traen una licencia clara para publicar.
- Las imágenes de las notas son de terceros.
- Facebook pide marcar los videos realistas hechos con IA; el publicador lo hace.
