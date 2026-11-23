"""Restaura reservas e visitantes ao estado dos arquivos de dados/.

Uso:
    uv run python -m aurora.restaurar            # mantém as sessões
    uv run python -m aurora.restaurar --sessoes  # também apaga as sessões
"""

from __future__ import annotations

import argparse

from . import banco, config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sessoes",
        action="store_true",
        help="também apaga todas as sessões e eventos (rode com a API parada)",
    )
    args = parser.parse_args()

    banco.restaurar(apagar_sessoes=args.sessoes)
    if args.sessoes:
        for sufixo in ("", "-wal", "-shm"):
            arquivo = config.SESSOES_DB.with_name(config.SESSOES_DB.name + sufixo)
            arquivo.unlink(missing_ok=True)

    print("Reservas e visitantes restaurados a partir de dados/.")
    if args.sessoes:
        print("Sessões apagadas.")


if __name__ == "__main__":
    main()
