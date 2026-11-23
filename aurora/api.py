"""API HTTP do assistente (FastAPI + Runner do ADK)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from google.adk.apps import App
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import Session
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types
from pydantic import BaseModel

from . import banco, config
from .agentes import root_agent
from .tools import CHAVE_APARTAMENTO

logger = logging.getLogger("aurora.api")

# Nome da chamada sintética que o ADK cria quando uma tool pede confirmação.
PEDIDO_CONFIRMACAO = "adk_request_confirmation"

# GARANTIA 3: sessões e eventos persistidos em SQLite (estado/sessoes.db).
session_service = SqliteSessionService(str(config.SESSOES_DB))
runner = Runner(
    app=App(name=config.APP_NAME, root_agent=root_agent),
    session_service=session_service,
)

# Uma execução por vez em cada sessão (sessões diferentes rodam em paralelo).
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@asynccontextmanager
async def lifespan(_: FastAPI):
    banco.inicializar()
    yield


app = FastAPI(title="Residencial Aurora", lifespan=lifespan)


class NovaSessao(BaseModel):
    apartamento: str | int


class NovaMensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


# ---------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------


async def _carregar_sessao(session_id: str) -> tuple[Session, str]:
    apartamento = banco.apartamento_da_sessao(session_id)
    if apartamento is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    sessao = await session_service.get_session(
        app_name=config.APP_NAME, user_id=apartamento, session_id=session_id
    )
    if sessao is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    return sessao, apartamento


def _confirmacoes_pendentes(eventos: list[Event]) -> list[dict[str, Any]]:
    """Pedidos de confirmação da sessão que ainda não receberam resposta."""
    pedidos: dict[str, types.FunctionCall] = {}
    respondidos: set[str] = set()
    for evento in eventos:
        for chamada in evento.get_function_calls():
            if chamada.name == PEDIDO_CONFIRMACAO and chamada.id:
                pedidos[chamada.id] = chamada
        for resposta in evento.get_function_responses():
            if resposta.name == PEDIDO_CONFIRMACAO and resposta.id:
                respondidos.add(resposta.id)

    pendentes = []
    for id_pedido, chamada in pedidos.items():
        if id_pedido in respondidos:
            continue
        args = chamada.args or {}
        original = args.get("originalFunctionCall") or {}
        confirmacao = args.get("toolConfirmation") or {}
        payload = confirmacao.get("payload") or {}
        pendentes.append(
            {
                "id": id_pedido,
                "acao": payload.get("acao") or original.get("name"),
                "detalhes": payload.get("detalhes") or original.get("args") or {},
            }
        )
    return pendentes


async def _executar(session_id: str, apartamento: str, mensagem: types.Content) -> str:
    textos: list[str] = []
    async for evento in runner.run_async(
        user_id=apartamento, session_id=session_id, new_message=mensagem
    ):
        if evento.author == "user" or evento.partial or not evento.content:
            continue
        for parte in evento.content.parts or []:
            if parte.text and not parte.thought:
                textos.append(parte.text.strip())
    return "\n".join(t for t in textos if t)


async def _resposta(session_id: str, texto: str) -> dict[str, Any]:
    sessao, _ = await _carregar_sessao(session_id)
    return {"resposta": texto, "confirmacoes_pendentes": _confirmacoes_pendentes(sessao.events)}


# ---------------------------------------------------------------------------
# Rotas de conversa
# ---------------------------------------------------------------------------


@app.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao) -> dict[str, str]:
    apartamento = str(corpo.apartamento).strip()
    if not banco.apartamento_existe(apartamento):
        raise HTTPException(status_code=422, detail="Apartamento inexistente.")
    # GARANTIA 2: o apartamento é fixado aqui, uma única vez: vira o user_id da
    # sessão no ADK, vai para o state e fica registrado em condominio.db.
    sessao = await session_service.create_session(
        app_name=config.APP_NAME,
        user_id=apartamento,
        state={CHAVE_APARTAMENTO: apartamento},
    )
    banco.registrar_sessao(sessao.id, apartamento)
    return {"session_id": sessao.id}


@app.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: NovaMensagem) -> dict[str, Any]:
    async with _locks[session_id]:
        _, apartamento = await _carregar_sessao(session_id)
        mensagem = types.Content(role="user", parts=[types.Part(text=corpo.texto)])
        texto = await _executar(session_id, apartamento, mensagem)
        return await _resposta(session_id, texto)


@app.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, corpo: RespostaConfirmacao) -> dict[str, Any]:
    async with _locks[session_id]:
        sessao, apartamento = await _carregar_sessao(session_id)

        # GARANTIA 1: só aceita resposta para um pedido pendente DESTA sessão.
        pendentes = {p["id"] for p in _confirmacoes_pendentes(sessao.events)}
        if corpo.id not in pendentes:
            raise HTTPException(
                status_code=409,
                detail="Não existe confirmação pendente com esse id nesta sessão.",
            )

        # Devolve a resposta como FunctionResponse do adk_request_confirmation.
        # O Runner encontra a invocação que fez o pedido e o agente principal
        # executa a tool original (aprovada) ou recebe a recusa (negada).
        mensagem = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=corpo.id,
                        name=PEDIDO_CONFIRMACAO,
                        response={"confirmed": corpo.confirmado},
                    )
                )
            ],
        )
        texto = await _executar(session_id, apartamento, mensagem)
        return await _resposta(session_id, texto)


@app.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str) -> list[dict[str, Any]]:
    sessao, _ = await _carregar_sessao(session_id)
    return [
        evento.model_dump(mode="json", exclude_none=True, by_alias=True)
        for evento in sessao.events
    ]


# ---------------------------------------------------------------------------
# Rotas de verificação (leem o banco direto, sem passar pelo modelo)
# ---------------------------------------------------------------------------


@app.get("/apartamentos/{apartamento}/reservas")
async def reservas_do_apartamento(apartamento: str) -> list[dict[str, str]]:
    return banco.listar_reservas(apartamento)


@app.get("/apartamentos/{apartamento}/visitantes")
async def visitantes_do_apartamento(apartamento: str) -> list[dict[str, str]]:
    return banco.listar_visitantes(apartamento)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)


if __name__ == "__main__":
    main()
