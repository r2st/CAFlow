"""Tests for the operational CLI (``python -m app.cli <command>``).

These commands are what a deploy runs, so the contract that matters is: seeding
is idempotent, generation only touches active firms, and an unknown command
exits non-zero instead of doing something surprising.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app import cli
from app.models.base import EntityType
from app.models.client import Client
from app.models.compliance import ComplianceItem, ComplianceType
from app.models.firm import Firm
from app.seeds.compliance_types import COMPLIANCE_TYPE_SEEDS


def _make_firm_with_client(db, *, is_active=True) -> Firm:
    firm = Firm(name="Sharma & Associates", email="office@sharma-ca.in", is_active=is_active)
    db.add(firm)
    db.flush()
    db.add(
        Client(
            firm_id=firm.id,
            name="Nimbus Textiles",
            entity_type=EntityType.PRIVATE_LIMITED,
            email="accounts@nimbus.in",
            gst_registered=True,
            tds_applicable=True,
            onboarded_on=date(2025, 4, 1),
        )
    )
    db.commit()
    return firm


class TestUsage:
    def test_no_arguments_prints_usage_and_fails(self, capsys):
        assert cli.main([]) == 1
        assert "Usage:" in capsys.readouterr().err

    def test_unknown_command_fails(self, capsys):
        assert cli.main(["migrate-everything"]) == 1
        assert "Usage:" in capsys.readouterr().err

    def test_every_advertised_command_is_registered(self):
        assert set(cli.COMMANDS) == {"seed", "create-db", "generate"}


class TestSeedCommand:
    def test_seeds_the_statutory_calendar(self, db, capsys):
        db.query(ComplianceType).delete()
        db.commit()

        assert cli.main(["seed"]) == 0

        out = capsys.readouterr().out
        assert f"{len(COMPLIANCE_TYPE_SEEDS)} created" in out
        assert db.scalar(select(ComplianceType).where(ComplianceType.code == "GSTR3B_MONTHLY"))

    def test_reseeding_creates_nothing_new(self, db, capsys):
        """Safe to run on every deploy."""
        cli.main(["seed"])
        capsys.readouterr()

        assert cli.main(["seed"]) == 0
        assert "0 created" in capsys.readouterr().out

    def test_reseeding_repairs_an_edited_row(self, db, capsys):
        gstr3b = db.scalar(
            select(ComplianceType).where(ComplianceType.code == "GSTR3B_MONTHLY")
        )
        gstr3b.due_day = 25
        db.commit()

        cli.main(["seed"])

        assert "1 updated" in capsys.readouterr().out
        db.expire_all()
        assert db.scalar(
            select(ComplianceType).where(ComplianceType.code == "GSTR3B_MONTHLY")
        ).due_day == 20


class TestCreateDbCommand:
    def test_reports_success(self, db, capsys):
        assert cli.main(["create-db"]) == 0
        assert "All tables created." in capsys.readouterr().out


class TestGenerateCommand:
    def test_tops_up_compliance_items(self, db, capsys):
        _make_firm_with_client(db)

        assert cli.main(["generate"]) == 0

        out = capsys.readouterr().out
        assert "Generated" in out
        assert db.scalar(select(ComplianceItem).limit(1)) is not None

    def test_second_run_generates_nothing(self, db, capsys):
        _make_firm_with_client(db)
        cli.main(["generate"])
        capsys.readouterr()

        cli.main(["generate"])

        assert "Generated 0 compliance item(s)." in capsys.readouterr().out

    def test_inactive_firms_are_skipped(self, db, capsys):
        _make_firm_with_client(db, is_active=False)

        cli.main(["generate"])

        assert "Generated 0 compliance item(s)." in capsys.readouterr().out

    def test_no_firms_is_not_an_error(self, db, capsys):
        assert cli.main(["generate"]) == 0
        assert "Generated 0 compliance item(s)." in capsys.readouterr().out


class TestEntrypoint:
    def test_module_main_uses_argv(self, db, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["app.cli", "seed"])
        assert cli.main() == 0
        assert "Compliance types:" in capsys.readouterr().out

    def test_module_main_rejects_a_bad_argv(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["app.cli", "nope"])
        assert cli.main() == 1
