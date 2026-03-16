"""Remittance tax calculation module for Form 15CB.

Handles all five tax scenarios:
  1. Normal TDS with DTAA rate (tax documents provided).
  2. Normal TDS with IT Act higher rate (20.80%) – no tax documents.
  3. Gross-up with DTAA rate (tax documents provided).
  4. Gross-up with IT Act higher rate (20.80%) – no tax documents.
  5. No-TDS remittance types (social security, reimbursement, etc.).

Priority order:
  No-TDS check → Tax-rate selection → Gross-up vs normal calculation → Remarks.
"""
from __future__ import annotations

import functools
import json
import logging
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: IT Act higher rate applied when TRC + Form 10F are not furnished (%).
IT_ACT_HIGHER_RATE: float = 20.80

#: Minimum DTAA rate to be numerically valid (fractional rates stored in JSON
#: are converted to percent, so 0.10 × 100 = 10.0 %).
_DTAA_FRACTION_TO_PERCENT = 100.0

# ---------------------------------------------------------------------------
# No-TDS category registry
# ---------------------------------------------------------------------------
# Keys are normalised (lower-case, stripped).  Values are the remarks text.
# Add new entries here to extend No-TDS coverage without touching any other
# part of the module.

_NO_TDS_REGISTRY: Dict[str, str] = {
    "social security charges": "No TDS applicable – Social Security remittance",
    "social security": "No TDS applicable – Social Security remittance",
    "reimbursement": "No TDS – reimbursement of expenses",
    "reimbursement of expenses": "No TDS – reimbursement of expenses",
    "expense reimbursement": "No TDS – reimbursement of expenses",
}


def _normalise(text: str) -> str:
    """Lower-case, collapse whitespace."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def is_no_tds_remittance(nature_of_remittance: str) -> bool:
    """Return True if *nature_of_remittance* falls under a No-TDS category."""
    return _normalise(nature_of_remittance) in _NO_TDS_REGISTRY


def get_no_tds_remark(nature_of_remittance: str) -> str:
    """Return the No-TDS remark for *nature_of_remittance*, or a generic fallback."""
    key = _normalise(nature_of_remittance)
    return _NO_TDS_REGISTRY.get(
        key,
        f"No TDS applicable – {nature_of_remittance.strip()}",
    )


# ---------------------------------------------------------------------------
# DTAA rate lookup
# ---------------------------------------------------------------------------

_DTAA_INFO_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "master" / "DTAA__APPLICABLE_INFO.json"
)


@functools.lru_cache(maxsize=1)
def _load_dtaa_info() -> list:
    """Load and cache the DTAA rate table from disk."""
    try:
        with open(_DTAA_INFO_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


@functools.lru_cache(maxsize=256)
def get_dtaa_rate(vendor_country: str) -> Optional[float]:
    """Return the DTAA tax rate (as a percentage, e.g. 10.0 for 10 %) for
    *vendor_country*, or ``None`` when the country is not found or its rate is
    non-numeric (e.g. "As per i.t act").

    Rates in the JSON are stored as decimal fractions (0.10 → 10 %).
    The function converts them to percent before returning.
    """
    normalised_query = _normalise(vendor_country)
    if not normalised_query:
        return None

    for record in _load_dtaa_info():
        if not isinstance(record, dict):
            continue
        country_key = _normalise(str(record.get("country") or ""))
        if country_key == normalised_query:
            raw = record.get("percentage")
            try:
                rate_percent = float(raw) * _DTAA_FRACTION_TO_PERCENT
                return rate_percent
            except (TypeError, ValueError):
                # e.g. "As per i.t act"
                return None

    return None  # country not in DTAA table


# ---------------------------------------------------------------------------
# Rate resolution
# ---------------------------------------------------------------------------


def resolve_rate(
    calculation_basis: str,
    dtaa_rate: Optional[float],
) -> Tuple[float, bool]:
    """Return ``(applied_rate_percent, dtaa_claimed)`` based on UI selection.

    ``calculation_basis`` is the authoritative source:
      * ``"IT_ACT_2080"`` → always use :data:`IT_ACT_HIGHER_RATE` (20.80 %).
      * ``"DTAA"``        → use *dtaa_rate* when available, otherwise fall back
                            to IT Act rate (e.g. country not in treaty table).

    DTAA auto-detection must **never** override an ``IT_ACT_2080`` UI choice.
    """
    if calculation_basis == "IT_ACT_2080":
        return IT_ACT_HIGHER_RATE, False

    # calculation_basis == "DTAA" (or any unrecognised value → DTAA path)
    if dtaa_rate is not None:
        return dtaa_rate, True

    # DTAA requested but no numeric rate found → fall back to IT Act rate
    return IT_ACT_HIGHER_RATE, False


# ---------------------------------------------------------------------------
# Core calculation
# ---------------------------------------------------------------------------


def calculate_remittance_tax(
    invoice_amount: float,
    vendor_country: str,
    nature_of_remittance: str,
    tax_documents_provided: bool,
    gross_up_flag: bool,
    calculation_basis: str = "IT_ACT_2080",
) -> Dict[str, object]:
    """Calculate TDS and payment amounts for a remittance.

    Parameters
    ----------
    invoice_amount:
        The contractual invoice amount in the transaction currency.
    vendor_country:
        ISO / common name of the vendor's country.  Used to look up the
        applicable DTAA rate.
    nature_of_remittance:
        Human-readable description of what is being paid.  Determines whether
        No-TDS logic applies.
    tax_documents_provided:
        ``True`` when both TRC (Tax Residency Certificate) and Form 10F have
        been furnished by the vendor, enabling DTAA benefit.
    gross_up_flag:
        ``True`` when the remitter (Indian company) bears the tax burden so the
        vendor receives the full *invoice_amount* as net.
    calculation_basis:
        UI-selected rate basis: ``"IT_ACT_2080"`` or ``"DTAA"``.
        **This field always takes priority.**  DTAA auto-detection never
        overrides an explicit ``IT_ACT_2080`` selection.

    Returns
    -------
    dict with keys:
        ``tax_rate``            – Effective rate applied (%).
        ``tds_amount``          – TDS deducted / borne (rounded to 2 dp).
        ``grossed_amount``      – Gross-up base (= invoice_amount when not gross-up).
        ``vendor_payment``      – Amount received by vendor.
        ``company_total_payment`` – Total outflow from the company.
        ``remarks``             – Auto-generated explanation string.
        ``rate_source``         – ``"no_tds"`` | ``"dtaa"`` | ``"it_act"``.
        ``dtaa_rate_available`` – Whether a numeric DTAA rate was found.
    """
    # ── Priority 1: No-TDS check ────────────────────────────────────────────
    if is_no_tds_remittance(nature_of_remittance):
        return {
            "tax_rate": 0.0,
            "tds_amount": 0.0,
            "grossed_amount": invoice_amount,
            "vendor_payment": invoice_amount,
            "company_total_payment": invoice_amount,
            "remarks": get_no_tds_remark(nature_of_remittance),
            "rate_source": "no_tds",
            "dtaa_rate_available": False,
        }

    # ── Priority 2: Determine applicable tax rate ───────────────────────────
    # Look up DTAA rate only when documents are provided (needed for resolve_rate).
    dtaa_rate: Optional[float] = None
    if tax_documents_provided:
        dtaa_rate = get_dtaa_rate(vendor_country)

    dtaa_rate_available = dtaa_rate is not None

    # UI calculation_basis is the authoritative selector — DTAA must never
    # override an explicit IT_ACT_2080 choice.
    tax_rate, dtaa_claimed = resolve_rate(calculation_basis, dtaa_rate)
    rate_source = "dtaa" if dtaa_claimed else "it_act"

    logger.info(
        "tax_rate_resolved calculation_basis=%s dtaa_rate=%s applied_rate=%s",
        calculation_basis,
        dtaa_rate,
        tax_rate,
    )

    # Use Decimal arithmetic for accuracy
    inv = Decimal(str(invoice_amount))
    rate_dec = Decimal(str(tax_rate))

    # ── Priority 3: Gross-up vs normal ──────────────────────────────────────
    if gross_up_flag:
        # Reverse tax: vendor receives full invoice_amount; company bears TDS.
        # grossed_amount = invoice_amount / (1 - rate/100)
        if rate_dec >= Decimal("100"):
            raise ValueError(f"Tax rate {tax_rate}% must be less than 100.")
        denom = Decimal("1") - rate_dec / Decimal("100")
        grossed_dec = (inv / denom).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        tds_dec = (grossed_dec - inv).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        vendor_payment_dec = inv
        company_total_dec = grossed_dec
    else:
        # Normal TDS: deduct from invoice amount.
        tds_dec = (inv * rate_dec / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        vendor_payment_dec = (inv - tds_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        company_total_dec = inv
        grossed_dec = inv

    # ── Generate remarks ────────────────────────────────────────────────────
    remarks = _build_remarks(
        nature_of_remittance=nature_of_remittance,
        rate_source=rate_source,
        tax_rate=tax_rate,
        gross_up_flag=gross_up_flag,
        vendor_country=vendor_country,
    )

    return {
        "tax_rate": tax_rate,
        "tds_amount": float(tds_dec),
        "grossed_amount": float(grossed_dec),
        "vendor_payment": float(vendor_payment_dec),
        "company_total_payment": float(company_total_dec),
        "remarks": remarks,
        "rate_source": rate_source,
        "dtaa_rate_available": dtaa_rate_available,
    }


# ---------------------------------------------------------------------------
# Remarks builder
# ---------------------------------------------------------------------------


def _build_remarks(
    nature_of_remittance: str,
    rate_source: str,
    tax_rate: float,
    gross_up_flag: bool,
    vendor_country: str,
) -> str:
    """Compose a human-readable remarks string for the tax calculation."""
    parts: list[str] = []

    if rate_source == "dtaa":
        parts.append("TDS calculated as per DTAA treaty rate")
    else:
        parts.append("Higher tax rate applied as tax documents not provided")

    if gross_up_flag:
        parts.append("Gross-up applied – tax borne by remitter")

    return ". ".join(parts) + "."
