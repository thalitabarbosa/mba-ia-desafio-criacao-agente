"""Tools dos agentes.

Nenhuma tool recebe o apartamento como parâmetro: ele vem sempre de
_apartamento_da_sessao(), que lê o user_id que a API definiu na criação da
sessão (GARANTIA 2). O modelo não tem como escolher outro apartamento.
"""

from __future__ import annotations

from google.adk.tools import ToolContext

from . import banco, regulamento

CHAVE_APARTAMENTO = "apartamento"


class SessaoSemApartamento(Exception):
    pass


def _apartamento_da_sessao(tool_context: ToolContext) -> str:
    """Apartamento do morador autenticado.

    O user_id da sessão é definido pela API no POST /sessoes (user_id = número
    do apartamento) e o mesmo valor é gravado no state. Nenhum dos dois pode
    ser alterado pelo modelo. O AgentTool repassa o mesmo user_id e uma cópia
    do state para os especialistas.
    """
    apartamento = str(tool_context.user_id)
    no_state = tool_context.state.get(CHAVE_APARTAMENTO)
    if not banco.apartamento_existe(apartamento) or (
        no_state is not None and str(no_state) != apartamento
    ):
        raise SessaoSemApartamento("Sessão sem apartamento válido.")
    return apartamento


def _erro(mensagem: str, **extra) -> dict:
    return {"status": "erro", "mensagem": mensagem, **extra}


def _areas_validas() -> list[str]:
    return list(banco.areas().keys())


# ---------------------------------------------------------------------------
# Ações (agente principal)
# ---------------------------------------------------------------------------


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador.

    Áreas com taxa geram cobrança e ficam pendentes até o morador aprovar
    pelo aplicativo. Áreas sem taxa são reservadas na hora.

    Args:
        area: id ou nome da área: salao-de-festas, churrasqueira ou quadra.
        data: data da reserva no formato AAAA-MM-DD.
    """
    try:
        apartamento = _apartamento_da_sessao(tool_context)
    except SessaoSemApartamento as erro:
        return _erro(str(erro))

    info = banco.area_por_texto(area)
    if info is None:
        return _erro("Área desconhecida.", areas_validas=_areas_validas())
    dia = banco.validar_data(data)
    if dia is None:
        return _erro("Data inválida. Use o formato AAAA-MM-DD.")

    gera_cobranca = float(info["taxa"]) > 0

    # GARANTIA 1: cobrança só depois da confirmação vinda da rota de confirmações.
    if gera_cobranca:
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            if not banco.data_livre(info["id"], dia):
                return {
                    "status": "indisponivel",
                    "mensagem": f"{info['nome']} não está disponível em {dia}.",
                }
            tool_context.request_confirmation(
                hint=(
                    f"Reservar {info['nome']} em {dia} gera cobrança de "
                    f"R$ {float(info['taxa']):.2f}. Aprovar?"
                ),
                payload={
                    "acao": "reservar_area",
                    "detalhes": {
                        "area": info["id"],
                        "data": dia,
                        "taxa": float(info["taxa"]),
                    },
                },
            )
            tool_context.actions.skip_summarization = True
            return {
                "status": "aguardando_confirmacao",
                "mensagem": "A reserva gera cobrança e aguarda a aprovação do morador no aplicativo.",
            }
        if not confirmacao.confirmed:
            return {
                "status": "nao_aprovada",
                "mensagem": "O morador não aprovou a cobrança. Nada foi reservado.",
            }

    # GARANTIA 5: a exclusividade é checada pelo banco no instante do INSERT.
    try:
        codigo = banco.criar_reserva(apartamento, info["id"], dia)
    except banco.DataOcupada:
        return {
            "status": "indisponivel",
            "mensagem": f"{info['nome']} não está disponível em {dia}.",
        }
    return {
        "status": "reservada",
        "codigo": codigo,
        "area": info["id"],
        "data": dia,
        "cobranca": float(info["taxa"]) if gera_cobranca else None,
    }


def cancelar_reserva(area: str, data: str, tool_context: ToolContext) -> dict:
    """Cancela uma reserva do próprio apartamento (não pede confirmação).

    Args:
        area: id ou nome da área: salao-de-festas, churrasqueira ou quadra.
        data: data da reserva no formato AAAA-MM-DD.
    """
    try:
        apartamento = _apartamento_da_sessao(tool_context)
    except SessaoSemApartamento as erro:
        return _erro(str(erro))

    info = banco.area_por_texto(area)
    if info is None:
        return _erro("Área desconhecida.", areas_validas=_areas_validas())
    dia = banco.validar_data(data)
    if dia is None:
        return _erro("Data inválida. Use o formato AAAA-MM-DD.")

    codigos = banco.cancelar_reserva(apartamento, info["id"], dia)
    if not codigos:
        return {
            "status": "nao_encontrada",
            "mensagem": (
                f"O apartamento do morador não tem reserva ativa de {info['nome']} "
                f"em {dia}. Nada foi cancelado."
            ),
        }
    return {"status": "cancelada", "codigos": codigos, "area": info["id"], "data": dia}


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante no prédio. Sempre exige aprovação
    do morador pelo aplicativo antes de liberar.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    try:
        apartamento = _apartamento_da_sessao(tool_context)
    except SessaoSemApartamento as erro:
        return _erro(str(erro))

    nome_limpo = " ".join((nome or "").split())
    if not nome_limpo:
        return _erro("Informe o nome do visitante.")
    dia = banco.validar_data(data)
    if dia is None:
        return _erro("Data inválida. Use o formato AAAA-MM-DD.")

    # GARANTIA 1: liberar acesso só depois da confirmação.
    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        tool_context.request_confirmation(
            hint=f"Liberar a entrada de {nome_limpo} em {dia}. Aprovar?",
            payload={
                "acao": "autorizar_visitante",
                "detalhes": {"nome": nome_limpo, "data": dia},
            },
        )
        tool_context.actions.skip_summarization = True
        return {
            "status": "aguardando_confirmacao",
            "mensagem": "A liberação aguarda a aprovação do morador no aplicativo.",
        }
    if not confirmacao.confirmed:
        return {
            "status": "nao_aprovada",
            "mensagem": "O morador não aprovou a liberação. Nada foi autorizado.",
        }

    banco.autorizar_visitante(apartamento, nome_limpo, dia)
    return {"status": "autorizado", "nome": nome_limpo, "data": dia}


# ---------------------------------------------------------------------------
# Consultas (especialista_agenda)
# ---------------------------------------------------------------------------


def listar_areas() -> dict:
    """Lista as áreas comuns e se cada uma gera cobrança."""
    return {
        "areas": [
            {
                "id": a["id"],
                "nome": a["nome"],
                "taxa": float(a["taxa"]),
                "gera_cobranca": float(a["taxa"]) > 0,
            }
            for a in banco.areas().values()
        ]
    }


def consultar_disponibilidade(area: str, data: str, tool_context: ToolContext) -> dict:
    """Diz se uma área está livre ou ocupada numa data.

    Args:
        area: id ou nome da área.
        data: data no formato AAAA-MM-DD.
    """
    info = banco.area_por_texto(area)
    if info is None:
        return _erro("Área desconhecida.", areas_validas=_areas_validas())
    dia = banco.validar_data(data)
    if dia is None:
        return _erro("Data inválida. Use o formato AAAA-MM-DD.")
    # Só livre/ocupada: nunca o código nem o apartamento dono da reserva.
    return {"area": info["id"], "data": dia, "disponivel": banco.data_livre(info["id"], dia)}


def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento do morador."""
    try:
        apartamento = _apartamento_da_sessao(tool_context)
    except SessaoSemApartamento as erro:
        return _erro(str(erro))
    return {"apartamento": apartamento, "reservas": banco.listar_reservas(apartamento)}


def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista os visitantes autorizados pelo apartamento do morador."""
    try:
        apartamento = _apartamento_da_sessao(tool_context)
    except SessaoSemApartamento as erro:
        return _erro(str(erro))
    return {"apartamento": apartamento, "visitantes": banco.listar_visitantes(apartamento)}


# ---------------------------------------------------------------------------
# Regulamento (especialista_regulamento)
# ---------------------------------------------------------------------------


def ler_capitulo(capitulo: str) -> dict:
    """Lê UM capítulo do regulamento interno.

    Args:
        capitulo: número romano ou arábico do capítulo (ex.: "IV" ou "4").
    """
    cap = regulamento.capitulo(capitulo)
    if cap is None:
        return _erro("Capítulo não encontrado.", indice=regulamento.indice())
    return {"capitulo": cap["romano"], "titulo": cap["titulo"], "texto": cap["texto"]}
