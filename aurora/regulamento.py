"""Consulta ao regulamento por capítulo.

GARANTIA 4: o regulamento nunca é carregado inteiro. O especialista recebe só
o índice (títulos) e lê um capítulo por vez com a tool ler_capitulo, dentro da
sua própria sessão em memória (AgentTool). Para a sessão do morador volta só a
resposta final do especialista.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from .config import DADOS_DIR

_CABECALHO = re.compile(r"^##\s+Cap[íi]tulo\s+([IVXLC]+)\s*[:\-–]\s*(.+?)\s*$", re.I)
_ROMANOS = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def _romano_para_int(romano: str) -> int:
    total = 0
    anterior = 0
    for letra in reversed(romano.upper()):
        valor = _ROMANOS[letra]
        total = total - valor if valor < anterior else total + valor
        anterior = max(anterior, valor)
    return total


def _normalizar(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()


@lru_cache
def capitulos() -> list[dict]:
    texto = (DADOS_DIR / "regulamento.md").read_text(encoding="utf-8")
    resultado: list[dict] = []
    atual: dict | None = None
    for linha in texto.splitlines():
        cabecalho = _CABECALHO.match(linha)
        if cabecalho:
            atual = {
                "romano": cabecalho.group(1).upper(),
                "numero": _romano_para_int(cabecalho.group(1)),
                "titulo": cabecalho.group(2),
                "linhas": [linha],
            }
            resultado.append(atual)
        elif atual is not None:
            atual["linhas"].append(linha)
    for cap in resultado:
        cap["texto"] = "\n".join(cap.pop("linhas")).strip()
    return resultado


def indice() -> str:
    """Só os títulos dos capítulos, sem nenhum conteúdo."""
    return "\n".join(f"- Capítulo {c['romano']}: {c['titulo']}" for c in capitulos())


def capitulo(referencia: str) -> dict | None:
    """Aceita 'IV', '4', 'Capítulo IV' ou uma palavra do título ('piscina')."""
    ref = _normalizar(referencia or "").replace("capitulo", "").strip(" :.-")
    if not ref:
        return None
    for cap in capitulos():
        if ref == cap["romano"].lower() or ref == str(cap["numero"]):
            return cap
    for cap in capitulos():
        if ref in _normalizar(cap["titulo"]):
            return cap
    return None
