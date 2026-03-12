"""
UI.py — State-driven Form 15CB review/edit form.

Implements the canonical field-mapping specification.

Post-processing review UI shown after "Process Invoice".

Canonical state model:
    state["form"]      — final XML-facing values
    state["meta"]      — mode / gross-up / rate / exchange / invoice context
    state["extracted"] — Gemini raw extraction
    state["resolved"]  — master-data resolved values

Public API:
    init_new_ui_from_state(state) -> None
        Seed st.session_state widget keys from canonical form values.
        Uses setdefault so existing user edits in session state are preserved.

    save_new_ui_to_form(state) -> None
        Write editable UI values back to state["form"] and trigger
        lookup / recompute cascades.

    render_form_15cb(state) -> Dict[str, object]
        Render the full form and return updated state.
        Reads from state["form"] on every render; writes back after each widget.

Value-source priority (per spec):
    A. Identity/address : form > extracted > resolved > blank
    B. Currency/amount  : form > meta (Excel/manual) > extracted > blank
    C. Tax/computed     : recompute result only (shown read-only)

TDS / NON_TDS disabled rules:
    TDS   — 9A active, 9D disabled, deduction_date active, grossed_up editable
    NON_TDS — 9D active, 9A disabled, deduction_date disabled, grossed_up disabled
    Computed fields — always read-only
"""
from __future__ import annotations

import copy
import functools
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st

from modules.form15cb_constants import (
    CA_DEFAULTS,
    FIELD_MAX_LENGTH,
    HONORIFIC_M_S,
    IOR_WE_CODE,
    MODE_NON_TDS,
    MODE_TDS,
    PROPOSED_DATE_OFFSET_DAYS,
    SEC_REM_COVERED_DEFAULT,
)
from modules.invoice_calculator import recompute_invoice
from modules.logger import get_logger
from modules.master_lookups import (
    get_bank_options,
    load_nature_options,
    load_purpose_grouped,
    match_remitter,
    resolve_bank_code,
    resolve_country_code,
    resolve_country_name,
    resolve_dtaa,
    split_dtaa_article_text,
)
from modules.currency_mapping import (
    load_currency_exact_index,
    load_currency_short_index,
    resolve_currency_selection,
)
from modules.ui_reference_options import COUNTRIES, CURRENCIES, INDIAN_STATES_AND_UTS

logger = get_logger()

# Project root (UI.py lives at the project root level)
ROOT = Path(__file__).resolve().parent
LOOKUPS_DIR = ROOT / "lookups"

_CSELECT = "SELECT"


# ─────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_iso_date(value: str) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _yn_to_yes_no(value: str, *, default_yes: bool = False) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return "YES" if default_yes else "NO"
    return "YES" if text in {"Y", "YES", "1", "TRUE"} else "NO"


def _yes_no_to_yn(value: str) -> str:
    return "Y" if str(value or "").strip().upper() == "YES" else "N"


def _idx(options: List[str], value: str) -> int:
    try:
        return options.index(value)
    except ValueError:
        return 0


@functools.lru_cache(maxsize=1)
def _country_maps() -> Tuple[Dict[str, str], Dict[str, str]]:
    n2c: Dict[str, str] = {}
    c2n: Dict[str, str] = {}
    for name in COUNTRIES:
        code = str(resolve_country_code(name) or "").strip()
        if code:
            n2c[name] = code
            c2n.setdefault(code, name)
    return n2c, c2n


def _country_code(label: str) -> str:
    n2c, _ = _country_maps()
    clean = str(label or "").strip()
    return n2c.get(clean) or str(resolve_country_code(clean) or "")


def _country_label(code: str) -> str:
    _, c2n = _country_maps()
    code_clean = str(code or "").strip()
    if not code_clean:
        return _CSELECT
    if code_clean in c2n:
        return c2n[code_clean]
    resolved = str(resolve_country_name(code_clean) or "").strip().upper()
    if not resolved:
        return "OTHERS"
    for name in COUNTRIES:
        if name.strip().upper() == resolved:
            return name
    return "OTHERS"


@functools.lru_cache(maxsize=1)
def _currency_maps() -> Tuple[Dict[str, str], Dict[str, str]]:
    exact_index = load_currency_exact_index()
    s2c: Dict[str, str] = {}
    c2s: Dict[str, str] = {}
    for short in CURRENCIES:
        resolved = resolve_currency_selection(short, exact_index)
        code = str(resolved.get("code") or "").strip()
        if code:
            s2c[short] = code
            c2s.setdefault(code, short)
    for code, short in load_currency_short_index().items():
        cs = str(code or "").strip()
        ss = str(short or "").strip().upper()
        if cs and ss and ss in CURRENCIES:
            c2s.setdefault(cs, ss)
    return s2c, c2s


def _currency_code(short: str) -> str:
    s2c, _ = _currency_maps()
    clean = str(short or "").strip().upper()
    if not clean:
        return ""
    code = s2c.get(clean, "")
    if not code:
        resolved = resolve_currency_selection(clean, load_currency_exact_index())
        code = str(resolved.get("code") or "")
    return code


def _currency_short(code: str) -> str:
    _, c2s = _currency_maps()
    return str(c2s.get(str(code or "").strip()) or "")


@functools.lru_cache(maxsize=1)
def _bank_maps() -> Tuple[List[str], Dict[str, str], Dict[str, str]]:
    rows = get_bank_options()
    n2c: Dict[str, str] = {}
    c2n: Dict[str, str] = {}
    for name, code in rows:
        n = str(name or "").strip()
        c = str(code or "").strip()
        if n and c:
            n2c[n] = c
            c2n.setdefault(c, n)
    return sorted(n2c.keys()), n2c, c2n


@functools.lru_cache(maxsize=1)
def _state_code_map() -> Dict[str, str]:
    path = LOOKUPS_DIR / "state_codes.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf8"))
    except Exception:
        return {}
    return {str(k).strip().upper(): str(v).strip() for k, v in raw.items() if k and v}


def _purpose_group_for_code(purpose_grouped: Dict, code: str) -> str:
    c = str(code or "").strip().upper()
    if not c:
        return ""
    for grp, rows in purpose_grouped.items():
        for row in rows:
            if str(row.get("purpose_code") or "").strip().upper() == c:
                return grp
    return ""


def _inject_styles() -> None:
    st.markdown(
        """
        <style>
        .mid-label{display:flex;align-items:center;height:38px;font-weight:600;font-size:1rem;}
        .flabel{padding-top:8px;font-size:0.98rem;}
        .flabel-ind1{padding-top:8px;font-size:0.98rem;padding-left:18px;}
        .flabel-ind2{padding-top:8px;font-size:0.98rem;padding-left:36px;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _lbl(text: str, *, indent: int = 0) -> None:
    cls = ["flabel", "flabel-ind1", "flabel-ind2"][min(indent, 2)]
    st.markdown(f"<div class='{cls}'>{text}</div>", unsafe_allow_html=True)


def _preview_computed(state: Dict[str, Any]) -> Dict[str, str]:
    """Run recompute on a deep copy and return the resulting form dict for display."""
    try:
        return recompute_invoice(copy.deepcopy(state)).get("form", {})
    except Exception:
        logger.exception("ui_preview_recompute_failed invoice_id=%s",
                         state.get("meta", {}).get("invoice_id", ""))
        return state.get("form", {})


def check_field_length_warnings(form: Dict[str, str]) -> List[str]:
    warnings: List[str] = []
    for field, max_len in FIELD_MAX_LENGTH.items():
        val = str(form.get(field) or "")
        if len(val) > max_len:
            warnings.append(
                f"'{field}' is {len(val)} chars (max {max_len}) — will be trimmed in XML."
            )
    return warnings


# ─────────────────────────────────────────────────────────────────────────────
# Lookup cascade helpers
# ─────────────────────────────────────────────────────────────────────────────

def _apply_remitter_lookup(state: Dict[str, Any], remitter_name: str) -> None:
    """remitter_name → PAN / bank / branch / BSR lookup cascade (4.3 → 4.4/4.22/4.24/4.25)."""
    form = state["form"]
    resolved = state["resolved"]
    invoice_id = str(state.get("meta", {}).get("invoice_id") or "")

    # Skip if name unchanged since last lookup
    if remitter_name == str(form.get("_ui_last_remitter_lookup") or ""):
        return

    form["_ui_last_remitter_lookup"] = remitter_name
    rec = match_remitter(remitter_name)
    if rec:
        resolved["pan"] = rec.get("pan", "")
        resolved["bank_name"] = rec.get("bank_name", "")
        resolved["branch"] = rec.get("branch", "")
        resolved["bsr"] = rec.get("bsr", "")
        bank_code = resolve_bank_code(rec.get("bank_name", ""))
        resolved["bank_code"] = bank_code
        form["RemitterPAN"] = rec.get("pan", "")
        form["NameBankDisplay"] = rec.get("bank_name", "")
        form["NameBankCode"] = str(bank_code or "")
        form["BranchName"] = rec.get("branch", "")
        form["BsrCode"] = rec.get("bsr", "")
        form["_lock_pan_bank_branch_bsr"] = "1"
        logger.info(
            "ui_remitter_match invoice_id=%s name=%s pan=%s bank=%s",
            invoice_id, remitter_name, rec.get("pan"), rec.get("bank_name"),
        )
    else:
        form["_lock_pan_bank_branch_bsr"] = "0"
        logger.warning(
            "ui_remitter_not_matched invoice_id=%s name=%s", invoice_id, remitter_name
        )


def _apply_country_dtaa_lookup(
    state: Dict[str, Any], country_code: str, country_label: str
) -> None:
    """country → DTAA / article / rate lookup cascade (4.14/4.16 → 4.38/4.39/4.44)."""
    form = state["form"]
    resolved = state["resolved"]

    if country_code == str(form.get("_ui_last_country_for_dtaa") or ""):
        return

    form["_ui_last_country_for_dtaa"] = country_code
    country_name = str(country_label or "").strip()
    if not country_name or country_name in (_CSELECT, "OTHERS"):
        country_name = str(resolve_country_name(country_code) or "")

    dtaa = resolve_dtaa(country_name) if country_name else None
    if not dtaa:
        logger.warning(
            "ui_dtaa_lookup_missing invoice_id=%s country=%s",
            state.get("meta", {}).get("invoice_id", ""), country_code,
        )
        return

    dtaa_text = str(dtaa.get("dtaa_applicable") or "")
    dtaa_no_art, dtaa_with_art = split_dtaa_article_text(dtaa_text)
    if dtaa_no_art:
        form["RelevantDtaa"] = dtaa_no_art
    if dtaa_with_art:
        form["RelevantArtDtaa"] = dtaa_with_art
        form.setdefault("ArtDtaa", dtaa_with_art)

    rate_raw = str(dtaa.get("percentage") or "")
    if "i.t act" in rate_raw.lower():
        form["dtaa_mode"] = "it_act"
        form["RateTdsADtaa"] = ""
        resolved["dtaa_rate_percent"] = ""
    else:
        try:
            rate_pct = str(float(rate_raw) * 100).rstrip("0").rstrip(".")
            form["dtaa_mode"] = "dtaa_rate"
            form["RateTdsADtaa"] = rate_pct
            resolved["dtaa_rate_percent"] = rate_pct
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 1) init_new_ui_from_state
# ─────────────────────────────────────────────────────────────────────────────

def init_new_ui_from_state(state: Dict[str, Any]) -> None:
    """
    Seed st.session_state widget keys from canonical form values.

    Uses setdefault — existing user edits in session state are preserved.
    Call once right after Process Invoice completes, before the first render.

    Priority (per spec):
        A. Identity/address : form > extracted > resolved > blank
        B. Currency/amount  : form > meta (Excel) > extracted > blank
        C. Tax/computed     : not set here — shown read-only from recompute
    """
    form = state.get("form", {})
    meta = state.get("meta", {})
    extracted = state.get("extracted", {})
    invoice_id = str(meta.get("invoice_id") or "")
    mode = str(meta.get("mode") or MODE_TDS)
    is_tds = (mode == MODE_TDS)

    def _f(key: str, fallback: str = "") -> str:
        return str(form.get(key) or fallback).strip()

    def _e(key: str, fallback: str = "") -> str:
        return str(extracted.get(key) or fallback).strip()

    px = f"{invoice_id}_"
    ss = st.session_state

    # ── Header ──────────────────────────────────────────────────────────────
    ss.setdefault(f"{px}h_remitter", _f("NameRemitterInput", _e("remitter_name")))
    ss.setdefault(f"{px}h_pan", _f("RemitterPAN"))
    ss.setdefault(f"{px}h_benef", _f("NameRemitteeInput", _e("beneficiary_name")))

    # ── Section A ───────────────────────────────────────────────────────────
    ss.setdefault(f"{px}a_benef", _f("NameRemitteeInput", _e("beneficiary_name")))
    ss.setdefault(f"{px}a_flat", _f("RemitteeFlatDoorBuilding"))
    ss.setdefault(f"{px}a_premises", _f("RemitteePremisesBuildingVillage"))
    ss.setdefault(f"{px}a_road", _f("RemitteeRoadStreet"))
    ss.setdefault(f"{px}a_area", _f("RemitteeAreaLocality"))
    ss.setdefault(f"{px}a_city", _f("RemitteeTownCityDistrict"))
    ss.setdefault(f"{px}a_state", _country_label(_f("RemitteeCountryCode")) or "OUTSIDE INDIA")
    ss.setdefault(f"{px}a_zip", _f("RemitteeZipCode", "999999"))

    # ── Section B ───────────────────────────────────────────────────────────
    # Currency: form > meta (Excel) > extracted
    curr_short = (
        _currency_short(_f("CurrencySecbCode"))
        or str(meta.get("source_currency_short") or "").strip().upper()
        or _e("currency_short")
    )
    ss.setdefault(f"{px}b_currency_short_seed", curr_short)
    ss.setdefault(f"{px}b_amt_fcy", _f("AmtPayForgnRem"))
    ss.setdefault(f"{px}b_amt_inr", _f("AmtPayIndRem"))
    ss.setdefault(f"{px}b_branch", _f("BranchName"))
    ss.setdefault(f"{px}b_bsr", _f("BsrCode"))

    # Nature / Purpose
    ss.setdefault(f"{px}b_nature_code", _f("NatureRemCategory"))
    ss.setdefault(f"{px}b_purpose_group", _f("_purpose_group"))
    ss.setdefault(f"{px}b_purpose_code", _f("_purpose_code"))

    # Gross-up
    gross_yn = bool(meta.get("is_gross_up")) or _f("TaxPayGrossSecb") == "Y"
    ss.setdefault(f"{px}b_gross_up", "YES" if gross_yn else "NO")

    # ── Section 9 ───────────────────────────────────────────────────────────
    # 9A
    rem_for_roy = _f("RemForRoyFlg", "Y" if is_tds else "N")
    ss.setdefault(f"{px}9a_applicable", _yn_to_yes_no(rem_for_roy, default_yes=is_tds))
    ss.setdefault(f"{px}9a_article", _f("ArtDtaa", _f("RelevantArtDtaa")))
    ss.setdefault(f"{px}9a_rate", _f("RateTdsADtaa"))

    # 9D
    other_yn = _yn_to_yes_no(_f("OtherRemDtaa", "Y" if not is_tds else "N"))
    if is_tds:
        other_yn = "NO"
    ss.setdefault(f"{px}9d_applicable", other_yn)
    ss.setdefault(f"{px}9d_nature", _f("NatureRemDtaa"))
    ss.setdefault(f"{px}9d_reasons", _f("RelArtDetlDDtaa"))

    # ── Section 13 ──────────────────────────────────────────────────────────
    dedn_d = _parse_iso_date(_f("DednDateTds"))
    ss.setdefault(f"{px}13_dedn_date", dedn_d if dedn_d else date.today())

    # ── Accountant details ──────────────────────────────────────────────────
    ss.setdefault(f"{px}acct_name",       _f("NameAcctnt") or CA_DEFAULTS.get("NameAcctnt", ""))
    ss.setdefault(f"{px}acct_firm",       _f("NameFirmAcctnt") or CA_DEFAULTS.get("NameFirmAcctnt", ""))
    ss.setdefault(f"{px}acct_flat",       _f("AcctntFlatDoorBuilding") or CA_DEFAULTS.get("AcctntFlatDoorBuilding", ""))
    ss.setdefault(f"{px}acct_premises",   _f("PremisesBuildingVillage") or CA_DEFAULTS.get("PremisesBuildingVillage", ""))
    ss.setdefault(f"{px}acct_road",       _f("AcctntRoadStreet") or CA_DEFAULTS.get("AcctntRoadStreet", ""))
    ss.setdefault(f"{px}acct_area",       _f("AcctntAreaLocality") or CA_DEFAULTS.get("AcctntAreaLocality", ""))
    ss.setdefault(f"{px}acct_city",       _f("AcctntTownCityDistrict") or CA_DEFAULTS.get("AcctntTownCityDistrict", ""))
    ss.setdefault(f"{px}acct_pin",        _f("AcctntPincode") or CA_DEFAULTS.get("AcctntPincode", ""))
    ss.setdefault(f"{px}acct_membership", _f("MembershipNumber") or CA_DEFAULTS.get("MembershipNumber", ""))


# ─────────────────────────────────────────────────────────────────────────────
# 2) save_new_ui_to_form
# ─────────────────────────────────────────────────────────────────────────────

def save_new_ui_to_form(state: Dict[str, Any]) -> None:
    """
    Write editable UI widget values from st.session_state back to state["form"].
    Trigger remitter lookup and country→DTAA lookup where values have changed.
    Call after all widgets have been rendered (or after form submit).
    """
    form = state.setdefault("form", {})
    meta = state.setdefault("meta", {})
    resolved = state.setdefault("resolved", {})
    invoice_id = str(meta.get("invoice_id") or "")
    mode = str(meta.get("mode") or MODE_TDS)
    is_tds = (mode == MODE_TDS)

    px = f"{invoice_id}_"
    ss = st.session_state

    def _ss(key: str, default: str = "") -> str:
        return str(ss.get(f"{px}{key}", default) or default).strip()

    # ── Header ──────────────────────────────────────────────────────────────
    form["NameRemitterInput"] = _ss("h_remitter")
    form["NameRemitter"]      = form["NameRemitterInput"]
    form["RemitterPAN"]       = _ss("h_pan").upper()
    form["NameRemitteeInput"] = _ss("h_benef") or _ss("a_benef")
    form["NameRemittee"]      = form["NameRemitteeInput"]

    # Remitter → PAN/bank/branch/BSR cascade
    if form["NameRemitterInput"]:
        _apply_remitter_lookup(state, form["NameRemitterInput"])

    # ── Section A ───────────────────────────────────────────────────────────
    form["NameRemitteeInput"]               = _ss("a_benef") or form["NameRemitteeInput"]
    form["NameRemittee"]                    = form["NameRemitteeInput"]
    form["RemitteeFlatDoorBuilding"]        = _ss("a_flat")
    form["RemitteePremisesBuildingVillage"] = _ss("a_premises")
    form["RemitteeRoadStreet"]              = _ss("a_road")
    form["RemitteeAreaLocality"]            = _ss("a_area")
    form["RemitteeTownCityDistrict"]        = _ss("a_city")
    form["RemitteeZipCode"]                 = _ss("a_zip", "999999")

    # ── Section B ───────────────────────────────────────────────────────────
    curr_short = _ss("b_currency_short_seed")
    if curr_short:
        code_c = _currency_code(curr_short)
        if code_c:
            form["CurrencySecbCode"] = code_c

    form["AmtPayForgnRem"] = _ss("b_amt_fcy")
    form["AmtPayIndRem"]   = _ss("b_amt_inr")
    form["BranchName"]     = _ss("b_branch")
    form["BsrCode"]        = _ss("b_bsr")

    # Nature
    form["NatureRemCategory"] = _ss("b_nature_code")

    # Purpose
    purpose_code  = _ss("b_purpose_code").upper()
    purpose_group = _ss("b_purpose_group")
    if purpose_code:
        form["_purpose_code"]  = purpose_code
        form["_purpose_group"] = purpose_group
        pg = load_purpose_grouped()
        for grp, rows in pg.items():
            for row in rows:
                if str(row.get("purpose_code") or "").strip().upper() == purpose_code:
                    gr = str(row.get("gr_no") or "00").strip()
                    gr_norm = str(int(gr)) if gr.isdigit() else gr
                    form["RevPurCategory"] = f"RB-{gr_norm}.1"
                    form["RevPurCode"]     = f"RB-{gr_norm}.1-{purpose_code}"
                    break

    # Gross-up
    gross_yes = (_ss("b_gross_up") == "YES") and is_tds
    form["TaxPayGrossSecb"] = "Y" if gross_yes else "N"
    meta["is_gross_up"]     = gross_yes

    # ── Section 9 ───────────────────────────────────────────────────────────
    # 9A
    if is_tds:
        form["RemForRoyFlg"] = _yes_no_to_yn(_ss("9a_applicable", "YES"))
        if str(form.get("RemForRoyFlg") or "N").upper() == "Y":
            form["ArtDtaa"]      = _ss("9a_article")
            form["RateTdsADtaa"] = _ss("9a_rate")
            if form["RateTdsADtaa"]:
                resolved["dtaa_rate_percent"] = form["RateTdsADtaa"]
    else:
        form["RemForRoyFlg"] = "N"

    # 9D
    if not is_tds:
        d_app = (_ss("9d_applicable") == "YES")
        form["OtherRemDtaa"] = "Y" if d_app else "N"
        if d_app:
            form["NatureRemDtaa"] = _ss("9d_nature")
            d_taxable = ss.get(f"{px}9d_taxable") == "YES"
            if not d_taxable:
                form["RelArtDetlDDtaa"] = _ss("9d_reasons")
    else:
        form["OtherRemDtaa"]   = "N"
        form["RelArtDetlDDtaa"] = "NOT APPLICABLE"

    # ── Section 13 ──────────────────────────────────────────────────────────
    if is_tds:
        dedn_d = ss.get(f"{px}13_dedn_date")
        if isinstance(dedn_d, date):
            form["DednDateTds"] = dedn_d.isoformat()

    # ── Accountant details ──────────────────────────────────────────────────
    form["NameAcctnt"]            = _ss("acct_name")
    form["NameFirmAcctnt"]        = _ss("acct_firm")
    form["AcctntFlatDoorBuilding"]= _ss("acct_flat")
    form["PremisesBuildingVillage"]= _ss("acct_premises")
    form["AcctntRoadStreet"]      = _ss("acct_road")
    form["AcctntAreaLocality"]    = _ss("acct_area")
    form["AcctntTownCityDistrict"]= _ss("acct_city")
    form["AcctntPincode"]         = _ss("acct_pin")
    form["MembershipNumber"]      = _ss("acct_membership")


# ─────────────────────────────────────────────────────────────────────────────
# 3) render_form_15cb  —  main rendering function
# ─────────────────────────────────────────────────────────────────────────────

def render_form_15cb(
    state: Dict[str, Any],
    *,
    show_header: bool = True,
) -> Dict[str, Any]:
    """
    Render the Form 15CB review/edit form and return the updated state.

    Pattern: read from state["form"] → render widgets → write widget
    return-values back to state["form"] → trigger cascades.

    Args:
        state:       Canonical invoice state dict.
        show_header: Whether to show the "FORM NO. 15CB" header.

    Returns:
        Updated state dict (same object, mutated in-place).
    """
    meta      = state.setdefault("meta", {})
    extracted = state.setdefault("extracted", {})
    form      = state.setdefault("form", {})
    resolved  = state.setdefault("resolved", {})

    invoice_id = str(meta.get("invoice_id") or "")
    mode       = str(meta.get("mode") or MODE_TDS)
    is_tds     = (mode == MODE_TDS)

    _inject_styles()

    # Fixed header constants (always set)
    form["IorWe"]                = IOR_WE_CODE
    form["RemitterHonorific"]    = HONORIFIC_M_S
    form["BeneficiaryHonorific"] = HONORIFIC_M_S

    if show_header:
        st.markdown("### FORM NO. 15CB")
        st.caption("Certificate of an accountant")

    if meta.get("extraction_quality") == "failed":
        st.error(
            "Automatic extraction failed for this invoice. "
            "Please review and fill fields manually."
        )

    for w in check_field_length_warnings(form):
        st.warning(w)

    ratio = [2, 3]

    # ── Precompute drop-down data (cached) ──────────────────────────────────
    country_options = [_CSELECT] + list(COUNTRIES)
    nature_rows     = [n for n in load_nature_options() if str(n.get("code") or "") != "-1"]
    nature_labels   = [_CSELECT] + [f"{n['code']} - {n['label']}" for n in nature_rows]
    bank_names, bank_n2c, bank_c2n = _bank_maps()
    bank_options    = [_CSELECT] + bank_names + ["Other Bank"]
    purpose_grouped = load_purpose_grouped()
    purpose_groups  = [_CSELECT] + sorted(purpose_grouped.keys())

    # ── Preview (for computed read-only fields) ─────────────────────────────
    preview = _preview_computed(state)

    # =========================================================================
    # HEADER — remitter / PAN / beneficiary
    # =========================================================================

    # 4.1 salutation — fixed "We"
    # 4.3 remitter_name_input → NameRemitterInput
    remitter_default = str(form.get("NameRemitterInput") or extracted.get("remitter_name") or "")
    benef_default    = str(form.get("NameRemitteeInput") or extracted.get("beneficiary_name") or "")
    pan_default      = str(form.get("RemitterPAN") or "")

    h1c1, h1c2, h1c3, h1c4 = st.columns([0.8, 4.8, 0.8, 3.2])
    with h1c1:
        # 4.1 salutation — disabled
        st.selectbox("I/We", ["I", "We"], index=1,
                     key=f"{invoice_id}_h_iorwe", disabled=True,
                     label_visibility="collapsed")
    with h1c2:
        st.markdown(
            "<div class='mid-label'>* have examined the agreement (wherever applicable) between</div>",
            unsafe_allow_html=True,
        )
    with h1c3:
        # 4.2 remitter_prefix — disabled
        st.selectbox("Honorific", ["Mr", "Ms", "M/s"], index=2,
                     key=f"{invoice_id}_h_rem_honor", disabled=True,
                     label_visibility="collapsed")
    with h1c4:
        # 4.3 remitter_name_input → NameRemitterInput — editable both modes
        remitter_val = st.text_input(
            "Name of Remitter",
            value=remitter_default,
            key=f"{invoice_id}_h_remitter",
            placeholder="Name of Remitter *",
            label_visibility="collapsed",
        ).strip()
        form["NameRemitterInput"] = remitter_val
        form["NameRemitter"]      = remitter_val

    # Trigger remitter → PAN / bank / branch / BSR lookup
    if remitter_val:
        _apply_remitter_lookup(state, remitter_val)
        pan_default = str(form.get("RemitterPAN") or "")

    h2c1, h2c2, h2c3, h2c4, h2c5 = st.columns([1.2, 2.1, 0.6, 0.8, 3.4])
    with h2c1:
        st.markdown("<div class='mid-label'>with PAN/TAN</div>", unsafe_allow_html=True)
    with h2c2:
        # 4.4 pan_tan_display → RemitterPAN — editable both modes
        pan_val = st.text_input(
            "PAN/TAN",
            value=pan_default,
            key=f"{invoice_id}_h_pan",
            label_visibility="collapsed",
        ).strip().upper()
        form["RemitterPAN"] = pan_val
    with h2c3:
        st.markdown("<div class='mid-label'>* and</div>", unsafe_allow_html=True)
    with h2c4:
        # 4.5 beneficiary_prefix — disabled
        st.selectbox("Beneficiary honorific", ["Mr", "Ms", "M/s"], index=2,
                     key=f"{invoice_id}_h_benef_honor", disabled=True,
                     label_visibility="collapsed")
    with h2c5:
        # 4.6 beneficiary_name_header → NameRemitteeInput — editable both modes
        benef_header_val = st.text_input(
            "Name of Beneficiary",
            value=benef_default,
            key=f"{invoice_id}_h_benef",
            placeholder="Name of Beneficiary *",
            label_visibility="collapsed",
        ).strip()
        form["NameRemitteeInput"] = benef_header_val
        form["NameRemittee"]      = benef_header_val

    st.markdown(
        "<div style='font-weight:600;font-size:0.97rem;margin-top:6px'>"
        "* requiring the above remittance as well as the relevant documents and books of account "
        "required for ascertaining the nature of remittance and for determining the rate of deduction "
        "of tax at source as per provisions of Chapter XVII-B.&nbsp; We hereby certify the following."
        "</div>",
        unsafe_allow_html=True,
    )

    st.divider()

    # =========================================================================
    # SECTION A — Beneficiary name and address
    # =========================================================================
    st.subheader("A  Name and address of the beneficiary of the remittance")

    # 4.7 beneficiary_name → NameRemitteeInput (mirrors header)
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("Name of the Beneficiary of the remittance")
    with rc:
        benef_a_val = st.text_input(
            "Beneficiary name (A)",
            value=str(form.get("NameRemitteeInput") or ""),
            key=f"{invoice_id}_a_benef",
            label_visibility="collapsed",
        ).strip()
        form["NameRemitteeInput"] = benef_a_val
        form["NameRemittee"]      = benef_a_val

    # 4.8–4.12 address fields — editable both modes
    _addr_fields = [
        ("Flat / Door / Building",                "RemitteeFlatDoorBuilding",        "flat"),
        ("Name of premises / Building / Village", "RemitteePremisesBuildingVillage", "premises"),
        ("Road / Street",                         "RemitteeRoadStreet",              "road"),
        ("Area / Locality",                       "RemitteeAreaLocality",            "area"),
        ("Town / City / District",                "RemitteeTownCityDistrict",        "city"),
    ]
    for lbl_text, fkey, sfx in _addr_fields:
        lc, rc = st.columns(ratio)
        with lc:
            _lbl(lbl_text)
        with rc:
            form[fkey] = st.text_input(
                lbl_text,
                value=str(form.get(fkey) or ""),
                key=f"{invoice_id}_a_{sfx}",
                label_visibility="collapsed",
            ).strip()

    # 4.13 state → RemitteeState — editable both modes
    _state_opts = [_CSELECT, "OUTSIDE INDIA"] + list(INDIAN_STATES_AND_UTS) + ["OTHER / MANUAL"]
    _cur_rem_state = str(form.get("RemitteeState") or "OUTSIDE INDIA")
    _state_disp = "OUTSIDE INDIA"
    for _opt in _state_opts[1:]:
        if _opt.upper() == _cur_rem_state.upper():
            _state_disp = _opt
            break
    if _state_disp == _CSELECT and _cur_rem_state:
        _state_disp = "OTHER / MANUAL"

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("State")
    with rc:
        sel_state = st.selectbox(
            "State",
            _state_opts,
            index=_idx(_state_opts, _state_disp),
            key=f"{invoice_id}_a_state",
            label_visibility="collapsed",
        )
        if sel_state == "OTHER / MANUAL":
            form["RemitteeState"] = st.text_input(
                "State (manual)",
                value=_cur_rem_state,
                key=f"{invoice_id}_a_state_manual",
                label_visibility="collapsed",
            ).strip()
        elif sel_state != _CSELECT:
            form["RemitteeState"] = sel_state

    # 4.14 country_addr → RemitteeCountryCode — editable both modes
    # Changing this also triggers DTAA lookup cascade
    _cur_ctry_a_code  = str(form.get("RemitteeCountryCode") or form.get("CountryRemMadeSecb") or "")
    _cur_ctry_a_label = _country_label(_cur_ctry_a_code)

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("Country")
    with rc:
        sel_ctry_a = st.selectbox(
            "Country (A)",
            country_options,
            index=_idx(country_options, _cur_ctry_a_label),
            key=f"{invoice_id}_a_country",
            label_visibility="collapsed",
        )
        resolved_code_a = _cur_ctry_a_code
        if sel_ctry_a == "OTHERS":
            # 4.14 — other country input
            _other_a = st.text_input(
                "Other country (A)",
                value=str(form.get("_ui_other_country_a") or ""),
                key=f"{invoice_id}_a_country_other",
                label_visibility="collapsed",
            ).strip().upper()
            form["_ui_other_country_a"] = _other_a
            if _other_a:
                resolved_code_a = _country_code(_other_a) or (_other_a if _other_a.isdigit() else "")
        elif sel_ctry_a != _CSELECT:
            resolved_code_a = _country_code(sel_ctry_a)
            form["_ui_other_country_a"] = ""

        if resolved_code_a:
            form["RemitteeCountryCode"] = resolved_code_a
            form["CountryRemMadeSecb"]  = resolved_code_a
            # Trigger DTAA cascade when country changes
            _apply_country_dtaa_lookup(state, resolved_code_a, sel_ctry_a)

    # 4.15 zip → RemitteeZipCode
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("ZIP Code")
    with rc:
        form["RemitteeZipCode"] = st.text_input(
            "ZIP Code",
            value=str(form.get("RemitteeZipCode") or ""),
            key=f"{invoice_id}_a_zip",
            label_visibility="collapsed",
        ).strip()

    st.divider()

    # =========================================================================
    # SECTION B — Remittance details
    # =========================================================================
    st.subheader("B  Remittance Details")
    st.markdown("**1. Country to which remittance is made**")

    # 4.16 remit_country → CountryRemMadeSecb — editable both modes
    _cur_ctry_b_code  = str(form.get("CountryRemMadeSecb") or form.get("RemitteeCountryCode") or "")
    _cur_ctry_b_label = _country_label(_cur_ctry_b_code)

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("Country", indent=1)
    with rc:
        sel_ctry_b = st.selectbox(
            "Country to which remittance is made",
            country_options,
            index=_idx(country_options, _cur_ctry_b_label),
            key=f"{invoice_id}_b_country",
            label_visibility="collapsed",
        )
        resolved_code_b = _cur_ctry_b_code
        if sel_ctry_b == "OTHERS":
            # 4.17 remit_country_other — only when OTHERS
            _other_b = st.text_input(
                "Other remittance country",
                value=str(form.get("_ui_other_country_b") or ""),
                key=f"{invoice_id}_b_country_other",
                label_visibility="collapsed",
            ).strip().upper()
            form["_ui_other_country_b"] = _other_b
            if _other_b:
                resolved_code_b = _country_code(_other_b) or (_other_b if _other_b.isdigit() else "")
        elif sel_ctry_b != _CSELECT:
            resolved_code_b = _country_code(sel_ctry_b)
            form["_ui_other_country_b"] = ""

        if resolved_code_b:
            form["CountryRemMadeSecb"] = resolved_code_b
            form["RemitteeCountryCode"] = resolved_code_b
            # Trigger DTAA cascade when country changes
            _apply_country_dtaa_lookup(state, resolved_code_b, sel_ctry_b)

    # 4.18 currency → CurrencySecbCode — editable both modes
    _curr_options = [_CSELECT] + list(CURRENCIES)
    _cur_curr_code = str(form.get("CurrencySecbCode") or "")
    if not _cur_curr_code:
        _seed = (
            str(form.get("_ui_currency_short") or "")
            or str(meta.get("source_currency_short") or "").strip().upper()
            or str(extracted.get("currency_short") or "").strip().upper()
        )
        if _seed:
            _c = _currency_code(_seed)
            if _c:
                _cur_curr_code = _c
                form["CurrencySecbCode"] = _c
                form["_ui_currency_short"] = _seed
    _curr_short_disp = _currency_short(_cur_curr_code) or str(form.get("_ui_currency_short") or _CSELECT)
    if _curr_short_disp not in _curr_options:
        _curr_short_disp = _CSELECT

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("Currency", indent=1)
    with rc:
        sel_curr = st.selectbox(
            "Currency",
            _curr_options,
            index=_idx(_curr_options, _curr_short_disp),
            key=f"{invoice_id}_b_currency",
            label_visibility="collapsed",
        )
        if sel_curr == "OTHERS":
            # 4.19 currency_other — only when OTHERS
            _other_curr = st.text_input(
                "Other currency",
                value=str(form.get("_ui_currency_other") or ""),
                key=f"{invoice_id}_b_currency_other",
                label_visibility="collapsed",
            ).strip().upper()
            form["_ui_currency_other"] = _other_curr
            if _other_curr:
                _rc = _currency_code(_other_curr)
                if _rc:
                    form["CurrencySecbCode"]    = _rc
                    form["_ui_currency_short"]  = _other_curr
        elif sel_curr != _CSELECT:
            _rc = _currency_code(sel_curr)
            if _rc:
                form["CurrencySecbCode"]   = _rc
                form["_ui_currency_short"] = sel_curr
            form["_ui_currency_other"] = ""

    _lbl("2. Amount payable")

    # 4.20 amt_fc → AmtPayForgnRem — editable both modes
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("In foreign currency", indent=1)
    with rc:
        form["AmtPayForgnRem"] = st.text_input(
            "In foreign currency",
            value=str(form.get("AmtPayForgnRem") or ""),
            key=f"{invoice_id}_b_amt_fcy",
            label_visibility="collapsed",
        ).strip()

    # 4.21 amt_inr → AmtPayIndRem — editable both modes
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("In Indian Rs", indent=1)
    with rc:
        form["AmtPayIndRem"] = st.text_input(
            "In Indian Rs",
            value=str(form.get("AmtPayIndRem") or ""),
            key=f"{invoice_id}_b_amt_inr",
            label_visibility="collapsed",
        ).strip()

    # 4.22 bank_name → NameBankCode / NameBankDisplay — editable both modes
    _cur_bank_code    = str(form.get("NameBankCode") or "")
    _cur_bank_display = str(form.get("NameBankDisplay") or "")
    _bank_disp_default = bank_c2n.get(_cur_bank_code, "Other Bank" if _cur_bank_display else _CSELECT)
    if _bank_disp_default not in bank_options:
        _bank_disp_default = "Other Bank" if _cur_bank_display else _CSELECT

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("3. Name of the bank")
    with rc:
        sel_bank = st.selectbox(
            "Name of the bank",
            bank_options,
            index=_idx(bank_options, _bank_disp_default),
            key=f"{invoice_id}_b_bank",
            label_visibility="collapsed",
        )
        if sel_bank == "Other Bank":
            # 4.23 bank_name_other — only when Other Bank
            _manual_bank = st.text_input(
                "Other bank name",
                value=_cur_bank_display,
                key=f"{invoice_id}_b_bank_other",
                label_visibility="collapsed",
            ).strip()
            form["NameBankDisplay"] = _manual_bank
            form["NameBankCode"]    = str(resolve_bank_code(_manual_bank) or "") if _manual_bank else ""
        elif sel_bank != _CSELECT:
            form["NameBankDisplay"] = sel_bank
            form["NameBankCode"]    = str(bank_n2c.get(sel_bank, ""))

    # 4.24 branch → BranchName — editable both modes
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("Branch of the bank", indent=1)
    with rc:
        form["BranchName"] = st.text_input(
            "Branch of the bank",
            value=str(form.get("BranchName") or ""),
            key=f"{invoice_id}_b_branch",
            label_visibility="collapsed",
        ).strip()

    # 4.25 bs_code → BsrCode — editable both modes
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("4. BSR Code of the bank branch (7 digit)")
    with rc:
        form["BsrCode"] = st.text_input(
            "BSR Code",
            value=str(form.get("BsrCode") or ""),
            key=f"{invoice_id}_b_bsr",
            label_visibility="collapsed",
        ).strip()

    # 4.26 prop_date → PropDateRem — READ-ONLY (always auto-computed)
    _prop_d = _parse_iso_date(str(form.get("PropDateRem") or ""))
    if not _prop_d:
        _prop_d = date.today() + timedelta(days=PROPOSED_DATE_OFFSET_DAYS)
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("5. Proposed date of remittance")
    with rc:
        _prop_date_val = st.date_input(
            "Proposed date of remittance",
            value=_prop_d,
            key=f"{invoice_id}_b_prop_date",
            disabled=True,
            label_visibility="collapsed",
            format="DD/MM/YYYY",
        )
        form["PropDateRem"] = _prop_date_val.strftime("%d/%m/%Y")

    # 4.27 nature → NatureRemCategory — editable both modes
    _cur_nature_code = str(form.get("NatureRemCategory") or "")
    if not _cur_nature_code and extracted.get("nature_of_remittance"):
        _ext_n = str(extracted["nature_of_remittance"]).strip().upper()
        for _row in nature_rows:
            if str(_row.get("label") or "").strip().upper() == _ext_n:
                _cur_nature_code = str(_row.get("code") or "")
                form["NatureRemCategory"] = _cur_nature_code
                break
    _cur_nature_label = _CSELECT
    for _row in nature_rows:
        if str(_row.get("code") or "") == _cur_nature_code:
            _cur_nature_label = f"{_row['code']} - {_row['label']}"
            break

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("6. Nature of remittance as per agreement / document")
    with rc:
        sel_nature = st.selectbox(
            "Nature of remittance",
            nature_labels,
            index=_idx(nature_labels, _cur_nature_label),
            key=f"{invoice_id}_b_nature",
            label_visibility="collapsed",
        )
        if sel_nature != _CSELECT:
            form["NatureRemCategory"] = sel_nature.split(" - ", 1)[0].strip()

    # 4.28 purpose_cat → RevPurCategory | 4.29 purpose_code → RevPurCode
    _cur_pc = str(form.get("_purpose_code") or "").strip().upper()
    if not _cur_pc:
        _rv = str(form.get("RevPurCode") or "")
        if "-" in _rv:
            _cur_pc = _rv.rsplit("-", 1)[-1].strip().upper()
    if not _cur_pc:
        _cur_pc = str(extracted.get("purpose_code") or "").strip().upper()

    _cur_pg = str(form.get("_purpose_group") or "")
    if not _cur_pg and _cur_pc:
        _cur_pg = _purpose_group_for_code(purpose_grouped, _cur_pc)
    if _cur_pg not in purpose_grouped:
        _cur_pg = ""

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("7. Please furnish the relevant purpose code as per RBI")
    with rc:
        sel_pg = st.selectbox(
            "Purpose category",
            purpose_groups,
            index=_idx(purpose_groups, _cur_pg if _cur_pg else _CSELECT),
            key=f"{invoice_id}_b_purpose_group",
            label_visibility="collapsed",
        )
        form["_purpose_group"] = sel_pg if sel_pg != _CSELECT else ""

        _grp_rows   = purpose_grouped.get(sel_pg if sel_pg != _CSELECT else "", [])
        _code_labels = [_CSELECT] + [f"{r['purpose_code']} - {r['description']}" for r in _grp_rows]
        _c2lbl = {
            str(r.get("purpose_code") or "").strip().upper(): f"{r['purpose_code']} - {r['description']}"
            for r in _grp_rows
        }
        sel_code_label = st.selectbox(
            "Specific purpose code",
            _code_labels,
            index=_idx(_code_labels, _c2lbl.get(_cur_pc, _CSELECT)),
            key=f"{invoice_id}_b_purpose_code",
            label_visibility="collapsed",
        )
        if sel_code_label != _CSELECT:
            _pcode = sel_code_label.split(" - ", 1)[0].strip().upper()
            form["_purpose_code"] = _pcode
            _sel_row = next(
                (r for r in _grp_rows if str(r.get("purpose_code") or "").strip().upper() == _pcode),
                None,
            )
            if _sel_row:
                _gr = str(_sel_row.get("gr_no") or "00").strip()
                _gr_norm = str(int(_gr)) if _gr.isdigit() else _gr
                form["RevPurCategory"] = f"RB-{_gr_norm}.1"
                form["RevPurCode"]     = f"RB-{_gr_norm}.1-{_pcode}"
        else:
            form["_purpose_code"] = ""

    # 4.30 grossed_up → TaxPayGrossSecb / meta["is_gross_up"]
    # SPEC: TDS = editable | NON_TDS = disabled (per spec rule 5)
    _gross_yn   = bool(meta.get("is_gross_up")) or str(form.get("TaxPayGrossSecb") or "N").upper() == "Y"
    _gross_disp = "YES" if _gross_yn else "NO"
    if not is_tds:
        _gross_disp          = "NO"
        meta["is_gross_up"]  = False
        form["TaxPayGrossSecb"] = "N"

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("In case the remittance is net of taxes, whether tax payable has been grossed up?")
    with rc:
        sel_gross = st.selectbox(
            "Grossed up?",
            ["YES", "NO"],
            index=_idx(["YES", "NO"], _gross_disp),
            key=f"{invoice_id}_b_gross_up",
            disabled=(not is_tds),   # SPEC: TDS editable, NON_TDS disabled
            label_visibility="collapsed",
        )
        if is_tds:
            _new_gross              = (sel_gross == "YES")
            form["TaxPayGrossSecb"] = "Y" if _new_gross else "N"
            meta["is_gross_up"]     = _new_gross

    st.divider()

    # =========================================================================
    # SECTION 8 — Taxability under IT Act  (computed → READ-ONLY)
    # =========================================================================
    st.markdown("**8. Taxability under the provisions of the Income-tax Act (without considering DTAA)**")

    if is_tds:
        form["RemittanceCharIndia"] = "Y"
    _chargeable = _yn_to_yes_no(form.get("RemittanceCharIndia", "Y"), default_yes=is_tds)

    # 4.31 taxable_india → RemittanceCharIndia — READ-ONLY
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(i) Is remittance chargeable to tax in India", indent=1)
    with rc:
        sel_chargeable = st.selectbox(
            "Is remittance chargeable?",
            ["YES", "NO"],
            index=_idx(["YES", "NO"], _chargeable),
            key=f"{invoice_id}_8_chargeable",
            disabled=True,   # SPEC: read-only
            label_visibility="collapsed",
        )
        form["RemittanceCharIndia"] = _yes_no_to_yn(sel_chargeable)

    # 4.32 reasons_not_taxable → ReasonNot — editable only when taxable_india=NO
    _not_chargeable = str(form.get("RemittanceCharIndia") or "Y").upper() == "N"
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(ii) If not, reasons thereof", indent=1)
    with rc:
        form["ReasonNot"] = st.text_input(
            "Reasons not chargeable",
            value=str(form.get("ReasonNot") or ""),
            key=f"{invoice_id}_8_reason_not",
            disabled=(not _not_chargeable),
            label_visibility="collapsed",
        ).strip()

    _lbl("(iii) If yes,", indent=1)

    # 4.33 section → SecRemCovered — READ-ONLY
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(a) The relevant section of the Act under which the remittance is covered", indent=2)
    with rc:
        form["SecRemCovered"] = st.text_input(
            "Relevant section",
            value=str(form.get("SecRemCovered") or form.get("SecRemitCovered") or SEC_REM_COVERED_DEFAULT),
            key=f"{invoice_id}_8_section",
            disabled=True,
            label_visibility="collapsed",
        ).strip()
        form["SecRemitCovered"] = form["SecRemCovered"]

    # 4.34 taxable_income → AmtIncChrgIt — READ-ONLY (recomputed)
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(b) The amount of income chargeable to tax", indent=2)
    with rc:
        st.text_input(
            "Amount chargeable to tax",
            value=str(preview.get("AmtIncChrgIt") or form.get("AmtIncChrgIt") or ""),
            disabled=True,
            label_visibility="collapsed",
        )

    # 4.35 tax_liability → TaxLiablIt — READ-ONLY (recomputed)
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(c) The tax liability", indent=2)
    with rc:
        st.text_input(
            "Tax liability",
            value=str(preview.get("TaxLiablIt") or form.get("TaxLiablIt") or ""),
            disabled=True,
            label_visibility="collapsed",
        )

    # 4.36 basis → BasisDeterTax — READ-ONLY (recomputed)
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(d) Basis of determining taxable income and tax liability", indent=2)
    with rc:
        st.text_area(
            "Basis",
            value=str(preview.get("BasisDeterTax") or form.get("BasisDeterTax") or ""),
            disabled=True,
            label_visibility="collapsed",
            height=80,
        )

    st.divider()

    # =========================================================================
    # SECTION 9 — DTAA relief
    # =========================================================================
    st.markdown("**9. If income is chargeable to tax in India and any relief is claimed under DTAA**")

    # 4.37 trc → TaxResidCert — visible, disabled (UI-only for now)
    _trc_val = _yn_to_yes_no(form.get("TaxResidCert", "Y"), default_yes=True)
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(i) Whether tax residency certificate is obtained from the recipient of remittance", indent=1)
    with rc:
        st.selectbox(
            "TRC obtained?",
            ["YES", "NO"],
            index=_idx(["YES", "NO"], _trc_val),
            key=f"{invoice_id}_9_trc",
            disabled=True,
            label_visibility="collapsed",
        )

    # 4.38 dtaa → RelevantDtaa — editable both modes
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(ii) Please specify relevant DTAA", indent=1)
    with rc:
        form["RelevantDtaa"] = st.text_input(
            "Relevant DTAA",
            value=str(form.get("RelevantDtaa") or ""),
            key=f"{invoice_id}_9_dtaa",
            label_visibility="collapsed",
        ).strip()

    # 4.39 dtaa_article → RelevantArtDtaa — editable both modes
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(iii) Please specify relevant article of DTAA", indent=1)
    with rc:
        form["RelevantArtDtaa"] = st.text_input(
            "DTAA article",
            value=str(form.get("RelevantArtDtaa") or ""),
            key=f"{invoice_id}_9_dtaa_art",
            label_visibility="collapsed",
        ).strip()

    # 4.40 dtaa_income → TaxIncDtaa — READ-ONLY (recomputed)
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(iv) Taxable income as per DTAA", indent=1)
    with rc:
        st.text_input(
            "Taxable income as per DTAA",
            value=str(preview.get("TaxIncDtaa") or form.get("TaxIncDtaa") or ""),
            disabled=True,
            label_visibility="collapsed",
        )

    # 4.41 dtaa_liability → TaxLiablDtaa — READ-ONLY (recomputed)
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(v) Tax liability as per DTAA", indent=1)
    with rc:
        st.text_input(
            "Tax liability as per DTAA",
            value=str(preview.get("TaxLiablDtaa") or form.get("TaxLiablDtaa") or ""),
            disabled=True,
            label_visibility="collapsed",
        )

    # ── Section 9A ──────────────────────────────────────────────────────────
    # SPEC Rule 3: TDS → 9A active | NON_TDS → 9A disabled
    # 4.42 dtaa_a_applicable → RemForRoyFlg
    _rem_roy = _yn_to_yes_no(form.get("RemForRoyFlg", "N"))
    if not is_tds:
        _rem_roy = "NO"

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("A. If the remittance is for royalties, fee for technical services, interest, dividend, etc,")
    with rc:
        sel_9a = st.selectbox(
            "9A applicable",
            ["YES", "NO"],
            index=_idx(["YES", "NO"], _rem_roy),
            key=f"{invoice_id}_9a_applicable",
            disabled=(not is_tds),   # SPEC: TDS editable, NON_TDS disabled
            label_visibility="collapsed",
        )
        if is_tds:
            form["RemForRoyFlg"] = _yes_no_to_yn(sel_9a)

    _lbl("(not connected with permanent establishment) please indicate", indent=1)
    _9a_active = is_tds and (str(form.get("RemForRoyFlg") or "N").upper() == "Y")

    # 4.43 dtaa_a_article → ArtDtaa
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(a) Article of DTAA", indent=1)
    with rc:
        form["ArtDtaa"] = st.text_input(
            "Article of DTAA (A)",
            value=str(form.get("ArtDtaa") or form.get("RelevantArtDtaa") or ""),
            key=f"{invoice_id}_9a_article",
            disabled=(not _9a_active),
            label_visibility="collapsed",
        ).strip()

    # 4.44 dtaa_a_rate → RateTdsADtaa
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(b) Rate of TDS required to be deducted in terms of such article of the applicable DTAA", indent=1)
    with rc:
        form["RateTdsADtaa"] = st.text_input(
            "Rate of TDS (DTAA A)",
            value=str(form.get("RateTdsADtaa") or ""),
            key=f"{invoice_id}_9a_rate",
            disabled=(not _9a_active),
            label_visibility="collapsed",
        ).strip()
        if _9a_active and form["RateTdsADtaa"]:
            resolved["dtaa_rate_percent"] = form["RateTdsADtaa"]

    # ── Section 9B — UI-only ─────────────────────────────────────────────────
    # 4.45–4.48 (not canonical — UI display only)
    form.setdefault("_ui_only_9b_applicable", "NO")
    form.setdefault("_ui_only_9b_liable",     "NO")
    form.setdefault("_ui_only_9b_basis",      "")
    form.setdefault("_ui_only_9b_reasons",    "")

    _b9b_app = str(form.get("_ui_only_9b_applicable") or "NO").upper()
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("B. In case the remittance is on account of business income, please indicate")
    with rc:
        form["_ui_only_9b_applicable"] = st.selectbox(
            "9B applicable",
            ["YES", "NO"],
            index=_idx(["YES", "NO"], _b9b_app),
            key=f"{invoice_id}_9b_applicable",
            label_visibility="collapsed",
        )
    _b9b_on = str(form.get("_ui_only_9b_applicable") or "NO").upper() == "YES"

    _b9b_liable = str(form.get("_ui_only_9b_liable") or "NO").upper()
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(a) Whether such income is liable to tax in India", indent=1)
    with rc:
        form["_ui_only_9b_liable"] = st.selectbox(
            "Liable in India (B)",
            ["YES", "NO"],
            index=_idx(["YES", "NO"], _b9b_liable),
            key=f"{invoice_id}_9b_liable",
            disabled=(not _b9b_on),
            label_visibility="collapsed",
        )
    _b9b_liable_yes = _b9b_on and str(form.get("_ui_only_9b_liable") or "NO").upper() == "YES"

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(b) If so, the basis of arriving at the rate of deduction of tax.", indent=1)
    with rc:
        form["_ui_only_9b_basis"] = st.text_input(
            "Basis (B)",
            value=str(form.get("_ui_only_9b_basis") or ""),
            key=f"{invoice_id}_9b_basis",
            disabled=(not _b9b_on) or (not _b9b_liable_yes),
            label_visibility="collapsed",
        ).strip()

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(c) If not, please furnish brief reasons thereof, specifying relevant article of DTAA", indent=1)
    with rc:
        form["_ui_only_9b_reasons"] = st.text_input(
            "Reasons (B)",
            value=str(form.get("_ui_only_9b_reasons") or ""),
            key=f"{invoice_id}_9b_reasons",
            disabled=(not _b9b_on) or _b9b_liable_yes,
            label_visibility="collapsed",
        ).strip()

    # ── Section 9C — UI-only ─────────────────────────────────────────────────
    # 4.49–4.52 (not canonical — UI display only)
    form.setdefault("_ui_only_9c_applicable", "NO")
    _c9c_app = str(form.get("_ui_only_9c_applicable") or "NO").upper()

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("C. In case the remittance is on account of capital gains, please indicate")
    with rc:
        form["_ui_only_9c_applicable"] = st.selectbox(
            "9C applicable",
            ["YES", "NO"],
            index=_idx(["YES", "NO"], _c9c_app),
            key=f"{invoice_id}_9c_applicable",
            label_visibility="collapsed",
        )
    _c9c_on = str(form.get("_ui_only_9c_applicable") or "NO").upper() == "YES"

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(a) Amount of long-term capital gains", indent=1)
    with rc:
        form["_ui_only_9c_ltcg"] = st.text_input(
            "LTCG",
            value=str(form.get("_ui_only_9c_ltcg") or "0"),
            key=f"{invoice_id}_9c_ltcg",
            disabled=(not _c9c_on),
            label_visibility="collapsed",
        ).strip()

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(b) Amount of short-term capital gains", indent=1)
    with rc:
        form["_ui_only_9c_stcg"] = st.text_input(
            "STCG",
            value=str(form.get("_ui_only_9c_stcg") or "0"),
            key=f"{invoice_id}_9c_stcg",
            disabled=(not _c9c_on),
            label_visibility="collapsed",
        ).strip()

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(c) Basis of arriving at taxable income", indent=1)
    with rc:
        form["_ui_only_9c_basis"] = st.text_input(
            "Basis (C)",
            value=str(form.get("_ui_only_9c_basis") or ""),
            key=f"{invoice_id}_9c_basis",
            disabled=(not _c9c_on),
            label_visibility="collapsed",
        ).strip()

    # ── Section 9D ───────────────────────────────────────────────────────────
    # SPEC Rule 4: NON_TDS → 9D active | TDS → 9D disabled
    # 4.53 oth_applicable → OtherRemDtaa
    # Seed 9D defaults from canonical form
    if is_tds:
        form["_ui_only_9d_applicable"] = "NO"
        form["_ui_only_9d_taxable"]    = "NO"
        form["_ui_only_9d_nature"]     = "NOT APPLICABLE"
        form["_ui_only_9d_reasons"]    = "NOT APPLICABLE"
    else:
        form.setdefault("_ui_only_9d_applicable", "YES")
        form.setdefault("_ui_only_9d_taxable",    "NO")
        if str(form.get("NatureRemDtaa") or "").strip() and not str(form.get("_ui_only_9d_nature") or "").strip():
            form["_ui_only_9d_nature"] = str(form.get("NatureRemDtaa") or "")
        if str(form.get("RelArtDetlDDtaa") or "").strip() and not str(form.get("_ui_only_9d_reasons") or "").strip():
            form["_ui_only_9d_reasons"] = str(form.get("RelArtDetlDDtaa") or "")

    _d9d_app = str(form.get("_ui_only_9d_applicable") or "NO").upper()
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("D. In case of other remittance not covered by sub-items A, B and C")
    with rc:
        form["_ui_only_9d_applicable"] = st.selectbox(
            "9D applicable",
            ["YES", "NO"],
            index=_idx(["YES", "NO"], _d9d_app),
            key=f"{invoice_id}_9d_applicable",
            disabled=is_tds,   # SPEC: TDS disabled, NON_TDS editable
            label_visibility="collapsed",
        )
    _d9d_on = (str(form.get("_ui_only_9d_applicable") or "NO").upper() == "YES") and (not is_tds)
    if not is_tds:
        form["OtherRemDtaa"] = "Y" if _d9d_on else "N"

    # 4.54 other_nature → NatureRemDtaa
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(a) Please specify nature of remittance", indent=1)
    with rc:
        form["_ui_only_9d_nature"] = st.text_input(
            "Nature (D)",
            value=str(form.get("_ui_only_9d_nature") or ""),
            key=f"{invoice_id}_9d_nature",
            disabled=(not _d9d_on),   # TDS disabled, NON_TDS editable when D applicable
            label_visibility="collapsed",
        ).strip()
        if _d9d_on:
            form["NatureRemDtaa"] = form["_ui_only_9d_nature"]

    # 4.55 other_taxable
    _d9d_taxable = str(form.get("_ui_only_9d_taxable") or "NO").upper()
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(b) Whether taxable in India as per DTAA", indent=1)
    with rc:
        form["_ui_only_9d_taxable"] = st.selectbox(
            "Taxable (D)",
            ["YES", "NO"],
            index=_idx(["YES", "NO"], _d9d_taxable),
            key=f"{invoice_id}_9d_taxable",
            disabled=(not _d9d_on),
            label_visibility="collapsed",
        )
    _d9d_taxable_yes = _d9d_on and str(form.get("_ui_only_9d_taxable") or "NO").upper() == "YES"

    # 4.56 other_rate (UI-only)
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(c) If yes, rate of TDS required to be deducted in terms of such article of the applicable DTAA", indent=1)
    with rc:
        form["_ui_only_9d_rate"] = st.text_input(
            "Rate (D)",
            value=str(form.get("_ui_only_9d_rate") or ""),
            key=f"{invoice_id}_9d_rate",
            disabled=(not _d9d_on) or (not _d9d_taxable_yes),
            label_visibility="collapsed",
        ).strip()

    # 4.57 other_reasons → RelArtDetlDDtaa
    # SPEC: TDS disabled | NON_TDS editable when oth_applicable=YES and other_taxable=NO
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("(d) If not, please furnish brief reasons thereof, specifying relevant article of DTAA", indent=1)
    with rc:
        form["_ui_only_9d_reasons"] = st.text_input(
            "Reasons (D)",
            value=str(form.get("_ui_only_9d_reasons") or ""),
            key=f"{invoice_id}_9d_reasons",
            disabled=(not _d9d_on) or _d9d_taxable_yes,
            label_visibility="collapsed",
        ).strip()
        # Write back to canonical field (only in NON_TDS, when 9D active, not taxable)
        if _d9d_on and not _d9d_taxable_yes:
            form["RelArtDetlDDtaa"] = form["_ui_only_9d_reasons"]

    st.divider()

    # =========================================================================
    # SECTIONS 10–13 — TDS amounts / rate / deduction date
    # =========================================================================
    # Refresh preview after 9D edits
    preview_after9 = _preview_computed(state)

    _lbl("10. Amount of TDS")

    # 4.58 tds_fc → AmtPayForgnTds — READ-ONLY (recomputed)
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("In foreign currency", indent=1)
    with rc:
        st.text_input(
            "TDS in foreign currency",
            value=str(preview_after9.get("AmtPayForgnTds") or form.get("AmtPayForgnTds") or ""),
            disabled=True,
            label_visibility="collapsed",
        )

    # 4.59 tds_inr → AmtPayIndianTds — READ-ONLY (recomputed)
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("In Indian Rs", indent=1)
    with rc:
        st.text_input(
            "TDS in INR",
            value=str(preview_after9.get("AmtPayIndianTds") or form.get("AmtPayIndianTds") or ""),
            disabled=True,
            label_visibility="collapsed",
        )

    # 4.60 tds_rate_type → RateTdsSecbFlg
    # SPEC: TDS editable | NON_TDS disabled
    _rate_type_opts = ["AS PER INCOME-TAX ACT", "AS PER DTAA", "LOWER DEDUCTION CERTIFICATE"]
    _cur_flag = str(form.get("RateTdsSecbFlg") or "")
    if _cur_flag == "2":
        _cur_rate_type = "AS PER DTAA"
    elif _cur_flag == "3":
        _cur_rate_type = "LOWER DEDUCTION CERTIFICATE"
    else:
        _cur_rate_type = "AS PER INCOME-TAX ACT"

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("11. Rate of TDS")
    with rc:
        sel_rate_type = st.selectbox(
            "Rate of TDS type",
            _rate_type_opts,
            index=_idx(_rate_type_opts, _cur_rate_type),
            key=f"{invoice_id}_11_rate_type",
            disabled=(not is_tds),   # SPEC: TDS editable, NON_TDS disabled
            label_visibility="collapsed",
        )
        if is_tds:
            if sel_rate_type == "AS PER DTAA":
                form["RateTdsSecbFlg"] = "2"
            elif sel_rate_type == "LOWER DEDUCTION CERTIFICATE":
                form["RateTdsSecbFlg"] = "3"
            else:
                form["RateTdsSecbFlg"] = "1"
        else:
            form["RateTdsSecbFlg"] = ""

        # 4.61 tds_rate → RateTdsSecB — READ-ONLY (recomputed)
        st.text_input(
            "Rate value",
            value=str(preview_after9.get("RateTdsSecB") or form.get("RateTdsSecB") or ""),
            disabled=True,
            key=f"{invoice_id}_11_rate_val",
            label_visibility="collapsed",
        )

    # 4.62 remit_after_tds → ActlAmtTdsForgn — READ-ONLY (recomputed)
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("12. Actual amount of remittance after TDS (In foreign currency)")
    with rc:
        st.text_input(
            "Remittance after TDS",
            value=str(preview_after9.get("ActlAmtTdsForgn") or form.get("ActlAmtTdsForgn") or ""),
            disabled=True,
            label_visibility="collapsed",
        )

    # 4.63 deduction_date → DednDateTds
    # SPEC: TDS active | NON_TDS visible but disabled
    _dedn_d = _parse_iso_date(str(form.get("DednDateTds") or ""))
    if not _dedn_d:
        _dedn_d = date.today()

    lc, rc = st.columns(ratio)
    with lc:
        _lbl("13. Date of deduction of tax at source, if any")
    with rc:
        if str(form.get("DednDateTds") or "").strip() and not _parse_iso_date(str(form.get("DednDateTds") or "")):
            st.warning("Existing deduction date is invalid. Please select a valid date.")
        _dedn_val = st.date_input(
            "Date of deduction",
            value=_dedn_d,
            key=f"{invoice_id}_13_dedn_date",
            disabled=(not is_tds),   # SPEC: TDS active, NON_TDS disabled
            label_visibility="collapsed",
            format="DD/MM/YYYY",
        )
        if is_tds:
            form["DednDateTds"] = _dedn_val.strftime("%d/%m/%Y")

    st.divider()

    # =========================================================================
    # ACCOUNTANT DETAILS — all editable (4.64–4.75)
    # =========================================================================
    st.subheader("Accountant Details")

    # 4.64 acc_name → NameAcctnt
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("Accountant Name")
    with rc:
        form["NameAcctnt"] = st.text_input(
            "Accountant Name",
            value=str(form.get("NameAcctnt") or ""),
            key=f"{invoice_id}_acct_name",
            label_visibility="collapsed",
        ).strip()

    # 4.65 firm → NameFirmAcctnt
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("Name of the proprietorship / firm")
    with rc:
        form["NameFirmAcctnt"] = st.text_input(
            "Firm name",
            value=str(form.get("NameFirmAcctnt") or ""),
            key=f"{invoice_id}_acct_firm",
            label_visibility="collapsed",
        ).strip()

    # 4.66–4.73 accountant address
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("Address")
    with rc:
        _ar1, _ar2, _ar3 = st.columns(3)
        with _ar1:
            # 4.66 addr_flat → AcctntFlatDoorBuilding
            form["AcctntFlatDoorBuilding"] = st.text_input(
                "Flat/Door/Building",
                value=str(form.get("AcctntFlatDoorBuilding") or ""),
                key=f"{invoice_id}_acct_flat",
                label_visibility="collapsed",
                placeholder="Flat/Door/Building",
            ).strip()
        with _ar2:
            # 4.67 addr_premises → PremisesBuildingVillage
            form["PremisesBuildingVillage"] = st.text_input(
                "Premises",
                value=str(form.get("PremisesBuildingVillage") or ""),
                key=f"{invoice_id}_acct_premises",
                label_visibility="collapsed",
                placeholder="Premises/Building/Village",
            ).strip()
        with _ar3:
            # 4.68 addr_road → AcctntRoadStreet
            form["AcctntRoadStreet"] = st.text_input(
                "Road/Street",
                value=str(form.get("AcctntRoadStreet") or ""),
                key=f"{invoice_id}_acct_road",
                label_visibility="collapsed",
                placeholder="Road/Street",
            ).strip()

        _ar4, _ar5, _ar6 = st.columns(3)
        with _ar4:
            # 4.69 addr_area → AcctntAreaLocality
            form["AcctntAreaLocality"] = st.text_input(
                "Area/Locality",
                value=str(form.get("AcctntAreaLocality") or ""),
                key=f"{invoice_id}_acct_area",
                label_visibility="collapsed",
                placeholder="Area/Locality",
            ).strip()
        with _ar5:
            # 4.70 addr_city → AcctntTownCityDistrict
            form["AcctntTownCityDistrict"] = st.text_input(
                "City",
                value=str(form.get("AcctntTownCityDistrict") or ""),
                key=f"{invoice_id}_acct_city",
                label_visibility="collapsed",
                placeholder="Town/City/District",
            ).strip()
        with _ar6:
            # 4.71 addr_state → AcctntState
            _acct_state_opts = [_CSELECT] + list(INDIAN_STATES_AND_UTS) + ["OTHER / MANUAL"]
            _acct_state_raw  = str(form.get("AcctntState") or "")
            _acct_state_disp = _CSELECT
            for _opt in INDIAN_STATES_AND_UTS:
                if _opt.strip().upper() == _acct_state_raw.upper():
                    _acct_state_disp = _opt
                    break
            if _acct_state_disp == _CSELECT:
                _rev_map = {v: k.title() for k, v in _state_code_map().items()}
                _acct_state_disp = _rev_map.get(_acct_state_raw, "OTHER / MANUAL" if _acct_state_raw else _CSELECT)

            sel_acct_state = st.selectbox(
                "Accountant state",
                _acct_state_opts,
                index=_idx(_acct_state_opts, _acct_state_disp),
                key=f"{invoice_id}_acct_state",
                label_visibility="collapsed",
            )
            if sel_acct_state == "OTHER / MANUAL":
                form["AcctntState"] = st.text_input(
                    "State (manual)",
                    value=_acct_state_raw,
                    key=f"{invoice_id}_acct_state_manual",
                    label_visibility="collapsed",
                ).strip()
            elif sel_acct_state != _CSELECT:
                _code_map = _state_code_map()
                form["AcctntState"] = str(_code_map.get(sel_acct_state.strip().upper(), sel_acct_state))

        _ar7, _ar8 = st.columns(2)
        with _ar7:
            # 4.72 addr_country → AcctntCountryCode
            _acct_ctry_opts = [_CSELECT] + list(COUNTRIES)
            _acct_ctry_code = str(form.get("AcctntCountryCode") or "")
            _acct_ctry_disp = _country_label(_acct_ctry_code)
            sel_acct_ctry = st.selectbox(
                "Accountant country",
                _acct_ctry_opts,
                index=_idx(_acct_ctry_opts, _acct_ctry_disp),
                key=f"{invoice_id}_acct_country",
                label_visibility="collapsed",
            )
            if sel_acct_ctry == "OTHERS":
                _other_acct_ctry = st.text_input(
                    "Accountant country (manual)",
                    value=str(form.get("_ui_only_acctnt_country_other") or ""),
                    key=f"{invoice_id}_acct_country_other",
                    label_visibility="collapsed",
                ).strip().upper()
                form["_ui_only_acctnt_country_other"] = _other_acct_ctry
                if _other_acct_ctry:
                    _mapped = _country_code(_other_acct_ctry)
                    if _mapped:
                        form["AcctntCountryCode"] = _mapped
                    elif _other_acct_ctry.isdigit():
                        form["AcctntCountryCode"] = _other_acct_ctry
            elif sel_acct_ctry != _CSELECT:
                _mapped = _country_code(sel_acct_ctry)
                if _mapped:
                    form["AcctntCountryCode"] = _mapped
                form["_ui_only_acctnt_country_other"] = ""

        with _ar8:
            # 4.73 addr_pin → AcctntPincode
            form["AcctntPincode"] = st.text_input(
                "PIN Code",
                value=str(form.get("AcctntPincode") or ""),
                key=f"{invoice_id}_acct_pin",
                label_visibility="collapsed",
                placeholder="PIN Code",
            ).strip()

    # 4.74 membership → MembershipNumber
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("Membership No")
    with rc:
        form["MembershipNumber"] = st.text_input(
            "Membership No",
            value=str(form.get("MembershipNumber") or ""),
            key=f"{invoice_id}_acct_membership",
            label_visibility="collapsed",
        ).strip()

    # 4.75 registration (UI-only — no canonical XML field confirmed yet)
    lc, rc = st.columns(ratio)
    with lc:
        _lbl("Registration No")
    with rc:
        form["_ui_only_registration_no"] = st.text_input(
            "Registration No",
            value=str(form.get("_ui_only_registration_no") or ""),
            key=f"{invoice_id}_acct_reg",
            label_visibility="collapsed",
        ).strip()

    st.divider()

    return state
