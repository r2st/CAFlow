"""Integrity of the seeded statutory calendar.

The calendar is data, and nothing at runtime checks that data against the code
that reads it. Two mistakes in particular fail *quietly* rather than loudly,
which is what makes them worth a test:

* An ``applicability_rule`` with no registered predicate. ``applies_to``
  answers False for an unknown key — deliberately, so a firm can add a custom
  type with no predicate and attach items by hand — which for a *system* seed
  means the filing is generated for nobody, in every firm.
* A ``due_overrides`` key that no period ever produces. The lookup simply
  misses, the ordinary due date stands, and a statutory extension goes
  unapplied — so every client is shown the wrong deadline for that filing.

Neither surfaces as an error, a failed deploy or a log line. The filing is just
absent, or dated wrongly, until somebody notices a client missed it. These
tests are the check that does not exist at runtime.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

import pytest

from app.core.periods import periods_for_frequency
from app.models.base import EntityType, GSTFilingFrequency
from app.models.client import Client as ClientModel
from app.seeds import compliance_types
from app.seeds.compliance_types import COMPLIANCE_TYPE_SEEDS
from app.services.applicability import APPLICABILITY_RULES, applies_to

# Two financial years, which is enough for every period key a frequency can
# emit: twelve months, four fiscal quarters.
WINDOW_START = date(2025, 4, 1)
WINDOW_END = date(2027, 3, 31)


def keys_produced(frequency: str) -> set[str]:
    """The override keys periods of this frequency actually offer for lookup."""
    return {p.key for p in periods_for_frequency(frequency, WINDOW_START, WINDOW_END)}


class TestApplicabilityRules:
    def test_every_rule_named_by_the_seed_has_a_predicate(self):
        named = {seed["applicability_rule"] for seed in COMPLIANCE_TYPE_SEEDS}
        assert named <= APPLICABILITY_RULES.keys(), (
            "a system compliance type names an applicability rule that no predicate "
            "implements, so it is generated for nobody in every firm"
        )

    def test_an_unnamed_rule_applies_to_nobody(self):
        """Why the test above matters, rather than being a tidiness check.

        A typo does not raise here and it does not raise at generation either.
        It reads as "no client qualifies", which is indistinguishable from a
        filing that genuinely applies to none of the firm's clients.
        """
        # A client that answers yes to every rule in the table, so that a False
        # here can only have come from the key not being found.
        qualifies_for_everything = ClientModel(
            name="Test Co",
            entity_type=EntityType.PRIVATE_LIMITED,
            gst_registered=True,
            gst_filing_frequency=GSTFilingFrequency.MONTHLY,
            tds_applicable=True,
            income_tax_applicable=True,
            tax_audit_applicable=True,
            roc_applicable=True,
            payroll_applicable=True,
        )
        assert applies_to("gst_monthly", qualifies_for_everything) is True

        assert applies_to("gst_montly", qualifies_for_everything) is False
        assert applies_to("", qualifies_for_everything) is False


class TestDueDateOverrides:
    @pytest.mark.parametrize(
        "seed",
        [s for s in COMPLIANCE_TYPE_SEEDS if s.get("due_overrides")],
        ids=lambda s: s["code"],
    )
    def test_every_override_key_is_one_its_own_frequency_produces(self, seed):
        # "03" for a monthly type, "Q4" for a quarterly one. A key of any other
        # shape is never looked up, and the extension it encodes never applies.
        unmatched = set(seed["due_overrides"]) - keys_produced(seed["frequency"])
        assert not unmatched, (
            f"{seed['code']} overrides {sorted(unmatched)}, which no "
            f"{seed['frequency']} period offers — the statutory extension is dead data"
        )

    def test_no_annual_type_carries_an_override(self):
        """An annual override cannot be written correctly, so none should exist.

        A period's override key is the last dash-separated piece of its label.
        For a financial year that is the year itself — "FY2025-26" keys on
        "26" — so an annual override matches exactly one financial year and
        silently stops applying the following April. A statute that shifts an
        annual deadline belongs in ``due_day``/``due_month_offset``.
        """
        assert keys_produced("annual") == {"26", "27"}  # FY-dependent, by inspection

        annual_with_overrides = [
            seed["code"]
            for seed in COMPLIANCE_TYPE_SEEDS
            if seed["frequency"] == "annual" and seed.get("due_overrides")
        ]
        assert annual_with_overrides == []

    def test_an_override_carries_a_day_a_month_offset_or_both(self):
        for seed in COMPLIANCE_TYPE_SEEDS:
            for key, override in (seed.get("due_overrides") or {}).items():
                assert set(override) <= {"day", "month_offset"}, (
                    f"{seed['code']} override {key} has keys compute_due_date "
                    f"does not read: {sorted(set(override) - {'day', 'month_offset'})}"
                )
                assert override, f"{seed['code']} override {key} changes nothing"


class TestSeedShape:
    def test_every_frequency_can_be_turned_into_periods(self):
        # periods_for_frequency raises on an unsupported frequency, which would
        # break generation for every client of every firm at once.
        for frequency in {seed["frequency"] for seed in COMPLIANCE_TYPE_SEEDS}:
            assert periods_for_frequency(frequency, WINDOW_START, WINDOW_END)

    def test_codes_are_unique(self):
        # The seeder keys on code and the table has a unique constraint on
        # (firm_id, code); a duplicate here is a failed migration on deploy.
        duplicates = [code for code, n in Counter(
            seed["code"] for seed in COMPLIANCE_TYPE_SEEDS
        ).items() if n > 1]
        assert duplicates == []

    def test_due_days_are_days_of_a_month(self):
        for seed in COMPLIANCE_TYPE_SEEDS:
            assert 1 <= seed["due_day"] <= 31, seed["code"]
            assert seed["due_month_offset"] >= 0, seed["code"]

    def test_reminders_fire_before_the_deadline_and_run_down_to_it(self):
        for seed in COMPLIANCE_TYPE_SEEDS:
            offsets = seed.get("reminder_offsets_days") or []
            assert all(offset > 0 for offset in offsets), (
                f"{seed['code']} has a reminder offset on or after the due date"
            )
            assert offsets == sorted(offsets, reverse=True), (
                f"{seed['code']} lists reminders out of order: {offsets}"
            )
            assert len(set(offsets)) == len(offsets), f"{seed['code']} repeats a reminder day"


class TestLookingUpASeedByCode:
    def test_a_code_the_calendar_carries_is_returned(self):
        assert compliance_types.seed_by_code("GSTR3B_MONTHLY")["frequency"] == "monthly"

    def test_a_code_it_does_not_carry_raises_rather_than_answering_none(self):
        """A silent ``None`` would reach the caller as a missing due-date rule.

        Every caller of this is naming a type it believes is seeded, so an
        unknown code is a typo or a seed that has been renamed — and both want
        to fail where they are, not several attributes later.
        """
        with pytest.raises(KeyError):
            compliance_types.seed_by_code("GSTR3B_FORTNIGHTLY")
