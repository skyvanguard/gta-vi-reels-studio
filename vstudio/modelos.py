"""Modelo de datos inmutable: Proyecto -> Fuente + Plan -> Escenas.

Los cambios se hacen con dataclasses.replace (nunca se muta un objeto existente).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum


class Estado(StrEnum):
    PLANIFICANDO = "planificando"
    ESPERANDO_APROBACION = "esperando_aprobacion"
    EN_COLA = "en_cola"
    GENERANDO = "generando"
    MONTANDO = "montando"
    PROGRAMADO = "programado"  # el reel esta armado y espera su horario de la agenda para llegarle al editor
    ENTREGADO = "entregado"
    ERROR = "error"
    CANCELADO = "cancelado"


ESTADOS_ACTIVOS = (Estado.EN_COLA, Estado.GENERANDO, Estado.MONTANDO)
# como se hace el clip de una escena (Escena.medio; no confundir con Fuente.medio, el medio periodistico)
MEDIO_IA, MEDIO_TOMA, MEDIO_FOTO = "ia", "toma", "foto"


def ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Escena:
    n: int
    imagen: str  # recorte 9:16 (archivo dentro de la carpeta del proyecto)
    origen: str  # imagen fuente de la que sale el recorte
    centro_x: float
    texto: str  # lo que se lee en pantalla
    narracion: str  # lo que dice la voz (puede diferir: "GTA seis" vs "GTA VI")
    motion_prompt: str = ""
    negative_extra: str = ""
    seed: int = 42
    zoom: float = 1.0  # 1 = toda la altura; 1.6 = primer plano
    centro_y: float = 0.5
    sujeto: str = ""  # lo que se ve en la imagen (para que el guionista escriba algo que pegue)
    clip: str = ""
    audio: str = ""
    segundos_gen: float = 0.0
    vram_pico_mib: int = 0
    error: str = ""
    revisada: bool = False  # el Qwen ya miro el clip (no se vuelve a mirar en cada montaje)
    regeneraciones: int = 0  # cuantas veces se rehizo por verse deforme
    # como se hace el clip: "ia" (Wan anima la imagen, en el turno de GPU), "toma" (video real de la biblioteca de
    # tomas, recortado) o "foto" (una foto de la noticia con un paneo). Los dos ultimos se arman con el plan, sin GPU
    medio: str = MEDIO_IA
    extras: tuple[str, ...] = ()  # tomas de video que siguen al clip dentro de la escena (cambia la imagen cada ~3 s)


@dataclass(frozen=True)
class Fuente:
    url: str = ""
    titulo: str = ""
    fecha: str = ""  # ISO yyyy-mm-dd si se pudo leer
    texto: str = ""
    imagenes: tuple[str, ...] = ()  # archivos dentro de la carpeta del proyecto
    nota_usuario: str = ""
    descripciones: tuple[str, ...] = ()  # lo que muestra cada imagen ("" si no se sabe); las oficiales lo traen
    guion: tuple[str, ...] = ()  # el guion del editor, oracion por oracion: la narracion va tal cual
    aviso: str = ""  # lo que no salio al juntar el material (p. ej. el link no se pudo leer)
    medio: str = ""  # de donde es la noticia ("IGN", "Rockstar Games"): va en el sello "FUENTE: ..." del reel


@dataclass(frozen=True)
class Gancho:
    """Alternativa para los primeros segundos: texto grande en pantalla + primera frase de la narracion."""
    titular: str
    narracion: str


@dataclass(frozen=True)
class Plan:
    titular: str
    escenas: tuple[Escena, ...]
    texto_post: str = ""
    advertencia: str = ""
    ganchos: tuple[Gancho, ...] = ()  # alternativas para la escena 1 (/gancho N)


@dataclass(frozen=True)
class Proyecto:
    id: str
    chat_id: int
    estado: Estado
    fuente: Fuente
    plan: Plan | None = None
    con_voz: bool = True
    variante: int = 0
    segundos_objetivo: int = 15
    reel: str = ""
    error: str = ""
    creado: str = field(default_factory=ahora_iso)
    actualizado: str = field(default_factory=ahora_iso)
    solo_animacion: bool = False  # "solo video": la imagen animada, sin voz, sin guion y sin textos en pantalla
    con_subtitulos: bool = True  # con voz: subtitulos palabra por palabra; sin voz: el texto de cada escena
    con_titular: bool = True  # el titular de los primeros segundos
    automatico: bool = False  # lo armo el bot con las noticias del dia (sale solo a Facebook si pasa el control)
    publicacion: str = ""  # Facebook: "" | "revisar" | "subiendo" | "programado" | "retirado" | "error"
    fb_id: str = ""  # ID del video en Facebook
    fb_hora: str = ""  # cuando sale (ISO, con zona)
    formato: str = "noticia"  # la estructura del guion (formatos.py): noticia, sabias, ranking, comparacion
    enviar_a: str = ""  # PROGRAMADO: cuando le llega al editor (ISO, con zona; vacio = en el proximo ciclo)
    entregas: int = 0  # cuantas veces le llego al editor (la agenda es para la primera: un rearmado va enseguida)

    @property
    def muestra_textos(self) -> bool:
        return self.con_subtitulos and not self.solo_animacion

    @property
    def muestra_titular(self) -> bool:
        return self.con_titular and not self.solo_animacion

    def con(self, **cambios) -> "Proyecto":
        """Copia con cambios y `actualizado` renovado."""
        return replace(self, actualizado=ahora_iso(), **cambios)


def reemplazar_escena(plan: Plan, escena: Escena) -> Plan:
    escenas = tuple(escena if e.n == escena.n else e for e in plan.escenas)
    return replace(plan, escenas=escenas)


def proyecto_a_dict(p: Proyecto) -> dict:
    return asdict(p)


def proyecto_desde_dict(d: dict) -> Proyecto:
    fuente = Fuente(**{**d["fuente"], "imagenes": tuple(d["fuente"].get("imagenes", ())),
                       "descripciones": tuple(d["fuente"].get("descripciones", ())),
                       "guion": tuple(d["fuente"].get("guion", ()))})
    plan = None
    if d.get("plan"):
        escenas = tuple(Escena(**{**e, "extras": tuple(e.get("extras", ()))}) for e in d["plan"]["escenas"])
        ganchos = tuple(Gancho(**g) for g in d["plan"].get("ganchos", ()))
        plan = Plan(**{**d["plan"], "escenas": escenas, "ganchos": ganchos})
    return Proyecto(**{**d, "estado": Estado(d["estado"]), "fuente": fuente, "plan": plan})
