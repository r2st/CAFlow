"""Small operational CLI: ``python -m app.cli <command>``.

Commands:
  seed          Insert/refresh the system compliance calendar.
  create-db     Create all tables directly (dev shortcut; prefer Alembic).
  generate      Top up compliance items for every client of every active firm.
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import Firm  # noqa: F401 - ensures all models are registered
from app.seeds import seed_compliance_types
from app.services.compliance_generator import regenerate_for_firm


def cmd_seed() -> None:
    with SessionLocal() as db:
        created, updated = seed_compliance_types(db)
        db.commit()
    print(f"Compliance types: {created} created, {updated} updated.")


def cmd_create_db() -> None:
    Base.metadata.create_all(engine)
    print("All tables created.")


def cmd_generate() -> None:
    with SessionLocal() as db:
        total = 0
        # Ordered for the reason the beat sweep is: generation holds each
        # firm's row, and this walks every firm in one transaction.
        for firm in db.scalars(
            select(Firm).where(Firm.is_active.is_(True)).order_by(Firm.id)
        ).all():
            total += regenerate_for_firm(db, firm.id)
        db.commit()
    print(f"Generated {total} compliance item(s).")


COMMANDS = {"seed": cmd_seed, "create-db": cmd_create_db, "generate": cmd_generate}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        print(f"Usage: python -m app.cli [{' | '.join(COMMANDS)}]", file=sys.stderr)
        return 1
    COMMANDS[argv[0]]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
