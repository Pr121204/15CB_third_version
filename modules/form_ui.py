from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import re
import streamlit as st

from config.settings import PROPOSED_DATE_OFFSET
from modules.currency_mapping import (
    load_currency_exact_index,
    preselect_currency_code,
    validate_short_code_targets,
)
from modules.master_data import (
    find_bank_by_name,
    find_dtaa,
    find_foreign_company,
    find_indian_company,
    find_nature_row,
    load_master,
    validate_bsr_code,
    validate_dtaa_rate,
    validate_pan,
)
from modules.logger import get_logger
from modules.master_lookups import (
    allowed_codes_for_groups,
    allowed_groups_for_nature,
    build_purpose_indexes,
    resolve_nature_mapping,
)


LOOKUP_DIR = Path(__file__).resolve().parent.parent / "lookups"
MASTER_DIR = Path(__file__).resolve().parent.parent / "data" / "master"
logger = get_logger()


def _load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf8") as f:
            return json.load(f)
    except Exception:
        return default


def _parse_date(raw: str) -> Optional[date]:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _format_dd_mmm_yyyy(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def _float_or_none(raw: str) -> Optional[float]:
    try:
        return float((raw or "").strip())
    except Exception:
        return None


def _yes_no_to_yn(v: str) -> str:
    return "Y" if v == "YES" else "N"


def _yn_to_yes_no(v: str) -> str:
    return "YES" if str(v or "").upper() in {"Y", "YES"} else "NO"


def yn(value: str) -> bool:
    return str(value or "").strip().upper() == "YES"


def _get_lookup_options() -> Dict[str, object]:
    country_map = _load_json(LOOKUP_DIR / "country_codes.json", {})
    bank_map = _load_json(LOOKUP_DIR / "bank_codes.json", {})
    currency_map = load_currency_exact_index()
    currency_short_resolved, currency_short_missing = validate_short_code_targets(currency_map)
    state_map = _load_json(LOOKUP_DIR / "state_codes.json", {})
    purpose_map = _load_json(LOOKUP_DIR / "purpose_codes.json", {})
    # Source of truth for RBI purpose dropdowns must be master Purpose_code_List.json.
    # Keep lookup copy only as fallback.
    purpose_list = _load_json(MASTER_DIR / "Purpose_code_List.json", None)
    if not purpose_list:
        purpose_list = _load_json(LOOKUP_DIR / "purpose_code_list.json", {"purpose_codes": []})

    grouped: Dict[str, List[Dict[str, str]]] = {}
    for row in purpose_list.get("purpose_codes", []):
        if not isinstance(row, dict):
            continue
        group_name = str(row.get("group_name") or "").strip()
        code = str(row.get("purpose_code") or "").strip().upper()
        desc = " ".join(str(row.get("description") or "").split())
        if group_name and code:
            grouped.setdefault(group_name, []).append({"code": code, "description": desc})

    for group_rows in grouped.values():
        group_rows.sort(key=lambda x: x["code"])

    return {
        "country_map": country_map,
        "bank_map": bank_map,
        "currency_map": currency_map,
        "currency_short_resolved": currency_short_resolved,
        "currency_short_missing": currency_short_missing,
        "state_map": state_map,
        "purpose_map": {str(k).lower(): str(v) for k, v in purpose_map.items()},
        "purpose_grouped": grouped,
    }


def _ensure_state_defaults() -> None:
    fields = st.session_state.setdefault("extracted_fields", {})
    fixed_defaults = {
        "SWVersionNo": "1",
        "SWCreatedBy": "DIT-EFILING-JAVA",
        "XMLCreatedBy": "DIT-EFILING-JAVA",
        "XMLCreationDate": datetime.now().strftime("%Y-%m-%d"),
        "IntermediaryCity": "Delhi",
        "FormName": "FORM15CB",
        "Description": "FORM15CB",
        "AssessmentYear": "2025",
        "SchemaVer": "Ver1.1",
        "FormVer": "1",
        "IorWe": "02",
        "RemitterHonorific": "03",
        "BeneficiaryHonorific": "03",
        "DeclarantPronoun": "We",
        "RemitterTitle": "M/s",
        "BeneficiaryTitle": "M/s",
        "ProposedDate": (date.today() + timedelta(days=15)).strftime("%d/%m/%Y"),
    }
    for k, v in fixed_defaults.items():
        fields.setdefault(k, v)


def _on_remitter_change() -> None:
    fields = st.session_state["extracted_fields"]
    remitter_name = st.session_state.get("ui_name_remitter", "").strip()
    fields["NameRemitter"] = remitter_name
    rec = find_indian_company(remitter_name)
    if rec:
        pan = str(rec.get("pan") or "").strip().upper()
        if pan:
            fields["RemitterPAN"] = pan


def _on_beneficiary_change() -> None:
    fields = st.session_state["extracted_fields"]
    beneficiary_name = st.session_state.get("ui_name_remittee", "").strip()
    fields["NameRemittee"] = beneficiary_name
    rec = find_foreign_company(beneficiary_name)
    if rec and rec.get("name"):
        fields["NameRemittee"] = str(rec.get("name")).strip()
    country_hint = fields.get("RemitteeTownCityDistrict") or fields.get("RelevantDtaa") or ""
    dtaa = find_dtaa(country_hint)
    if dtaa:
        country = str(dtaa.get("country") or "").strip()
        article = str(dtaa.get("article") or "").strip()
        rate = dtaa.get("rate")
        if country:
            fields["RelevantDtaa"] = country
        if article:
            fields["RelevantArtDtaa"] = article
        if rate is not None:
            try:
                fields["RateTdsADtaa"] = str(round(float(rate) * 100, 2)).rstrip("0").rstrip(".")
            except Exception:
                pass


def _on_bank_change() -> None:
    fields = st.session_state["extracted_fields"]
    bank_name = st.session_state.get("ui_bank_name", "").strip()
    fields["NameBankCode"] = bank_name
    party_name = fields.get("NameRemitter", "")
    rec = find_bank_by_name(bank_name, party_name)
    if rec:
        bsr = str(rec.get("bsr_code") or "").strip()
        branch = str(rec.get("branch") or "").strip()
        if bsr:
            fields["BsrCode"] = "".join(ch for ch in bsr if ch.isdigit())
        if branch:
            fields["BranchName"] = branch


def _nature_to_groups(master: Dict[str, object]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for row in master.get("nature_map", []):
        if not isinstance(row, dict):
            continue
        nature = str(row.get("agreement_nature") or "").strip()
        group = str(row.get("service_category") or "").strip()
        if nature and group:
            out.setdefault(nature, [])
            if group not in out[nature]:
                out[nature].append(group)
    return out


def _reset_dtaa_fields(fields: Dict[str, str]) -> None:
    for key, default in {
        "TaxResidCert": "N",
        "RelevantDtaa": "",
        "RelevantArtDtaa": "",
        "TaxIncDtaa": "",
        "TaxLiablDtaa": "",
        "RemForRoyFlg": "N",
        "ArtDtaa": "",
        "RateTdsADtaa": "",
        "RemAcctBusIncFlg": "N",
        "IncLiabIndiaFlg": "N",
        "RemOnCapGainFlg": "N",
        "OtherRemDtaa": "N",
        "RelArtDetlDDtaa": "",
        "_inc_liab_india_detail": "",
    }.items():
        fields[key] = default


def render_form() -> Dict[str, str]:
    _ensure_state_defaults()
    fields = st.session_state["extracted_fields"]
    master = load_master()
    lookups = _get_lookup_options()
    nature_groups = _nature_to_groups(master)
    if lookups.get("currency_short_missing"):
        missing = lookups["currency_short_missing"]
        details = ", ".join(f"{k}->{v}" for k, v in sorted(missing.items()))
        st.error(
            "Currency short-code mapping configuration is incomplete in data/master/currency_codes.json: "
            f"{details}. Affected invoices require manual currency selection."
        )

    st.subheader("Step 3: Review in Structured Form")

    st.markdown("#### 1. Remitter and Beneficiary")
    st.markdown(
        """
<div class="f15cb-header-main">
  FORM NO. 15CB
</div>
<div class="f15cb-header-sub">
  (See rule 37BB)
</div>
<div class="f15cb-header-title">
  Certificate of an accountant
</div>
""",
        unsafe_allow_html=True,
    )

    row1 = st.columns([1.0, 3.8, 1.2, 2.7, 0.2])
    with row1[0]:
        # Fixed as per spec: "I/We" must be "We"
        st.text_input("I / We", value="We", disabled=True, label_visibility="collapsed", key="ui_iorwe_fixed")
    with row1[1]:
        st.markdown(
            "<div class='f15cb-line-text'>* have examined the agreement (wherever applicable) between</div>",
            unsafe_allow_html=True,
        )
    with row1[2]:
        st.text_input(
            "Remitter honorific",
            value="M/s",
            disabled=True,
            label_visibility="collapsed",
            key="ui_remitter_honorific_fixed",
        )
    with row1[3]:
        st.text_input(
            "Name of the Remitter",
            key="ui_name_remitter",
            value=fields.get("NameRemitter", ""),
            placeholder="Name of the Remitter",
            on_change=_on_remitter_change,
            label_visibility="collapsed",
        )
    with row1[4]:
        st.markdown("<div class='f15cb-line-text'>*</div>", unsafe_allow_html=True)

    row2 = st.columns([1.4, 1.2, 0.45, 1.2, 2.7, 4.2])
    with row2[0]:
        st.markdown("<div class='f15cb-line-text'>with PAN/TAN</div>", unsafe_allow_html=True)
    with row2[1]:
        # Make Remitter PAN editable so CA can override pre-filled PANs
        st.text_input(
            "Remitter PAN/TAN",
            value=fields.get("RemitterPAN", ""),
            disabled=False,
            label_visibility="collapsed",
        )
    with row2[2]:
        st.markdown("<div class='f15cb-line-text'>and</div>", unsafe_allow_html=True)
    with row2[3]:
        st.text_input(
            "Beneficiary honorific",
            value="M/s",
            disabled=True,
            label_visibility="collapsed",
            key="ui_beneficiary_honorific_fixed",
        )
    with row2[4]:
        st.text_input(
            "Name of the Beneficiary",
            key="ui_name_remittee",
            value=fields.get("NameRemittee", ""),
            placeholder="Name of the Beneficiary",
            on_change=_on_beneficiary_change,
            label_visibility="collapsed",
        )
    with row2[5]:
        st.markdown(
            "<div class='f15cb-line-text'>requiring the above remittance as well as the relevant documents and books of account required for ascertaining the nature of remittance and for determining the rate of deduction of tax at source as per provisions of Charter- XVII-B. We hereby certify the following.</div>",
            unsafe_allow_html=True,
        )

    fields["IorWe"] = "02"
    fields["RemitterHonorific"] = "03"
    fields["BeneficiaryHonorific"] = "03"
    fields["NameRemitter"] = st.session_state.get("ui_name_remitter", fields.get("NameRemitter", ""))
    fields["NameRemittee"] = st.session_state.get("ui_name_remittee", fields.get("NameRemittee", ""))
    if fields.get("NameRemitter", "").strip() and not fields.get("RemitterPAN", "").strip():
        rec = find_indian_company(fields["NameRemitter"])
        if rec and rec.get("pan"):
            fields["RemitterPAN"] = str(rec.get("pan")).strip().upper()
    pan = fields.get("RemitterPAN", "")
    if pan:
        st.caption("Valid PAN format" if validate_pan(pan) else "Invalid PAN format: expected AAAAA9999A")

    st.markdown("#### 2. Remittee Details")
    country_map = lookups["country_map"]
    country_items: List[Tuple[str, str]] = sorted(
        [(name.title(), code) for name, code in country_map.items()],
        key=lambda x: x[0],
    )
    country_labels = [f"{label} ({code})" for label, code in country_items] + ["OTHERS"]
    current_country = fields.get("RemitteeCountryCode", "")
    default_country_idx = 0
    for idx, (_, code) in enumerate(country_items):
        if code == current_country:
            default_country_idx = idx
            break
    col1, col2, col3 = st.columns(3)
    with col1:
        fields["RemitteeFlatDoorBuilding"] = st.text_input(
            "Flat / Door / Building",
            value=fields.get("RemitteeFlatDoorBuilding", ""),
        )
        fields["RemitteeAreaLocality"] = st.text_input(
            "Area / Locality",
            value=fields.get("RemitteeAreaLocality", ""),
        )
    with col2:
        fields["RemitteeTownCityDistrict"] = st.text_input(
            "Town / City / District",
            value=fields.get("RemitteeTownCityDistrict", ""),
        )
        fields["RemitteeZipCode"] = st.text_input("Zip Code", value=fields.get("RemitteeZipCode", ""))
    with col3:
        fields["RemitteeState"] = st.text_input("State", value=fields.get("RemitteeState", ""))
        sel_country = st.selectbox("Country", country_labels, index=default_country_idx)
        if sel_country == "OTHERS":
            fields["RemitteeCountryCode"] = st.text_input("Country Code (manual)", value=current_country)
        else:
            fields["RemitteeCountryCode"] = sel_country.split("(")[-1].replace(")", "").strip()

    st.markdown("#### 3. Bank and Remittance Details")
    bank_map = lookups["bank_map"]
    bank_names = sorted({str(k).title() for k in bank_map.keys()}) + ["Other Bank"]
    bank_default_idx = bank_names.index("Other Bank")
    if fields.get("NameBankCode"):
        for i, bn in enumerate(bank_names):
            if bn.lower() == fields["NameBankCode"].lower():
                bank_default_idx = i
                break
    rem_col1, rem_col2, rem_col3 = st.columns(3)
    with rem_col1:
        chosen_bank = st.selectbox("Name of Bank", bank_names, index=bank_default_idx, key="ui_bank_name", on_change=_on_bank_change)
        if chosen_bank == "Other Bank":
            fields["NameBankCode"] = st.text_input("Bank (manual)", value=fields.get("NameBankCode", ""))
        else:
            fields["NameBankCode"] = chosen_bank
        # Allow BSR editing so users can fix or override detected values
        fields["BsrCode"] = st.text_input("BSR Code", value=fields.get("BsrCode", ""), disabled=False)
        if fields["BsrCode"]:
            st.caption("Valid BSR code" if validate_bsr_code(fields["BsrCode"]) else "BSR should be exactly 7 digits")
        fields["BranchName"] = st.text_input("Branch Name", value=fields.get("BranchName", ""))

    with rem_col2:
        purpose_by_group, purpose_by_code = build_purpose_indexes()
        
        nature_options = sorted(nature_groups.keys())
        nature_guess = fields.get("NatureRemCategory", "")
        
        # Pre-select from Gemini suggestion if available
        if not nature_guess and fields.get("_gemini_nature_of_remittance"):
            gemini_nature = fields.get("_gemini_nature_of_remittance", "")
            if gemini_nature in nature_options:
                nature_guess = gemini_nature
        
        nature_idx = nature_options.index(nature_guess) if nature_guess in nature_options else 0
        selected_nature = st.selectbox("Nature of Remittance", nature_options or [""], index=nature_idx if nature_options else 0)
        
        # Reset invalid downstream values on nature change
        prev_nature = st.session_state.get("_prev_nature", "")
        if prev_nature != selected_nature:
            fields["_purpose_group_name"] = ""
            fields["_purpose_s_code"] = ""
            fields["RevPurCode"] = ""
            st.session_state["_prev_nature"] = selected_nature

        fields["NatureRemCategory"] = selected_nature
        mapping = resolve_nature_mapping(selected_nature, purpose_by_code)
        allowed_groups = allowed_groups_for_nature(mapping, purpose_by_group, purpose_by_code)

        current_group = fields.get("_purpose_group_name", "")
        if current_group not in allowed_groups:
            current_group = allowed_groups[0] if allowed_groups else ""
        
        if len(allowed_groups) == 1:
            selected_group = allowed_groups[0]
            st.text_input("Purpose Group Name", value=selected_group, disabled=True)
        else:
            group_idx = allowed_groups.index(current_group) if current_group in allowed_groups else 0
            selected_group = st.selectbox("Purpose Group Name", allowed_groups or [""], index=group_idx if allowed_groups else 0)
        
        fields["_purpose_group_name"] = selected_group

        allowed_rows = allowed_codes_for_groups([selected_group] if selected_group else [], purpose_by_group)
        code_labels = [f"{r.get('purpose_code')} - {r.get('description')}" for r in allowed_rows]
        code_value_to_label = {str(r.get('purpose_code')): f"{r.get('purpose_code')} - {r.get('description')}" for r in allowed_rows}
        
        current_code = fields.get("_purpose_s_code", "")
        mapped_code = mapping.get("mapped_purpose_code", "")
        
        if mapped_code and mapped_code in code_value_to_label:
            current_code = mapped_code
        elif current_code not in code_value_to_label:
            current_code = ""

        if len(code_labels) == 1:
            selected_code_label = code_labels[0]
            st.text_input("Purpose Code - Description", value=selected_code_label, disabled=True)
        else:
            code_idx = 0
            if current_code in code_value_to_label:
                code_label = code_value_to_label[current_code]
                if code_label in code_labels:
                    code_idx = code_labels.index(code_label)
            
            selected_code_label = st.selectbox(
                "Purpose Code - Description",
                code_labels or [""],
                index=code_idx if code_labels else 0,
            )
        
        selected_s_code = selected_code_label.split(" - ", 1)[0] if selected_code_label else ""
        fields["_purpose_s_code"] = selected_s_code

        purpose_lookup = lookups["purpose_map"]
        rb_category = purpose_lookup.get(selected_group.lower(), fields.get("RevPurCategory", ""))
        fields["RevPurCategory"] = rb_category or selected_group
        if selected_s_code:
            fields["RevPurCode"] = (
                f"{fields['RevPurCategory']}-{selected_s_code}"
                if str(fields["RevPurCategory"]).startswith("RB-")
                else selected_s_code
            )

    with rem_col3:
        invoice_default = _parse_date(fields.get("_invoice_date", "")) or date.today()
        invoice_date = st.date_input("Invoice Date", value=invoice_default)
        fields["_invoice_date"] = invoice_date.isoformat()
        
        # Proposed date: current date + 15 days, editable but preset
        if not fields.get("PropDateRem"):
            prop_date = date.today() + timedelta(days=PROPOSED_DATE_OFFSET)
            fields["PropDateRem"] = prop_date.strftime("%d/%m/%Y")
        
        fields["PropDateRem"] = st.text_input("Proposed Date of Remittance", value=fields.get("PropDateRem"), help="Format: DD/MM/YYYY")
        st.caption(f"Default offset is {PROPOSED_DATE_OFFSET} days from today.")

        currency_map = lookups["currency_map"]
        selected_code, requires_manual = preselect_currency_code(
            fields.get("CurrencySecbCode", ""),
            fields.get("_currency_short_code", ""),
            currency_map,
        )
        if selected_code:
            fields["CurrencySecbCode"] = selected_code
        fields["_currency_requires_manual_selection"] = "1" if requires_manual else ""
        if requires_manual:
            st.warning(
                f"Could not auto-map short code '{fields.get('_currency_short_code', '')}' to a valid currency name. "
                "Select currency manually."
            )

        currency_items = sorted([(k.upper(), v) for k, v in currency_map.items()], key=lambda x: x[0])
        currency_labels = [f"{label} ({code})" for label, code in currency_items]
        curr_idx = 0
        for i, (_, ccode) in enumerate(currency_items):
            if ccode == fields.get("CurrencySecbCode", ""):
                curr_idx = i
                break
        curr_sel = st.selectbox("Currency", currency_labels or [""], index=curr_idx if currency_labels else 0)
        if curr_sel:
            fields["CurrencySecbCode"] = curr_sel.split("(")[-1].replace(")", "").strip()
            if fields["CurrencySecbCode"] and fields["CurrencySecbCode"] != "-1":
                fields["_currency_requires_manual_selection"] = ""
        fields["AmtPayForgnRem"] = st.text_input("Amount (foreign)", value=fields.get("AmtPayForgnRem", ""))
        fields["AmtPayIndRem"] = st.text_input("Amount (INR)", value=fields.get("AmtPayIndRem", ""))
        fields["CountryRemMadeSecb"] = fields.get("RemitteeCountryCode", fields.get("CountryRemMadeSecb", ""))

    st.markdown("#### 4. Taxability and DTAA")
    # YES/NO flags for state-driven UI enablement
    ui_flags = {
        "remittance_chargeable": fields.get("RemittanceCharIndia", "Y") == "Y",
        "dtaa_applicable": _yn_to_yes_no(fields.get("TaxIndDtaaFlg", "N")) == "YES",
        "trc_available": fields.get("TaxResidCert", "N") == "Y",
        "royalty_fts": _yn_to_yes_no(fields.get("RemForRoyFlg", "N")) == "YES",
        "business_inc": _yn_to_yes_no(fields.get("RemAcctBusIncFlg", "N")) == "YES",
        "liable_india": _yn_to_yes_no(fields.get("IncLiabIndiaFlg", "N")) == "YES",
        "cap_gains": _yn_to_yes_no(fields.get("RemOnCapGainFlg", "N")) == "YES",
        "other_rem": _yn_to_yes_no(fields.get("OtherRemDtaa", "N")) == "YES",
    }

    tax_col1, tax_col2, tax_col3 = st.columns(3)
    with tax_col1:
        rem_ch = st.selectbox("Remittance chargeable in India?", ["Y", "N"], index=0 if ui_flags["remittance_chargeable"] else 1)
        fields["RemittanceCharIndia"] = rem_ch
        
        # Determine if meta is available from session state or similar
        is_gross_up = False
        if "meta" in globals():
            is_gross_up = bool(globals()["meta"].get("is_gross_up", False))
        
        st.text_input("Tax grossed up (Y/N)", value="Y" if is_gross_up else "N", disabled=True, key="tax_grossed_up_input")
        fields["TaxPayGrossSecb"] = "N"
        fields["SecRemCovered"] = st.text_input("Section covered under IT Act", value=fields.get("SecRemCovered", ""), disabled=not ui_flags["remittance_chargeable"])
        fields["AmtIncChrgIt"] = st.text_input("Amount of income chargeable (INR)", value=fields.get("AmtIncChrgIt", ""), disabled=not ui_flags["remittance_chargeable"])
        fields["TaxLiablIt"] = st.text_input("Tax liability under IT Act (INR)", value=fields.get("TaxLiablIt", ""), disabled=not ui_flags["remittance_chargeable"])
    with tax_col2:
        fields["BasisDeterTax"] = st.text_area("Basis of determining tax", value=fields.get("BasisDeterTax", ""), height=120)
        dtaa_label = st.selectbox("DTAA applicable?", ["NO", "YES"], index=1 if ui_flags["dtaa_applicable"] else 0, key="ui_dtaa_applicable")
        dtaa_enabled = dtaa_label == "YES"
        fields["TaxIndDtaaFlg"] = _yes_no_to_yn(dtaa_label)
        
        if not dtaa_enabled:
            _reset_dtaa_fields(fields)
            ui_flags["dtaa_applicable"] = False # Update for immediate use in following fields
            
        trc_label = st.selectbox(
            "Tax Residency Certificate?",
            ["Y", "N"],
            index=0 if fields.get("TaxResidCert", "N") == "Y" else 1,
            disabled=not ui_flags["dtaa_applicable"],
        )
        fields["TaxResidCert"] = trc_label if dtaa_enabled else "N"
    with tax_col3:
        fields["RelevantDtaa"] = st.text_input("Relevant DTAA (country)", value=fields.get("RelevantDtaa", ""), disabled=not ui_flags["dtaa_applicable"])
        fields["RelevantArtDtaa"] = st.text_input("Relevant Article of DTAA", value=fields.get("RelevantArtDtaa", ""), disabled=not ui_flags["dtaa_applicable"])
        fields["TaxIncDtaa"] = st.text_input("Taxable income per DTAA", value=fields.get("TaxIncDtaa", ""), disabled=not ui_flags["dtaa_applicable"])
        fields["TaxLiablDtaa"] = st.text_input("Tax liability per DTAA", value=fields.get("TaxLiablDtaa", ""), disabled=not ui_flags["dtaa_applicable"])

    st.markdown("#### 5. DTAA Sub-flags")
    flag_col1, flag_col2, flag_col3, flag_col4, flag_col5 = st.columns(5)
    with flag_col1:
        rem_for_roy = st.selectbox("Royalty/FTS?", ["NO", "YES"], index=1 if ui_flags["royalty_fts"] else 0, disabled=not ui_flags["dtaa_applicable"])
        fields["RemForRoyFlg"] = _yes_no_to_yn(rem_for_roy)
    with flag_col2:
        rem_bus = st.selectbox("Business Income?", ["NO", "YES"], index=1 if ui_flags["business_inc"] else 0, disabled=not ui_flags["dtaa_applicable"])
        fields["RemAcctBusIncFlg"] = _yes_no_to_yn(rem_bus)
    with flag_col3:
        inc_india = st.selectbox("Income liable in India?", ["NO", "YES"], index=1 if ui_flags["liable_india"] else 0, disabled=not ui_flags["dtaa_applicable"])
        fields["IncLiabIndiaFlg"] = _yes_no_to_yn(inc_india)
    with flag_col4:
        cap_gain = st.selectbox("Capital Gains?", ["NO", "YES"], index=1 if ui_flags["cap_gains"] else 0, disabled=not ui_flags["dtaa_applicable"])
        fields["RemOnCapGainFlg"] = _yes_no_to_yn(cap_gain)
    with flag_col5:
        other_rem = st.selectbox("Other Remittance?", ["NO", "YES"], index=1 if ui_flags["other_rem"] else 0, disabled=not ui_flags["dtaa_applicable"])
        fields["OtherRemDtaa"] = _yes_no_to_yn(other_rem)

    det_col1, det_col2, det_col3 = st.columns(3)
    with det_col1:
        fields["ArtDtaa"] = st.text_input("Article of DTAA (Royalty)", value=fields.get("ArtDtaa", ""), disabled=not ui_flags["royalty_fts"])
        fields["RateTdsADtaa"] = st.text_input("Rate of TDS per DTAA (%)", value=fields.get("RateTdsADtaa", ""), disabled=not ui_flags["royalty_fts"])
    with det_col2:
        fields["_inc_liab_india_detail"] = st.text_input(
            "Income liable in India details",
            value=fields.get("_inc_liab_india_detail", ""),
            disabled=not ui_flags["liable_india"],
        )
    with det_col3:
        fields["RelArtDetlDDtaa"] = st.text_input("Other remittance details", value=fields.get("RelArtDetlDDtaa", ""), disabled=not ui_flags["other_rem"])

    st.markdown("#### 6. TDS Rate and Location")
    tds_col1, tds_col2, tds_col3 = st.columns(3)
    with tds_col1:
        rate_options = ["IT Act Rate", "DTAA Rate", "Lower Deduction Cert"]
        code_to_label = {"1": rate_options[0], "2": rate_options[1], "3": rate_options[2]}
        label_to_code = {v: k for k, v in code_to_label.items()}
        current_rate_label = code_to_label.get(fields.get("RateTdsSecbFlg", "1"), rate_options[0])
        rate_idx = rate_options.index(current_rate_label)
        selected_rate_label = st.selectbox("Rate of TDS flag", rate_options, index=rate_idx)
        fields["RateTdsSecbFlg"] = label_to_code[selected_rate_label]
        default_rate = fields.get("RateTdsSecB", "")
        if fields["RateTdsSecbFlg"] == "2" and fields.get("TaxIndDtaaFlg", "N") == "Y" and fields.get("RateTdsADtaa"):
            default_rate = fields["RateTdsADtaa"]
        fields["RateTdsSecB"] = st.text_input("TDS Rate %", value=default_rate)
        fields["AmtPayForgnTds"] = st.text_input("TDS Amount (foreign)", value=fields.get("AmtPayForgnTds", ""))
        fields["AmtPayIndianTds"] = st.text_input("TDS Amount (INR)", value=fields.get("AmtPayIndianTds", ""))
    with tds_col2:
        gross = _float_or_none(fields.get("AmtPayForgnRem", ""))
        tds = _float_or_none(fields.get("AmtPayForgnTds", ""))
        if gross is not None and tds is not None:
            net = gross - tds
            fields["ActlAmtTdsForgn"] = str(int(net)) if float(net).is_integer() else str(net)
        # Allow actual remittance to be editable so CA can correct computed values
        fields["ActlAmtTdsForgn"] = st.text_input("Actual remittance after TDS (auto)", value=fields.get("ActlAmtTdsForgn", ""), disabled=False)
        dedn_default = _parse_date(fields.get("DednDateTds", "")) or date.today()
        fields["DednDateTds"] = st.date_input("Date of TDS deduction", value=dedn_default).isoformat()
        st.caption(f"Display format: {_format_dd_mmm_yyyy(dedn_default)}")
        fields["_deduction_country"] = st.text_input("Country (of deduction)", value=fields.get("_deduction_country", ""))
    with tds_col3:
        state_map = lookups["state_map"]
        state_items = sorted([(k.title(), v) for k, v in state_map.items()], key=lambda x: x[0])
        state_labels = [f"{label} ({code})" for label, code in state_items]
        s_idx = 0
        for i, (_, scode) in enumerate(state_items):
            if scode == fields.get("AcctntState", ""):
                s_idx = i
                break
        state_sel = st.selectbox("State", state_labels or [""], index=s_idx if state_labels else 0)
        if state_sel:
            fields["AcctntState"] = state_sel.split("(")[-1].replace(")", "").strip()
        fields["NameAcctnt"] = st.text_input("Accountant Name", value=fields.get("NameAcctnt", ""))
        fields["NameFirmAcctnt"] = st.text_input("Firm Name", value=fields.get("NameFirmAcctnt", ""))
        fields["MembershipNumber"] = st.text_input("Membership Number", value=fields.get("MembershipNumber", ""))

    # Pre-submit validation
    purpose_grouped = lookups.get("purpose_grouped", {})
    errors = validate_remittance_fields(fields, purpose_grouped)
    if errors:
        st.error("Missing or invalid fields for XML generation:")
        for err in errors:
            st.write(f"- {err}")
    else:
        st.success("Remittance fields valid for XML generation.")

    return {k: str(v) for k, v in fields.items() if not str(k).startswith("_")}


def validate_remittance_fields(fields: dict, purpose_by_group: dict) -> list[str]:
    errors = []

    # 1. Nature selection
    if not fields.get("NatureRemCategory"):
        errors.append("Nature of Remittance is required.")

    # 2. Purpose Group
    grp = fields.get("_purpose_group_name", "")
    if not grp:
        errors.append("Purpose Group is required.")

    # 3. Purpose Code
    code = fields.get("_purpose_s_code", "")
    if not code:
        errors.append("Purpose Code (selected from dropdown) is required.")

    # 4. Cross-check: Is code in allowed group?
    if grp and code:
        allowed_rows = purpose_by_group.get(grp, [])
        allowed_codes = [str(r.get("purpose_code")) for r in allowed_rows]
        if code not in allowed_codes:
            errors.append(f"Purpose Code '{code}' is not valid for Group '{grp}'.")

    # 5. Proposed Date format
    prop_date = fields.get("PropDateRem", "")
    if prop_date and not re.match(r"^\d{2}/\d{2}/\d{4}$", prop_date):
        errors.append("Proposed Date must be in DD/MM/YYYY format.")

    # 6. Basic data quality check for XML fields
    if not fields.get("RevPurCode"):
        errors.append("Canonical Purpose Code (RevPurCode) is missing.")

    return errors
