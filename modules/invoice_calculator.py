from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Dict, Optional

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
    return text.strip(" .,-")


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


def recompute_invoice(state: Dict[str, object]) -> Dict[str, object]:
    meta = state.setdefault("meta", {})
    extracted = state.setdefault("extracted", {})
    form = state.setdefault("form", {})
    resolved = state.setdefault("resolved", {})
    computed = state.setdefault("computed", {})

    mode = str(meta.get("mode") or MODE_TDS)
    invoice_id = str(meta.get("invoice_id") or "")
    exchange_rate = _to_float(str(meta.get("exchange_rate") or "")) or 0.0
    fcy = _to_float(str(form.get("AmtPayForgnRem") or extracted.get("amount") or "")) or 0.0
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
    raw_rate = _to_float(form.get("ItActRateSelected"))
    if raw_rate not in IT_ACT_RATES:
        selected_it_rate = IT_ACT_RATE_DEFAULT
    else:
        selected_it_rate = raw_rate
    form["ItActRateSelected"] = str(selected_it_rate)

    # ── Calculation basis override: "20.80% (IT Act)" toggle ──────────────
    # When user selects 20.80% IT Act as calculation basis in TDS mode,
    # bypass DTAA and compute TDS at 20.80% instead.
    non_tds_basis = str(form.get("NonTdsBasisRateMode") or "dtaa")
    if mode == MODE_TDS and non_tds_basis == "it_act_2080":
        selected_it_rate = 20.80
        form["ItActRateSelected"] = "20.8"
        form["dtaa_mode"] = "it_act"
        # Force RateTdsSecbFlg to IT Act ("1") so RemForRoyFlg becomes "N" downstream
        form["RateTdsSecbFlg"] = RATE_TDS_SECB_FLG_IT_ACT

    # --- PRIORITY 1: GROSS-UP FLOW ---
    if mode == MODE_TDS and is_gross_up:
        it_factor, basis_text = get_effective_it_rate(selected_it_rate)
        it_rate_dec = Decimal(str(it_factor))

        # Use DTAA rate for gross-up when available; fall back to IT Act rate.
        dtaa_available = dtaa_rate_percent is not None
        gross_rate_dec = Decimal(str(dtaa_rate_percent)) if dtaa_available else it_rate_dec

        if gross_rate_dec < 100:
            # Net → Gross: gross_inr = net_inr * 100 / (100 - rate)
            gross_inr_exact = invoice_inr_exact * Decimal("100") / (Decimal("100") - gross_rate_dec)
            gross_inr_rounded = gross_inr_exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

            # TDS at gross-up rate on grossed-up INR
            tds_inr_exact = gross_inr_rounded * gross_rate_dec / Decimal("100")
            tds_inr_rounded = tds_inr_exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

            # IT Act liability at IT rate on grossed-up INR (may differ from TDS when DTAA applies)
            it_liab_exact = gross_inr_rounded * it_rate_dec / Decimal("100")
            it_liab_rounded = it_liab_exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

            # TDS in FCY: derived from tds_inr / fx (more accurate than gross_fcy * rate)
            if exchange_rate_dec > 0:
                tds_fcy = (tds_inr_exact / exchange_rate_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                tds_fcy = Decimal("0.00")

            form["AmtIncChrgIt"] = str(int(gross_inr_rounded))
            form["TaxLiablIt"] = str(int(it_liab_rounded))
            form["AmtPayIndianTds"] = str(int(tds_inr_rounded))
            form["TaxPayGrossSecb"] = "Y"
            form["AmtPayForgnTds"] = f"{tds_fcy:.2f}"
            # In gross-up, contractual remittance is the beneficiary's net receipt.
            form["ActlAmtTdsForgn"] = _fmt_num(float(invoice_fcy))
            form["BasisDeterTax"] = f"{basis_text} GROSS-UP APPLIED (TAX BORNE BY REMITTER).".strip()
            form["RateTdsSecB"] = _fmt_num(float(gross_rate_dec))
            form["RemittanceCharIndia"] = "Y"

            if dtaa_available:
                # DTAA gross-up: populate DTAA fields with grossed-up amounts
                form["TaxIncDtaa"] = str(int(gross_inr_rounded))
                form["TaxLiablDtaa"] = str(int(tds_inr_rounded))
                form["RateTdsADtaa"] = str(int(round(float(gross_rate_dec))))
                form["OtherRemDtaa"] = "N"
                form["RateTdsSecbFlg"] = RATE_TDS_SECB_FLG_DTAA
            else:
                # IT Act only gross-up: clear DTAA fields
                form["TaxIncDtaa"] = ""
                form["TaxLiablDtaa"] = ""
                form["RateTdsADtaa"] = ""
                form["OtherRemDtaa"] = "N"
                form["RateTdsSecbFlg"] = RATE_TDS_SECB_FLG_IT_ACT

            logger.info(
                "recompute_tds_done invoice_id=%s dtaa_claimed=%s values=%s",
                invoice_id,
                dtaa_available,
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

    elif mode == MODE_TDS and (dtaa_rate_percent is not None or form.get("dtaa_mode") == "it_act"):
        it_factor, it_basis = get_effective_it_rate(selected_it_rate)

        dtaa_mode = form.get("dtaa_mode")
        if dtaa_mode == "it_act":
            dtaa_claimed = False
            applied_rate_dec = Decimal(str(it_factor))
        else:
            dtaa_rate_dec = Decimal(str(dtaa_rate_percent))
            it_rate_dec = Decimal(str(it_factor))
            dtaa_claimed = _is_integer_rate(float(dtaa_rate_dec)) and dtaa_rate_dec <= it_rate_dec
            applied_rate_dec = dtaa_rate_dec if dtaa_claimed else it_rate_dec

        it_rate_dec = Decimal(str(it_factor))
        it_liab = invoice_inr * (it_rate_dec / Decimal("100"))
        dtaa_liab = invoice_inr * (applied_rate_dec / Decimal("100")) if dtaa_claimed else Decimal("0")
        tds_fcy_dec = invoice_fcy * (applied_rate_dec / Decimal("100"))
        tds_inr_dec = invoice_inr * (applied_rate_dec / Decimal("100"))
        actual_fcy = max(invoice_fcy - tds_fcy_dec, Decimal("0"))

        # INR tax amounts should be whole rupees (rounded)
        form["AmtIncChrgIt"] = _fmt_num(_round_to_int(float(invoice_inr)))
        form["TaxLiablIt"] = _fmt_num(_round_to_int(float(it_liab)))
        if dtaa_claimed:
            form["TaxIncDtaa"] = _fmt_num(_round_to_int(float(invoice_inr)))
            form["TaxLiablDtaa"] = _fmt_num(_round_to_int(float(dtaa_liab)))
            form["RateTdsADtaa"] = str(int(round(float(applied_rate_dec))))
            form["RateTdsSecB"] = form["RateTdsADtaa"]
            form["OtherRemDtaa"] = "N"
            form["RateTdsSecbFlg"] = RATE_TDS_SECB_FLG_DTAA
        else:
            form["TaxIncDtaa"] = ""
            form["TaxLiablDtaa"] = ""
            form["RateTdsADtaa"] = ""
            form["RateTdsSecB"] = _fmt_num(float(applied_rate_dec))
            form["OtherRemDtaa"] = "N"
            form["RateTdsSecbFlg"] = RATE_TDS_SECB_FLG_IT_ACT

        # Foreign currency TDS and actual remittance keep up to 2 decimals
        form["AmtPayForgnTds"] = _fmt_num(float(tds_fcy_dec))
        form["AmtPayIndianTds"] = _fmt_num(_round_to_int(float(tds_inr_dec)))
        form["ActlAmtTdsForgn"] = _fmt_num(float(actual_fcy))
        form["RemForRoyFlg"] = "Y" if dtaa_claimed else "N"

        form["BasisDeterTax"] = it_basis
        form["RemittanceCharIndia"] = "Y"
        logger.info(
            "recompute_tds_done invoice_id=%s dtaa_claimed=%s values=%s",
            invoice_id,
            dtaa_claimed,
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
    elif mode == MODE_TDS and str(form.get("BasisDeterTax") or "").strip() == "Act":
        # Income Tax Act Section 195 path – uses user-selected rate
        effective_rate, basis_text = get_effective_it_rate(selected_it_rate)
        tax_liable_it = _round_to_int(inr * (effective_rate / 100.0))
        tax_fcy = float(tax_liable_it) / exchange_rate if exchange_rate else 0.0
        
        form["TaxLiablIt"] = _fmt_num(tax_liable_it)
        form["BasisDeterTax"] = basis_text
        form["RateTdsSecB"] = "{:.2f}".format(effective_rate)
        form["RateTdsSecbFlg"] = RATE_TDS_SECB_FLG_IT_ACT
        form.setdefault("RemittanceCharIndia", "Y")
        form["OtherRemDtaa"] = "N"
        form["RemForRoyFlg"] = "N"
        # Clear DTAA-specific fields since we're using IT Act
        form["TaxIncDtaa"] = ""
        form["TaxLiablDtaa"] = ""
        form["RateTdsADtaa"] = ""
        form["AmtPayForgnTds"] = f"{tax_fcy:.2f}"
        form["AmtPayIndianTds"] = str(tax_liable_it)
        form["ActlAmtTdsForgn"] = _fmt_num(max(fcy - tax_fcy, 0.0))
        logger.info(
            "recompute_it_act_done invoice_id=%s rate=%s inr_amount=%s tax_liable=%s",
            invoice_id,
            effective_rate,
            inr,
            tax_liable_it,
        )
    elif mode == MODE_NON_TDS:
        # IT Act liability is computed for documentation purposes even though no TDS is withheld.
        # Rate used depends on user toggle: DTAA rate (default) or 20.80% (IT Act).
        non_tds_rate_mode = str(form.get("NonTdsBasisRateMode") or "dtaa")
        if non_tds_rate_mode == "it_act_2080":
            use_rate_dec = Decimal("20.80")
            basis_text = IT_ACT_BASIS.get(
                20.80,
                "GROSS AMOUNT OF REMITTANCE IS CONSIDERED AS TAXABLE INCOME "
                "AND TAX LIABILITY IS CALCULATED AT 20.80 PERCENTAGE OF ABOVE.",
            )
        else:
            # DTAA mode: use resolved DTAA rate if available, else fall back to 20.80%
            _doc_dtaa_rate = dtaa_rate_percent
            if _doc_dtaa_rate is not None and _doc_dtaa_rate > 0:
                use_rate_dec = Decimal(str(_doc_dtaa_rate))
                basis_text = (
                    f"GROSS AMOUNT OF REMITTANCE IS CONSIDERED AS TAXABLE INCOME "
                    f"AND TAX LIABILITY IS CALCULATED AT {_fmt_num(float(use_rate_dec))} "
                    f"PERCENTAGE OF ABOVE AS PER APPLICABLE DTAA."
                )
            else:
                # No DTAA rate available — fall back to 20.80%
                use_rate_dec = Decimal("20.80")
                basis_text = IT_ACT_BASIS.get(
                    20.80,
                    "GROSS AMOUNT OF REMITTANCE IS CONSIDERED AS TAXABLE INCOME "
                    "AND TAX LIABILITY IS CALCULATED AT 20.80 PERCENTAGE OF ABOVE.",
                )
                logger.info("non_tds_dtaa_rate_unavailable invoice_id=%s fallback=20.80", invoice_id)

        it_liab = invoice_inr * (use_rate_dec / Decimal("100"))
        form["AmtIncChrgIt"] = _fmt_num(_round_to_int(float(invoice_inr)))
        form["TaxLiablIt"] = _fmt_num(_round_to_int(float(it_liab)))
        form["BasisDeterTax"] = basis_text
        # DTAA exemption applies — no TDS deducted
        form["RemittanceCharIndia"] = "Y"
        form["AmtPayForgnTds"] = "0"
        form["AmtPayIndianTds"] = "0"
        form["ActlAmtTdsForgn"] = _fmt_num(fcy)
        # Respect UI selection from Section 9D in NON_TDS (default remains "Y").
        form["OtherRemDtaa"] = str(form.get("OtherRemDtaa") or "Y").strip().upper()
        form["RemForRoyFlg"] = "Y" if non_tds_rate_mode == "dtaa" else "N"
        form["RateTdsSecbFlg"] = ""
        form["RateTdsSecB"] = ""
        # form["DednDateTds"] = ""  # Removed to allow "Deduction Date" mapping in non-tds mode
        # DTAA tax fields must be absent in non-TDS XML
        form["TaxIncDtaa"] = ""
        form["TaxLiablDtaa"] = ""
        form["RateTdsADtaa"] = ""
        # Ensure DTAA comment fields are never blank — regenerate from nature if cleared.
        if not str(form.get("NatureRemDtaa") or "").strip():
            form["NatureRemDtaa"] = "FEES FOR TECHNICAL SERVICES"
        if not str(form.get("RelArtDetlDDtaa") or "").strip():
            from modules.non_tds_lookup import _comment_for_nature
            form["RelArtDetlDDtaa"] = _comment_for_nature(form["NatureRemDtaa"])
            logger.info("recompute_non_tds_comment_regenerated invoice_id=%s nature=%r", invoice_id, form["NatureRemDtaa"])
        logger.info(
            "recompute_non_tds_done invoice_id=%s AmtIncChrgIt=%s TaxLiablIt=%s",
            invoice_id, form["AmtIncChrgIt"], form["TaxLiablIt"],
        )
    elif mode == MODE_TDS:
        country_code = str(form.get("CountryRemMadeSecb") or "").strip()
        skip_reason = "country_blank" if not country_code else "country_selected_rate_missing"
        logger.warning(
            "recompute_tds_skipped invoice_id=%s reason=%s country=%s remitter_pan=%s",
            invoice_id,
            skip_reason,
            country_code,
            str(form.get("RemitterPAN") or ""),
        )

    # Restore Section 8 manual overrides (if any) to preserve UI edits
    for field in ["AmtIncChrgIt", "TaxLiablIt", "BasisDeterTax", "SecRemCovered"]:
        override_key = f"_ui_override_sec8_{field}"
        if override_key in form:
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

    meta = state.get("meta", {})
    extracted = state.get("extracted", {})
    form = state.get("form", {})
    resolved = state.get("resolved", {})
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
    
    name_remittee = _build_name_remittee(beneficiary, invoice_no, dotted)
    raw_relevant_dtaa = str(form.get("RelevantDtaa") or "").strip()
    raw_relevant_article = str(form.get("RelevantArtDtaa") or form.get("ArtDtaa") or "").strip()
    dtaa_source = raw_relevant_article or raw_relevant_dtaa
    dtaa_without_article, dtaa_with_article = split_dtaa_article_text(dtaa_source)
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

    # If OtherRemDtaa is "Y", ensure NatureRemDtaa is not blank.
    if other_rem_dtaa_val == "Y" and not out.get("NatureRemDtaa"):
        # 1. Try to resolve label from NatureRemCategory code
        cat_code = str(out.get("NatureRemCategory") or "").strip()
        label = ""
        if cat_code:
            options = load_nature_options()
            for opt in options:
                if str(opt.get("code")).strip() == cat_code:
                    label = str(opt.get("label") or "").strip()
                    break
        
        # 2. Fallback to AI-extracted nature or default
        if not label:
            label = str(form.get("nature_of_remittance") or "FEES FOR TECHNICAL SERVICES").strip()
        
        out["NatureRemDtaa"] = label

    # For non-chargeable remittance, keep reason text and suppress IT Act tax block fields.
    if str(out.get("RemittanceCharIndia") or "Y").strip().upper() != "Y":
        out["SecRemCovered"] = ""
        out["AmtIncChrgIt"] = ""
        out["TaxLiablIt"] = ""
        out["BasisDeterTax"] = ""

    out = _redistribute_address_overflow(out)
    return _enforce_field_limits(out)
