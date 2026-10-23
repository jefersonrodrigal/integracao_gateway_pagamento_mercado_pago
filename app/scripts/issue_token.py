"""Emite um JWT de teste assinado com JWT_SECRET (somente fora de produção).

python -m app.scripts.issue_token --sub cliente-123
python -m app.scripts.issue_token --sub operador-1 --role admin --hours 8
"""

from __future__ import annotations

import argparse
from datetime import timedelta

from app.core.config import get_settings
from app.core.security import KNOWN_ROLES, create_access_token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sub", required=True, help="identificador do cliente")
    parser.add_argument("--role", action="append", choices=sorted(KNOWN_ROLES))
    parser.add_argument("--hours", type=float, default=1.0)
    args = parser.parse_args()

    settings = get_settings()
    if settings.is_production:
        raise SystemExit("Emissão de tokens de teste desabilitada em produção.")
    token = create_access_token(
        settings, args.sub, args.role or ["customer"], timedelta(hours=args.hours)
    )
    print(token)


if __name__ == "__main__":
    main()
