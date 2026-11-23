"""Caminhos e configurações do projeto."""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # permite usar banco/regulamento sem as dependências
    load_dotenv = None

RAIZ = Path(__file__).resolve().parent.parent

if load_dotenv is not None:
    load_dotenv(RAIZ / ".env")

# Estado inicial do condomínio (somente leitura, nunca alterado).
DADOS_DIR = RAIZ / "dados"

# Onde ficam as mudanças feitas pelo assistente e as sessões do ADK.
ESTADO_DIR = Path(os.getenv("AURORA_ESTADO_DIR") or RAIZ / "estado")
ESTADO_DIR.mkdir(parents=True, exist_ok=True)
CONDOMINIO_DB = ESTADO_DIR / "condominio.db"
SESSOES_DB = ESTADO_DIR / "sessoes.db"

APP_NAME = "residencial_aurora"
MODELO = os.getenv("AURORA_MODELO") or "gemini-2.5-flash"

API_HOST = os.getenv("AURORA_HOST") or "127.0.0.1"
API_PORT = int(os.getenv("AURORA_PORT") or 8000)
