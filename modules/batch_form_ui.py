from __future__ import annotations

import copy
import functools
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

import streamlit as st

from modules.currency_mapping import (
    load_currency_exact_index,
    load_currency_short_index,
    resolve_currency_selection,
)
from modules.form15cb_constants import (
    CA_FIRM_OPTIONS,
    FIELD_MAX_LENGTH,
    HONORIFIC_M_S,
    IOR_WE_CODE,
    MODE_TDS,
    PROPOSED_DATE_OFFSET_DAYS,
    SEC_REM_COVERED_DEFAULT,
)
from modules.invoice_calculator import recompute_invoice
from modules.logger import get_logger
from modules.invoice_calculator import _build_name_remittee, format_dotted_date
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
from modules.ui_reference_options import COUNTRIES, CURRENCIES, INDIAN_STATES_AND_UTS


logger = get_logger()

ROOT = Path(__file__).resolve().parent.parent
LOOKUPS_DIR = ROOT / "lookups"


def compose_name_remitter(remitter_name: str, remitter_address: str) -> str:
    remitter_name = (remitter_name or "").strip()
    remitter_address = (remitter_address or "").strip()

    if remitter_name and remitter_address:
        if remitter_address.upper().startswith(remitter_name.upper()):
            return remitter_address
        return f"{remitter_name}. {remitter_address}"
    return remitter_name or remitter_address


def compose_name_remittee(beneficiary_name: str, invoice_number: str, invoice_date_iso: str) -> str:
    beneficiary_name = (beneficiary_name or "").strip()
    invoice_number = (invoice_number or "").strip()
    invoice_date_iso = (invoice_date_iso or "").strip()

    dt_text = ""
    if invoice_date_iso:
        try:
            dt_text = datetime.strptime(invoice_date_iso, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            dt_text = invoice_date_iso

    parts = [beneficiary_name]
    if invoice_number:
        parts.append(f"INVOICE NO. {invoice_number}")
    if dt_text:
        parts.append(f"DT {dt_text}")

    return " ".join(p for p in parts if p).strip()


def _parse_iso_date(value: str) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _to_float_or_none(raw: str) -> float | None:
    text = str(raw or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _round_half_up_int(value: float) -> int:
    try:
        return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return int(round(value))


def _dtaa_rate_percent(raw: str) -> str:
    raw_s = str(raw or "").strip()
    if "i.t act" in raw_s.lower():
        return "it_act"
    try:
        return str(float(raw_s) * 100).rstrip("0").rstrip(".")
    except Exception:
        return ""


def _purpose_group_for_code(purpose_grouped: Dict[str, List[Dict[str, str]]], purpose_code: str) -> str:
    code = str(purpose_code or "").strip().upper()
    if not code:
        return ""
    for group_name, rows in purpose_grouped.items():
        for row in rows:
            if str(row.get("purpose_code") or "").strip().upper() == code:
                return group_name
    return ""


def _selectbox_index_from_value(options: List[str], value: str) -> int:
    if not options:
        return 0
    try:
        return options.index(value)
    except ValueError:
        return 0


def _ensure_linked_text_inputs(key_a: str, key_b: str, initial_value: str) -> None:
    if key_a not in st.session_state:
        st.session_state[key_a] = str(initial_value or "")
    if key_b not in st.session_state:
        st.session_state[key_b] = str(initial_value or "")


def _mirror_text_value(source_key: str, target_key: str) -> None:
    st.session_state[target_key] = str(st.session_state.get(source_key) or "")


def _apply_remitter_match(state: Dict[str, object], remitter_name: str) -> None:
    invoice_id = str(state.get("meta", {}).get("invoice_id") or "")
    form = state["form"]
    resolved = state["resolved"]
    rec = match_remitter(remitter_name)
    if rec:
        resolved["remitter_match"] = "1"
        resolved["pan"] = rec.get("pan", "")
        resolved["bank_name"] = rec.get("bank_name", "")
        resolved["branch"] = rec.get("branch", "")
        resolved["bsr"] = rec.get("bsr", "")
        resolved["bank_code"] = resolve_bank_code(rec.get("bank_name", ""))
        form["RemitterPAN"] = rec.get("pan", "")
        form["NameBankDisplay"] = rec.get("bank_name", "")
        form["NameBankCode"] = resolved["bank_code"]
        form["BranchName"] = rec.get("branch", "")
        form["BsrCode"] = rec.get("bsr", "")
        form["_lock_pan_bank_branch_bsr"] = "1"
        logger.info(
            "ui_remitter_match invoice_id=%s remitter_name=%s pan=%s bank=%s",
            invoice_id,
            remitter_name,
            rec.get("pan", ""),
            rec.get("bank_name", ""),
        )
    else:
        resolved["remitter_match"] = "0"
        form["_lock_pan_bank_branch_bsr"] = "0"
        logger.warning("ui_remitter_not_matched invoice_id=%s remitter_name=%s", invoice_id, remitter_name)


def check_field_length_warnings(form: Dict[str, str]) -> List[str]:
    warnings: List[str] = []
    for field, max_len in FIELD_MAX_LENGTH.items():
        val = str(form.get(field) or "")
        if len(val) > max_len:
            warnings.append(f"'{field}' is {len(val)} chars (max {max_len}) - it will be trimmed in XML.")
    return warnings


def _yes_no_to_yn(value: str) -> str:
    return "Y" if str(value or "").strip().upper() == "YES" else "N"


def _yn_to_yes_no(value: str, *, default_yes: bool = False) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return "YES" if default_yes else "NO"
    return "YES" if text in {"Y", "YES", "1", "TRUE"} else "NO"


@functools.lru_cache(maxsize=1)
def _country_maps() -> Tuple[Dict[str, str], Dict[str, str]]:
    name_to_code: Dict[str, str] = {}
    code_to_name: Dict[str, str] = {}
    for name in COUNTRIES:
        code = str(resolve_country_code(name) or "").strip()
        if code:
            name_to_code[name] = code
            code_to_name.setdefault(code, name)
    return name_to_code, code_to_name


def _country_code_from_label(label: str) -> str:
    name_to_code, _ = _country_maps()
    clean = str(label or "").strip()
    if not clean:
        return ""
    code = name_to_code.get(clean, "")
    if code:
        return code
    return str(resolve_country_code(clean) or "").strip()


def _country_label_from_code(code: str) -> str:
    _, code_to_name = _country_maps()
    code_clean = str(code or "").strip()
    if not code_clean:
        return "SELECT"
    if code_clean in code_to_name:
        return code_to_name[code_clean]
    resolved_name = str(resolve_country_name(code_clean) or "").strip().upper()
    if not resolved_name:
        return "OTHERS"
    for name in COUNTRIES:
        if name.strip().upper() == resolved_name:
            return name
    fallback_code = str(resolve_country_code(resolved_name) or "").strip()
    if fallback_code and fallback_code == code_clean:
        return resolved_name
    return "OTHERS"


def _seed_accountant_defaults(form: Dict[str, str]) -> None:
    """Seed the form with absolute accountant default fields if they are blank."""
    if not str(form.get("NameAcctnt") or "").strip():
        form["NameAcctnt"] = "SONDUR ANAND"
    if not str(form.get("AcctntFlatDoorBuilding") or "").strip():
        form["AcctntFlatDoorBuilding"] = "NO. 55, SECOND FLOOR"
    if not str(form.get("PremisesBuildingVillage") or "").strip():
        form["PremisesBuildingVillage"] = "S.V. COMPLEX"
    if not str(form.get("AcctntRoadStreet") or "").strip():
        form["AcctntRoadStreet"] = "K.R. ROAD"
    if not str(form.get("AcctntAreaLocality") or "").strip():
        form["AcctntAreaLocality"] = "BASAVANAGUDI"
    if not str(form.get("AcctntTownCityDistrict") or "").strip():
        form["AcctntTownCityDistrict"] = "BENGALURU"
    if not str(form.get("AcctntPincode") or "").strip():
        form["AcctntPincode"] = "560004"
    if not str(form.get("MembershipNumber") or "").strip():
        form["MembershipNumber"] = "216066"
    if not str(form.get("AcctntState") or "").strip():
        code_map = _accountant_state_code_map()
        form["AcctntState"] = str(code_map.get("KARNATAKA", "29"))
    if not str(form.get("AcctntCountryCode") or "").strip():
        mapped_code = _country_code_from_label("INDIA")
        form["AcctntCountryCode"] = mapped_code if mapped_code else "91"


@functools.lru_cache(maxsize=1)
def _currency_maps() -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    exact_index = load_currency_exact_index()
    short_index = load_currency_short_index()
    short_to_code: Dict[str, str] = {}
    code_to_short: Dict[str, str] = {}

    for short in CURRENCIES:
        resolved = resolve_currency_selection(short, exact_index)
        code = str(resolved.get("code") or "").strip()
        if code:
            short_to_code[short] = code
            code_to_short.setdefault(code, short)

    for code, short in short_index.items():
        code_clean = str(code or "").strip()
        short_clean = str(short or "").strip().upper()
        if code_clean and short_clean and short_clean in CURRENCIES:
            code_to_short.setdefault(code_clean, short_clean)

    return short_to_code, code_to_short, exact_index


def _currency_code_from_short(short_code: str) -> str:
    short_to_code, _, exact_index = _currency_maps()
    short_clean = str(short_code or "").strip().upper()
    if not short_clean:
        return ""
    code = short_to_code.get(short_clean, "")
    if code:
        return code
    resolved = resolve_currency_selection(short_clean, exact_index)
    return str(resolved.get("code") or "").strip()


def _currency_short_from_code(code: str) -> str:
    _, code_to_short, _ = _currency_maps()
    return str(code_to_short.get(str(code or "").strip()) or "")


@functools.lru_cache(maxsize=1)
def _bank_maps() -> Tuple[List[str], Dict[str, str], Dict[str, str]]:
    rows = get_bank_options()
    name_to_code: Dict[str, str] = {}
    code_to_name: Dict[str, str] = {}
    for name, code in rows:
        clean_name = str(name or "").strip()
        clean_code = str(code or "").strip()
        if not clean_name or not clean_code:
            continue
        name_to_code[clean_name] = clean_code
        code_to_name.setdefault(clean_code, clean_name)
    names = sorted(name_to_code.keys())
    return names, name_to_code, code_to_name


@functools.lru_cache(maxsize=1)
def _accountant_state_code_map() -> Dict[str, str]:
    path = LOOKUPS_DIR / "state_codes.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in raw.items():
        key = str(k or "").strip().upper()
        val = str(v or "").strip()
        if key and val:
            out[key] = val
    return out


def _accountant_state_display_from_value(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return "SELECT"
    for state_name in INDIAN_STATES_AND_UTS:
        if state_name.strip().upper() == value.upper():
            return state_name
    reverse = {v: k.title() for k, v in _accountant_state_code_map().items()}
    return reverse.get(value, "OTHER / MANUAL")


def _set_fixed_header_defaults(form: Dict[str, str]) -> None:
    form["IorWe"] = IOR_WE_CODE
    form["RemitterHonorific"] = HONORIFIC_M_S
    form["BeneficiaryHonorific"] = HONORIFIC_M_S


def _apply_mode_ui_defaults(form: Dict[str, str], *, is_tds_mode: bool) -> None:
    """Apply mode-driven defaults for visible conditional UI blocks."""
    current_mode = "TDS" if is_tds_mode else "NON_TDS"
    previous_mode = str(form.get("_ui_last_mode") or "")
    mode_changed = bool(previous_mode) and previous_mode != current_mode

    if is_tds_mode:
        # 9D inactive in TDS.
        form["OtherRemDtaa"] = "N"
        form["_ui_only_9d_applicable"] = "NO"
        form["_ui_only_9d_taxable"] = "NO"
    else:
        # 9A inactive in NON_TDS.
        form["RemForRoyFlg"] = "N"
        raw_9d = str(form.get("_ui_only_9d_applicable") or "").strip().upper()
        # On mode switch into NON_TDS, default D applicable to YES.
        if mode_changed or raw_9d not in {"YES", "NO"}:
            form["_ui_only_9d_applicable"] = "YES"
        form["OtherRemDtaa"] = "Y" if str(form.get("_ui_only_9d_applicable") or "").strip().upper() == "YES" else "N"

    form["_ui_last_mode"] = current_mode


def _inject_ui_styles() -> None:
    st.markdown(
        """
        <style>
        .mid-label { display:flex; align-items:center; height:38px; font-weight:600; font-size:1rem; }
        .flabel { padding-top:8px; font-size:0.98rem; }
        .flabel-ind1 { padding-top:8px; font-size:0.98rem; padding-left:18px; }
        .flabel-ind2 { padding-top:8px; font-size:0.98rem; padding-left:36px; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _label(text: str, *, indent: int = 0) -> None:
    cls = "flabel"
    if indent == 1:
        cls = "flabel-ind1"
    elif indent == 2:
        cls = "flabel-ind2"
    st.markdown(f"<div class='{cls}'>{text}</div>", unsafe_allow_html=True)


def _safe_preview_form(state: Dict[str, object]) -> Dict[str, str]:
    try:
        preview_state = recompute_invoice(copy.deepcopy(state))
        return preview_state.get("form", {})
    except Exception:
        logger.exception("ui_preview_recompute_failed invoice_id=%s", state.get("meta", {}).get("invoice_id", ""))
        return state.get("form", {})


def _default_prop_date(form: Dict[str, str]) -> date:
    existing = _parse_iso_date(str(form.get("PropDateRem") or ""))
    if existing:
        return existing
    return date.today() + timedelta(days=PROPOSED_DATE_OFFSET_DAYS)


def _default_dedn_date(form: Dict[str, str], meta: Dict[str, str]) -> date:
    existing = _parse_iso_date(str(form.get("DednDateTds") or ""))
    if existing:
        return existing
    from_meta = _parse_iso_date(str(meta.get("tds_deduction_date") or ""))
    if from_meta:
        return from_meta
    return date.today()


def _display_remitter_address(remitter_name: str, remitter_address: str) -> str:
    name = str(remitter_name or "").strip().upper().rstrip(". ")
    address = str(remitter_address or "").strip()
    if not address:
        return ""
    if not name:
        return address
    upper_addr = address.upper()
    if upper_addr.startswith(name):
        # Keep display clean when OCR extracts "NAME + address" in one line.
        return address[len(name):].lstrip(" .,;:-")
    return address


def _sync_dtaa_from_country(
    *,
    state: Dict[str, object],
    selected_country_code: str,
    selected_country_label: str,
) -> None:
    form = state["form"]
    resolved = state["resolved"]

    prev_country_code = str(form.get("_ui_last_country_for_dtaa") or "")
    code = str(selected_country_code or "").strip()
    if not code or code == prev_country_code:
        return

    form["_ui_last_country_for_dtaa"] = code
    country_name = str(selected_country_label or "").strip()
    if not country_name or country_name == "OTHERS":
        country_name = str(resolve_country_name(code) or "").strip()

    dtaa = resolve_dtaa(country_name) if country_name else None
    if not dtaa:
        logger.warning("ui_dtaa_lookup_missing invoice_id=%s country_code=%s", state.get("meta", {}).get("invoice_id", ""), code)
        return

    dtaa_without_article, dtaa_with_article = split_dtaa_article_text(str(dtaa.get("dtaa_applicable") or ""))
    if dtaa_without_article:
        form["RelevantDtaa"] = dtaa_without_article
    if dtaa_with_article:
        form["RelevantArtDtaa"] = dtaa_with_article
        form.setdefault("ArtDtaa", dtaa_with_article)

    rate_text = _dtaa_rate_percent(str(dtaa.get("percentage") or ""))
    if rate_text == "it_act":
        form["dtaa_mode"] = "it_act"
        form["RateTdsADtaa"] = ""
        resolved["dtaa_rate_percent"] = ""
    elif rate_text:
        form["dtaa_mode"] = "dtaa_rate"
        form["RateTdsADtaa"] = rate_text
        resolved["dtaa_rate_percent"] = rate_text
    else:
        form.setdefault("dtaa_mode", "")
        resolved["dtaa_rate_percent"] = str(form.get("RateTdsADtaa") or "")

    # ── Reset Streamlit session_state for Section 9 widgets so that the
    # programmatic form update (above) is reflected in the UI immediately.
    # Without this, the widgets return stale session_state values on the
    # very next render, which the override detection mistakes for intentional
    # user edits and locks in the wrong (old) values as permanent overrides.
    invoice_id = str(state.get("meta", {}).get("invoice_id") or "")
    if invoice_id:
        new_rate_display = "" if (rate_text == "it_act" or not rate_text) else rate_text
        if "_ui_override_sec9_RateTdsADtaa" not in form:
            st.session_state[f"{invoice_id}_9a_rate"] = new_rate_display
        if "_ui_override_sec9_RelevantDtaa" not in form:
            st.session_state[f"{invoice_id}_9_dtaa"] = str(form.get("RelevantDtaa") or "")
        if "_ui_override_sec9_RelevantArtDtaa" not in form:
            st.session_state[f"{invoice_id}_9_dtaa_article"] = str(form.get("RelevantArtDtaa") or "")
        if "_ui_override_sec9_ArtDtaa" not in form:
            st.session_state[f"{invoice_id}_9a_article"] = str(form.get("ArtDtaa") or "")


def render_invoice_tab(state: Dict[str, object], *, show_header: bool = True, is_single_mode: bool = False) -> Dict[str, object]:
    meta = state.setdefault("meta", {})
    extracted = state.setdefault("extracted", {})
    form = state.setdefault("form", {})
    state.setdefault("resolved", {})

    invoice_id = str(meta.get("invoice_id") or "")
    mode = str(meta.get("mode") or MODE_TDS)
    is_tds_mode = mode == MODE_TDS

    _set_fixed_header_defaults(form)
    _apply_mode_ui_defaults(form, is_tds_mode=is_tds_mode)
    _inject_ui_styles()

    if show_header:
        st.markdown("### FORM NO. 15CB")
        st.caption("Certificate of an accountant")
        if not is_single_mode:
            st.caption(f"Mode: {'TDS' if is_tds_mode else 'Non-TDS'}")

    if meta.get("extraction_quality") == "failed":
        st.error(
            "Automatic extraction failed for this invoice. Please review and fill fields manually."
        )

    for warning in check_field_length_warnings(form):
        st.warning(warning)

    # Invoice reference (legacy fields from previous review UI).
    invoice_number_default = str(form.get("InvoiceNumber") or extracted.get("invoice_number") or "")
    invoice_date_default = str(form.get("InvoiceDate") or extracted.get("invoice_date_iso") or "")
    remitter_address_default = _display_remitter_address(
        str(form.get("NameRemitterInput") or extracted.get("remitter_name") or ""),
        str(form.get("RemitterAddress") or extracted.get("remitter_address") or ""),
    )

    st.subheader("Invoice Reference")
    inv_lc, inv_rc = st.columns([2, 3])
    with inv_lc:
        _label("Invoice Number")
    with inv_rc:
        new_inv_num = st.text_input(
            "Invoice Number",
            value=invoice_number_default,
            key=f"{invoice_id}_inv_number",
            label_visibility="collapsed",
        ).strip()
        form["InvoiceNumber"] = new_inv_num

    inv_lc, inv_rc = st.columns([2, 3])
    with inv_lc:
        _label("Invoice Date (DD/MM/YYYY)")
    with inv_rc:
        new_inv_date = st.text_input(
            "Invoice Date",
            value=invoice_date_default,
            key=f"{invoice_id}_inv_date",
            placeholder="DD/MM/YYYY",
            label_visibility="collapsed",
        ).strip()
        form["InvoiceDate"] = new_inv_date

        if form["InvoiceDate"] and _parse_iso_date(form["InvoiceDate"]) is None:
            st.warning("Invoice Date format should be DD/MM/YYYY.")

    inv_lc, inv_rc = st.columns([2, 3])
    with inv_lc:
        _label("Remitter Address (as per invoice)")
    with inv_rc:
        new_rem_addr = st.text_area(
            "Remitter Address",
            value=remitter_address_default,
            key=f"{invoice_id}_inv_remitter_address",
            label_visibility="collapsed",
            height=80,
        ).strip()
        form["RemitterAddress"] = new_rem_addr

    st.divider()

    # Header / preamble block.
    # The UI should display the same composed values that go into XML.
    # Use form["NameRemitter"] / form["NameRemittee"] when available, otherwise
    # compose from raw inputs + extracted values.

    raw_remitter = str(form.get("NameRemitterInput") or extracted.get("remitter_name") or "").strip()
    raw_remitter_address = str(form.get("RemitterAddress") or extracted.get("remitter_address") or "").strip()
    display_remitter = str(form.get("NameRemitter") or "").strip()
    if not display_remitter:
        display_remitter = compose_name_remitter(raw_remitter, raw_remitter_address)
        form["NameRemitter"] = display_remitter

    raw_beneficiary = str(form.get("NameRemitteeInput") or extracted.get("beneficiary_name") or "").strip()
    invoice_no = str(form.get("InvoiceNumber") or extracted.get("invoice_number") or "").strip()
    invoice_date_iso = str(form.get("InvoiceDate") or extracted.get("invoice_date_iso") or "").strip()

    # Compose the final beneficiary text that is expected in XML
    composed_beneficiary = compose_name_remittee(raw_beneficiary, invoice_no, invoice_date_iso)

    # If user has not explicitly overridden the beneficiary display, keep it in sync with composition
    if "_ui_override_name_remittee" not in form:
        form["NameRemittee"] = composed_beneficiary

    display_beneficiary = str(form.get("NameRemittee") or "").strip()

    beneficiary_header_key = f"{invoice_id}_header_benef_name"
    beneficiary_section_a_key = f"{invoice_id}_a_benef_name"

    # Ensure the header and Section A both display the final composed beneficiary string.
    if beneficiary_header_key not in st.session_state:
        st.session_state[beneficiary_header_key] = display_beneficiary
    if beneficiary_section_a_key not in st.session_state:
        st.session_state[beneficiary_section_a_key] = display_beneficiary

    pan_default = str(form.get("RemitterPAN") or "")

    h1c1, h1c2, h1c3, h1c4 = st.columns([0.8, 4.8, 0.8, 3.2])
    with h1c1:
        st.selectbox(
            "I / We",
            ["I", "We"],
            index=1,
            key=f"{invoice_id}_header_iorwe",
            disabled=True,
            label_visibility="collapsed",
        )
    with h1c2:
        st.markdown(
            "<div class='mid-label'>* have examined the agreement (wherever applicable) between</div>",
            unsafe_allow_html=True,
        )
    with h1c3:
        st.selectbox(
            "Remitter honorific",
            ["Mr", "Ms", "M/s"],
            index=2,
            key=f"{invoice_id}_header_remitter_honorific",
            disabled=True,
            label_visibility="collapsed",
        )
    with h1c4:
        # Show final composed remitter (XML-ready) in the top header field.
        if f"{invoice_id}_header_remitter_name" not in st.session_state:
            st.session_state[f"{invoice_id}_header_remitter_name"] = display_remitter
        new_remitter = st.text_input(
            "Name of the Remitter",
            key=f"{invoice_id}_header_remitter_name",
            placeholder="Name of the Remitter *",
            label_visibility="collapsed",
        ).strip()
        # Keep both the editable input and final XML field in sync so XML generation reflects UI edits.
        form["NameRemitter"] = new_remitter
        form["NameRemitterInput"] = new_remitter

    if form["NameRemitterInput"]:
        prev_lookup_name = str(form.get("_ui_last_remitter_lookup_name") or "")
        if form["NameRemitterInput"] != prev_lookup_name:
            _apply_remitter_match(state, form["NameRemitterInput"])
            form["_ui_last_remitter_lookup_name"] = form["NameRemitterInput"]

    h2c1, h2c2, h2c3, h2c4, h2c5 = st.columns([1.2, 2.1, 0.6, 0.8, 3.4])
    with h2c1:
        st.markdown("<div class='mid-label'>with PAN/TAN</div>", unsafe_allow_html=True)
    with h2c2:
        form["RemitterPAN"] = st.text_input(
            "PAN/TAN",
            value=pan_default,
            key=f"{invoice_id}_header_pan",
            label_visibility="collapsed",
        ).strip().upper()
    with h2c3:
        st.markdown("<div class='mid-label'>* and</div>", unsafe_allow_html=True)
    with h2c4:
        st.selectbox(
            "Beneficiary honorific",
            ["Mr", "Ms", "M/s"],
            index=2,
            key=f"{invoice_id}_header_benef_honorific",
            disabled=True,
            label_visibility="collapsed",
        )
    with h2c5:
        # Show final composed beneficiary (XML-ready) in the top header field.
        # Mirrored into Section A as read-only.
        if beneficiary_header_key not in st.session_state:
            st.session_state[beneficiary_header_key] = display_beneficiary
        if beneficiary_section_a_key not in st.session_state:
            st.session_state[beneficiary_section_a_key] = display_beneficiary

        new_beneficiary = st.text_input(
            "Name of the Beneficiary",
            key=beneficiary_header_key,
            placeholder="Name of Beneficiary *",
            on_change=_mirror_text_value,
            args=(beneficiary_header_key, beneficiary_section_a_key),
            label_visibility="collapsed",
        ).strip()
        if new_beneficiary != display_beneficiary:
            form["_ui_override_name_remittee"] = new_beneficiary
        # Keep both the editable input key and the final XML field in sync.
        form["NameRemittee"] = new_beneficiary
        form["NameRemitteeInput"] = new_beneficiary

    st.markdown(
        """
        <div style='font-weight:600;font-size:0.97rem;margin-top:6px'>
        * requiring the above remittance as well as the relevant documents and books of account
        required for ascertaining the nature of remittance and for determining the rate of deduction
        of tax at source as per provisions of Chapter XVII-B. We hereby certify the following.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.divider()

    ratio = [2, 3]

    # Section A
    st.subheader("A  Name and address of the beneficiary of the remittance")

    lc, rc = st.columns(ratio)
    with lc:
        _label("Name of the Beneficiary of the remittance")
    with rc:
        # Section A beneficiary field is a read-only mirror of the final NameRemittee.
        st.text_input(
            "Name of the Beneficiary of the remittance",
            key=beneficiary_section_a_key,
            disabled=True,
            label_visibility="collapsed",
        )
        form["NameRemittee"] = str(st.session_state.get(beneficiary_section_a_key) or "")

    section_a_rows = [
        ("Flat / Door / Building", "RemitteeFlatDoorBuilding"),
        ("Name of premises / Building / Village", "RemitteePremisesBuildingVillage"),
        ("Road / Street", "RemitteeRoadStreet"),
        ("Area / Locality", "RemitteeAreaLocality"),
        ("Town / City / District", "RemitteeTownCityDistrict"),
    ]
    for label, key in section_a_rows:
        lc, rc = st.columns(ratio)
        with lc:
            _label(label)
        with rc:
            form[key] = st.text_input(
                label,
                value=str(form.get(key) or ""),
                key=f"{invoice_id}_a_{key}",
                label_visibility="collapsed",
            ).strip()

    state_options = ["SELECT", "OUTSIDE INDIA"] + INDIAN_STATES_AND_UTS + ["OTHER / MANUAL"]
    current_state = str(form.get("RemitteeState") or "")
    state_display = "SELECT"
    for opt in state_options:
        if opt == "SELECT":
            continue
        if opt.upper() == current_state.upper():
            state_display = opt
            break
    if state_display == "SELECT" and current_state:
        state_display = "OTHER / MANUAL"

    lc, rc = st.columns(ratio)
    with lc:
        _label("State")
    with rc:
        selected_state = st.selectbox(
            "State",
            state_options,
            index=_selectbox_index_from_value(state_options, state_display),
            key=f"{invoice_id}_a_state_select",
            label_visibility="collapsed",
        )
        if selected_state == "OTHER / MANUAL":
            form["RemitteeState"] = st.text_input(
                "State (manual)",
                value=current_state,
                key=f"{invoice_id}_a_state_manual",
                label_visibility="collapsed",
            ).strip()
        elif selected_state != "SELECT":
            form["RemitteeState"] = selected_state

    country_select_options = ["SELECT"] + COUNTRIES
    current_remittee_country_code = str(form.get("RemitteeCountryCode") or form.get("CountryRemMadeSecb") or "")
    remittee_country_display = _country_label_from_code(current_remittee_country_code)

    lc, rc = st.columns(ratio)
    with lc:
        _label("Country")
    with rc:
        selected_country_a = st.selectbox(
            "Country",
            country_select_options,
            index=_selectbox_index_from_value(country_select_options, remittee_country_display),
            key=f"{invoice_id}_a_country",
            label_visibility="collapsed",
        )
        resolved_country_code_a = str(form.get("RemitteeCountryCode") or "")
        if selected_country_a == "OTHERS":
            other_country = st.text_input(
                "Other country",
                value=str(form.get("_ui_other_country_a") or ""),
                key=f"{invoice_id}_a_country_other",
                label_visibility="collapsed",
            ).strip().upper()
            form["_ui_other_country_a"] = other_country
            if other_country:
                resolved_country_code_a = _country_code_from_label(other_country)
                if not resolved_country_code_a and other_country.isdigit():
                    resolved_country_code_a = other_country
        elif selected_country_a != "SELECT":
            resolved_country_code_a = _country_code_from_label(selected_country_a)
            form["_ui_other_country_a"] = ""

        if resolved_country_code_a:
            form["RemitteeCountryCode"] = resolved_country_code_a
            form["CountryRemMadeSecb"] = resolved_country_code_a

    lc, rc = st.columns(ratio)
    with lc:
        _label("ZIP Code")
    with rc:
        form["RemitteeZipCode"] = st.text_input(
            "ZIP Code",
            value=str(form.get("RemitteeZipCode") or ""),
            key=f"{invoice_id}_a_zip",
            label_visibility="collapsed",
        ).strip()

    st.divider()

    # Section B
    st.subheader("B  Remittance Details")
    st.markdown("**1. Country to which remittance is made**")

    current_country_code_b = str(form.get("CountryRemMadeSecb") or form.get("RemitteeCountryCode") or "")
    country_b_display = _country_label_from_code(current_country_code_b)

    lc, rc = st.columns(ratio)
    with lc:
        _label("Country", indent=1)
    with rc:
        selected_country_b = st.selectbox(
            "Country to which remittance is made",
            country_select_options,
            index=_selectbox_index_from_value(country_select_options, country_b_display),
            key=f"{invoice_id}_b_country",
            label_visibility="collapsed",
        )
        resolved_country_code_b = current_country_code_b
        if selected_country_b == "OTHERS":
            other_country_b = st.text_input(
                "Other country for remittance",
                value=str(form.get("_ui_other_country_b") or ""),
                key=f"{invoice_id}_b_country_other",
                label_visibility="collapsed",
            ).strip().upper()
            form["_ui_other_country_b"] = other_country_b
            if other_country_b:
                resolved_country_code_b = _country_code_from_label(other_country_b)
                if not resolved_country_code_b and other_country_b.isdigit():
                    resolved_country_code_b = other_country_b
        elif selected_country_b != "SELECT":
            resolved_country_code_b = _country_code_from_label(selected_country_b)
            form["_ui_other_country_b"] = ""

        if resolved_country_code_b:
            form["CountryRemMadeSecb"] = resolved_country_code_b
            form["RemitteeCountryCode"] = resolved_country_code_b
            _sync_dtaa_from_country(
                state=state,
                selected_country_code=resolved_country_code_b,
                selected_country_label=selected_country_b,
            )

    lc, rc = st.columns(ratio)
    with lc:
        _label("Currency", indent=1)
    with rc:
        currency_options = ["SELECT"] + CURRENCIES
        current_currency_code = str(form.get("CurrencySecbCode") or "")
        if not current_currency_code:
            seed_short = (
                str(form.get("_ui_currency_short") or "")
                or str(meta.get("source_currency_short") or "")
                or str(extracted.get("currency_short") or "")
            ).strip().upper()
            if seed_short:
                seed_code = _currency_code_from_short(seed_short)
                if seed_code:
                    current_currency_code = seed_code
                    form["CurrencySecbCode"] = seed_code
                    form["_ui_currency_short"] = seed_short
        currency_display = _currency_short_from_code(current_currency_code) or str(form.get("_ui_currency_short") or "SELECT")
        if currency_display not in currency_options:
            currency_display = "SELECT"

        selected_currency = st.selectbox(
            "Currency",
            currency_options,
            index=_selectbox_index_from_value(currency_options, currency_display),
            key=f"{invoice_id}_b_currency",
            label_visibility="collapsed",
        )
        if selected_currency == "OTHERS":
            other_currency = st.text_input(
                "Other currency",
                value=str(form.get("_ui_currency_other") or ""),
                key=f"{invoice_id}_b_currency_other",
                label_visibility="collapsed",
            ).strip().upper()
            form["_ui_currency_other"] = other_currency
            if other_currency:
                resolved_code = _currency_code_from_short(other_currency)
                if resolved_code:
                    form["CurrencySecbCode"] = resolved_code
                    form["_ui_currency_short"] = other_currency
        elif selected_currency != "SELECT":
            resolved_code = _currency_code_from_short(selected_currency)
            if resolved_code:
                form["CurrencySecbCode"] = resolved_code
                form["_ui_currency_short"] = selected_currency
            form["_ui_currency_other"] = ""

    _label("2. Amount payable")

    lc, rc = st.columns(ratio)
    with lc:
        _label("In foreign currency", indent=1)
    with rc:
        form["AmtPayForgnRem"] = st.text_input(
            "In foreign currency",
            value=str(form.get("AmtPayForgnRem") or ""),
            key=f"{invoice_id}_b_amt_fcy",
            label_visibility="collapsed",
        ).strip()
        current_fcy_driver = str(form.get("AmtPayForgnRem") or "").strip()
        current_fx_driver = str(meta.get("exchange_rate") or "").strip()
        last_fcy_driver = str(form.get("_ui_last_amt_fcy_driver") or "").strip()
        last_fx_driver = str(form.get("_ui_last_fx_driver") or "").strip()
        if (current_fcy_driver != last_fcy_driver) or (current_fx_driver != last_fx_driver):
            # Reset INR manual-override when FCY/FX drivers change.
            form["_ui_inr_manual_override"] = "0"
            form["_ui_last_amt_fcy_driver"] = current_fcy_driver
            form["_ui_last_fx_driver"] = current_fx_driver

    lc, rc = st.columns(ratio)
    with lc:
        _label("In Indian Rs", indent=1)
    with rc:
        form["AmtPayIndRem"] = st.text_input(
            "In Indian Rs",
            value=str(form.get("AmtPayIndRem") or ""),
            key=f"{invoice_id}_b_amt_inr",
            label_visibility="collapsed",
        ).strip()
        inr_input_num = _to_float_or_none(form.get("AmtPayIndRem", ""))
        fcy_input_num = _to_float_or_none(form.get("AmtPayForgnRem", ""))
        fx_input_num = _to_float_or_none(str(meta.get("exchange_rate") or ""))
        suggested_inr_num: float | None = None
        if fcy_input_num is not None and fx_input_num is not None:
            suggested_inr_num = float(_round_half_up_int(fcy_input_num * fx_input_num))

        if inr_input_num is None:
            form["_ui_inr_manual_override"] = "0"
        elif suggested_inr_num is None:
            form["_ui_inr_manual_override"] = "1"
        else:
            form["_ui_inr_manual_override"] = "1" if abs(inr_input_num - suggested_inr_num) > 1e-9 else "0"

    bank_names, bank_name_to_code, bank_code_to_name = _bank_maps()
    bank_options = ["SELECT"] + bank_names + ["Other Bank"]
    current_bank_code = str(form.get("NameBankCode") or "")
    current_bank_display = str(form.get("NameBankDisplay") or "")
    selected_bank_display = bank_code_to_name.get(current_bank_code, "Other Bank" if current_bank_display else "SELECT")
    if selected_bank_display not in bank_options:
        selected_bank_display = "Other Bank" if current_bank_display else "SELECT"

    lc, rc = st.columns(ratio)
    with lc:
        _label("3. Name of the bank")
    with rc:
        selected_bank = st.selectbox(
            "Name of the bank",
            bank_options,
            index=_selectbox_index_from_value(bank_options, selected_bank_display),
            key=f"{invoice_id}_b_bank",
            label_visibility="collapsed",
        )
        if selected_bank == "Other Bank":
            manual_bank_name = st.text_input(
                "Other bank",
                value=current_bank_display,
                key=f"{invoice_id}_b_bank_other",
                label_visibility="collapsed",
            ).strip()
            form["NameBankDisplay"] = manual_bank_name
            resolved_code = resolve_bank_code(manual_bank_name) if manual_bank_name else ""
            form["NameBankCode"] = str(resolved_code or "")
        elif selected_bank != "SELECT":
            form["NameBankDisplay"] = selected_bank
            form["NameBankCode"] = str(bank_name_to_code.get(selected_bank, ""))

    lc, rc = st.columns(ratio)
    with lc:
        _label("Branch of the bank", indent=1)
    with rc:
        form["BranchName"] = st.text_input(
            "Branch of the bank",
            value=str(form.get("BranchName") or ""),
            key=f"{invoice_id}_b_branch",
            label_visibility="collapsed",
        ).strip()

    lc, rc = st.columns(ratio)
    with lc:
        _label("4. BSR Code of the bank branch (7 digit)")
    with rc:
        form["BsrCode"] = st.text_input(
            "BSR Code",
            value=str(form.get("BsrCode") or ""),
            key=f"{invoice_id}_b_bsr",
            label_visibility="collapsed",
        ).strip()

    lc, rc = st.columns(ratio)
    with lc:
        _label("5. Proposed date of remittance")
    with rc:
        prop_date = st.date_input(
            "Proposed date of remittance",
            value=_default_prop_date(form),
            key=f"{invoice_id}_b_prop_date",
            label_visibility="collapsed",
            format="DD/MM/YYYY",
        )
        form["PropDateRem"] = prop_date.strftime("%d/%m/%Y")

    nature_rows = [n for n in load_nature_options() if str(n.get("code") or "") != "-1"]
    nature_labels = ["SELECT"] + [f"{n['code']} - {n['label']}" for n in nature_rows]
    current_nature_code = str(form.get("NatureRemCategory") or "")
    if not current_nature_code and extracted.get("nature_of_remittance"):
        extracted_nature = str(extracted.get("nature_of_remittance") or "").strip()
        for row in nature_rows:
            if str(row.get("label") or "").strip().upper() == extracted_nature.upper():
                current_nature_code = str(row.get("code") or "")
                form["NatureRemCategory"] = current_nature_code
                break
    current_nature_label = "SELECT"
    for row in nature_rows:
        if str(row.get("code") or "") == current_nature_code:
            current_nature_label = f"{row['code']} - {row['label']}"
            break

    lc, rc = st.columns(ratio)
    with lc:
        _label("6. Nature of remittance as per agreement/document")
    with rc:
        selected_nature = st.selectbox(
            "Nature of remittance",
            nature_labels,
            index=_selectbox_index_from_value(nature_labels, current_nature_label),
            key=f"{invoice_id}_b_nature",
            label_visibility="collapsed",
        )
        if selected_nature != "SELECT":
            form["NatureRemCategory"] = selected_nature.split(" - ", 1)[0].strip()

    purpose_grouped = load_purpose_grouped()
    purpose_groups = ["SELECT"] + sorted(purpose_grouped.keys())
    current_purpose_code = str(form.get("_purpose_code") or "").strip().upper()
    if not current_purpose_code:
        existing_rev_code = str(form.get("RevPurCode") or "").strip().upper()
        if "-" in existing_rev_code:
            current_purpose_code = existing_rev_code.rsplit("-", 1)[-1].strip().upper()
    if not current_purpose_code:
        current_purpose_code = str(extracted.get("purpose_code") or "").strip().upper()

    current_purpose_group = str(form.get("_purpose_group") or "").strip()
    if not current_purpose_group and current_purpose_code:
        current_purpose_group = _purpose_group_for_code(purpose_grouped, current_purpose_code)
    if current_purpose_group not in purpose_grouped:
        current_purpose_group = ""

    selected_group_default = current_purpose_group if current_purpose_group else "SELECT"
    lc, rc = st.columns(ratio)
    with lc:
        _label("7. Please furnish the relevant purpose code as per RBI")
    with rc:
        selected_group = st.selectbox(
            "Purpose category",
            purpose_groups,
            index=_selectbox_index_from_value(purpose_groups, selected_group_default),
            key=f"{invoice_id}_b_purpose_group",
            label_visibility="collapsed",
        )
        form["_purpose_group"] = selected_group if selected_group != "SELECT" else ""

        group_rows = purpose_grouped.get(selected_group if selected_group != "SELECT" else "", [])
        purpose_row_labels = ["SELECT"] + [f"{row['purpose_code']} - {row['description']}" for row in group_rows]
        code_to_label = {
            str(row.get("purpose_code") or "").strip().upper(): f"{row['purpose_code']} - {row['description']}"
            for row in group_rows
        }
        selected_code_default = code_to_label.get(current_purpose_code, "SELECT")
        selected_code_label = st.selectbox(
            "Specific purpose code",
            purpose_row_labels,
            index=_selectbox_index_from_value(purpose_row_labels, selected_code_default),
            key=f"{invoice_id}_b_purpose_code",
            label_visibility="collapsed",
        )
        if selected_code_label != "SELECT":
            purpose_code = selected_code_label.split(" - ", 1)[0].strip().upper()
            form["_purpose_code"] = purpose_code
            selected_row = next(
                (row for row in group_rows if str(row.get("purpose_code") or "").strip().upper() == purpose_code),
                None,
            )
            if selected_row:
                gr_no_raw = selected_row.get("gr_no")
                gr_no = str(gr_no_raw).strip() if gr_no_raw is not None else "00"
                gr_no_norm = str(int(gr_no)) if gr_no.isdigit() else gr_no
                form["RevPurCategory"] = f"RB-{gr_no_norm}.1"
                form["RevPurCode"] = f"RB-{gr_no_norm}.1-{purpose_code}"
        else:
            form["_purpose_code"] = ""

    gross_options = ["YES", "NO"]
    gross_current_yes = bool(meta.get("is_gross_up", False)) or str(form.get("TaxPayGrossSecb") or "").strip().upper() == "Y"
    gross_display_value = "YES" if gross_current_yes else "NO"
    gross_disabled = not is_tds_mode
    if not is_tds_mode:
        gross_display_value = "NO"
        meta["is_gross_up"] = False
        form["TaxPayGrossSecb"] = "N"

    lc, rc = st.columns(ratio)
    with lc:
        _label("In case the remittance is net of taxes, whether tax payable has been grossed up?")
    with rc:
        gross_selected = st.selectbox(
            "Grossed up?",
            gross_options,
            index=_selectbox_index_from_value(gross_options, gross_display_value),
            key=f"{invoice_id}_b_gross_up",
            disabled=gross_disabled,
            label_visibility="collapsed",
        )
        gross_yes = gross_selected == "YES"
        form["TaxPayGrossSecb"] = "Y" if gross_yes else "N"
        if not gross_disabled:
            meta["is_gross_up"] = gross_yes

    st.divider()

    # Section 8
    preview_form = _safe_preview_form(state)
    st.markdown("**8. Taxability under the provisions of the Income-tax Act (without considering DTAA)**")

    if form.get("RemittanceCharIndia") not in ["Y", "N"]:
        form["RemittanceCharIndia"] = "Y" if is_tds_mode else "N"
    chargeable_yes_no = _yn_to_yes_no(form.get("RemittanceCharIndia"))

    lc, rc = st.columns(ratio)
    with lc:
        _label("(i) Is remittance chargeable to tax in India", indent=1)
    with rc:
        chargeable_selected = st.selectbox(
            "Is remittance chargeable?",
            ["YES", "NO"],
            index=_selectbox_index_from_value(["YES", "NO"], chargeable_yes_no),
            key=f"{invoice_id}_8_chargeable",
            label_visibility="collapsed",
        )
        form["RemittanceCharIndia"] = _yes_no_to_yn(chargeable_selected)

    is_chargeable = str(form.get("RemittanceCharIndia") or "Y").upper() == "Y"

    # Housekeeping: when chargeability flips, clear the stale opposite-side data from form
    # so disabled fields do not retain previous values.
    if is_chargeable:
        # User says income IS chargeable — reason-not is no longer applicable
        form.pop("ReasonNot", None)
    else:
        # User says income is NOT chargeable — clear IT Act override keys so disabled fields
        # do not show computed/stale values that won't appear in the final XML
        for _f in ("AmtIncChrgIt", "TaxLiablIt", "BasisDeterTax", "SecRemCovered"):
            form.pop(f"_ui_override_sec8_{_f}", None)

    lc, rc = st.columns(ratio)
    with lc:
        _label("(ii) If not, reasons thereof", indent=1)
    with rc:
        form["ReasonNot"] = st.text_input(
            "Reason not chargeable",
            value="" if is_chargeable else str(form.get("ReasonNot") or ""),
            key=f"{invoice_id}_8_reason_not",
            disabled=is_chargeable,
            label_visibility="collapsed",
        ).strip()

    _label("(iii) If yes,", indent=1)

    lc, rc = st.columns(ratio)
    with lc:
        _label("(a) The relevant section of the Act under which the remittance is covered", indent=2)
    with rc:
        # When not chargeable, section of Act must be blank (not the default placeholder).
        if is_chargeable:
            fallback_sec = str(preview_form.get("SecRemCovered") or form.get("SecRemCovered") or SEC_REM_COVERED_DEFAULT)
        else:
            fallback_sec = ""
        if "_ui_override_sec8_SecRemCovered" in form:
            val_sec = str(form["_ui_override_sec8_SecRemCovered"]) if is_chargeable else ""
        else:
            val_sec = fallback_sec
        new_sec = st.text_input(
            "Relevant section of Act",
            value=val_sec,
            key=f"{invoice_id}_8_section",
            disabled=not is_chargeable,
            label_visibility="collapsed",
        ).strip()
        if is_chargeable and new_sec != fallback_sec:
            form["_ui_override_sec8_SecRemCovered"] = new_sec
        form["SecRemCovered"] = new_sec
        form["SecRemitCovered"] = new_sec

    lc, rc = st.columns(ratio)
    with lc:
        _label("(b) The amount of income chargeable to tax", indent=2)
    with rc:
        # When not chargeable, force blank (override keys were already popped above).
        fallback_inc = "" if not is_chargeable else str(preview_form.get("AmtIncChrgIt") or form.get("AmtIncChrgIt") or "")
        val_inc = str(form["_ui_override_sec8_AmtIncChrgIt"]) if ("_ui_override_sec8_AmtIncChrgIt" in form and is_chargeable) else fallback_inc
        new_inc = st.text_input(
            "Amount chargeable to tax",
            value=val_inc,
            key=f"{invoice_id}_8_amt_inc",
            disabled=not is_chargeable,
            label_visibility="collapsed",
        ).strip()
        if is_chargeable and new_inc != fallback_inc:
            form["_ui_override_sec8_AmtIncChrgIt"] = new_inc
        form["AmtIncChrgIt"] = new_inc if is_chargeable else ""

    lc, rc = st.columns(ratio)
    with lc:
        _label("(c) The tax liability", indent=2)
    with rc:
        fallback_tax = "" if not is_chargeable else str(preview_form.get("TaxLiablIt") or form.get("TaxLiablIt") or "")
        val_tax = str(form["_ui_override_sec8_TaxLiablIt"]) if ("_ui_override_sec8_TaxLiablIt" in form and is_chargeable) else fallback_tax
        new_tax = st.text_input(
            "Tax liability",
            value=val_tax,
            key=f"{invoice_id}_8_tax_liab",
            disabled=not is_chargeable,
            label_visibility="collapsed",
        ).strip()
        if is_chargeable and new_tax != fallback_tax:
            form["_ui_override_sec8_TaxLiablIt"] = new_tax
        form["TaxLiablIt"] = new_tax if is_chargeable else ""

    lc, rc = st.columns(ratio)
    with lc:
        _label("(d) Basis of determining taxable income and tax liability", indent=2)
    with rc:
        fallback_basis = "" if not is_chargeable else str(preview_form.get("BasisDeterTax") or form.get("BasisDeterTax") or "")
        val_basis = str(form["_ui_override_sec8_BasisDeterTax"]) if ("_ui_override_sec8_BasisDeterTax" in form and is_chargeable) else fallback_basis
        new_basis = st.text_area(
            "Basis",
            value=val_basis,
            key=f"{invoice_id}_8_basis",
            disabled=not is_chargeable,
            label_visibility="collapsed",
            height=80,
        ).strip()
        if is_chargeable and new_basis != fallback_basis:
            form["_ui_override_sec8_BasisDeterTax"] = new_basis
        form["BasisDeterTax"] = new_basis if is_chargeable else ""

    st.divider()

    # Section 9
    st.markdown("**9. If income is chargeable to tax in India and any relief is claimed under DTAA**")
    other_rem_flag = str(form.get("OtherRemDtaa") or ("N" if is_tds_mode else "Y")).strip().upper()
    dtaa_style_active = other_rem_flag == "N"

    trc_current = _yn_to_yes_no(form.get("TaxResidCert", ""))
    trc_ui_current = str(form.get("_ui_only_trc") or trc_current).strip().upper()
    if trc_ui_current not in {"YES", "NO"}:
        trc_ui_current = "Select"
    lc, rc = st.columns(ratio)
    with lc:
        _label("(i) Whether tax residency certificate is obtained from the recipient of remittance", indent=1)
    with rc:
        selected_trc = st.selectbox(
            "TRC obtained?",
            ["Select", "YES", "NO"],
            index=_selectbox_index_from_value(["Select", "YES", "NO"], trc_ui_current),
            key=f"{invoice_id}_9_trc",
            label_visibility="collapsed",
        )
        form["_ui_only_trc"] = selected_trc
        if selected_trc == "YES":
            form["TaxResidCert"] = "Y"
        elif selected_trc == "NO":
            form["TaxResidCert"] = "N"

    lc, rc = st.columns(ratio)
    with lc:
        _label("(ii) Please specify relevant DTAA", indent=1)
    with rc:
        fallback_dtaa = str(preview_form.get("RelevantDtaa") or form.get("RelevantDtaa") or "")
        if "_ui_override_sec9_RelevantDtaa" in form:
            val_dtaa = str(form["_ui_override_sec9_RelevantDtaa"])
        else:
            val_dtaa = fallback_dtaa
        # Ensure session_state drives the widget value to avoid Streamlit warnings
        if f"{invoice_id}_9_dtaa" not in st.session_state:
            st.session_state[f"{invoice_id}_9_dtaa"] = val_dtaa
        new_dtaa = st.text_input(
            "Relevant DTAA",
            key=f"{invoice_id}_9_dtaa",
            label_visibility="collapsed",
        ).strip()
        if new_dtaa != fallback_dtaa:
            form["_ui_override_sec9_RelevantDtaa"] = new_dtaa
        form["RelevantDtaa"] = new_dtaa

    lc, rc = st.columns(ratio)
    with lc:
        _label("(iii) Please specify relevant article of DTAA", indent=1)
    with rc:
        fallback_dtaa_art = str(preview_form.get("RelevantArtDtaa") or form.get("RelevantArtDtaa") or "")
        if "_ui_override_sec9_RelevantArtDtaa" in form:
            val_dtaa_art = str(form["_ui_override_sec9_RelevantArtDtaa"])
        else:
            val_dtaa_art = fallback_dtaa_art
        if not dtaa_style_active:
            val_dtaa_art = ""
        if f"{invoice_id}_9_dtaa_article" not in st.session_state:
            st.session_state[f"{invoice_id}_9_dtaa_article"] = val_dtaa_art
        new_dtaa_art = st.text_input(
            "Relevant Article of DTAA",
            key=f"{invoice_id}_9_dtaa_article",
            disabled=(not dtaa_style_active),
            label_visibility="collapsed",
        ).strip()
        if dtaa_style_active and new_dtaa_art != fallback_dtaa_art:
            form["_ui_override_sec9_RelevantArtDtaa"] = new_dtaa_art
        if dtaa_style_active:
            form["RelevantArtDtaa"] = new_dtaa_art
        else:
            form.pop("RelevantArtDtaa", None)

    lc, rc = st.columns(ratio)
    with lc:
        _label("(iv) Taxable income as per DTAA", indent=1)
    with rc:
        fallback_taxinc = str(preview_form.get("TaxIncDtaa") or form.get("TaxIncDtaa") or "")
        if "_ui_override_sec9_TaxIncDtaa" in form:
            val_taxinc = str(form["_ui_override_sec9_TaxIncDtaa"])
        else:
            val_taxinc = fallback_taxinc
        if not dtaa_style_active:
            val_taxinc = ""
        new_taxinc = st.text_input(
            "Taxable income as per DTAA",
            value=val_taxinc,
            key=f"{invoice_id}_9_taxinc",
            disabled=not dtaa_style_active,
            label_visibility="collapsed",
        ).strip()
        if dtaa_style_active and new_taxinc != fallback_taxinc:
            form["_ui_override_sec9_TaxIncDtaa"] = new_taxinc
        if dtaa_style_active:
            form["TaxIncDtaa"] = new_taxinc
        else:
            form.pop("TaxIncDtaa", None)

    lc, rc = st.columns(ratio)
    with lc:
        _label("(v) Tax liability as per DTAA", indent=1)
    with rc:
        fallback_taxliabl = str(preview_form.get("TaxLiablDtaa") or form.get("TaxLiablDtaa") or "")
        if "_ui_override_sec9_TaxLiablDtaa" in form:
            val_taxliabl = str(form["_ui_override_sec9_TaxLiablDtaa"])
        else:
            val_taxliabl = fallback_taxliabl
        if not dtaa_style_active:
            val_taxliabl = ""
        new_taxliabl = st.text_input(
            "Tax liability as per DTAA",
            value=val_taxliabl,
            key=f"{invoice_id}_9_taxliabl",
            disabled=not dtaa_style_active,
            label_visibility="collapsed",
        ).strip()
        if dtaa_style_active and new_taxliabl != fallback_taxliabl:
            form["_ui_override_sec9_TaxLiablDtaa"] = new_taxliabl
        if dtaa_style_active:
            form["TaxLiablDtaa"] = new_taxliabl
        else:
            form.pop("TaxLiablDtaa", None)

    rem_for_roy = _yn_to_yes_no(form.get("RemForRoyFlg", ""))
    if rem_for_roy not in ("YES", "NO"):
        rem_for_roy = "Select"
    lc, rc = st.columns(ratio)
    with lc:
        _label("A. If the remittance is for royalties, fee for technical services, interest, dividend, etc,")
    with rc:
        selected_a = st.selectbox(
            "DTAA A applicable",
            ["Select", "YES", "NO"],
            index=_selectbox_index_from_value(["Select", "YES", "NO"], rem_for_roy),
            key=f"{invoice_id}_9a_applicable",
            label_visibility="collapsed",
        )
        if selected_a == "YES":
            form["RemForRoyFlg"] = "Y"
        elif selected_a == "NO":
            form["RemForRoyFlg"] = "N"

    _label("(not connected with permanent establishment) please indicate", indent=1)
    section_a_enabled = selected_a == "YES"

    lc, rc = st.columns(ratio)
    with lc:
        _label("(a) Article of DTAA", indent=1)
    with rc:
        fallback_arta = str(preview_form.get("ArtDtaa") or form.get("ArtDtaa") or "")
        if not section_a_enabled:
            form.pop("_ui_override_sec9_ArtDtaa", None)
            val_arta = ""
        elif "_ui_override_sec9_ArtDtaa" in form:
            val_arta = str(form["_ui_override_sec9_ArtDtaa"])
        else:
            val_arta = fallback_arta
        key_9a_art = f"{invoice_id}_9a_article"
        if key_9a_art not in st.session_state or not section_a_enabled:
            st.session_state[key_9a_art] = val_arta
        new_arta = st.text_input(
            "Article of DTAA (A)",
            key=key_9a_art,
            disabled=not section_a_enabled,
            label_visibility="collapsed",
        ).strip()
        if section_a_enabled and new_arta != fallback_arta:
            form["_ui_override_sec9_ArtDtaa"] = new_arta
        form["ArtDtaa"] = new_arta if section_a_enabled else ""

    lc, rc = st.columns(ratio)
    with lc:
        _label("(b) Rate of TDS required to be deducted in terms of such article of the applicable DTAA", indent=1)
    with rc:
        fallback_ratea = str(preview_form.get("RateTdsADtaa") or form.get("RateTdsADtaa") or "")
        if not section_a_enabled:
            form.pop("_ui_override_sec9_RateTdsADtaa", None)
            val_ratea = ""
        elif "_ui_override_sec9_RateTdsADtaa" in form:
            val_ratea = str(form["_ui_override_sec9_RateTdsADtaa"])
        else:
            val_ratea = fallback_ratea
        key_9a_rate = f"{invoice_id}_9a_rate"
        if key_9a_rate not in st.session_state or not section_a_enabled:
            st.session_state[key_9a_rate] = val_ratea
        new_ratea = st.text_input(
            "Rate of TDS (DTAA A)",
            key=key_9a_rate,
            disabled=not section_a_enabled,
            label_visibility="collapsed",
        ).strip()
        if section_a_enabled and new_ratea != fallback_ratea:
            form["_ui_override_sec9_RateTdsADtaa"] = new_ratea
        form["RateTdsADtaa"] = new_ratea if section_a_enabled else ""

    # 9B UI-only
    form.setdefault("_ui_only_9b_applicable", "Select")
    form.setdefault("_ui_only_9b_liable", "Select")
    form.setdefault("_ui_only_9b_basis", "")
    form.setdefault("_ui_only_9b_reasons", "")

    ui_9b_applicable = str(form.get("_ui_only_9b_applicable")).strip().upper()
    if ui_9b_applicable not in ("Select", "YES", "NO"):
        ui_9b_applicable = "Select"
        
    ui_9b_liable = str(form.get("_ui_only_9b_liable")).strip().upper()
    if ui_9b_liable not in ("Select", "YES", "NO"):
        ui_9b_liable = "Select"

    lc, rc = st.columns(ratio)
    with lc:
        _label("B. In case the remittance is on account of business income, please indicate")
    with rc:
        form["_ui_only_9b_applicable"] = st.selectbox(
            "B applicable",
            ["Select", "YES", "NO"],
            index=_selectbox_index_from_value(["Select", "YES", "NO"], ui_9b_applicable),
            key=f"{invoice_id}_9b_applicable",
            label_visibility="collapsed",
        )

    b_applicable = form["_ui_only_9b_applicable"] == "YES"
    lc, rc = st.columns(ratio)
    with lc:
        _label("(a) Whether such income is liable to tax in India", indent=1)
    with rc:
        form["_ui_only_9b_liable"] = st.selectbox(
            "Liable in India (B)",
            ["Select", "YES", "NO"],
            index=_selectbox_index_from_value(["Select", "YES", "NO"], ui_9b_liable),
            key=f"{invoice_id}_9b_liable",
            disabled=not b_applicable,
            label_visibility="collapsed",
        )

    b_liable = form["_ui_only_9b_liable"] == "YES"
    b_not_liable = form["_ui_only_9b_liable"] == "NO"
    lc, rc = st.columns(ratio)
    with lc:
        _label("(b) If so, the basis of arriving at the rate of deduction of tax.", indent=1)
    with rc:
        form["_ui_only_9b_basis"] = st.text_input(
            "Basis (B)",
            value=str(form.get("_ui_only_9b_basis") or "") if (b_applicable and b_liable) else "",
            key=f"{invoice_id}_9b_basis",
            disabled=(not b_applicable) or (not b_liable),
            label_visibility="collapsed",
        ).strip()

    lc, rc = st.columns(ratio)
    with lc:
        _label("(c) If not, then please furnish brief reasons thereof, specifying relevant article of DTAA", indent=1)
    with rc:
        form["_ui_only_9b_reasons"] = st.text_input(
            "Reasons (B)",
            value=str(form.get("_ui_only_9b_reasons") or "") if (b_applicable and b_not_liable) else "",
            key=f"{invoice_id}_9b_reasons",
            disabled=(not b_applicable) or (not b_not_liable),
            label_visibility="collapsed",
        ).strip()

    # 9C UI-only
    form.setdefault("_ui_only_9c_applicable", "Select")
    form.setdefault("_ui_only_9c_ltcg", "0")
    form.setdefault("_ui_only_9c_stcg", "0")
    form.setdefault("_ui_only_9c_basis", "")

    ui_9c_applicable = str(form.get("_ui_only_9c_applicable")).strip().upper()
    if ui_9c_applicable not in ("Select", "YES", "NO"):
        ui_9c_applicable = "Select"

    lc, rc = st.columns(ratio)
    with lc:
        _label("C. In case the remittance is on account of capital gains, please indicate")
    with rc:
        form["_ui_only_9c_applicable"] = st.selectbox(
            "C applicable",
            ["Select", "YES", "NO"],
            index=_selectbox_index_from_value(["Select", "YES", "NO"], ui_9c_applicable),
            key=f"{invoice_id}_9c_applicable",
            label_visibility="collapsed",
        )

    c_applicable = form["_ui_only_9c_applicable"] == "YES"
    lc, rc = st.columns(ratio)
    with lc:
        _label("(a) Amount of long-term capital gains", indent=1)
    with rc:
        form["_ui_only_9c_ltcg"] = st.text_input(
            "LTCG",
            value=str(form.get("_ui_only_9c_ltcg") or "0") if c_applicable else "",
            key=f"{invoice_id}_9c_ltcg",
            disabled=not c_applicable,
            label_visibility="collapsed",
        ).strip()

    lc, rc = st.columns(ratio)
    with lc:
        _label("(b) Amount of short-term capital gains", indent=1)
    with rc:
        form["_ui_only_9c_stcg"] = st.text_input(
            "STCG",
            value=str(form.get("_ui_only_9c_stcg") or "0") if c_applicable else "",
            key=f"{invoice_id}_9c_stcg",
            disabled=not c_applicable,
            label_visibility="collapsed",
        ).strip()

    lc, rc = st.columns(ratio)
    with lc:
        _label("(c) Basis of arriving at taxable income", indent=1)
    with rc:
        form["_ui_only_9c_basis"] = st.text_input(
            "Basis (C)",
            value=str(form.get("_ui_only_9c_basis") or "") if c_applicable else "",
            key=f"{invoice_id}_9c_basis",
            disabled=not c_applicable,
            label_visibility="collapsed",
        ).strip()

    # 9D applicable
    form.setdefault("_ui_only_9d_applicable", "Select")
    form.setdefault("_ui_only_9d_taxable", "Select")
    form.setdefault("_ui_only_9d_rate", "")

    ui_9d_applicable = str(form.get("_ui_only_9d_applicable")).strip().upper()
    if ui_9d_applicable not in ("Select", "YES", "NO"):
        # Auto-fill based on canonical key
        if form.get("OtherRemDtaa") == "Y":
             ui_9d_applicable = "YES"
        elif form.get("OtherRemDtaa") == "N":
             ui_9d_applicable = "NO"
        else:
             ui_9d_applicable = "Select"

    # Keep session state in sync to avoid Streamlit warnings.
    if invoice_id:
        _applicable_key = f"{invoice_id}_9d_applicable"
        existing = str(st.session_state.get(_applicable_key) or "").strip().upper()
        if existing in ("Select", "YES", "NO") and existing != ui_9d_applicable:
            del st.session_state[_applicable_key]

    ui_9d_taxable = str(form.get("_ui_only_9d_taxable")).strip().upper()
    if ui_9d_taxable not in ("Select", "YES", "NO"):
        ui_9d_taxable = "Select"

    # Ensure widget default matches session state to avoid Streamlit warnings.
    if invoice_id:
        _taxable_key = f"{invoice_id}_9d_taxable"
        existing = str(st.session_state.get(_taxable_key) or "").strip().upper()
        if existing in ("YES", "NO") and existing != ui_9d_taxable:
            # Remove the stale value so the widget can be created with the desired default.
            del st.session_state[_taxable_key]

    lc, rc = st.columns(ratio)
    with lc:
        _label("D. In case of other remittance not covered by sub-items A, B and C")
    with rc:
        form["_ui_only_9d_applicable"] = st.selectbox(
            "D applicable",
            ["Select", "YES", "NO"],
            index=_selectbox_index_from_value(["Select", "YES", "NO"], ui_9d_applicable),
            key=f"{invoice_id}_9d_applicable",
            label_visibility="collapsed",
        )

    d_applicable = form["_ui_only_9d_applicable"] == "YES"
    if d_applicable:
        form["OtherRemDtaa"] = "Y"
    elif form["_ui_only_9d_applicable"] == "NO":
        form["OtherRemDtaa"] = "N"

    lc, rc = st.columns(ratio)
    with lc:
        _label("(a) Please specify nature of remittance", indent=1)
    with rc:
        fallback_nature = str(preview_form.get("NatureRemDtaa") or form.get("NatureRemDtaa") or "")
        if "_ui_override_sec9_NatureRemDtaa" in form:
            val_nature = str(form["_ui_override_sec9_NatureRemDtaa"])
        else:
            val_nature = fallback_nature
        if not d_applicable:
            val_nature = ""
        new_nature = st.text_input(
            "Nature (D)",
            value=val_nature,
            key=f"{invoice_id}_9d_nature",
            disabled=not d_applicable,
            label_visibility="collapsed",
        ).strip()
        if d_applicable and new_nature != fallback_nature:
            form["_ui_override_sec9_NatureRemDtaa"] = new_nature
        if d_applicable:
            form["NatureRemDtaa"] = new_nature
        else:
            form.pop("NatureRemDtaa", None)

    lc, rc = st.columns(ratio)
    with lc:
        _label("(b) Whether taxable in India as per DTAA", indent=1)
    with rc:
        form["_ui_only_9d_taxable"] = st.selectbox(
            "Taxable (D)",
            ["Select", "YES", "NO"],
            index=_selectbox_index_from_value(["Select", "YES", "NO"], ui_9d_taxable),
            key=f"{invoice_id}_9d_taxable",
            disabled=(not d_applicable),
            label_visibility="collapsed",
        )

    d_taxable = form["_ui_only_9d_taxable"] == "YES"
    d_not_taxable = form["_ui_only_9d_taxable"] == "NO"
    
    lc, rc = st.columns(ratio)
    with lc:
        _label("(c) If yes, rate of TDS required to be deducted in terms of such article of the applicable DTAA", indent=1)

    # Avoid Streamlit warning by ensuring widget default matches session state.
    rate_value = str(form.get("_ui_only_9d_rate") or "") if (d_applicable and d_taxable) else ""
    if invoice_id:
        _rate_key = f"{invoice_id}_9d_rate"
        existing_rate = str(st.session_state.get(_rate_key) or "")
        if existing_rate and existing_rate != rate_value:
            del st.session_state[_rate_key]

    with rc:
        form["_ui_only_9d_rate"] = st.text_input(
            "Rate (D)",
            value=rate_value,
            key=f"{invoice_id}_9d_rate",
            disabled=(not d_applicable) or (not d_taxable),
            label_visibility="collapsed",
        ).strip()

    lc, rc = st.columns(ratio)
    with lc:
        _label("(d) If not, then please furnish brief reasons thereof, specifying relevant article of DTAA", indent=1)
    with rc:
        fallback_reasons = str(preview_form.get("RelArtDetlDDtaa") or form.get("RelArtDetlDDtaa") or "")
        if "_ui_override_sec9_RelArtDetlDDtaa" in form:
            val_reasons = str(form["_ui_override_sec9_RelArtDetlDDtaa"])
        else:
            val_reasons = fallback_reasons
        if not (d_applicable and d_not_taxable):
            val_reasons = ""
        new_reasons = st.text_input(
            "Reasons (D)",
            value=val_reasons,
            key=f"{invoice_id}_9d_reasons",
            disabled=not (d_applicable and d_not_taxable),
            label_visibility="collapsed",
        ).strip()
        if d_applicable and d_not_taxable and new_reasons != fallback_reasons:
            form["_ui_override_sec9_RelArtDetlDDtaa"] = new_reasons
        if d_applicable and d_not_taxable:
            form["RelArtDetlDDtaa"] = new_reasons
        else:
            form.pop("RelArtDetlDDtaa", None)

    st.divider()

    # Sections 10 to 13
    preview_form_after_9 = _safe_preview_form(state)
    _label("10. Amount of TDS")

    # In NON_TDS mode the backend enforces 0 for TDS amounts; discard any stale manual
    # overrides so that switching back to TDS mode starts from a clean state.
    if not is_tds_mode:
        form.pop("_ui_override_sec10_AmtPayForgnTds", None)
        form.pop("_ui_override_sec10_AmtPayIndianTds", None)

    lc, rc = st.columns(ratio)
    with lc:
        _label("In foreign currency", indent=1)
    with rc:
        fallback_amt_fc = str(preview_form_after_9.get("AmtPayForgnTds") or form.get("AmtPayForgnTds") or "")
        # If the computed value changed upstream (e.g. DTAA rate or FCY changed) and the
        # user has no active manual override, sync session_state so the widget shows the
        # new computed value instead of locking in a stale value as a false override.
        key_amt_fc = f"{invoice_id}_10_amt_fc"
        if "_ui_override_sec10_AmtPayForgnTds" not in form:
            if key_amt_fc not in st.session_state or form.get("AmtPayForgnTds") != fallback_amt_fc:
                st.session_state[key_amt_fc] = fallback_amt_fc
        val_amt_fc = str(form.get("_ui_override_sec10_AmtPayForgnTds", fallback_amt_fc))
        new_amt_fc = st.text_input(
            "TDS in foreign currency",
            key=key_amt_fc,
            disabled=not is_tds_mode,
            label_visibility="collapsed",
        ).strip()
        if is_tds_mode and new_amt_fc != fallback_amt_fc:
            form["_ui_override_sec10_AmtPayForgnTds"] = new_amt_fc
        form["AmtPayForgnTds"] = new_amt_fc

    lc, rc = st.columns(ratio)
    with lc:
        _label("In Indian Rs", indent=1)
    with rc:
        fallback_amt_inr = str(preview_form_after_9.get("AmtPayIndianTds") or form.get("AmtPayIndianTds") or "")
        key_amt_inr = f"{invoice_id}_10_amt_inr"
        if "_ui_override_sec10_AmtPayIndianTds" not in form:
            if key_amt_inr not in st.session_state or form.get("AmtPayIndianTds") != fallback_amt_inr:
                st.session_state[key_amt_inr] = fallback_amt_inr
        val_amt_inr = str(form.get("_ui_override_sec10_AmtPayIndianTds", fallback_amt_inr))
        new_amt_inr = st.text_input(
            "TDS in INR",
            key=key_amt_inr,
            disabled=not is_tds_mode,
            label_visibility="collapsed",
        ).strip()
        if is_tds_mode and new_amt_inr != fallback_amt_inr:
            form["_ui_override_sec10_AmtPayIndianTds"] = new_amt_inr
        form["AmtPayIndianTds"] = new_amt_inr

    rate_type_options = [
        "AS PER INCOME-TAX ACT",
        "AS PER DTAA",
        "LOWER DEDUCTION CERTIFICATE",
    ]
    current_flag = str(form.get("RateTdsSecbFlg") or "").strip()
    if current_flag == "2":
        current_rate_type = "AS PER DTAA"
    elif current_flag == "3":
        current_rate_type = "LOWER DEDUCTION CERTIFICATE"
    else:
        current_rate_type = "AS PER INCOME-TAX ACT"
    lc, rc = st.columns(ratio)
    with lc:
        _label("11. Rate of TDS")
    with rc:
        if is_tds_mode:
            selected_rate_type = st.selectbox(
                "Rate of TDS type",
                rate_type_options,
                index=_selectbox_index_from_value(rate_type_options, current_rate_type),
                key=f"{invoice_id}_11_rate_type",
                label_visibility="collapsed",
            )
            if selected_rate_type == "AS PER DTAA":
                form["RateTdsSecbFlg"] = "2"
            elif selected_rate_type == "LOWER DEDUCTION CERTIFICATE":
                form["RateTdsSecbFlg"] = "3"
            else:
                form["RateTdsSecbFlg"] = "1"
        else:
            form["RateTdsSecbFlg"] = ""
            st.text_input(
                "Rate of TDS type",
                value="",
                key=f"{invoice_id}_11_rate_type_disabled",
                disabled=True,
                label_visibility="collapsed",
            )

        # In NON_TDS mode discard stale rate override so it does not survive a mode switch.
        if not is_tds_mode:
            form.pop("_ui_override_sec11_RateTdsSecB", None)
        fallback_rate = str(preview_form_after_9.get("RateTdsSecB") or form.get("RateTdsSecB") or "")
        key_rate = f"{invoice_id}_11_rate"
        if "_ui_override_sec11_RateTdsSecB" not in form:
            if key_rate not in st.session_state or form.get("RateTdsSecB") != fallback_rate:
                st.session_state[key_rate] = fallback_rate
        val_rate = str(form.get("_ui_override_sec11_RateTdsSecB", fallback_rate))
        new_rate = st.text_input(
            "Rate of TDS value",
            key=key_rate,
            disabled=not is_tds_mode,
            label_visibility="collapsed",
        ).strip()
        if is_tds_mode and new_rate != fallback_rate:
            form["_ui_override_sec11_RateTdsSecB"] = new_rate
        form["RateTdsSecB"] = new_rate

    lc, rc = st.columns(ratio)
    with lc:
        _label("12. Actual amount of remittance after TDS (In foreign currency)")
    with rc:
        # In NON_TDS the actual remittance always equals the full FCY amount (no withholding).
        # Clear stale overrides so the backend-computed value is always shown.
        if not is_tds_mode:
            form.pop("_ui_override_sec12_ActlAmtTdsForgn", None)
        fallback_actl = str(preview_form_after_9.get("ActlAmtTdsForgn") or form.get("ActlAmtTdsForgn") or "")
        key_actl = f"{invoice_id}_12_actl_remit"
        if "_ui_override_sec12_ActlAmtTdsForgn" not in form:
            if key_actl not in st.session_state or form.get("ActlAmtTdsForgn") != fallback_actl:
                st.session_state[key_actl] = fallback_actl
        val_actl = str(form.get("_ui_override_sec12_ActlAmtTdsForgn", fallback_actl))
        new_actl = st.text_input(
            "Actual remittance after TDS",
            key=key_actl,
            disabled=not is_tds_mode,
            label_visibility="collapsed",
        ).strip()
        if is_tds_mode and new_actl != fallback_actl:
            form["_ui_override_sec12_ActlAmtTdsForgn"] = new_actl
        form["ActlAmtTdsForgn"] = new_actl

    lc, rc = st.columns(ratio)
    with lc:
        _label("13. Date of deduction of tax at source, if any")
    with rc:
        # In NON_TDS the deduction date is stripped from the final XML entirely.
        # Clear any stale override and show a blank/disabled field to avoid false values.
        if not is_tds_mode:
            form.pop("_ui_override_sec13_DednDateTds", None)
            form["DednDateTds"] = ""

        _parsed = _parse_iso_date(str(form.get("DednDateTds") or ""))
        if is_tds_mode and _parsed is None and str(form.get("DednDateTds") or "").strip():
            st.warning("Existing deduction date is invalid. Expected DD/MM/YYYY.")

        if is_tds_mode:
            base_date_val = str(form.get("DednDateTds") or _default_dedn_date(form, meta).strftime("%d/%m/%Y"))
        else:
            base_date_val = ""

        fallback_date = str(preview_form_after_9.get("DednDateTds") or base_date_val) if is_tds_mode else ""
        key_dedn_date = f"{invoice_id}_13_dedn_date"
        if "_ui_override_sec13_DednDateTds" not in form:
            if key_dedn_date not in st.session_state or form.get("DednDateTds") != fallback_date:
                st.session_state[key_dedn_date] = fallback_date
        val_date = str(form.get("_ui_override_sec13_DednDateTds", fallback_date))

        new_date = st.text_input(
            "Date of deduction",
            key=key_dedn_date,
            disabled=not is_tds_mode,
            label_visibility="collapsed",
            placeholder="DD/MM/YYYY" if is_tds_mode else "",
        ).strip()

        if is_tds_mode and new_date != fallback_date:
            form["_ui_override_sec13_DednDateTds"] = new_date
        form["DednDateTds"] = new_date

    st.divider()

    # Accountant details
    _seed_accountant_defaults(form)
    st.subheader("Accountant Details")

    lc, rc = st.columns(ratio)
    with lc:
        _label("Accountant Name")
    with rc:
        form["NameAcctnt"] = st.text_input(
            "Accountant Name",
            value=str(form.get("NameAcctnt") or ""),
            key=f"{invoice_id}_acct_name",
            label_visibility="collapsed",
        ).strip()

    lc, rc = st.columns(ratio)
    with lc:
        _label("Name of the proprietorship / firm")
    with rc:
        current_firm_name = str(form.get("NameFirmAcctnt") or "")
        firm_options = list(dict.fromkeys(list(CA_FIRM_OPTIONS) + ["OTHER / MANUAL"]))
        if current_firm_name and current_firm_name not in CA_FIRM_OPTIONS:
            selected_firm = "OTHER / MANUAL"
        elif current_firm_name in CA_FIRM_OPTIONS:
            selected_firm = current_firm_name
        else:
            selected_firm = firm_options[0] if firm_options else "OTHER / MANUAL"

        chosen_firm = st.selectbox(
            "Name of the proprietorship / firm",
            firm_options,
            index=_selectbox_index_from_value(firm_options, selected_firm),
            key=f"{invoice_id}_acct_firm_select",
            label_visibility="collapsed",
        )
        if chosen_firm == "OTHER / MANUAL":
            form["NameFirmAcctnt"] = st.text_input(
                "Firm name (manual)",
                value=current_firm_name if selected_firm == "OTHER / MANUAL" else "",
                key=f"{invoice_id}_acct_firm_manual",
                label_visibility="collapsed",
            ).strip()
        else:
            form["NameFirmAcctnt"] = chosen_firm

    lc, rc = st.columns(ratio)
    with lc:
        _label("Address")
    with rc:
        row1c1, row1c2, row1c3 = st.columns(3)
        with row1c1:
            form["AcctntFlatDoorBuilding"] = st.text_input(
                "Flat / Door / Building",
                value=str(form.get("AcctntFlatDoorBuilding") or ""),
                key=f"{invoice_id}_acct_flat",
                label_visibility="collapsed",
                placeholder="Flat / Door / Building",
            ).strip()
        with row1c2:
            form["PremisesBuildingVillage"] = st.text_input(
                "Premises / Building / Village",
                value=str(form.get("PremisesBuildingVillage") or ""),
                key=f"{invoice_id}_acct_premises",
                label_visibility="collapsed",
                placeholder="Premises / Building / Village",
            ).strip()
        with row1c3:
            form["AcctntRoadStreet"] = st.text_input(
                "Road / Street",
                value=str(form.get("AcctntRoadStreet") or ""),
                key=f"{invoice_id}_acct_road",
                label_visibility="collapsed",
                placeholder="Road / Street",
            ).strip()

        row2c1, row2c2, row2c3 = st.columns(3)
        with row2c1:
            form["AcctntAreaLocality"] = st.text_input(
                "Area / Locality",
                value=str(form.get("AcctntAreaLocality") or ""),
                key=f"{invoice_id}_acct_area",
                label_visibility="collapsed",
                placeholder="Area / Locality",
            ).strip()
        with row2c2:
            form["AcctntTownCityDistrict"] = st.text_input(
                "Town / City / District",
                value=str(form.get("AcctntTownCityDistrict") or ""),
                key=f"{invoice_id}_acct_city",
                label_visibility="collapsed",
                placeholder="Town / City / District",
            ).strip()
        with row2c3:
            acct_state_options = ["SELECT"] + INDIAN_STATES_AND_UTS + ["OTHER / MANUAL"]
            acct_state_raw = str(form.get("AcctntState") or "")
            acct_state_display = _accountant_state_display_from_value(acct_state_raw)
            selected_acct_state = st.selectbox(
                "Accountant state",
                acct_state_options,
                index=_selectbox_index_from_value(acct_state_options, acct_state_display),
                key=f"{invoice_id}_acct_state",
                label_visibility="collapsed",
            )
            if selected_acct_state == "OTHER / MANUAL":
                form["AcctntState"] = st.text_input(
                    "State (manual)",
                    value=acct_state_raw,
                    key=f"{invoice_id}_acct_state_manual",
                    label_visibility="collapsed",
                ).strip()
            elif selected_acct_state != "SELECT":
                code_map = _accountant_state_code_map()
                form["AcctntState"] = str(code_map.get(selected_acct_state.strip().upper(), selected_acct_state))

        row3c1, row3c2 = st.columns(2)
        with row3c1:
            acct_country_options = ["SELECT"] + COUNTRIES
            acct_country_code = str(form.get("AcctntCountryCode") or "")
            acct_country_display = _country_label_from_code(acct_country_code)
            selected_acct_country = st.selectbox(
                "Accountant country",
                acct_country_options,
                index=_selectbox_index_from_value(acct_country_options, acct_country_display),
                key=f"{invoice_id}_acct_country",
                label_visibility="collapsed",
            )
            if selected_acct_country == "OTHERS":
                acct_country_other = st.text_input(
                    "Accountant country (manual)",
                    value=str(form.get("_ui_only_acctnt_country_other") or ""),
                    key=f"{invoice_id}_acct_country_other",
                    label_visibility="collapsed",
                ).strip().upper()
                form["_ui_only_acctnt_country_other"] = acct_country_other
                if acct_country_other:
                    mapped_code = _country_code_from_label(acct_country_other)
                    if mapped_code:
                        form["AcctntCountryCode"] = mapped_code
                    elif acct_country_other.isdigit():
                        form["AcctntCountryCode"] = acct_country_other
            elif selected_acct_country != "SELECT":
                mapped_code = _country_code_from_label(selected_acct_country)
                if mapped_code:
                    form["AcctntCountryCode"] = mapped_code
                form["_ui_only_acctnt_country_other"] = ""

        with row3c2:
            form["AcctntPincode"] = st.text_input(
                "PIN Code",
                value=str(form.get("AcctntPincode") or ""),
                key=f"{invoice_id}_acct_pin",
                label_visibility="collapsed",
                placeholder="PIN Code",
            ).strip()

    lc, rc = st.columns(ratio)
    with lc:
        _label("Membership No")
    with rc:
        form["MembershipNumber"] = st.text_input(
            "Membership No",
            value=str(form.get("MembershipNumber") or ""),
            key=f"{invoice_id}_acct_membership",
            label_visibility="collapsed",
        ).strip()

    lc, rc = st.columns(ratio)
    with lc:
        _label("Registration No")
    with rc:
        form["_ui_only_registration_no"] = st.text_input(
            "Registration No",
            value=str(form.get("_ui_only_registration_no") or ""),
            key=f"{invoice_id}_acct_registration_no",
            label_visibility="collapsed",
        ).strip()

    return state
