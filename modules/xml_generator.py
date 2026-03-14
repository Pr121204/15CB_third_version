from __future__ import annotations

import os
import re
import uuid
from typing import Dict, Iterable

from config.settings import OUTPUT_FOLDER
from modules.form15cb_constants import MODE_NON_TDS, MODE_TDS, RATE_TDS_SECB_FLG_DTAA


def escape_xml(value):
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def normalize_numeric_value(value: str, preserve_decimals: bool = False) -> str:
    """Convert numeric strings like '5355.0' or '535.50' to '5355' or '535.5' (default).
    If preserve_decimals=True, ensures 2 decimal places (e.g., '20.80').
    """
    if not value or not isinstance(value, str):
        return value
    # Preserve code-like numeric strings that intentionally carry leading zeros (e.g., "02", "03").
    if re.fullmatch(r"0\d+", value):
        return value
    try:
        # Try to parse as float
        num = float(value)
        if preserve_decimals:
            return f"{num:.2f}"
            
        # Default behavior: Return as integer if whole number, otherwise format with 2 decimals and strip trailing zeros
        if num == int(num):
            return str(int(num))
        else:
            formatted = f"{num:.2f}".rstrip("0").rstrip(".")
            return formatted
    except (ValueError, TypeError):
        # Not a numeric value, return as-is
        return value


def validate_required_fields(fields: Dict[str, str], mode: str = MODE_TDS) -> None:
    required = ["SWVersionNo", "FormName", "AssessmentYear", "RemitterPAN", "NameRemitter", "CurrencySecbCode"]
    missing = [k for k in required if not str(fields.get(k, "")).strip()]
    if str(mode or MODE_TDS) == MODE_NON_TDS:
        # NON_TDS is a non-withholding documentation flow.
        # TDS fields are forced to zero/blank; 9D justification is the main path for
        # common non-taxable cases (OtherRemDtaa=Y, NatureRemDtaa, RelArtDetlDDtaa).

        # OtherRemDtaa must always be present (Y = 9D path, N = article-based DTAA path).
        if not str(fields.get("OtherRemDtaa", "")).strip():
            missing.append("OtherRemDtaa")

        _other_rem = str(fields.get("OtherRemDtaa", "")).strip().upper()

        # When 9D path is active, NatureRemDtaa is the primary classification field.
        if _other_rem == "Y" and not str(fields.get("NatureRemDtaa", "")).strip():
            missing.append("NatureRemDtaa")

        # RelArtDetlDDtaa (9D legal reasons) is required on the non-taxable 9D path.
        # When 9D taxable = Yes, reasons are not legally required — the rate field carries
        # the justification instead. The private key _9d_taxable_state is injected by
        # build_xml_fields_by_mode from the UI form so we can distinguish the two cases.
        if _other_rem == "Y":
            _9d_taxable = str(fields.get("_9d_taxable_state", "")).strip().upper()
            if _9d_taxable != "YES" and not str(fields.get("RelArtDetlDDtaa", "")).strip():
                missing.append("RelArtDetlDDtaa")

        # ReasonNot is mandatory when income is not chargeable to tax in India.
        if str(fields.get("RemittanceCharIndia", "")).strip().upper() == "N":
            if not str(fields.get("ReasonNot", "")).strip():
                missing.append("ReasonNot")
    if str(mode or MODE_TDS) == MODE_TDS:
        # Core fields required for any TDS reporting
        tds_required = [
            "TaxLiablIt",
            "BasisDeterTax",
            "RateTdsSecB",
            "AmtPayForgnTds",
            "AmtPayIndianTds",
            "ActlAmtTdsForgn",
        ]
        missing.extend([k for k in tds_required if not str(fields.get(k, "")).strip()])
        
        # DTAA detail values are mandatory when DTAA is explicitly claimed.
        dtaa_active = (
            str(fields.get("TaxResidCert", "")).strip().upper() == "Y"
            and str(fields.get("OtherRemDtaa", "")).strip().upper() == "N"
            and str(fields.get("RateTdsSecbFlg", "")).strip() == RATE_TDS_SECB_FLG_DTAA
        )
        if dtaa_active:
            dtaa_required = [
                "TaxIncDtaa",
                "TaxLiablDtaa",
                "RateTdsADtaa",
            ]
            missing.extend([k for k in dtaa_required if not str(fields.get(k, "")).strip()])
            try:
                rate_dtaa = float(str(fields.get("RateTdsADtaa", "")).strip())
                if not rate_dtaa.is_integer():
                    missing.append("RateTdsADtaa (must be an integer when DTAA is claimed)")
            except Exception:
                missing.append("RateTdsADtaa (must be numeric when DTAA is claimed)")
            
    if missing:
        uniq_missing = sorted(set(missing))
        raise ValueError(f"Missing or empty mandatory fields: {', '.join(uniq_missing)}")


def _fill_template(fields: Dict[str, str], template_path: str) -> str:
    with open(template_path, "r", encoding="utf8") as f:
        xml_content = f.read()
    for field_name, field_value in fields.items():
        # Normalize numeric values first, then escape for XML.
        # Rate fields should drop .00 if whole, according to modern requirements.
        preserve = False
        normalized_value = normalize_numeric_value(field_value, preserve_decimals=preserve)
        escaped_value = escape_xml(normalized_value)
        xml_content = xml_content.replace("{{" + field_name + "}}", escaped_value)
    return re.sub(r"\{\{[^}]+\}\}", "", xml_content)


def _remove_tag_block(xml_text: str, tag: str) -> str:
    pattern = rf"\s*<FORM15CB:{tag}>.*?</FORM15CB:{tag}>"
    return re.sub(pattern, "", xml_text, flags=re.DOTALL)


def _remove_empty_optional_tags(xml_text: str) -> str:
    optional_tags = [
        "ReasonNot",
        "NatureRemDtaa",
        "RelevantDtaa",
        "RelevantArtDtaa",
        "TaxIncDtaa",
        "TaxLiablDtaa",
        "ArtDtaa",
        "RateTdsADtaa",
        "SecRemCovered",
        "AmtIncChrgIt",
        "TaxLiablIt",
        "BasisDeterTax",
        "PremisesBuildingVillage",  # In RemitteeAddrs: actual tag name (not RemitteePremisesBuildingVillage)
        "RoadStreet",  # In RemitteeAddrs: actual tag name (not RemitteeRoadStreet)
    ]
    for tag in optional_tags:
        pattern = rf"\s*<FORM15CB:{tag}>\s*</FORM15CB:{tag}>"
        xml_text = re.sub(pattern, "", xml_text, flags=re.DOTALL)
    return xml_text


def generate_xml_content(xml_fields: Dict[str, str], mode: str = MODE_TDS, template_path: str = "templates/form15cb_template.xml") -> str:
    # Consistency assertion for debugging
    if os.getenv("DEBUG", "0").lower() in ("1", "true", "yes"):
        from modules.form15cb_constants import XML_SENSITIVE_FORM_KEYS
        from modules.logger import get_logger
        logger = get_logger()
        
        # Filter out keys that are known to be 'input-only' or meta-keys not directly in XML
        # or keys that are transformed (like NameRemittee vs NameRemitteeInput)
        EXPECTED_XML_KEYS = {k for k in XML_SENSITIVE_FORM_KEYS if not k.endswith("Input")}
        # Special case: NameRemitter is computed from NameRemitterInput + Address
        # RemitterPAN is in XML but might be named PAN in some contexts (it's RemitterPAN in out dict)
        
        missing = [k for k in EXPECTED_XML_KEYS if k not in xml_fields]
        if missing:
            logger.warning(f"DEBUG: XML consistency check failed. Missing keys in generated field set: {', '.join(sorted(missing))}")
        else:
            logger.info("DEBUG: XML consistency check passed.")

    validate_required_fields(xml_fields, mode=mode)
    xml_text = _fill_template(xml_fields, template_path)
    xml_text = _remove_empty_optional_tags(xml_text)
    if mode == MODE_NON_TDS:
        for tag in ("RateTdsSecbFlg", "RateTdsSecB", "DednDateTds"):
            xml_text = _remove_tag_block(xml_text, tag)
    return xml_text


def build_xml_fields_by_mode(state: Dict[str, object]) -> Dict[str, str]:
    from modules.invoice_calculator import invoice_state_to_xml_fields

    out = invoice_state_to_xml_fields(state)
    meta = state.get("meta", {})
    mode = str((meta if isinstance(meta, dict) else {}).get("mode") or MODE_TDS)
    if mode == MODE_NON_TDS:
        out["AmtPayForgnTds"] = "0"
        out["AmtPayIndianTds"] = "0"
        out["RateTdsSecbFlg"] = ""
        out["RateTdsSecB"] = ""
        out["DednDateTds"] = ""  # No deduction date in NON_TDS; tag is stripped by generate_xml_content
        # Inject the 9D taxable UI state as a private meta key so validate_required_fields
        # can distinguish the non-taxable path (requires RelArtDetlDDtaa) from the taxable
        # path (does not). This key has no matching template placeholder and is harmless.
        _form = state.get("form", {}) if isinstance(state.get("form"), dict) else {}
        out["_9d_taxable_state"] = str(_form.get("_ui_only_9d_taxable") or "").strip().upper()
    return out


def write_xml_content(xml_content: str, filename: str | None = None) -> str:
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    if not filename:
        hex_str = uuid.uuid4().hex
        filename = f"generated_{hex_str[:12]}.xml"
    out_path = os.path.join(OUTPUT_FOLDER, filename)
    with open(out_path, "w", encoding="utf8") as f:
        f.write(xml_content)
    return out_path


def generate_xml(fields, template_path: str = "templates/form15cb_template.xml"):
    xml_content = generate_xml_content({k: str(v) for k, v in fields.items()}, mode=MODE_TDS, template_path=template_path)
    return write_xml_content(xml_content)


def generate_zip_from_xmls(xml_payloads: Iterable[tuple[str, bytes]]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in xml_payloads:
            zf.writestr(name, data)
    return buf.getvalue()


def validate_xml_structure(xml_path: str):
    try:
        import xml.etree.ElementTree as ET

        ET.parse(xml_path)
        return True
    except Exception:
        return False
