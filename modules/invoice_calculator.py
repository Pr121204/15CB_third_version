from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Dict, Optional, Any, cast

from modules.form15cb_constants import (
    ASSESSMENT_YEAR,
    CA_DEFAULTS,
    FORM_DESCRIPTION,
    FORM_NAME,
    FORM_VER,
    HONORIFIC_M_S,
    INC_LIAB_INDIA_ALWAYS,
    INTERMEDIARY_CITY,
    IOR_WE_CODE,
    IT_ACT_BASIS,
    IT_ACT_RATE_DEFAULT,
    IT_ACT_RATES,
    MODE_NON_TDS,
    MODE_TDS,
    FIELD_MAX_LENGTH,
    NAME_REMITTEE_DATE_FORMAT,
    PROPOSED_DATE_OFFSET_DAYS,
    RATE_TDS_SECB_FLG_DTAA,
    RATE_TDS_SECB_FLG_IT_ACT,
    RATE_TDS_SECB_FLG_TDS,
    REMITTEE_STATE,
    REMITTEE_ZIP_CODE,
    SCHEMA_VER,
    SEC_REM_COVERED_DEFAULT,
    SW_CREATED_BY,
    SW_VERSION_NO,
    TAX_IND_DTAA_ALWAYS,
    TAX_RESID_CERT_Y,
    XML_CREATED_BY,
)
from modules.logger import get_logger
from modules.master_lookups import split_dtaa_article_text, load_nature_options


logger = get_logger()


def _to_float(raw: str) -> Optional[float]:
    try:
        return float(str(raw or "").strip())
    except Exception:
        return None


def _parse_date(raw: str) -> Optional[date]:
    t = str(raw or "").strip()
    if not t:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def _format_iso(raw: str | object) -> str:
    d = _parse_date(str(raw or ""))
    if not d:
        return str(raw or "").strip()
    return d.strftime("%Y-%m-%d")


def format_dotted_date(raw: str) -> str:
    d = _parse_date(raw)
    if not d:
        return str(raw or "").strip()
    return d.strftime(NAME_REMITTEE_DATE_FORMAT)


def _fmt_num(n: Optional[float]) -> str:
    if n is None:
        return ""
    f = float(n)
    if f.is_integer():
        return str(int(f))
    # Use up to 10 significant figures via 'g' format (strips trailing zeros
    # automatically).  This preserves exchange-rate precision (e.g. 84.5678)
    # while still displaying currency amounts cleanly (e.g. 12.57 stays 12.57).
    return f"{f:.10g}"


def _round_to_int(value: float) -> int:
    try:
        return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return int(round(value))


def _fmt_fcy(n: Optional[float]) -> str:
    """Format a foreign currency amount to exactly 2 decimal places (ROUND_HALF_UP)."""
    if n is None:
        return ""
    try:
        return str(Decimal(str(n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return f"{float(n):.2f}"


def _fmt_inr(n: Optional[float]) -> str:
    """Format an INR amount to integer (no decimals)."""
    if n is None:
        return ""
    return str(_round_to_int(float(n)))


# Fields rounded to 2 dp at the final output step (FCY amounts).
_FCY_AMOUNT_FIELDS = ("AmtPayForgnRem", "ActlAmtTdsForgn", "AmtPayForgnTds")
# Fields rounded to integer at the final output step (INR amounts).
_INR_AMOUNT_FIELDS = (
    "AmtPayIndRem", "AmtPayIndianTds",
    "AmtIncChrgIt", "TaxLiablIt",
    "TaxIncDtaa", "TaxLiablDtaa",
)


def _apply_amount_rounding(d: Dict[str, str]) -> None:
    """Round amount fields in-place: FCY → 2 dp, INR → integer.

    Called as the very last step of recompute_invoice() and
    invoice_state_to_xml_fields() so all intermediate calculations
    run at full precision.
    """
    for key in _FCY_AMOUNT_FIELDS:
        val = _to_float(str(d.get(key) or ""))
        if val is not None:
            d[key] = _fmt_fcy(val)
    for key in _INR_AMOUNT_FIELDS:
        val = _to_float(str(d.get(key) or ""))
        if val is not None:
            d[key] = _fmt_inr(val)


def _is_integer_rate(value: float | None) -> bool:
    if value is None:
        return False
    return abs(value - round(value)) < 1e-9


def _build_name_remittee(beneficiary: str, invoice_no: str, dotted_date: str) -> str:
    b = str(beneficiary or "").strip().upper()
    inv = str(invoice_no or "").strip().upper()
    d = str(dotted_date or "").strip().upper()
    
    # If the beneficiary name already contains the invoice number or date, 
    # don't append it again to avoid "INV-123 INV-123" redundancy.
    has_inv = inv and (inv in b or f"INV" in b)
    has_date = d and (d in b)

    if b and inv and d and not (has_inv or has_date):
        return f"{b} INVOICE NO. {inv} DT {d}"
    if b and inv and not has_inv:
        return f"{b} INVOICE NO. {inv}"
    if b and d and not has_date:
        return f"{b} DT {d}"
    return b


_BENEFICIARY_NOISE_PATTERNS = (
    re.compile(r"\bINVOICE\s*NO\.?\s*[:\-]?\s*.*$", re.IGNORECASE),
    re.compile(r"\bINVOICE\s*NUMBER\s*[:\-]?\s*.*$", re.IGNORECASE),
    re.compile(r"\bDT\s+\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b.*$", re.IGNORECASE),
    re.compile(r"\bNUMBER\s*[:\-]?\s*\S+.*$", re.IGNORECASE),
)


def clean_beneficiary_name(name: str) -> str:
    """Strip invoice metadata noise from beneficiary name candidates."""
    text = str(name or "").strip()
    if not text:
        return ""
    for pattern in _BENEFICIARY_NOISE_PATTERNS:
        text = pattern.sub("", text).strip()
    return text.strip(" .,-").upper()


def get_effective_it_rate(rate: float | None = None) -> tuple[float, str]:
    """Return (effective_rate_percent, basis_text) for a user-selected IT Act rate.

    If *rate* is ``None`` the default rate (21.84%) is used.
    """
    if rate is None:
        rate = IT_ACT_RATE_DEFAULT
    basis = IT_ACT_BASIS.get(
        rate,
        f"GROSS AMOUNT OF REMITTANCE IS CONSIDERED AS TAXABLE INCOME "
        f"AND TAX LIABILITY IS CALCULATED AT {rate} PERCENTAGE OF ABOVE.",
    )
    return rate, basis


def _format_basis_rate_text(rate: Decimal | float | None) -> str:
    """Render basis-text percentages using the same display style as the IT Act selector."""
    parsed_rate = _to_float(str(rate or ""))
    if parsed_rate is None:
        return ""
    if parsed_rate == 20.80:
        return "20.80"
    if parsed_rate == 21.22:
        return "21.22"
    if parsed_rate == 21.84:
        return "21.84"
    if parsed_rate == 21.216:
        return "21.216"
    return _fmt_num(parsed_rate)


def apply_non_tds_reason_sync(fields: dict) -> dict:
    """Keep NatureRemDtaa, RelArtDetlDDtaa, and ReasonNot in sync for NON_TDS.

    Single source of truth: lookup_non_tds(NatureRemCategory, RevPurCode).

    Rules:
    - NatureRemDtaa  — auto-filled from lookup when blank; user edits preserved.
    - RelArtDetlDDtaa — auto-filled from lookup when blank; user edits preserved.
    - ReasonNot       — always mirrored from RelArtDetlDDtaa (unconditional).

    The unconditional mirror for ReasonNot ensures the two "if not, reasons"
    fields (Section 8-ii and Section 9D-d) never carry different text.
    """
    from modules.non_tds_lookup import lookup_non_tds

    nature_text = (
        str(fields.get("NatureRemCategory") or "").strip()
        or str(fields.get("nature_of_remittance") or "").strip()
    )
    purpose_code = str(fields.get("RevPurCode") or "").strip()

    lookup = lookup_non_tds(nature_text, purpose_code)
    nature_rem_dtaa = (lookup.get("NatureRemDtaa") or "").strip()
    reason_text = (lookup.get("RelArtDetlDDtaa") or "").strip()

    # Auto-fill NatureRemDtaa only when blank; preserves deliberate user edits.
    if nature_rem_dtaa and not str(fields.get("NatureRemDtaa") or "").strip():
        fields["NatureRemDtaa"] = nature_rem_dtaa

    # Auto-fill RelArtDetlDDtaa only when blank; preserves deliberate user edits.
    if reason_text and not str(fields.get("RelArtDetlDDtaa") or "").strip():
        fields["RelArtDetlDDtaa"] = reason_text

    # Always mirror RelArtDetlDDtaa → ReasonNot so both fields are always identical.
    # RelArtDetlDDtaa is the authoritative field; ReasonNot is its Section-8 mirror.
    current_reason = str(fields.get("RelArtDetlDDtaa") or "").strip()
    if current_reason:
        fields["ReasonNot"] = current_reason

    return fields


@dataclass(frozen=True)
class TaxComputationInput:
    invoice_fcy: Decimal
    exchange_rate: Decimal
    it_rate: Decimal
    dtaa_rate: Optional[Decimal]
    is_gross_up: bool
    is_tds: bool
    basis_mode: str  # "dtaa" or "it_act_2080"


@dataclass(frozen=True)
class TaxComputationResult:
    gross_fcy: Decimal
    gross_inr: Decimal
    tax_fcy: Decimal
    tax_inr: Decimal
    net_fcy: Decimal
    net_inr: Decimal
    it_liability_fcy: Decimal
    it_liability_inr: Decimal
    dtaa_liability_inr: Decimal
    applied_rate: Decimal
    dtaa_claimed: bool
    basis_text: str
    is_tds: bool


def calculate_taxes(inp: TaxComputationInput) -> TaxComputationResult:
    """Pure idempotent function to compute all tax permutations mathematically exactly in FCY."""
    # Local assignments for type checker narrowing
    dtaa_rate_val = inp.dtaa_rate
    it_rate_val = inp.it_rate
    
    # 1. Determine applied rates and DTAA claim status
    dtaa_claimed = False
    applied_rate = it_rate_val
    if inp.basis_mode == "dtaa" and dtaa_rate_val is not None:
        if inp.is_tds:
            dtaa_claimed = _is_integer_rate(float(dtaa_rate_val)) and dtaa_rate_val <= it_rate_val
            applied_rate = dtaa_rate_val if dtaa_claimed else it_rate_val
        else:
            applied_rate = dtaa_rate_val

    # 2. Perform Precise Full-Precision Pipeline
    fx = inp.exchange_rate

    if not inp.is_tds:
        net_fcy_precise = inp.invoice_fcy
        net_inr_precise = net_fcy_precise * fx
        
        gross_fcy_precise = net_fcy_precise
        gross_inr_precise = net_inr_precise

        tax_fcy_precise = Decimal("0.00")
        tax_inr_precise = Decimal("0.00")

        it_liability_inr_precise = gross_inr_precise * it_rate_val / Decimal("100")
        dtaa_liability_inr_precise = Decimal("0.00")
    
    elif inp.is_gross_up:
        if applied_rate >= Decimal("100"):
            raise ValueError("Tax rate cannot be >= 100% for gross-up.")
        
        net_fcy_precise = inp.invoice_fcy
        net_inr_precise = net_fcy_precise * fx
        
        gross_inr_precise = net_inr_precise / (Decimal("1") - (applied_rate / Decimal("100")))
        gross_fcy_precise = net_fcy_precise / (Decimal("1") - (applied_rate / Decimal("100")))
        
        tax_inr_precise = gross_inr_precise - net_inr_precise
        tax_fcy_precise = gross_fcy_precise - net_fcy_precise

        it_liability_inr_precise = gross_inr_precise * it_rate_val / Decimal("100")
        dtaa_liability_inr_precise = tax_inr_precise if dtaa_claimed else Decimal("0.00")
    
    else:  # Normal TDS deduction
        gross_fcy_precise = inp.invoice_fcy
        gross_inr_precise = gross_fcy_precise * fx
        
        tax_inr_precise = gross_inr_precise * (applied_rate / Decimal("100"))
        tax_fcy_precise = gross_fcy_precise * (applied_rate / Decimal("100"))
        
        net_inr_precise = gross_inr_precise - tax_inr_precise
        net_fcy_precise = gross_fcy_precise - tax_fcy_precise

        it_liability_inr_precise = gross_inr_precise * it_rate_val / Decimal("100")
        dtaa_liability_inr_precise = tax_inr_precise if dtaa_claimed else Decimal("0.00")

    # 3. Final Step Only - Absolute Single-Point Quantization
    # User's explicit pipeline target: "Round ONLY here"
    
    # 0 Decimal Places (Nearest Integer) for INR values
    gross_inr = gross_inr_precise.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    tax_inr = tax_inr_precise.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    
    # Preserve additive/subtractive accuracy on integers: gross - tax = net
    net_inr = gross_inr - tax_inr
    
    it_liability_inr = it_liability_inr_precise.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    dtaa_liability_inr = dtaa_liability_inr_precise.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    # 2 Decimal Places for FCY values
    # Following user's direct rule: `final_tax_fcy = round(tax_inr_precise / fx, 2)` or equivalent exact
    # We round from the exact FCY representations equivalently:
    gross_fcy_rounded = gross_fcy_precise.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tax_fcy_rounded = tax_fcy_precise.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    net_fcy_rounded = net_fcy_precise.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    
    # Force exact 2dp invariant structurally against standard floating anomalies
    if inp.is_gross_up:
        gross_fcy_rounded = net_fcy_rounded + tax_fcy_rounded
    else:
        net_fcy_rounded = gross_fcy_rounded - tax_fcy_rounded
        
    it_liability_fcy_rounded = (it_liability_inr_precise / fx if fx else Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    dtaa_liability_fcy_rounded = (dtaa_liability_inr_precise / fx if fx else Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # 6. Basis text logic
    if inp.is_tds and inp.is_gross_up:
        _, basis_text_raw = get_effective_it_rate(float(inp.it_rate))
        basis_text = f"{basis_text_raw} GROSS-UP APPLIED (TAX BORNE BY REMITTER).".strip()
    elif inp.basis_mode == "it_act_2080" or (not dtaa_claimed and inp.is_tds):
        _, basis_text = get_effective_it_rate(float(inp.it_rate))
    else:
        if not inp.is_tds and inp.basis_mode == "it_act_2080":
            basis_text = IT_ACT_BASIS.get(
                float(inp.it_rate),
                f"GROSS AMOUNT OF REMITTANCE IS CONSIDERED AS TAXABLE INCOME AND TAX LIABILITY IS CALCULATED AT {_fmt_num(float(inp.it_rate))} PERCENTAGE OF ABOVE."
            )
        elif not inp.is_tds and inp.dtaa_rate is None:
            basis_text = IT_ACT_BASIS.get(
                float(inp.it_rate),
                f"GROSS AMOUNT OF REMITTANCE IS CONSIDERED AS TAXABLE INCOME AND TAX LIABILITY IS CALCULATED AT {_fmt_num(float(inp.it_rate))} PERCENTAGE OF ABOVE."
            )
        else:
            basis_text = (
                f"GROSS AMOUNT OF REMITTANCE IS CONSIDERED AS TAXABLE INCOME "
                f"AND TAX LIABILITY IS CALCULATED AT {_format_basis_rate_text(inp.it_rate)} "
                f"PERCENTAGE OF ABOVE AS PER APPLICABLE DTAA."
            )

    return TaxComputationResult(
        gross_fcy=gross_fcy_rounded,
        gross_inr=gross_inr,
        tax_fcy=tax_fcy_rounded,
        tax_inr=tax_inr,
        net_fcy=net_fcy_rounded,
        net_inr=net_inr,
        it_liability_fcy=it_liability_fcy_rounded,
        it_liability_inr=it_liability_inr,
        dtaa_liability_inr=dtaa_liability_inr,
        applied_rate=applied_rate,
        dtaa_claimed=dtaa_claimed,
        basis_text=basis_text,
        is_tds=inp.is_tds,
    )


def recompute_invoice(state: Dict[str, object]) -> Dict[str, object]:
    meta = cast(Dict[str, Any], state.setdefault("meta", {}))
    extracted = cast(Dict[str, Any], state.setdefault("extracted", {}))
    form = cast(Dict[str, Any], state.setdefault("form", {}))
    resolved = cast(Dict[str, Any], state.setdefault("resolved", {}))
    computed = cast(Dict[str, Any], state.setdefault("computed", {}))

    mode = str(meta.get("mode") or MODE_TDS)
    invoice_id = str(meta.get("invoice_id") or "")
    exchange_rate = _to_float(str(meta.get("exchange_rate") or "")) or 0.0

    # Source of truth for the base invoice amount: prioritize extracted (paper) value.
    # We use this as a 'frozen' anchor for all tax derivations (VAT, TDS, Gross-up)
    # to prevent recursive feedback loops if AmtPayForgnRem is grossed up in the UI.
    _fcy_extracted = _to_float(str(extracted.get("amount") or "")) or 0.0
    _fcy_ui = _to_float(str(form.get("AmtPayForgnRem") or "")) or 0.0
    fcy = _fcy_extracted if _fcy_extracted > 0 else _fcy_ui
    
    inr_exact_calc = fcy * exchange_rate
    inr_calc = float(_round_to_int(inr_exact_calc))
    manual_inr_override = str(form.get("_ui_inr_manual_override") or "").strip().upper() in {"1", "Y", "YES", "TRUE"}
    manual_inr_value = _to_float(str(form.get("AmtPayIndRem") or ""))
    if manual_inr_override and manual_inr_value is not None:
        inr_exact = float(manual_inr_value)
        inr = float(manual_inr_value)
        computed["inr_amount"] = _fmt_num(inr)
        form["AmtPayIndRem"] = computed["inr_amount"]
    else:
        inr_exact = inr_exact_calc
        inr = inr_calc
        computed["inr_amount"] = str(int(inr))
        form["AmtPayIndRem"] = computed["inr_amount"]
    if not form.get("AmtPayForgnRem"):
        form["AmtPayForgnRem"] = _fmt_num(fcy)

    prop = date.today() + timedelta(days=PROPOSED_DATE_OFFSET_DAYS)
    form.setdefault("PropDateRem", prop.isoformat())

    # Keep beneficiary address fields user-editable in review UI; only default when missing.
    form.setdefault("RemitteeZipCode", REMITTEE_ZIP_CODE)
    form.setdefault("RemitteeState", REMITTEE_STATE)
    form.setdefault("SecRemCovered", SEC_REM_COVERED_DEFAULT)
    # Keep consistent with government utility output even for gross-up mode.
    form["TaxPayGrossSecb"] = "N"
    form.setdefault("TaxResidCert", TAX_RESID_CERT_Y)
    # Income chargeable should mirror INR equivalent in XML output.
    form["AmtIncChrgIt"] = computed["inr_amount"]

    # Read canonical DTAA rate from form first (editable by user/tests), then resolved fallback
    dtaa_rate_percent = _to_float(
        str(
            form.get("RateTdsADtaa")
            or form.get("dtaa_rate")
            or resolved.get("dtaa_rate_percent")
            or ""
        )
    )
    computed["dtaa_rate_percent"] = _fmt_num(dtaa_rate_percent) if dtaa_rate_percent is not None else ""
    
    # Convert key values to Decimal for precise calculations early to avoid NameErrors in logs
    invoice_fcy = Decimal(str(fcy))
    invoice_inr_exact = Decimal(str(inr_exact))
    invoice_inr = Decimal(str(inr)) # Rounded INR amount
    exchange_rate_dec = Decimal(str(exchange_rate))

    # VAT case: invoice has an explicit net+VAT breakdown; net_amount is the taxable income
    # (VAT is not income chargeable to tax under Section 8B).
    _net_fcy_raw = str(extracted.get("net_amount") or "").strip()
    net_fcy = _to_float(_net_fcy_raw) or 0.0
    if _net_fcy_raw and net_fcy == 0.0:
        logger.warning(
            "vat_case_skipped invoice_id=%s reason=net_amount_not_parseable "
            "raw_net_amount=%r — treating as standard case",
            invoice_id, _net_fcy_raw,
        )
    # VAT detection strictly uses the un-grossed paper base (fcy) as the anchor.
    is_vat_case = bool(net_fcy > 0 and fcy > 0 and net_fcy < fcy)

    if is_vat_case:
        logger.info(
            "vat_case_detected invoice_id=%s net_fcy=%s total_fcy=%s",
            invoice_id, _fmt_num(net_fcy), _fmt_num(fcy),
        )

    logger.info(
        "recompute_start invoice_id=%s mode=%s fcy=%s inr=%s fx=%s dtaa_rate=%s",
        invoice_id,
        mode,
        _fmt_num(fcy),
        computed["inr_amount"],
        _fmt_num(exchange_rate),
        computed["dtaa_rate_percent"],
    )
    
    if exchange_rate == 0 and fcy > 0:
        logger.warning(
            "recompute_fx_missing invoice_id=%s fcy=%s currency=%r reason=currency_blank_cannot_lookup_excel action=inr_set_to_zero_pending_manual_entry",
            invoice_id, fcy, meta.get("source_currency_short") or ""
        )

    is_gross_up = bool(meta.get("is_gross_up", False))

    # ── Read user-selected IT Act rate ────────────────────────────────────
    raw_rate = _to_float(str(form.get("ItActRateSelected") or ""))
    selected_it_rate = raw_rate if raw_rate in IT_ACT_RATES else IT_ACT_RATE_DEFAULT
    form["ItActRateSelected"] = str(selected_it_rate)

    # ── Map inputs to Pure Function format ────────────────────────────────
    non_tds_basis = str(form.get("NonTdsBasisRateMode") or "dtaa")
    basis_mode = "dtaa"
    
    if mode == MODE_TDS:
        dtaa_mode = form.get("dtaa_mode")
        if dtaa_mode == "it_act" or str(form.get("BasisDeterTax") or "").strip() == "Act":
            basis_mode = "it_act_2080"
        
        if basis_mode == "it_act_2080" or non_tds_basis == "it_act_2080":
            # Some UI toggles enforce 20.80% explicitly when switching to IT_ACT
            if "20.80" in str(form.get("BasisDeterTax") or "") or non_tds_basis == "it_act_2080":
                selected_it_rate = 20.80
                form["ItActRateSelected"] = "20.8"
                form["dtaa_mode"] = "it_act"
                form["RateTdsSecbFlg"] = RATE_TDS_SECB_FLG_IT_ACT
    else:
        basis_mode = non_tds_basis
        if basis_mode == "it_act_2080":
            selected_it_rate = 20.80

    effective_it_rate, _ = get_effective_it_rate(selected_it_rate)
    
    tax_input = TaxComputationInput(
        invoice_fcy=Decimal(str(net_fcy)) if is_vat_case else invoice_fcy, # VAT case passes down the net base
        exchange_rate=exchange_rate_dec if exchange_rate_dec > 0 else Decimal("0.00"),
        it_rate=Decimal(str(effective_it_rate)),
        dtaa_rate=Decimal(str(dtaa_rate_percent)) if dtaa_rate_percent is not None else None,
        is_gross_up=is_gross_up,
        is_tds=(mode == MODE_TDS),
        basis_mode=basis_mode
    )
    
    # ── Execute Pure Computation ──────────────────────────────────────────
    res = calculate_taxes(tax_input)
    
    # ── Map Results Back To Form (No inline math allowed) ─────────────────
    if mode == MODE_TDS:
        form["AmtIncChrgIt"] = str(int(res.gross_inr))
        form["TaxLiablIt"] = str(int(res.it_liability_inr))
        form["AmtPayIndianTds"] = str(int(res.tax_inr))
        form["AmtPayForgnTds"] = f"{res.tax_fcy:.2f}"
        form["ActlAmtTdsForgn"] = f"{res.net_fcy:.2f}"
        form["BasisDeterTax"] = res.basis_text
        form["RateTdsSecB"] = str(int(res.applied_rate)) if res.applied_rate.to_integral_value() == res.applied_rate else f"{res.applied_rate:.2f}"
        
        # Determine gross remittance out of India
        form["AmtPayForgnRem"] = f"{(res.gross_fcy if is_gross_up else (invoice_fcy if is_vat_case else res.gross_fcy)):.2f}"
        
        if is_gross_up:
            form["TaxPayGrossSecb"] = "Y"
            form["RemittanceCharIndia"] = "Y"
        else:
            form.setdefault("RemittanceCharIndia", "Y")

        if res.dtaa_claimed:
            form["TaxIncDtaa"] = str(int(res.gross_inr))
            form["TaxLiablDtaa"] = str(int(res.tax_inr))
            form["RateTdsADtaa"] = str(int(res.applied_rate)) if res.applied_rate.to_integral_value() == res.applied_rate else f"{res.applied_rate:.2f}"
            form["OtherRemDtaa"] = "N"
            form["RateTdsSecbFlg"] = RATE_TDS_SECB_FLG_DTAA
            form["RemForRoyFlg"] = "Y"
        else:
            form["TaxIncDtaa"] = ""
            form["TaxLiablDtaa"] = ""
            form["RateTdsADtaa"] = ""
            form["OtherRemDtaa"] = "N"
            form["RateTdsSecbFlg"] = RATE_TDS_SECB_FLG_IT_ACT
            form["RemForRoyFlg"] = "N"

        logger.info(
            "recompute_tds_done invoice_id=%s dtaa_claimed=%s values=%s",
            invoice_id, res.dtaa_claimed,
            {
                "TaxLiablIt": form.get("TaxLiablIt", ""),
                "TaxIncDtaa": form.get("TaxIncDtaa", ""),
                "TaxLiablDtaa": form.get("TaxLiablDtaa", ""),
                "AmtPayForgnTds": form.get("AmtPayForgnTds", ""),
                "AmtPayIndianTds": form.get("AmtPayIndianTds", ""),
                "RateTdsSecB": form.get("RateTdsSecB", ""),
                "ActlAmtTdsForgn": form.get("ActlAmtTdsForgn", ""),
            },
        )
    else:
        # MODE_NON_TDS Flow
        existing_chargeable = str(form.get("RemittanceCharIndia") or "").strip().upper()
        form["RemittanceCharIndia"] = existing_chargeable if existing_chargeable in ("Y", "N") else "N"
        
        if form["RemittanceCharIndia"] == "Y":
            form["AmtIncChrgIt"] = str(int(res.gross_inr))
            form["TaxLiablIt"] = str(int(res.it_liability_inr))
            form["BasisDeterTax"] = res.basis_text
        else:
            form["AmtIncChrgIt"] = ""
            form["TaxLiablIt"] = ""
            form["BasisDeterTax"] = ""
            
        # DTAA exemption applies — no TDS deducted
        form["AmtPayForgnTds"] = "0.00"
        form["AmtPayIndianTds"] = "0"
        form["ActlAmtTdsForgn"] = f"{res.net_fcy:.2f}"
        # Respect UI selection from Section 9D in NON_TDS (default remains "Y").
        form["OtherRemDtaa"] = str(form.get("OtherRemDtaa") or "Y").strip().upper()
        # Preserve user's 9A/9B/9C selections; the canonical NON_TDS path uses 9D,
        # so all three default to N unless the user explicitly chose otherwise.
        existing_rem_for_roy = str(form.get("RemForRoyFlg") or "").strip().upper()
        form["RemForRoyFlg"] = existing_rem_for_roy if existing_rem_for_roy in ("Y", "N") else "N"
        existing_bus_inc = str(form.get("RemAcctBusIncFlg") or "").strip().upper()
        form["RemAcctBusIncFlg"] = existing_bus_inc if existing_bus_inc in ("Y", "N") else "N"
        existing_cap_gain = str(form.get("RemOnCapGainFlg") or "").strip().upper()
        form["RemOnCapGainFlg"] = existing_cap_gain if existing_cap_gain in ("Y", "N") else "N"
        form["RateTdsSecbFlg"] = ""
        form["RateTdsSecB"] = ""
        form["DednDateTds"] = ""  # No deduction date for NON_TDS; tag is stripped from final XML
        # DTAA tax fields must be absent in non-TDS XML
        form["TaxIncDtaa"] = ""
        form["TaxLiablDtaa"] = ""
        form["RateTdsADtaa"] = ""
        # Fix 4: Auto-default _ui_only_9d_taxable to "NO" on the canonical NON_TDS path
        # (RemittanceCharIndia=N, OtherRemDtaa=Y) when the user has not yet made a choice.
        if (
            form["RemittanceCharIndia"] == "N"
            and form["OtherRemDtaa"] == "Y"
            and str(form.get("_ui_only_9d_taxable") or "").strip().upper() in ("", "SELECT")
        ):
            form["_ui_only_9d_taxable"] = "NO"

        _9d_taxable_ui = str(form.get("_ui_only_9d_taxable") or "").strip().upper()

        # Fix 5: On the canonical non-taxable 9D path, clear section 9 article/DTAA fields
        # that are irrelevant and would otherwise produce stale XML values.
        _canonical_non_tds_path = (
            form["RemittanceCharIndia"] == "N"
            and form["OtherRemDtaa"] == "Y"
            and _9d_taxable_ui == "NO"
        )
        if _canonical_non_tds_path:
            for _stale in ("RelevantArtDtaa", "TaxIncDtaa", "TaxLiablDtaa", "RateTdsADtaa", "BasisDeterTax", "RateTdsSecB"):
                form[_stale] = ""

        # Cleanup: when 9D taxable = Yes, reasons are not legally required.
        # Remove stale values so they do not appear in XML or confuse the UI.
        # The authoritative sync (NatureRemDtaa / RelArtDetlDDtaa / ReasonNot) happens
        # once in invoice_state_to_xml_fields via apply_non_tds_reason_sync — not here.
        _9d_needs_reasons = form["OtherRemDtaa"] == "Y" and _9d_taxable_ui != "YES"
        if not _9d_needs_reasons:
            form.pop("RelArtDetlDDtaa", None)
            form.pop("ReasonNot", None)
        logger.info(
            "recompute_non_tds_done invoice_id=%s AmtIncChrgIt=%s TaxLiablIt=%s",
            invoice_id, form["AmtIncChrgIt"], form["TaxLiablIt"],
        )

    # Restore Section 8 manual overrides (if any) to preserve UI edits.
    # Skip IT Act numeric/text fields when RemittanceCharIndia=N — blanking them is intentional
    # and restoring a stale override would contradict what goes into the final XML.
    _chargeable = str(form.get("RemittanceCharIndia") or "Y").strip().upper() == "Y"
    _it_act_fields = {"AmtIncChrgIt", "TaxLiablIt", "BasisDeterTax"}
    for field in ["AmtIncChrgIt", "TaxLiablIt", "BasisDeterTax", "SecRemCovered"]:
        override_key = f"_ui_override_sec8_{field}"
        if override_key in form:
            if field in _it_act_fields and not _chargeable:
                continue  # Do not restore — user marked not chargeable
            form[field] = str(form[override_key])
            if field == "SecRemCovered":
                form["SecRemitCovered"] = form[field]

    # Restore Section 9 manual overrides (if any) to preserve UI edits
    sec9_fields = [
        "RelevantDtaa",
        "RelevantArtDtaa",
        "TaxIncDtaa",
        "TaxLiablDtaa",
        "ArtDtaa",
        "RateTdsADtaa",
        "NatureRemDtaa",
        "RelArtDetlDDtaa",
    ]
    for field in sec9_fields:
        override_key = f"_ui_override_sec9_{field}"
        if override_key in form:
            form[field] = str(form[override_key])

    # Restore Section 10-13 manual overrides (if any) to preserve UI edits
    for sec, field in [
        ("sec10", "AmtPayForgnTds"),
        ("sec10", "AmtPayIndianTds"),
        ("sec11", "RateTdsSecB"),
        ("sec12", "ActlAmtTdsForgn"),
        ("sec13", "DednDateTds"),
    ]:
        override_key = f"_ui_override_{sec}_{field}"
        if override_key in form:
            form[field] = str(form[override_key])

    # NON_TDS enforcement: actual remittance in foreign currency must always equal
    # the invoice amount exactly — no TDS is deducted so nothing is withheld.
    # This re-locks ActlAmtTdsForgn after the UI-override loop above, so any
    # accidental manual edit to Section 12 in non-TDS mode cannot create a
    # discrepancy between AmtPayForgnRem and ActlAmtTdsForgn.
    if mode == MODE_NON_TDS:
        form["ActlAmtTdsForgn"] = _fmt_num(fcy)

    # Scrubber: if DTAA is not claimed, strictly eliminate all DTAA tax configuration 
    # to prevent hidden state dependencies and override leakage.
    dtaa_claimed = mode == MODE_TDS and form.get("RateTdsSecbFlg") == RATE_TDS_SECB_FLG_DTAA
    if not dtaa_claimed:
        computed.pop("dtaa_rate_percent", None)
        for dtaa_field in ["RelevantDtaa", "RelevantArtDtaa", "TaxIncDtaa", "TaxLiablDtaa", "ArtDtaa", "RateTdsADtaa"]:
            form[dtaa_field] = ""
            form.pop(f"_ui_override_sec9_{dtaa_field}", None)

    # Final step: round FCY amounts to 2 dp and INR amounts to integer.
    # All intermediate calculations above run at full precision; rounding
    # only happens here so there is no cascading error mid-calculation.
    _apply_amount_rounding(form)

    return state

def _split_at_boundary(text: str, max_len: int) -> tuple:
    """Split text at the last word boundary at or before max_len.
    Returns (first_part, remainder). If no space exists before max_len, splits hard."""
    if len(text) <= max_len:
        return text, ""
    split_pos = text.rfind(" ", 0, max_len)
    if split_pos == -1:
        split_pos = max_len  # no word boundary found — hard split
    return text[:split_pos].strip(), text[split_pos:].strip()


def _redistribute_address_overflow(out: Dict[str, str], max_len: int = 50) -> Dict[str, str]:
    """Redistribute address overflow across adjacent fields instead of truncating.

    If RemitteeAreaLocality exceeds max_len, the excess spills into RemitteeRoadStreet,
    then into RemitteeFlatDoorBuilding, then into RemitteePremisesBuildingVillage.
    Each field is split at a word boundary. Any overflow beyond the last field is
    handled by _enforce_field_limits (hard truncation as a last resort).
    """
    overflow_order = [
        "RemitteeAreaLocality",
        "RemitteeRoadStreet",
        "RemitteeFlatDoorBuilding",
        "RemitteePremisesBuildingVillage",
    ]
    for i, field in enumerate(overflow_order):
        val = str(out.get(field) or "").strip()
        if len(val) <= max_len:
            continue
        first, remainder = _split_at_boundary(val, max_len)
        out[field] = first
        if remainder and i + 1 < len(overflow_order):
            next_field = overflow_order[i + 1]
            existing = str(out.get(next_field) or "").strip()
            # Prepend overflow so existing content stays at the end
            out[next_field] = (remainder + (" " + existing if existing else "")).strip()
            logger.info(
                "address_overflow_redistributed from=%s to=%s overflow_len=%s",
                field, next_field, len(remainder),
            )
    return out


def _enforce_field_limits(out: Dict[str, str]) -> Dict[str, str]:
    """Truncate fields to their maximum allowed lengths defined in FIELD_MAX_LENGTH."""
    for field, max_len in FIELD_MAX_LENGTH.items():
        if field in out:
            val = str(out[field])
            if len(val) > max_len:
                logger.warning(
                    "field_truncated field=%s original_len=%s max=%s",
                    field,
                    len(val),
                    max_len,
                )
                out[field] = val[:max_len]
    return out


def invoice_state_to_xml_fields(state: Dict[str, object]) -> Dict[str, str]:
    def _ensure_comma_after_number(value: str) -> str:
        """Insert a comma after the building number if missing (e.g. "NO. 55 SECOND FLOOR" → "NO. 55, SECOND FLOOR")."""
        if not value or "," in value:
            return value
        m = re.match(r"^(NO\.?\s*\d+)(\s+)(.+)$", value, re.IGNORECASE)
        if m:
            return f"{m.group(1)},{m.group(2)}{m.group(3)}"
        return value

    meta = cast(Dict[str, Any], state.get("meta", {}))
    extracted = cast(Dict[str, Any], state.get("extracted", {}))
    form = cast(Dict[str, Any], state.get("form", {}))
    resolved = cast(Dict[str, Any], state.get("resolved", {}))
    mode = str(meta.get("mode") or MODE_TDS)

    def _form_or_extracted(form_key: str, extracted_key: str | None = None, default: str = "") -> str:
        """Prefer user-edited form values (even empty) over extracted defaults."""
        if form_key in form:
            return str(form.get(form_key) or "")
        if extracted_key:
            return str(extracted.get(extracted_key) or "")
        return str(default or "")

    remitter_name = _form_or_extracted("NameRemitterInput", "remitter_name") or _form_or_extracted("NameRemitter")
    remitter_address = _form_or_extracted("RemitterAddress", "remitter_address").strip().upper()
    # Strip company name if Gemini prepended it to the address (e.g. "Bosch Ltd. Adogodi, Hosur..." → "Adogodi, Hosur...")
    _name_prefix = remitter_name.upper().rstrip(". ")
    if _name_prefix and remitter_address.startswith(_name_prefix):
        remitter_address = remitter_address[len(_name_prefix):].lstrip(" .,;:-").strip()
    beneficiary = (
        _form_or_extracted("NameRemitteeInput")
        or _form_or_extracted("NameRemittee")
        or clean_beneficiary_name(str(extracted.get("beneficiary_name") or ""))
        or ""
    ).strip()
    # Read invoice number and date from form (user-editable), with fallback to extracted
    invoice_no = _form_or_extracted("InvoiceNumber", "invoice_number").strip()
    invoice_date_iso = (
        _form_or_extracted("InvoiceDate", "invoice_date_iso")
        or _form_or_extracted("InvoiceDate", "invoice_date_display")
        or _form_or_extracted("InvoiceDate", "invoice_date_raw")
    ).strip()
    dotted = format_dotted_date(invoice_date_iso)

    name_remitter = remitter_name.strip()
    # Only append address if it's not already visibly a part of the remitter name 
    # (avoiding duplication from Gemini extraction)
    if remitter_address and remitter_address not in name_remitter.upper():
        name_remitter = f"{name_remitter}. {remitter_address}".strip(". ").strip()
    name_remitter = name_remitter.upper()
    
    name_remittee = _build_name_remittee(beneficiary, invoice_no, dotted)
    raw_relevant_dtaa = str(form.get("RelevantDtaa") or "").strip()
    raw_relevant_article = str(form.get("RelevantArtDtaa") or form.get("ArtDtaa") or "").strip()
    dtaa_source = raw_relevant_article or raw_relevant_dtaa
    dtaa_without_article, dtaa_with_article = split_dtaa_article_text(dtaa_source)

    # Ensure that if only the plain DTAA name is present (no ARTICLE prefix), we
    # attempt to enrich it with the canonical ARTICLE text from the DTAA master map.
    # This prevents XML output from dropping the “ARTICLE X OF ...” prefix.
    if dtaa_with_article and not re.match(r"(?i)^ARTICLE\s+\d+", dtaa_with_article):
        # Try to infer the country from the DTAA phrase and look it up.
        m = re.search(r"DTAA\s+BTWN\s+INDIA\s+AND\s+(.+)$", dtaa_with_article, flags=re.IGNORECASE)
        if m:
            country_hint = m.group(1).strip()
            from modules.master_lookups import resolve_dtaa

            dtaa = resolve_dtaa(country_hint)
            if dtaa:
                enriched = str(dtaa.get("dtaa_applicable") or "").strip()
                if enriched:
                    dtaa_with_article = enriched
                    # Keep the plain DTAA name if it was explicitly provided.
                    if not dtaa_without_article:
                        dtaa_without_article = dtaa_with_article

    if not dtaa_without_article:
        dtaa_without_article = raw_relevant_dtaa
    if not dtaa_with_article:
        dtaa_with_article = raw_relevant_article

    acctnt_flat = str(form.get("AcctntFlatDoorBuilding") or CA_DEFAULTS.get("AcctntFlatDoorBuilding") or "")
    acctnt_flat = _ensure_comma_after_number(acctnt_flat)

    out: Dict[str, str] = {
        "SWVersionNo": SW_VERSION_NO,
        "SWCreatedBy": SW_CREATED_BY,
        "XMLCreatedBy": XML_CREATED_BY,
        "XMLCreationDate": datetime.now().strftime("%Y-%m-%d"),
        "IntermediaryCity": INTERMEDIARY_CITY,
        "FormName": FORM_NAME,
        "Description": FORM_DESCRIPTION,
        "AssessmentYear": ASSESSMENT_YEAR,
        "SchemaVer": SCHEMA_VER,
        "FormVer": FORM_VER,
        "IorWe": IOR_WE_CODE,
        "RemitterHonorific": HONORIFIC_M_S,
        "BeneficiaryHonorific": HONORIFIC_M_S,
        "NameRemitter": name_remitter,
        "RemitterPAN": str(form.get("RemitterPAN") or resolved.get("pan") or ""),
        "NameRemittee": name_remittee,
        "RemitteePremisesBuildingVillage": str(form.get("RemitteePremisesBuildingVillage") or ""),
        "RemitteeFlatDoorBuilding": str(form.get("RemitteeFlatDoorBuilding") or ""),
        "RemitteeAreaLocality": str(form.get("RemitteeAreaLocality") or ""),
        "RemitteeTownCityDistrict": str(form.get("RemitteeTownCityDistrict") or ""),
        "RemitteeRoadStreet": str(form.get("RemitteeRoadStreet") or ""),
        "RemitteeZipCode": str(form.get("RemitteeZipCode") or REMITTEE_ZIP_CODE),
        "RemitteeState": str(form.get("RemitteeState") or REMITTEE_STATE),
        "RemitteeCountryCode": str(form.get("RemitteeCountryCode") or ""),
        "CountryRemMadeSecb": str(form.get("CountryRemMadeSecb") or ""),
        "CurrencySecbCode": str(form.get("CurrencySecbCode") or ""),
        "AmtPayForgnRem": str(form.get("AmtPayForgnRem") or ""),
        "AmtPayIndRem": str(form.get("AmtPayIndRem") or ""),
        "NameBankCode": str(form.get("NameBankCode") or ""),
        "BranchName": str(form.get("BranchName") or ""),
        "BsrCode": str(form.get("BsrCode") or ""),
        "PropDateRem": _format_iso(form.get("PropDateRem")),
        "NatureRemCategory": str(form.get("NatureRemCategory") or ""),
        "RevPurCategory": str(form.get("RevPurCategory") or ""),
        "RevPurCode": str(form.get("RevPurCode") or ""),
        "TaxPayGrossSecb": str(form.get("TaxPayGrossSecb") or "N"),
        "RemittanceCharIndia": str(form.get("RemittanceCharIndia") or "Y"),
        "ReasonNot": str(form.get("ReasonNot") or ""),
        "SecRemCovered": str(form.get("SecRemCovered") or SEC_REM_COVERED_DEFAULT),
        "AmtIncChrgIt": str(form.get("AmtIncChrgIt") or ""),
        "TaxLiablIt": str(form.get("TaxLiablIt") or ""),
        "BasisDeterTax": str(form.get("BasisDeterTax") or ""),
        "TaxResidCert": str(form.get("TaxResidCert") or TAX_RESID_CERT_Y),
        "RelevantDtaa": dtaa_without_article,
        "RelevantArtDtaa": dtaa_with_article,
        "TaxIncDtaa": str(form.get("TaxIncDtaa") or ""),
        "TaxLiablDtaa": str(form.get("TaxLiablDtaa") or ""),
        "RemForRoyFlg": str(form.get("RemForRoyFlg") or ("Y" if mode == MODE_TDS else "N")),
        "ArtDtaa": dtaa_with_article,
        "RateTdsADtaa": str(form.get("RateTdsADtaa") or ""),
        "RemAcctBusIncFlg": str(form.get("RemAcctBusIncFlg") or "N"),
        "IncLiabIndiaFlg": INC_LIAB_INDIA_ALWAYS,
        "RemOnCapGainFlg": str(form.get("RemOnCapGainFlg") or "N"),
        "OtherRemDtaa": str(form.get("OtherRemDtaa") or ("N" if mode == MODE_TDS else "Y")),
        "NatureRemDtaa": str(form.get("NatureRemDtaa") or ""),
        "TaxIndDtaaFlg": TAX_IND_DTAA_ALWAYS,
        "RelArtDetlDDtaa": str(form.get("RelArtDetlDDtaa") or ("NOT APPLICABLE" if mode == MODE_TDS else "")),
        "AmtPayForgnTds": str(form.get("AmtPayForgnTds") or ("0" if mode == MODE_NON_TDS else "")),
        "AmtPayIndianTds": str(form.get("AmtPayIndianTds") or ("0" if mode == MODE_NON_TDS else "")),
        "RateTdsSecbFlg": str(form.get("RateTdsSecbFlg") or (RATE_TDS_SECB_FLG_IT_ACT if mode == MODE_TDS else "")),
        "RateTdsSecB": str(form.get("RateTdsSecB") or ""),
        "ActlAmtTdsForgn": str(form.get("ActlAmtTdsForgn") or ""),
        "DednDateTds": _format_iso(form.get("DednDateTds")),
    }
    out.update(CA_DEFAULTS)
    out["AcctntFlatDoorBuilding"] = acctnt_flat
    out["NameFirmAcctnt"] = str(form.get("NameFirmAcctnt") or CA_DEFAULTS["NameFirmAcctnt"])
    out["NameAcctnt"] = str(form.get("NameAcctnt") or CA_DEFAULTS["NameAcctnt"])

    # Enforce canonical field relationships to match utility output.
    is_gross_up_xml = bool(meta.get("is_gross_up"))
    out["TaxPayGrossSecb"] = "Y" if is_gross_up_xml else "N"
    # For gross-up, AmtIncChrgIt is the grossed-up INR already set by recompute_invoice.
    # For non-gross-up, trust what recompute_invoice computed (same value is shown in UI).
    # Only fall back to AmtPayIndRem if recompute did not populate AmtIncChrgIt (e.g. mode
    # branch not reached).  This prevents a silent discrepancy between what the user sees
    # on screen and what ends up in the XML.
    if not is_gross_up_xml:
        if not str(out.get("AmtIncChrgIt") or "").strip():
            out["AmtIncChrgIt"] = str(out.get("AmtPayIndRem") or "")

    gross_fcy = _to_float(out.get("AmtPayForgnRem", ""))
    tds_fcy = _to_float(out.get("AmtPayForgnTds", ""))
    # For gross-up, ActlAmtTdsForgn is already set correctly by recompute_invoice
    # (= gross_fcy - tds_fcy = original net). Do not recalculate here or it becomes
    # AmtPayForgnRem(net) - tds_fcy which gives the wrong (too-low) value.
    # For non-gross-up TDS, similarly trust recompute_invoice; only derive when blank.
    if mode == MODE_TDS and not is_gross_up_xml and gross_fcy is not None and tds_fcy is not None:
        if not str(out.get("ActlAmtTdsForgn") or "").strip():
            net_fcy = max(gross_fcy - tds_fcy, 0.0)
            out["ActlAmtTdsForgn"] = _fmt_num(net_fcy)
    elif mode == MODE_NON_TDS and gross_fcy is not None:
        # Non-TDS: no withholding is applied, so the actual remittance in foreign
        # currency is always exactly the invoice amount.  Enforce this unconditionally
        # so that any stale UI value or manual edit cannot cause a discrepancy in the
        # generated XML.
        out["ActlAmtTdsForgn"] = _fmt_num(gross_fcy)

    tax_resid_cert = str(out.get("TaxResidCert") or "N").strip().upper()
    if mode == MODE_TDS:
        other_rem_dtaa = "N"
    else:
        other_rem_dtaa = str(out.get("OtherRemDtaa") or "Y").strip().upper()
    rate_secb = _to_float(out.get("RateTdsSecB", ""))
    rate_dtaa = _to_float(out.get("RateTdsADtaa", ""))
    rate_flag = str(out.get("RateTdsSecbFlg") or "").strip()
    dtaa_claimed = mode == MODE_TDS and tax_resid_cert == "Y" and rate_flag == RATE_TDS_SECB_FLG_DTAA

    if dtaa_claimed:
        rate_for_claim = rate_dtaa if rate_dtaa is not None else rate_secb
        if not _is_integer_rate(rate_for_claim):
            dtaa_claimed = False
            out["RateTdsSecbFlg"] = RATE_TDS_SECB_FLG_IT_ACT
        else:
            integer_rate = str(int(round(float(rate_for_claim))))
            out["RateTdsADtaa"] = integer_rate
            out["RateTdsSecB"] = integer_rate
            out["TaxIncDtaa"] = str(out.get("TaxIncDtaa") or out.get("AmtPayIndRem") or "")
            out["TaxLiablDtaa"] = str(out.get("TaxLiablDtaa") or out.get("AmtPayIndianTds") or out.get("TaxLiablIt") or "")

    if not dtaa_claimed:
        out["TaxIncDtaa"] = ""
        out["TaxLiablDtaa"] = ""
        out["RateTdsADtaa"] = ""
        # When DTAA relief is not claimed, DTAA-article fields should be omitted.
        out["RelevantArtDtaa"] = ""
        out["ArtDtaa"] = ""

    out["RemForRoyFlg"] = "Y" if dtaa_claimed else "N"

    # If treaty is not claimed (IT Act rate case), OtherRemDtaa must be "Y" 
    # to signal that treaty article details are omitted / not applied.
    # In MODE_TDS, we set it to "N" only if DTAA is explicitly claimed.
    other_rem_dtaa_val = other_rem_dtaa if mode != MODE_TDS else ("N" if dtaa_claimed else "Y")
    out["OtherRemDtaa"] = other_rem_dtaa_val

    # Final-field-map sync for NON_TDS 9D non-taxable path.
    # apply_non_tds_reason_sync is the single source of truth for NatureRemDtaa,
    # RelArtDetlDDtaa, and ReasonNot.  Calling it here covers the case where the
    # recompute branch did not run (e.g. direct XML generation without UI recompute).
    # Guard mirrors the recompute condition: OtherRemDtaa=Y and 9D taxable ≠ YES.
    if mode == MODE_NON_TDS and other_rem_dtaa_val == "Y":
        _9d_taxable_out = str(form.get("_ui_only_9d_taxable") or "").strip().upper()
        if _9d_taxable_out != "YES":
            out = apply_non_tds_reason_sync(out)

    # For non-chargeable remittance, keep reason text and suppress IT Act tax block fields.
    if str(out.get("RemittanceCharIndia") or "Y").strip().upper() != "Y":
        out["SecRemCovered"] = ""
        out["AmtIncChrgIt"] = ""
        out["TaxLiablIt"] = ""
        out["BasisDeterTax"] = ""

    # Final step: enforce FCY → 2 dp and INR → integer in XML output.
    # This covers any amounts derived within this function (e.g. ActlAmtTdsForgn
    # computed from AmtPayForgnRem − AmtPayForgnTds) that bypass recompute_invoice.
    _apply_amount_rounding(out)

    out = _redistribute_address_overflow(out)
    return _enforce_field_limits(out)
