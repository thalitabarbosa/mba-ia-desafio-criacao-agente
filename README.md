# Residencial Aurora: assistente virtual com Google ADK

Assistente do aplicativo dos moradores do Residencial Aurora. Pelo chat, o morador reserva o salão de festas, a churrasqueira e a quadra, cancela as próprias reservas, autoriza visitantes e tira dúvidas sobre o regulamento.

A ideia central: **o modelo decide o caminho, o código decide o que é permitido.** Todas as regras críticas estão nas tools, no banco e na API, e continuam valendo não importa o que o morador escreva.

```
aurora/
  api.py          FastAPI + Runner do ADK (rotas do contrato, confirmações, eventos)
  agentes.py      agente principal e os dois especialistas
  tools.py        tools que leem e gravam reservas, visitantes e o regulamento
  banco.py        SQLite do condomínio (reservas, visitantes, sessões)
  regulamento.py  leitura do regulamento por capítulo
  restaurar.py    comando de restauração dos dados
  config.py       caminhos e variáveis de ambiente
  agent.py        expõe root_agent para depurar no adk web
dados/            estado inicial do condomínio (não alterado)
estado/           criado em tempo de execução (fora do Git): condominio.db e sessoes.db
```

## Arquitetura

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `assistente_aurora` (principal) | Conversa com o morador, decide o que fazer e executa as ações: `reservar_area`, `cancelar_reserva`, `autorizar_visitante`. | É o `root_agent` do `App`, chamado pelo `Runner` em toda mensagem. | As tools que pedem confirmação ficam no agente raiz de propósito. Quando a resposta da confirmação chega, o Runner precisa entregá-la ao agente que fez o pedido. Com tudo na raiz e sem `sub_agents`/transferências, esse agente é sempre o mesmo, com sessão em memória ou persistida e antes ou depois de reiniciar a API. Isso elimina a "armadilha silenciosa" descrita no enunciado. |
| `especialista_agenda` | Consulta reservas e visitantes do apartamento da sessão, lista as áreas e diz se uma área está livre numa data. | `AgentTool`, chamado pelo principal. | Consultas não pedem confirmação, então podem rodar isoladas. O principal recebe só a resposta final do especialista. |
| `especialista_regulamento` | Responde dúvidas lendo apenas o capítulo necessário do regulamento com a tool `ler_capitulo`. | `AgentTool`, chamado pelo principal só quando o morador pergunta sobre regras. | O `AgentTool` roda o especialista numa sessão própria em memória: o texto do capítulo fica lá dentro, e só a resposta curta entra nos eventos da sessão do morador. Isso mantém o histórico enxuto (Garantia 4). |

Fluxo de uma mensagem: `POST /sessoes/{id}/mensagens` → `Runner.run_async` → `assistente_aurora` → tools e/ou especialistas → resposta + `confirmacoes_pendentes` montadas a partir dos eventos da sessão.

Fluxo de uma confirmação: a tool chama `tool_context.request_confirmation(...)`, o ADK grava um evento com a chamada sintética `adk_request_confirmation` e a execução para. A API lista esse pedido em `confirmacoes_pendentes`. Em `POST /sessoes/{id}/confirmacoes`, a API devolve um `FunctionResponse` com o mesmo `id`, nome `adk_request_confirmation` e `{"confirmed": true|false}`. O Runner encontra a invocação que fez o pedido, e o agente principal executa a tool original já com `tool_context.tool_confirmation` preenchido.

Armazenamento: SQLite em dois arquivos dentro de `estado/`. `condominio.db` guarda reservas, visitantes e o vínculo sessão → apartamento. `sessoes.db` é do `SqliteSessionService` do ADK e guarda sessões e eventos. Os arquivos de `dados/` só são lidos.

## Garantias

### 1. Cobrança ou acesso só com confirmação

- **`aurora/tools.py`, função `reservar_area`**: se a área tem `taxa > 0` e ainda não existe `tool_context.tool_confirmation`, a tool chama `tool_context.request_confirmation(...)` com `payload={"acao": ..., "detalhes": {"area", "data", "taxa"}}` e retorna sem gravar nada. Só grava quando `tool_context.tool_confirmation.confirmed` é verdadeiro. Área com taxa zero (quadra) grava direto, sem confirmação.
- **`aurora/tools.py`, função `autorizar_visitante`**: sempre pede confirmação, com `detalhes {"nome", "data"}`, e só chama `banco.autorizar_visitante` depois da aprovação.
- **`aurora/api.py`, função `responder_confirmacao`**: só aceita um `id` que esteja em `_confirmacoes_pendentes(sessao.events)` daquela sessão. Qualquer outro id (inexistente, de outra sessão ou já respondido) recebe **409** e nada é executado. Como o pedido respondido passa a ter um `FunctionResponse` nos eventos, reenviar a mesma resposta dá 409.
- **`aurora/api.py`, função `_confirmacoes_pendentes`**: monta a lista a partir dos eventos `adk_request_confirmation` sem resposta, usando o `payload` gravado pela tool.

**Por que não depende do modelo:** a confirmação só existe como `FunctionResponse` criado pela rota de confirmações. Texto do morador ("já estou confirmando aqui") vira uma mensagem comum, e a tool continua sem `tool_confirmation`, então só pode pedir a aprovação de novo.

### 2. Cada sessão pertence a um apartamento

- **`aurora/api.py`, função `criar_sessao`**: o apartamento é fixado uma única vez. Ele vira o `user_id` da sessão no ADK, vai para o `state` (`{"apartamento": ...}`) e é registrado em `condominio.db` (`banco.registrar_sessao`).
- **`aurora/tools.py`, função `_apartamento_da_sessao`**: todas as tools obtêm o apartamento do `tool_context.user_id` e conferem com o `state`. Nenhuma tool tem parâmetro de apartamento.
- **`aurora/banco.py`**: `listar_reservas`, `listar_visitantes` e `cancelar_reserva` filtram sempre por `apartamento = ?`. `data_livre` devolve só um booleano, nunca o código ou o dono da reserva.

**Por que não depende do modelo:** o modelo não tem como passar outro apartamento, porque nenhuma assinatura de tool aceita um. Mesmo que o morador diga "sou do 302", as consultas e o cancelamento só enxergam linhas do apartamento da sessão. A tentativa de cancelar a reserva do 302 retorna `nao_encontrada`, e reservar uma data ocupada retorna só `indisponivel`.

### 3. Nada se perde no reinício

- **`aurora/api.py`**: `session_service = SqliteSessionService(str(config.SESSOES_DB))` persiste sessões e todos os eventos em `estado/sessoes.db`.
- **`aurora/banco.py`**: reservas, cancelamentos (status `cancelada`, a linha não é apagada) e visitantes ficam em `estado/condominio.db`. `inicializar()` só carrega os dados iniciais na primeira execução (chave `dados_iniciais_carregados` na tabela `meta`), então reiniciar a API não sobrescreve nada.
- Como a confirmação sempre volta ao agente raiz (ver Arquitetura), aprovar depois de reiniciar também funciona.

### 4. O regulamento é consultado, não carregado

- **`aurora/agentes.py`, `INSTRUCAO_PRINCIPAL`**: o agente principal não recebe o regulamento nas instruções.
- **`aurora/agentes.py`, `especialista_regulamento`**: recebe só o índice (títulos dos capítulos, via `regulamento.indice()`) e lê um capítulo por vez com `ler_capitulo`.
- **`aurora/regulamento.py`, funções `capitulos` e `capitulo`**: dividem `dados/regulamento.md` pelos cabeçalhos `## Capítulo ...` e devolvem um capítulo isolado.
- **`AgentTool`** (`google/adk/tools/agent_tool.py`) executa o especialista numa `InMemorySessionService` separada e devolve ao principal só o texto final. O capítulo lido nunca entra nos eventos da sessão do morador, e a resposta do especialista é instruída a tratar só do que foi perguntado.

### 5. Dois moradores, uma reserva

- **`aurora/banco.py`, `ESQUEMA`**: índice único parcial
  ```sql
  CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa_por_area_data
      ON reservas (area, data) WHERE status = 'ativa';
  ```
- **`aurora/banco.py`, função `criar_reserva`**: grava dentro de `BEGIN IMMEDIATE` e converte o `sqlite3.IntegrityError` em `DataOcupada`. A tool `reservar_area` transforma isso numa resposta normal (`status: indisponivel`). Nada de erro 500.
- Códigos novos (`_novo_codigo`) são aleatórios e conferidos contra a tabela `codigos_emitidos`, que guarda todo código já emitido, inclusive de reservas canceladas e de antes de uma restauração.

**Por que não depende do modelo nem da conferência prévia:** a checagem de agenda antes da confirmação é só para não pedir aprovação à toa. Quem garante a exclusividade é o banco, no instante do `INSERT`. Se duas aprovações chegarem juntas, a segunda falha na restrição única.

## Como rodar

### Pré-requisitos

- Python 3.12 ou superior
- [uv](https://docs.astral.sh/uv/)
- Chave do Google AI Studio

Não há serviço externo: o SQLite é criado automaticamente em `estado/`.

### Variáveis do `.env`

```bash
cp .env.example .env
```

| Variável | Valor |
|---|---|
| `GOOGLE_API_KEY` | sua chave do Google AI Studio |
| `GOOGLE_GENAI_USE_VERTEXAI` | `FALSE` |
| `AURORA_MODELO` | modelo Gemini dos agentes (padrão `gemini-2.5-flash`) |

Opcionais: `AURORA_HOST` (padrão `127.0.0.1`), `AURORA_PORT` (padrão `8000`) e `AURORA_ESTADO_DIR` (padrão `./estado`).

### Instalar

```bash
uv sync
```

A versão do ADK está fixada em `google-adk==2.9.1` no `pyproject.toml`.

### Restaurar os dados iniciais

```bash
uv run python -m aurora.restaurar
```

Volta reservas e visitantes ao estado de `dados/` e mantém as sessões. Para apagar também as sessões (com a API parada):

```bash
uv run python -m aurora.restaurar --sessoes
```

### Subir a API

```bash
uv run python -m aurora.api
```

A API responde em `http://localhost:8000`.

### Exemplo rápido

```bash
curl -s -X POST localhost:8000/sessoes -H 'Content-Type: application/json' -d '{"apartamento": "101"}'
curl -s -X POST localhost:8000/sessoes/<ID>/mensagens -H 'Content-Type: application/json' \
  -d '{"texto": "Reserve o salão de festas para 2030-04-20."}'
curl -s -X POST localhost:8000/sessoes/<ID>/confirmacoes -H 'Content-Type: application/json' \
  -d '{"id": "<ID_DA_CONFIRMACAO>", "confirmado": true}'
curl -s localhost:8000/apartamentos/101/reservas
```

### Depurar no adk web (opcional)

```bash
uv run adk web .
```

Escolha o app `aurora` para ver transferências, chamadas de tool e pedidos de confirmação.
