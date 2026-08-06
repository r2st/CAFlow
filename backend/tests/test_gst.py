"""Place of supply, the CGST/SGST/IGST split, and GSTIN validation.

The arithmetic of a total is unaffected by any of this — what the client pays
at 18% is the same either way. What is affected is whether the document is a
tax invoice: whether it names the heads the client claims credit against, and
whether the firm can produce a GSTR-1 from its own ledger.
"""

from __future__ import annotations

import pytest

from app.models.base import SupplyType
from app.models.client import Client
from app.models.firm import Firm
from app.models.invoice import Invoice, InvoiceLine
from app.schemas.common import validate_gstin
from app.services import billing, gst

API = "/api/v1"

# Three GSTINs whose check digits are genuinely correct, so a test asserting
# that a valid one is accepted is not asserting it against the implementation
# that produced it.
VALID_GSTINS = ("27AAPFU0939F1ZV", "24AAACC1206D1ZM", "09AAACH7409R1ZZ")


class TestTheCheckDigit:
    """A GSTIN carries a checksum, and it is there to be checked."""

    @pytest.mark.parametrize("gstin", VALID_GSTINS)
    def test_a_real_gstin_passes(self, gstin: str):
        assert gst.gstin_checksum_valid(gstin)

    @pytest.mark.parametrize("gstin", VALID_GSTINS)
    def test_every_single_character_typo_is_caught(self, gstin: str):
        """The property the check digit exists for.

        Not a sample: every one of the 14 positions is tried against every one
        of the other 35 characters in the alphabet, and none of the 490 may
        slip through.
        """
        for position in range(14):
            for char in gst.CHECKSUM_CHARSET:
                if char == gstin[position]:
                    continue
                mistyped = gstin[:position] + char + gstin[position + 1 :]
                assert not gst.gstin_checksum_valid(mistyped), mistyped

    @pytest.mark.parametrize("gstin", VALID_GSTINS)
    def test_transposing_two_adjacent_characters_is_caught(self, gstin: str):
        """The other half of how a fifteen-character code gets mistyped."""
        for position in range(13):
            if gstin[position] == gstin[position + 1]:
                continue
            chars = list(gstin)
            chars[position], chars[position + 1] = chars[position + 1], chars[position]
            assert not gst.gstin_checksum_valid("".join(chars)), position

    def test_a_string_of_the_wrong_length_is_not_valid(self):
        assert not gst.gstin_checksum_valid("27AAPFU0939F1Z")
        assert not gst.gstin_checksum_valid("")


class TestValidatingAGstin:
    """What the schema layer refuses, and what it says about why."""

    @pytest.mark.parametrize("gstin", VALID_GSTINS)
    def test_a_valid_one_is_accepted_and_upper_cased(self, gstin: str):
        assert validate_gstin(gstin.lower()) == gstin

    def test_an_empty_value_stays_empty(self):
        assert validate_gstin(None) is None
        assert validate_gstin("") is None

    def test_the_shape_is_still_checked_first(self):
        with pytest.raises(ValueError, match="15-character"):
            validate_gstin("NOT-A-GSTIN")

    def test_a_state_code_naming_no_state_is_refused(self):
        """``00`` and ``40`` match the pattern and are not states.

        Worth its own refusal rather than falling through to the checksum,
        because this field decides how the client's invoices are taxed and a
        code naming no state takes them down the undetermined path silently.
        """
        body = "00AAPFU0939F1Z"
        with pytest.raises(ValueError, match="not a GST state code"):
            validate_gstin(body + gst.gstin_check_digit(body))

    def test_a_mistyped_character_is_refused_by_the_check_digit(self):
        # Same GSTIN, one character out — the shape and the state code are both
        # still fine, so the check digit is the only thing that can catch it.
        with pytest.raises(ValueError, match="check digit"):
            validate_gstin("27AAPFU0939F1ZW")

    def test_the_two_refusals_say_which_mistake_was_made(self):
        """A wrong state and a wrong character are different things to go and fix."""
        with pytest.raises(ValueError) as bad_state:
            validate_gstin("40AAPFU0939F1ZV")
        with pytest.raises(ValueError) as bad_digit:
            validate_gstin("27AAPFU0939F1ZW")
        assert str(bad_state.value) != str(bad_digit.value)


class TestReadingAStateOutOfTheParties:
    def test_the_gstin_names_the_state(self):
        assert gst.state_code_of_gstin("27AAPFU0939F1ZV") == "27"
        assert gst.state_name("27") == "Maharashtra"

    def test_a_gstin_with_no_state_code_names_nothing(self):
        assert gst.state_code_of_gstin("00AAPFU0939F1ZV") is None
        assert gst.state_code_of_gstin(None) is None

    @pytest.mark.parametrize(
        ("typed", "code"),
        [
            ("Maharashtra", "27"),
            ("maharashtra", "27"),
            ("  MAHARASHTRA  ", "27"),
            ("Tamil Nadu", "33"),
            ("tamilnadu", "33"),
            # Renamed states, where both names are still in daily use and a
            # record entered five years ago says one of them.
            ("Orissa", "21"),
            ("Odisha", "21"),
            ("Pondicherry", "34"),
            ("Puducherry", "34"),
            ("Uttaranchal", "05"),
            ("NCT of Delhi", "07"),
        ],
    )
    def test_a_typed_state_name_is_matched_however_it_is_spelt(self, typed, code):
        assert gst.state_code_of_name(typed) == code

    def test_something_that_is_not_a_state_matches_nothing(self):
        assert gst.state_code_of_name("Bengaluru") is None
        assert gst.state_code_of_name("") is None
        assert gst.state_code_of_name(None) is None

    def test_a_name_shared_by_two_codes_resolves_to_the_current_one(self):
        """28 and 37 are both "Andhra Pradesh"; 37 is the one issued today."""
        assert gst.state_code_of_name("Andhra Pradesh") == "37"

    def test_the_historical_codes_are_still_readable(self):
        """A client's existing registration is not invalidated by a renumbering."""
        assert gst.state_name("28") == "Andhra Pradesh"
        assert gst.state_name("25") == "Daman and Diu"


class TestResolvingTheSupply:
    """Which of the two ways a supply is taxed, from the two parties' states."""

    def make(self, firm_gstin=None, firm_state=None, client_gstin=None, client_state=None):
        return (
            Firm(name="Firm", email="f@example.com", gstin=firm_gstin, state=firm_state),
            Client(name="Client", gstin=client_gstin, state=client_state),
        )

    def test_the_same_state_is_taxed_as_cgst_and_sgst(self):
        firm, client = self.make(firm_gstin="27AAPFU0939F1ZV", client_gstin="27AABCN2345P1Z5")
        assert gst.resolve_supply(firm, client) == ("27", SupplyType.INTRA_STATE)

    def test_a_different_state_is_taxed_as_igst(self):
        firm, client = self.make(firm_gstin="27AAPFU0939F1ZV", client_gstin="29AABCN2345P1Z1")
        assert gst.resolve_supply(firm, client) == ("29", SupplyType.INTER_STATE)

    def test_an_unregistered_client_is_placed_by_their_recorded_address(self):
        """s.12(2)(b) — the location on the supplier's record."""
        firm, client = self.make(firm_gstin="27AAPFU0939F1ZV", client_state="Karnataka")
        assert gst.resolve_supply(firm, client) == ("29", SupplyType.INTER_STATE)

    def test_a_registered_client_is_placed_by_their_registration_not_their_address(self):
        """s.12(2)(a). A client registered in Karnataka is located there even if
        the address on file still says something else."""
        firm, client = self.make(
            firm_gstin="27AAPFU0939F1ZV",
            client_gstin="29AABCN2345P1Z1",
            client_state="Maharashtra",
        )
        assert gst.resolve_supply(firm, client) == ("29", SupplyType.INTER_STATE)

    def test_the_firm_falls_back_to_its_typed_state_too(self):
        firm, client = self.make(firm_state="Maharashtra", client_gstin="27AABCN2345P1Z5")
        assert gst.resolve_supply(firm, client) == ("27", SupplyType.INTRA_STATE)

    def test_an_undetermined_supply_is_intra_state_with_no_place_recorded(self):
        """The firm has not entered its own GSTIN, which is the ordinary way here.

        Intra-state rather than inter-state on purpose: a practice bills the
        clients in its own city, and IGST charged wrongly is credit the client
        cannot take. The blank place of supply is the marker that says nothing
        established it.
        """
        firm, client = self.make(client_gstin="27AABCN2345P1Z5")
        assert gst.resolve_supply(firm, client) == ("27", SupplyType.INTRA_STATE)

    def test_a_client_with_neither_records_no_place_at_all(self):
        firm, client = self.make(firm_gstin="27AAPFU0939F1ZV")
        assert gst.resolve_supply(firm, client) == (None, SupplyType.INTRA_STATE)


class TestSplittingTheTax:
    def test_an_intra_state_supply_is_halved(self):
        assert gst.split_tax(18_000, SupplyType.INTRA_STATE) == (9_000, 9_000, 0)

    def test_an_inter_state_supply_is_all_igst(self):
        assert gst.split_tax(18_000, SupplyType.INTER_STATE) == (0, 0, 18_000)

    @pytest.mark.parametrize("tax", [0, 1, 2, 3, 17, 18_000, 18_001, 999_999_999])
    @pytest.mark.parametrize("supply", list(SupplyType))
    def test_the_heads_always_sum_to_the_tax(self, tax: int, supply: SupplyType):
        """The invariant the document depends on: what is printed adds up."""
        assert sum(gst.split_tax(tax, supply)) == tax

    def test_an_odd_total_puts_the_extra_paise_on_sgst(self):
        """Only reachable on a subtotal that is not a whole number of rupees,
        which fees are not — but it must be decided rather than lost."""
        assert gst.split_tax(1, SupplyType.INTRA_STATE) == (0, 1, 0)
        assert gst.split_tax(3, SupplyType.INTRA_STATE) == (1, 2, 0)

    def test_nothing_is_charged_on_a_nil_invoice(self):
        assert gst.split_tax(0, SupplyType.INTRA_STATE) == (0, 0, 0)
        assert gst.split_tax(0, SupplyType.INTER_STATE) == (0, 0, 0)


class TestRecalculateCarriesTheSplit:
    """The split is part of totalling an invoice, not a separate step to forget."""

    def invoice(self, amount: int, supply_type=None) -> Invoice:
        invoice = Invoice(gst_rate_bps=1800, lines=[], supply_type=supply_type)
        invoice.lines.append(
            InvoiceLine(description="Work", quantity=1, unit_price_paise=amount)
        )
        return billing.recalculate(invoice)

    def test_an_intra_state_invoice_splits_into_cgst_and_sgst(self):
        invoice = self.invoice(100_000, SupplyType.INTRA_STATE)
        assert invoice.tax_paise == 18_000
        assert (invoice.cgst_paise, invoice.sgst_paise, invoice.igst_paise) == (
            9_000,
            9_000,
            0,
        )

    def test_an_inter_state_invoice_is_all_igst(self):
        invoice = self.invoice(100_000, SupplyType.INTER_STATE)
        assert (invoice.cgst_paise, invoice.sgst_paise, invoice.igst_paise) == (
            0,
            0,
            18_000,
        )

    def test_an_invoice_built_before_it_reaches_the_database_still_splits(self):
        """``supply_type`` is unset on an object that has not taken the column's
        default yet, and totalling one must not depend on that having happened."""
        invoice = self.invoice(100_000, supply_type=None)
        assert invoice.cgst_paise + invoice.sgst_paise == invoice.tax_paise

    def test_the_total_is_unchanged_by_which_way_it_splits(self):
        """The point: the client pays the same either way. Only the heads move."""
        intra = self.invoice(123_456, SupplyType.INTRA_STATE)
        inter = self.invoice(123_456, SupplyType.INTER_STATE)
        assert intra.total_paise == inter.total_paise
        assert intra.tax_paise == inter.tax_paise

    def test_the_heads_sum_to_the_tax_on_the_totals_too(self):
        invoice = self.invoice(111_100, SupplyType.INTRA_STATE)
        assert (
            invoice.cgst_paise + invoice.sgst_paise + invoice.igst_paise
            == invoice.tax_paise
        )
        assert invoice.subtotal_paise + invoice.tax_paise == invoice.total_paise
