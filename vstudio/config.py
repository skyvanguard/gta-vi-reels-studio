"""Configuracion de Video Studio: un TOML opcional sobre valores por defecto.

Cada seccion del TOML ([llm], [gpu], [video], [voz], [telegram], [montaje], [subtitulos], [automatico]) pisa solo
las claves que define. Las rutas aceptan "~".
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

RAIZ = Path.home() / "video-studio"


def _p(valor: str | Path) -> Path:
    return Path(valor).expanduser()


@dataclass(frozen=True)
class ConfigLlm:
    url: str = "http://127.0.0.1:8080"
    clave_archivo: Path = _p("~/llm-server/api-keys.txt")
    modelo: str = "qwen"  # el nombre con el que el servidor publica el modelo
    timeout_s: int = 600


@dataclass(frozen=True)
class ConfigGpu:
    politica: str = "inactivo"  # "inactivo" (espera a que nadie use el Qwen) | "inmediato"
    inactividad_min: int = 15
    max_turno_min: int = 75
    comfy_puertos: tuple[int, ...] = (8188, 8189)
    # carpeta donde otra aplicacion que usa el mismo Qwen guarda sus sesiones (*/*/session.jsonl*): si
    # escribe ahi, cuenta como uso. Sin valor, solo se miran las metricas del servidor del LLM
    sesiones_app: Path | None = None
    script_qwen: Path = RAIZ / "app" / "scripts" / "qwen.sh"
    script_comfy: Path = RAIZ / "app" / "scripts" / "comfy.sh"
    comfy_dir: Path = RAIZ / "ComfyUI"
    aviso_espera_h: float = 6.0


@dataclass(frozen=True)
class ConfigVideo:
    ancho: int = 720
    alto: int = 1280
    duracion_s: float = 5.0
    fps_modelo: int = 16
    workflow: Path = RAIZ / "app" / "workflows" / "wan" / "wan22_i2v_14b.api.json"
    timeout_clip_s: int = 1800
    revisar_deformes: bool = True  # el Qwen mira un cuadro de cada clip y rehace los deformes
    max_regeneraciones: int = 2  # por escena: despues va igual, con un aviso
    # clips mas fluidos: Wan entrega 16 cuadros por segundo y el reel va a 30; antes se repetian
    # cuadros y al lado del video real se veia entrecortado. ComfyUI genera un cuadro intermedio entre cada par
    # (RIFE, nodos propios: no hace falta instalar nada) y el clip sale con 161 cuadros a 30 por segundo
    interpolar: bool = True
    modelo_interpolacion: str = "rife_v4.25.safetensors"  # en ComfyUI/models/frame_interpolation
    fps_salida: int = 30

    @property
    def cuadros(self) -> int:
        return int(self.duracion_s * self.fps_modelo) + 1  # Wan pide 4k+1 cuadros: 81 = 5,06 s a 16 fps

    @property
    def segundos_clip(self) -> float:
        if self.interpolar:  # 2 x 81 - 1 = 161 cuadros a 30 por segundo: 5,37 s
            return (2 * self.cuadros - 1) / self.fps_salida
        return self.cuadros / self.fps_modelo


@dataclass(frozen=True)
class ConfigVoz:
    motor: str = "chatterbox"  # "chatterbox" (espanol latino) | "qwen3" | "ninguna"
    voces_dir: Path = RAIZ / "assets" / "voz"
    dispositivo: str = "cpu"  # la voz se genera antes del turno de GPU: no alarga el tiempo sin Qwen
    # si la CPU esta saturada (otro trabajo en el servidor), la voz y el control de pronunciacion van en la GPU al
    # principio del turno (con la CPU llena, 3 frases tardaban 17 min; en la GPU, segundos)
    gpu_si_cpu_ocupada: bool = True
    umbral_carga: float = 0.75  # carga de 1 min / nucleos
    timeout_s: int = 1800
    chatterbox_dir: Path = RAIZ / "tts-cb"
    chatterbox_python: Path = RAIZ / "tts-cb" / ".venv" / "bin" / "python"
    chatterbox_script: Path = RAIZ / "app" / "scripts" / "tts_chatterbox.py"
    exageracion: float = 0.5  # expresividad (0.25-2.0; 0.5 = neutro)
    cfg_ritmo: float = 0.5  # ritmo / apego a la referencia (0.2-1.0)
    temperatura: float = 0.8
    # escenas que se vuelven a generar si Whisper no oye bien una palabra con tilde o enie (0 = sin control)
    reintentos_pronunciacion: int = 2
    python: Path = RAIZ / "tts" / ".venv" / "bin" / "python"  # Qwen3-TTS
    script: Path = RAIZ / "app" / "scripts" / "tts_qwen.py"
    modelos: Path = RAIZ / "tts" / "models"


@dataclass(frozen=True)
class ConfigTelegram:
    token_archivo: Path = RAIZ / "secrets" / "telegram_token"
    usuarios_archivo: Path = RAIZ / "secrets" / "telegram_usuarios.json"
    codigo_archivo: Path = RAIZ / "secrets" / "codigo_vinculacion"


@dataclass(frozen=True)
class ConfigMontaje:
    fuentes_dir: Path = RAIZ / "assets" / "fonts"
    musica_dir: Path = RAIZ / "assets" / "musica"
    marca_texto: str = ""
    volumen_musica: float = 0.15  # debajo de la voz; ademas baja sola cuando hay habla (sidechain)
    # ritmo y sonido: corte con acercamiento a mitad de escena, barra de progreso, sello de la fuente
    # y efectos propios (golpe, whoosh, tick)
    cortes_rapidos: bool = True
    barra_progreso: bool = True
    sello_fuente: bool = True
    efectos: bool = True
    volumen_efectos: float = 0.35
    minimo_monetizable_s: float = 62.0  # piso de los reels de 60 s o mas (margen sobre el minuto)


@dataclass(frozen=True)
class ConfigSubtitulos:
    activo: bool = True  # subtitulos palabra por palabra (los reels arrancan sin sonido en el feed)
    python: Path = RAIZ / "tts-cb" / ".venv" / "bin" / "python"  # el venv de Chatterbox trae faster-whisper
    script: Path = RAIZ / "app" / "scripts" / "palabras_whisper.py"
    modelo: str = "small"  # solo hace falta el tiempo de cada palabra: el texto ya se conoce
    # para el control de pronunciacion hace falta oir bien (sin pista): el grande (se descarga una vez)
    modelo_revision: str = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    hilos: int = 16
    dispositivo: str = "cpu"  # "cuda" solo dentro del turno de GPU (con el Qwen apagado)
    timeout_s: int = 900


@dataclass(frozen=True)
class ConfigMusica:
    """Musica de fondo propia generada con ACE-Step 1.5 (musica.py)."""
    activo: bool = True
    acestep_dir: Path = RAIZ / "acestep"
    python: Path = RAIZ / "acestep" / ".venv" / "bin" / "python"
    script: Path = RAIZ / "app" / "scripts" / "musica_acestep.py"
    por_estilo: int = 4  # 6 estilos x 4 = 24 temas
    por_turno: int = 6  # en cada turno de GPU se completan hasta estos (mientras falten)
    segundos: float = 95.0  # mas que cualquier reel (90 s max): no se nota la vuelta del loop
    timeout_s: int = 1800


@dataclass(frozen=True)
class ConfigFacebook:
    """Publicacion en la pagina de Facebook (publicador.py)."""
    activo: bool = True  # los reels automaticos que pasan el control se programan solos
    horarios: tuple[str, ...] = ("08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00", "22:00")
    max_por_dia: int = 8  # Meta permite 30 por API cada 24 h
    con_ia: bool = True  # is_ai_generated: Meta pide marcar el contenido hecho con IA
    token_archivo: Path = RAIZ / "secrets" / "facebook_token"  # token de PAGINA (600)
    pagina_archivo: Path = RAIZ / "secrets" / "facebook_pagina.json"
    token_usuario_archivo: Path = RAIZ / "secrets" / "facebook_usuario_token"  # solo para configurar; se borra


@dataclass(frozen=True)
class ConfigAutomatico:
    """Reels automaticos con las noticias del dia (automatico.py)."""
    activo: bool = True  # /auto off lo apaga (queda en data/automatico.json)
    hora: str = "05:00"  # hora local (zona_horaria): busca, planifica y genera
    segundos: int = 30
    maximo: int = 8  # tope por dia
    horas_atras: float = 30.0  # noticias de las ultimas horas (lo ya publicado se descarta igual)
    formatos: bool = True  # hasta la mitad de la tanda sale en otro formato (¿sabias que?, ranking, antes y ahora)


@dataclass(frozen=True)
class ConfigAgenda:
    """Agenda de publicacion (agenda.py): los reels automaticos llegan repartidos en el dia."""
    activa: bool = True  # /agenda off la apaga (queda en data/agenda.json)
    # hora local (zona_horaria): un reel por horario; si hay mas reels que horarios, los que sobran van al ultimo
    horarios: tuple[str, ...] = ("10:30", "12:00", "13:30", "15:00", "16:30", "18:00", "19:30", "21:00")


@dataclass(frozen=True)
class Config:
    datos: Path = RAIZ / "data"
    proyectos: Path = RAIZ / "proyectos"
    zona_horaria: str = "UTC"  # la de la agenda y la corrida automatica: "America/Bogota", "Europe/Madrid"...
    escenas_por_reel: int = 3  # sin uso: se calcula con segundos_por_escena (queda por compatibilidad del TOML)
    segundos_por_defecto: int = 75  # mas de un minuto: monetizable. /largo lo cambia por chat
    segundos_por_escena: float = 4.9  # neto en el reel: clip de 5,06 s con voz de 10-15 palabras, menos el fundido
    max_escenas: int = 18
    noticia_vieja_dias: int = 7
    buscador_url: str = "http://127.0.0.1:8888"  # SearXNG local; "" desactiva la busqueda de imagenes
    biblioteca_dir: Path = RAIZ / "assets" / "biblioteca"  # imagenes oficiales (rockstargames.com/VI/media)
    # video real: tomas de los trailers oficiales y clips del editor, cortadas con `vstudio tomas`
    tomas_dir: Path = RAIZ / "assets" / "tomas"
    tomas_extra: bool = True  # cada escena lleva tomas que siguen a su clip: la imagen cambia cada ~3 s
    fotos_de_la_noticia: bool = True  # busca fotos de lo que la nota nombra (con buscador_url) y el Qwen las filtra
    diccionario: Path = RAIZ / "assets" / "diccionario" / "es_ES"  # hunspell-es (.aff/.dic): control de ortografia
    llm: ConfigLlm = field(default_factory=ConfigLlm)
    gpu: ConfigGpu = field(default_factory=ConfigGpu)
    video: ConfigVideo = field(default_factory=ConfigVideo)
    voz: ConfigVoz = field(default_factory=ConfigVoz)
    telegram: ConfigTelegram = field(default_factory=ConfigTelegram)
    montaje: ConfigMontaje = field(default_factory=ConfigMontaje)
    subtitulos: ConfigSubtitulos = field(default_factory=ConfigSubtitulos)
    automatico: ConfigAutomatico = field(default_factory=ConfigAutomatico)
    facebook: ConfigFacebook = field(default_factory=ConfigFacebook)
    musica: ConfigMusica = field(default_factory=ConfigMusica)
    agenda: ConfigAgenda = field(default_factory=ConfigAgenda)


def _aplicar(base, valores: dict):
    """Devuelve una copia de la dataclass `base` con las claves de `valores` (rutas expandidas)."""
    nombres = {f.name: f for f in fields(base)}
    cambios = {}
    for clave, valor in valores.items():
        if clave not in nombres:
            raise ValueError(f"clave de configuracion desconocida: {clave}")
        tipo = str(nombres[clave].type)
        if "Path" in tipo:  # una ruta opcional vacia es "sin valor": Path("") seria la carpeta de trabajo
            cambios[clave] = _p(valor) if valor or "None" not in tipo else None
        elif tipo.startswith("tuple"):
            cambios[clave] = tuple(valor)
        else:
            cambios[clave] = valor
    return replace(base, **cambios)


def cargar_config(ruta: Path | None = None) -> Config:
    ruta = ruta or RAIZ / "config.toml"
    cfg = Config()
    if not ruta.exists():
        return cfg
    datos = tomllib.loads(ruta.read_text(encoding="utf-8"))
    secciones = {"llm", "gpu", "video", "voz", "telegram", "montaje", "subtitulos", "automatico", "facebook",
                  "musica", "agenda"}
    generales = {k: v for k, v in datos.items() if k not in secciones}
    cfg = _aplicar(cfg, generales)
    for nombre in secciones & datos.keys():
        cfg = replace(cfg, **{nombre: _aplicar(getattr(cfg, nombre), datos[nombre])})
    return cfg
