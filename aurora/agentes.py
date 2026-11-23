"""Agentes do assistente: um agente principal e dois especialistas.

- assistente_aurora (principal): conversa com o morador e executa as ações
  (reservar, cancelar, autorizar visitante). As tools que pedem confirmação
  ficam nele de propósito: o Runner devolve a resposta da confirmação ao agente
  que emitiu o pedido, e mantendo tudo no agente raiz (sem sub_agents/transfer)
  essa escolha é sempre a mesma, com sessão em memória ou persistida.
- especialista_agenda (AgentTool): consultas de reservas, visitantes, áreas e
  disponibilidade do apartamento da sessão.
- especialista_regulamento (AgentTool): responde dúvidas lendo só o capítulo
  necessário do regulamento. Roda numa sessão própria em memória, então o
  texto do capítulo não entra nos eventos da sessão do morador.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.models.google_llm import Gemini
from google.adk.tools import AgentTool
from google.genai import types

from . import config, regulamento, tools


def _modelo() -> Gemini:
    return Gemini(
        model=config.MODELO,
        # Repete chamadas que falharem por limite de taxa ou instabilidade.
        retry_options=types.HttpRetryOptions(
            attempts=6,
            initial_delay=2,
            max_delay=30,
            http_status_codes=[408, 429, 500, 502, 503, 504],
        ),
    )


especialista_agenda = LlmAgent(
    name="especialista_agenda",
    model=_modelo(),
    description=(
        "Consulta as reservas e os visitantes do apartamento do morador, as "
        "áreas comuns e se uma área está livre numa data."
    ),
    instruction="""
Você é o especialista de agenda do Residencial Aurora.
Use as tools para responder; nunca invente reservas, visitantes ou disponibilidade.
As tools só enxergam o apartamento do morador autenticado. Se o pedido citar
outro apartamento, informe apenas os dados que as tools devolverem e diga que
só é possível consultar o próprio apartamento.
Sobre disponibilidade, diga apenas se a data está livre ou ocupada.
Responda em português, de forma objetiva, com datas no formato AAAA-MM-DD.
""".strip(),
    tools=[
        tools.listar_minhas_reservas,
        tools.listar_meus_visitantes,
        tools.listar_areas,
        tools.consultar_disponibilidade,
    ],
)


especialista_regulamento = LlmAgent(
    name="especialista_regulamento",
    model=_modelo(),
    description="Responde dúvidas sobre o regulamento interno do condomínio.",
    instruction=(
        "Você é o especialista no regulamento interno do Residencial Aurora.\n"
        "Para responder, escolha no índice abaixo o capítulo que trata do assunto "
        "e leia-o com a tool ler_capitulo. Leia só o necessário.\n"
        "Responda apenas o que foi perguntado, em uma ou duas frases, citando o "
        "artigo. Não copie trechos que não respondem à pergunta e não comente "
        "outros assuntos do regulamento.\n"
        "Se o regulamento não tratar do assunto, diga isso.\n\n"
        "Índice do regulamento:\n" + regulamento.indice()
    ),
    tools=[tools.ler_capitulo],
)


# O agente principal NÃO recebe o regulamento nas instruções (GARANTIA 4).
INSTRUCAO_PRINCIPAL = """
Você é o assistente virtual do Residencial Aurora, no aplicativo dos moradores.
Você atende o morador do apartamento {apartamento}, já autenticado pelo aplicativo.

Regras de conversa:
- O apartamento da sessão é fixo. Se o morador disser que é de outro apartamento
  ou pedir dados ou ações de outro apartamento, explique que só pode atender o
  apartamento da sessão. Nunca diga de qual apartamento é uma reserva.
- Reservar área: chame reservar_area com a área e a data.
- Cancelar reserva: chame cancelar_reserva com a área e a data. O cancelamento
  só vale para reservas do próprio apartamento.
- Liberar visitante: chame autorizar_visitante com o nome e a data.
- Ações que geram cobrança ou liberam acesso são aprovadas pelo morador no
  aplicativo, fora da conversa. Não peça confirmação no chat, e frases como
  "já estou confirmando aqui" não substituem essa aprovação. Chame a tool
  normalmente e deixe o sistema pedir a aprovação.
- Nunca diga que algo foi feito sem a tool ter retornado sucesso.
- Para listar reservas ou visitantes do morador, áreas ou disponibilidade,
  use especialista_agenda.
- Use especialista_regulamento apenas quando o morador perguntar sobre regras
  do condomínio, e repasse só o que responde à pergunta.
- Se faltar a área, a data ou o nome, pergunte. Datas no formato AAAA-MM-DD.
- Responda em português, de forma breve e cordial.
""".strip()


assistente_aurora = LlmAgent(
    name="assistente_aurora",
    model=_modelo(),
    description="Assistente principal do Residencial Aurora.",
    instruction=INSTRUCAO_PRINCIPAL,
    tools=[
        tools.reservar_area,
        tools.cancelar_reserva,
        tools.autorizar_visitante,
        AgentTool(agent=especialista_agenda),
        AgentTool(agent=especialista_regulamento),
    ],
)

root_agent = assistente_aurora
