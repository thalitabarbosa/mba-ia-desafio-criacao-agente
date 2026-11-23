"""Armazenamento do condomínio em SQLite.

Os arquivos de dados/ são o estado inicial e nunca são alterados. As mudanças
feitas pelo assistente (reservas, cancelamentos, visitantes) ficam em
estado/condominio.db, que sobrevive ao reinício da API.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date
from functools import lru_cache
from typing import Iterator

from .config import CONDOMINIO_DB, DADOS_DIR

ESQUEMA = """
CREATE TABLE IF NOT EXISTS reservas (
    codigo       TEXT PRIMARY KEY,
    apartamento  TEXT NOT NULL,
    area         TEXT NOT NULL,
    data         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ativa'
                 CHECK (status IN ('ativa', 'cancelada')),
    criada_em    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cancelada_em TEXT
);

-- GARANTIA 5: no máximo uma reserva ATIVA por área e data.
-- O índice único parcial é verificado pelo SQLite no próprio INSERT,
-- então a exclusividade vale no instante da gravação.
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa_por_area_data
    ON reservas (area, data) WHERE status = 'ativa';

-- Todo código já emitido (inclusive de reservas canceladas e de antes de uma
-- restauração). Nunca é limpo, para que nenhum código se repita.
CREATE TABLE IF NOT EXISTS codigos_emitidos (
    codigo TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL,
    criada_em   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- GARANTIA 2: apartamento de cada sessão, gravado uma única vez no POST /sessoes.
CREATE TABLE IF NOT EXISTS sessoes (
    session_id  TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    criada_em   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS meta (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
"""


class DataOcupada(Exception):
    """A área já tem reserva ativa na data pedida."""


# ---------------------------------------------------------------------------
# Conexão e transações
# ---------------------------------------------------------------------------


def _conectar() -> sqlite3.Connection:
    con = sqlite3.connect(CONDOMINIO_DB, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    return con


@contextmanager
def _leitura() -> Iterator[sqlite3.Connection]:
    con = _conectar()
    try:
        yield con
    finally:
        con.close()


@contextmanager
def _transacao() -> Iterator[sqlite3.Connection]:
    """Transação com BEGIN IMMEDIATE: pega o lock de escrita logo no início."""
    con = _conectar()
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def inicializar() -> None:
    """Cria o esquema e, na primeira execução, carrega os dados iniciais."""
    con = _conectar()
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(ESQUEMA)
    finally:
        con.close()
    with _transacao() as con:
        ja_carregado = con.execute(
            "SELECT 1 FROM meta WHERE chave = 'dados_iniciais_carregados'"
        ).fetchone()
        if not ja_carregado:
            _carregar_dados_iniciais(con)


def _ler_json(nome: str):
    with open(DADOS_DIR / nome, encoding="utf-8") as arquivo:
        return json.load(arquivo)


def _carregar_dados_iniciais(con: sqlite3.Connection) -> None:
    con.execute("DELETE FROM reservas")
    con.execute("DELETE FROM visitantes")
    for r in _ler_json("reservas.json"):
        con.execute(
            "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
            (r["codigo"], str(r["apartamento"]), r["area"], r["data"]),
        )
        con.execute(
            "INSERT OR IGNORE INTO codigos_emitidos (codigo) VALUES (?)",
            (r["codigo"],),
        )
    for v in _ler_json("visitantes.json"):
        con.execute(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            (str(v["apartamento"]), v["nome"], v["data"]),
        )
    con.execute(
        "INSERT OR REPLACE INTO meta (chave, valor) VALUES ('dados_iniciais_carregados', '1')"
    )


def restaurar(apagar_sessoes: bool = False) -> None:
    """Volta reservas e visitantes ao estado dos arquivos de dados/."""
    inicializar()
    with _transacao() as con:
        _carregar_dados_iniciais(con)
        if apagar_sessoes:
            con.execute("DELETE FROM sessoes")


# ---------------------------------------------------------------------------
# Catálogo (somente leitura, vem dos arquivos de dados/)
# ---------------------------------------------------------------------------


@lru_cache
def apartamentos() -> dict[str, str]:
    return {str(a["numero"]): a["morador"] for a in _ler_json("apartamentos.json")}


def apartamento_existe(numero: str | None) -> bool:
    return bool(numero) and str(numero) in apartamentos()


@lru_cache
def areas() -> dict[str, dict]:
    return {a["id"]: a for a in _ler_json("areas.json")}


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return "-".join(sem_acento.lower().replace("_", " ").replace("-", " ").split())


def area_por_texto(texto: str) -> dict | None:
    """Aceita o id ('salao-de-festas') ou o nome ('Salão de festas')."""
    alvo = _normalizar(texto or "")
    if not alvo:
        return None
    for area in areas().values():
        if alvo in (area["id"], _normalizar(area["nome"])):
            return area
    for area in areas().values():
        chave = area["id"].split("-")[0]  # salao, churrasqueira, quadra
        if chave in alvo.split("-"):
            return area
    return None


def validar_data(texto: str) -> str | None:
    try:
        return date.fromisoformat((texto or "").strip()).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Sessões
# ---------------------------------------------------------------------------


def registrar_sessao(session_id: str, apartamento: str) -> None:
    with _transacao() as con:
        con.execute(
            "INSERT INTO sessoes (session_id, apartamento) VALUES (?, ?)",
            (session_id, apartamento),
        )


def apartamento_da_sessao(session_id: str) -> str | None:
    with _leitura() as con:
        linha = con.execute(
            "SELECT apartamento FROM sessoes WHERE session_id = ?", (session_id,)
        ).fetchone()
    return linha["apartamento"] if linha else None


# ---------------------------------------------------------------------------
# Reservas
# ---------------------------------------------------------------------------


def listar_reservas(apartamento: str) -> list[dict]:
    with _leitura() as con:
        linhas = con.execute(
            "SELECT codigo, area, data FROM reservas "
            "WHERE apartamento = ? AND status = 'ativa' ORDER BY data, codigo",
            (str(apartamento),),
        ).fetchall()
    return [dict(l) for l in linhas]


def data_livre(area_id: str, data: str) -> bool:
    """Diz só se a data está livre; nunca expõe de quem é a reserva."""
    with _leitura() as con:
        linha = con.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
            (area_id, data),
        ).fetchone()
    return linha is None


def _novo_codigo(con: sqlite3.Connection) -> str:
    while True:
        codigo = f"RSV-{secrets.token_hex(3).upper()}"
        usado = con.execute(
            "SELECT 1 FROM codigos_emitidos WHERE codigo = ? "
            "UNION SELECT 1 FROM reservas WHERE codigo = ?",
            (codigo, codigo),
        ).fetchone()
        if not usado:
            return codigo


def criar_reserva(apartamento: str, area_id: str, data: str) -> str:
    """Grava a reserva. Levanta DataOcupada se já houver reserva ativa.

    A exclusividade é garantida pelo índice único parcial
    ux_reserva_ativa_por_area_data, verificado no próprio INSERT: se duas
    gravações concorrentes chegarem juntas, só a primeira passa.
    """
    try:
        with _transacao() as con:
            codigo = _novo_codigo(con)
            con.execute("INSERT INTO codigos_emitidos (codigo) VALUES (?)", (codigo,))
            con.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
                (codigo, str(apartamento), area_id, data),
            )
    except sqlite3.IntegrityError as erro:
        raise DataOcupada(f"{area_id} em {data}") from erro
    return codigo


def cancelar_reserva(apartamento: str, area_id: str, data: str) -> list[str]:
    """Cancela as reservas ativas DO PRÓPRIO apartamento naquela área e data."""
    with _transacao() as con:
        linhas = con.execute(
            "SELECT codigo FROM reservas "
            "WHERE apartamento = ? AND area = ? AND data = ? AND status = 'ativa'",
            (str(apartamento), area_id, data),
        ).fetchall()
        codigos = [l["codigo"] for l in linhas]
        for codigo in codigos:
            con.execute(
                "UPDATE reservas SET status = 'cancelada', cancelada_em = CURRENT_TIMESTAMP "
                "WHERE codigo = ? AND apartamento = ?",
                (codigo, str(apartamento)),
            )
    return codigos


# ---------------------------------------------------------------------------
# Visitantes
# ---------------------------------------------------------------------------


def listar_visitantes(apartamento: str) -> list[dict]:
    with _leitura() as con:
        linhas = con.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id",
            (str(apartamento),),
        ).fetchall()
    return [dict(l) for l in linhas]


def autorizar_visitante(apartamento: str, nome: str, data: str) -> None:
    with _transacao() as con:
        con.execute(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            (str(apartamento), nome, data),
        )
