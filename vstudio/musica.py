"""Musica de fondo propia ("mas atractivo"; sin musica de terceros por derechos).

Una biblioteca de temas instrumentales generados con ACE-Step 1.5 en el servidor (assets/musica/<estilo>/), por estilos.
Se genera con la GPU libre (turno de GPU: `vstudio musica`, y un poco en cada turno hasta completar). Para cada
reel se elige el estilo por lo que cuenta (una filtracion: suspenso; un anuncio oficial: epico) y, dentro del
estilo, el tema que hace mas que no sale.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import biblioteca

log = logging.getLogger(__name__)

ESTILOS = {
    "synthwave": "80s synthwave, retro Miami night drive, analog synths, punchy drums, neon, energetic, instrumental",
    "trap": "dark cinematic trap beat, heavy 808 bass, crisp hi-hats, tense and confident, instrumental",
    "epico": "epic cinematic trailer music, big percussion, rising strings and brass, triumphant, instrumental",
    "funk80": "80s funk pop, slap bass, bright synth brass, upbeat groove, retro and fun, instrumental",
    "suspenso": "suspenseful dark ambient, ticking pulse, low drones, mysterious tension, instrumental",
    "chill": "chill retro synth groove, sunset beach, mellow keys, relaxed beat, warm, instrumental",
}
POR_DEFECTO = ("synthwave", "funk80", "trap", "chill")  # rotan cuando la nota no pide un clima
CLIMAS = (  # (estilo, palabras de la nota que lo piden)
    ("suspenso", r"filtra|rumor|secreto|misterio|ocult|leak|insider|pista|teoria|hallaz|codigo"),
    ("epico", r"trailer|tr[aá]iler|lanzamiento|oficial|confirm|record|r[eé]cord|ventas|anuncio|estreno|precarga"),
    ("trap", r"polic|robo|atraco|crimen|armas|banda|carcel|persecuc|asalto|swat"),
)


@dataclass(frozen=True)
class Pedido:
    caption: str
    segundos: float
    semilla: int
    destino: Path


def temas(musica_dir: Path) -> dict[str, list[Path]]:
    """Los temas de cada estilo que ya hay."""
    return {estilo: sorted((musica_dir / estilo).glob("*.wav")) for estilo in ESTILOS}


def faltantes(musica_dir: Path, por_estilo: int, maximo: int, segundos: float) -> list[Pedido]:
    """Los proximos temas a generar, de a uno por estilo (asi, aunque se generen pocos, hay de todos)."""
    actuales = temas(musica_dir)
    pedidos: list[Pedido] = []
    for ronda in range(por_estilo):
        for estilo, caption in ESTILOS.items():
            if len(pedidos) >= maximo:
                return pedidos
            if len(actuales[estilo]) > ronda:
                continue
            n = ronda + 1
            pedidos.append(Pedido(caption=caption, segundos=segundos, semilla=secrets.randbelow(2**31),
                                  destino=musica_dir / estilo / f"{estilo}_{n:02d}.wav"))
    return pedidos


def generar(cfg, pedidos: list[Pedido]) -> list[Path]:
    """Corre ACE-Step (en su venv, en la GPU: tiene que estar libre). Devuelve los temas que salieron."""
    if not pedidos:
        return []
    if not (cfg.python.exists() and cfg.script.exists() and cfg.acestep_dir.exists()):
        raise RuntimeError(f"falta ACE-Step ({cfg.acestep_dir}) o su script")
    entrada = pedidos[0].destino.parent.parent / "pedidos.json"
    entrada.parent.mkdir(parents=True, exist_ok=True)
    entrada.write_text(json.dumps([{"caption": p.caption, "segundos": p.segundos, "semilla": p.semilla,
                                    "destino": str(p.destino)} for p in pedidos], ensure_ascii=False),
                       encoding="utf-8")
    res = subprocess.run([str(cfg.python), str(cfg.script), "--raiz", str(cfg.acestep_dir), "--pedidos",
                          str(entrada), "--dispositivo", "cuda"], capture_output=True, text=True,
                         timeout=cfg.timeout_s, check=False)
    linea = next((x for x in res.stdout.splitlines() if x.startswith("RESULTADO_JSON ")), None)
    if res.returncode != 0 or linea is None:
        raise RuntimeError(f"ACE-Step fallo (codigo {res.returncode}): {res.stderr[-400:]}")
    salieron = []
    for r in json.loads(linea.removeprefix("RESULTADO_JSON ")):
        if r.get("error"):
            log.warning("un tema no salio (%s): %s", r.get("destino"), r["error"])
        else:
            salieron.append(Path(r["destino"]))
    return salieron


def _plano(texto: str) -> str:
    sin = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in sin if not unicodedata.combining(c))


def estilo_para(texto: str, disponibles: set[str], turno: int = 0) -> str | None:
    """El estilo por lo que cuenta el reel; si no pide uno, rota entre los de siempre."""
    plano = _plano(texto)
    for estilo, patron in CLIMAS:
        if estilo in disponibles and re.search(patron, plano):
            return estilo
    rotacion = [e for e in POR_DEFECTO if e in disponibles] or sorted(disponibles)
    return rotacion[turno % len(rotacion)] if rotacion else None


def elegir(musica_dir: Path, texto: str, usos: biblioteca.Usos) -> Path | None:
    """El tema para un reel (None si todavia no hay musica). Marca el uso para rotar."""
    actuales = {e: t for e, t in temas(musica_dir).items() if t}
    if not actuales:
        return None
    vistos = usos.leer()
    estilo = estilo_para(texto, set(actuales), turno=len(vistos))
    tema = min(actuales[estilo], key=lambda t: (vistos.get(t.name, 0.0), t.name))
    usos.marcar([tema.name])
    return tema
