"""
Local Invoice Extractor — Gemini-free extraction for known Bosch templates.

Tries to extract invoice fields using the deterministic regex/PDF extractors
in invoice_extractor_project/. If extraction is complete and valid, the caller
can skip the Gemini API call entirely. Falls back to Gemini for:
  - Unrecognized invoice formats (template = "generic")
  - Any critical field missing after extraction
  - Any exception (missing dependencies, malformed PDF, etc.)

The dict returned by map_local_to_gemini_format() is key-for-key identical to
the dict returned by invoice_gemini_extractor.extract_invoice_core_fields(),
so the rest of the pipeline (build_invoice_state, recompute_invoice, XML
generation) requires zero changes.
"""

import logging
import os
import re
import sys
import tempfile
from typing import Dict, Optional, Tuple

from dateutil import parser as _dateutil_parser

from modules.currency_mapping import SHORT_CODE_TARGET_NAME as _VALID_CURRENCY_CODES  # type: ignore

logger = logging.getLogger(__name__)

# Frozenset of recognised 3-letter currency codes (sourced from currency_mapping,
# which is the single source of truth for all form-valid currencies).
_VALID_CURRENCIES: frozenset = frozenset(_VALID_CURRENCY_CODES)

# ---------------------------------------------------------------------------
# Import invoice_extractor_project modules
# ---------------------------------------------------------------------------
# The extractor package lives one level up from modules/ and uses flat (non-
# namespaced) imports like "from text_utils import …", so we add its directory
# to sys.path.  This is safe: none of its module names shadow stdlib modules.

_EXTRACTOR_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "invoice_extractor_project")
)
if _EXTRACTOR_DIR not in sys.path:
    sys.path.insert(0, _EXTRACTOR_DIR)

_extractor_imported = False
try:
    from pdf_reader import extract_pdf_data  # type: ignore
    from text_utils import remove_hex_strings  # type: ignore
    from extractor import detect_template  # type: ignore
    from extractors import bosch_vietnam, bosch_germany, bosch_sap, bosch_sap_de, sap_se, syntegon  # type: ignore

    _extractor_imported = True
except Exception as _import_err:
    logger.warning(
        "local_invoice_extractor: import_failed reason=%s — Gemini will always be used",
        _import_err,
    )

# ---------------------------------------------------------------------------
# ISO-2 country code → full country name
# ---------------------------------------------------------------------------
# bosch_sap_de and bosch_sap return ISO-2 codes (e.g. "DE") from VAT ID
# detection.  bosch_germany and bosch_vietnam return full names already.
# build_invoice_state does a case-insensitive full-name lookup against
# data/master/country_codes.json, so we must expand codes before passing on.

_ISO2_TO_NAME: Dict[str, str] = {
    "AE": "United Arab Emirates",
    "AT": "Austria",
    "AU": "Australia",
    "BD": "Bangladesh",
    "BE": "Belgium",
    "BR": "Brazil",
    "CA": "Canada",
    "CH": "Switzerland",
    "CN": "China",
    "CZ": "Czech Republic",
    "DE": "Germany",
    "DK": "Denmark",
    "EG": "Egypt",
    "ES": "Spain",
    "FI": "Finland",
    "FR": "France",
    "GB": "United Kingdom",
    "UK": "United Kingdom",   # non-standard but widely used alias for GB
    "GR": "Greece",
    "HK": "Hong Kong",
    "HR": "Croatia",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IN": "India",
    "IT": "Italy",
    "JP": "Japan",
    "KR": "Korea",
    "MX": "Mexico",
    "MY": "Malaysia",
    "NL": "Netherlands",
    "NO": "Norway",
    "PH": "Philippines",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "RS": "Serbia",
    "SE": "Sweden",
    "SG": "Singapore",
    "SI": "Slovenia",
    "SK": "Slovakia",
    "TH": "Thailand",
    "TR": "Turkey",
    "TW": "Taiwan",
    "UA": "Ukraine",
    "US": "United States",
    "VN": "Vietnam",
    "ZA": "South Africa",
}


def _expand_country(val: str) -> str:
    """
    Expand an ISO-2 country code to its full name.
    Leaves full names (e.g. "Germany", "Czech Republic") unchanged.
    Handles the Austrian VAT prefix "ATU" as a special case.
    """
    if not val:
        return val
    s = val.strip()
    upper = s.upper()
    # Bare 2-letter code
    if re.match(r"^[A-Z]{2}$", upper):
        return _ISO2_TO_NAME.get(upper, s)
    # Austrian VAT ID prefix
    if upper == "ATU":
        return "Austria"
    # Already a full name (contains lowercase or spaces)
    return s


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------

def _normalize_date(raw: str) -> Tuple[str, str, str]:
    """
    Parse a raw date string and return (raw, iso, display).

    iso    = "YYYY-MM-DD"  or ""  when parsing fails
    display = "DD/MM/YYYY" or raw when parsing fails

    Edge cases that return iso="":
      - Empty string
      - Bare year like "2025"  (dateutil would silently parse as 2025-01-01)
      - 1-2 digit numbers      (stray tokens from OCR)
      - Truly unparseable text
    """
    if not raw:
        return "", "", ""
    raw = raw.strip()

    # Guard: bare year — would be silently parsed as Jan 1 of that year
    if re.match(r"^\d{4}$", raw):
        return raw, "", raw

    # Guard: stray 1-2 digit OCR token
    if re.match(r"^\d{1,2}$", raw):
        return raw, "", raw

    # First attempt: strict (no fuzzy) with dayfirst=True
    # Handles: DD.MM.YYYY  DD/MM/YYYY  DD-MM-YYYY  DD-Mon-YYYY  YYYY-MM-DD
    try:
        dt = _dateutil_parser.parse(raw, dayfirst=True, fuzzy=False)
        return raw, dt.strftime("%Y-%m-%d"), dt.strftime("%d/%m/%Y")
    except Exception:
        pass

    # Second attempt: fuzzy — handles stray text like "Date: 26.11.2025"
    try:
        dt = _dateutil_parser.parse(raw, dayfirst=True, fuzzy=True)
        return raw, dt.strftime("%Y-%m-%d"), dt.strftime("%d/%m/%Y")
    except Exception:
        pass

    return raw, "", raw


# ---------------------------------------------------------------------------
# Amount cleaning
# ---------------------------------------------------------------------------

def _clean_amount(raw: str) -> str:
    """
    Parse an OCR amount string into a plain decimal string suitable for
    float() conversion.  Handles EU (1.234,56), US (1,234.56), plain
    decimals, spaces as thousands separators, currency symbols, and common
    OCR noise (letter-O instead of zero, etc.).

    Returns "" when the value cannot be parsed as a positive number.

    Examples:
      "2,935.29"    → "2935.29"
      "289.500,00"  → "289500.0"   (EU thousands-dot + comma-decimal)
      "289,500.00"  → "289500.0"
      "EUR 1.234,56"→ "1234.56"
      "538,25"      → "538.25"     (comma-decimal, no thousands)
      "2935.29"     → "2935.29"
      ""            → ""
    """
    if not raw:
        return ""
    from text_utils import parse_invoice_amount  # type: ignore
    result = parse_invoice_amount(raw)
    if result is None:
        return ""
    # Format as a plain decimal string (no trailing ".0" for whole numbers
    # only when there are no cents — preserve cents precision)
    return str(result)


# ---------------------------------------------------------------------------
# Completeness check
# ---------------------------------------------------------------------------

# Fields that MUST be non-empty for local extraction to be trusted.
# currency_short and remitter_country_text are intentionally excluded:
#   - currency falls back to Excel/manual selection when the extractor misses it
#   - remitter_country defaults to "India" when missing
# If any field in this list is empty, Gemini is called instead.
_CRITICAL_FIELDS = [
    "beneficiary_name",
    "beneficiary_country_text",
    "beneficiary_address",
    "remitter_name",
    "remitter_address",
    "invoice_number",
    "invoice_date_iso",
    "amount",
]

# Values that look non-empty but are effectively meaningless placeholders.
# A critical field containing any of these is treated as blank → Gemini fallback.
_PLACEHOLDER_VALUES = frozenset({
    "n/a", "na", "nil", "none", "null", "-", "--", "---",
    ".", "..", "tbd", "unknown", "not available", "not applicable",
})

# Minimum character length for a plausible invoice number.
# Single or double character values are almost always OCR noise.
_MIN_INVOICE_NUMBER_LEN = 3

# Minimum number of characters the raw PDF text layer must contain for the
# local extractor to attempt extraction.  Below this the PDF is treated as
# scanned/image-only and is routed directly to Gemini vision.
_PDF_TEXT_MIN_CHARS = 200

def check_local_completeness(mapped: Dict, inv_id: str = "") -> bool:
    """
    Return True only when every critical field is populated AND each value
    passes basic sanity checks.  Any failure causes Gemini fallback.

    Fields NOT checked here (handled by fallbacks before this call):
      - currency_short    : missing → taken from Excel or user's manual selection;
                            never triggers Gemini on its own.
      - remitter_country_text : missing → defaults to "India";
                            never triggers Gemini on its own.
    """
    tag = f"invoice_id={inv_id} " if inv_id else ""

    # ── 1. All critical fields must be non-empty and non-placeholder ──────────
    for field in _CRITICAL_FIELDS:
        val = str(mapped.get(field, "") or "").strip()
        if not val:
            logger.info(
                "local_completeness_fail %sreason=blank_field field=%s — Gemini will be called",
                tag, field,
            )
            return False
        if val.lower() in _PLACEHOLDER_VALUES:
            logger.info(
                "local_completeness_fail %sreason=placeholder_value field=%s value=%r "
                "— Gemini will be called",
                tag, field, val,
            )
            return False

    # ── 2. Invoice number must be plausibly real (not a single OCR character) ─
    inv_num = str(mapped.get("invoice_number", "") or "").strip()
    if len(inv_num) < _MIN_INVOICE_NUMBER_LEN:
        logger.info(
            "local_completeness_fail %sreason=invoice_number_too_short len=%d value=%r "
            "— Gemini will be called",
            tag, len(inv_num), inv_num,
        )
        return False

    # ── 3. Amount must parse as a positive number ─────────────────────────────
    try:
        amt = float(mapped["amount"])
        if amt <= 0:
            logger.info(
                "local_completeness_fail %sreason=amount_not_positive value=%s — Gemini will be called",
                tag, mapped["amount"],
            )
            return False
    except (ValueError, TypeError):
        logger.info(
            "local_completeness_fail %sreason=amount_not_numeric value=%s — Gemini will be called",
            tag, mapped["amount"],
        )
        return False

    # ── 4. Beneficiary country must have expanded to a full name ──────────────
    # _expand_country() maps all known ISO-2 codes (including "UK") to full
    # names.  If the value is still a bare 2-letter code the expansion failed,
    # meaning the extractor returned an unrecognised country code.
    country = str(mapped.get("beneficiary_country_text", "") or "").strip()
    if re.match(r"^[A-Z]{2}$", country.upper()):
        logger.info(
            "local_completeness_fail %sreason=unexpanded_country value=%s — Gemini will be called",
            tag, country,
        )
        return False

    # ── 5. Beneficiary must not be India (remitter/beneficiary likely swapped) ─
    if "india" in country.lower():
        logger.info(
            "local_completeness_fail %sreason=beneficiary_is_india — Gemini will be called", tag,
        )
        return False

    # ── 6. Beneficiary name/address must not mention India ────────────────────
    bene_name = str(mapped.get("beneficiary_name", "") or "").strip()
    bene_addr = str(mapped.get("beneficiary_address", "") or "").strip()
    if "india" in bene_name.lower() or "india" in bene_addr.lower():
        logger.info(
            "local_completeness_fail %sreason=india_in_beneficiary_name_or_address "
            "— Gemini will be called",
            tag,
        )
        return False

    # ── 7. Beneficiary name too long → almost certainly an address block ──────
    if len(bene_name) > 200:
        logger.info(
            "local_completeness_fail %sreason=beneficiary_name_too_long len=%d "
            "— Gemini will be called",
            tag, len(bene_name),
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Field mapping: extractor.py keys → Gemini output format
# ---------------------------------------------------------------------------

def map_local_to_gemini_format(
    raw_fields: Dict,
    raw_text: str,
    excel_data: Optional[Dict] = None,
) -> Dict:
    """
    Convert the dict returned by any bosch_* extractor into the exact dict
    format returned by invoice_gemini_extractor.extract_invoice_core_fields().

    Key differences handled here:
      beneficiary_country  → beneficiary_country_text  (+ ISO-2 expansion)
      remitter_country     → remitter_country_text      (+ ISO-2 expansion)
      invoice_date         → invoice_date_raw / invoice_date_iso / invoice_date_display
      currency             → currency_short
      amount_foreign       → amount                     (commas stripped)

    Fields left blank (classifier fills them from _raw_invoice_text):
      nature_of_remittance, purpose_group, purpose_code
    """
    if excel_data is None:
        excel_data = {}

    # Date
    date_raw_str = str(raw_fields.get("invoice_date", "") or "").strip()
    date_raw, date_iso, date_display = _normalize_date(date_raw_str)

    # Amount
    amount_clean = _clean_amount(str(raw_fields.get("amount_foreign", "") or ""))

    # Currency: validate the extractor's value against the known-good set.
    # An extractor may return garbled values (e.g. "EUR.", "EUUR", "XYZ") that
    # are non-empty but not a recognised currency code.  Treat any such value
    # the same as a missing currency: discard it and fall back to Excel/manual.
    currency = str(raw_fields.get("currency", "") or "").strip().upper()
    if currency and currency not in _VALID_CURRENCIES:
        logger.info(
            "currency_invalid_from_extractor value=%r — discarding, trying Excel fallback",
            currency,
        )
        currency = ""

    if not currency:
        excel_currency = str(
            excel_data.get("currency") or excel_data.get("currency_short") or ""
        ).strip().upper()
        # Also validate the Excel currency — a corrupt Excel cell must not
        # silently propagate an invalid code into the form.
        if excel_currency and excel_currency not in _VALID_CURRENCIES:
            logger.info(
                "currency_invalid_from_excel value=%r — discarding, will require manual entry",
                excel_currency,
            )
            excel_currency = ""
        if excel_currency:
            logger.info(
                "currency_fallback_from_excel currency=%s — extractor missed/invalid, using Excel value",
                excel_currency,
            )
        currency = excel_currency
    # If currency is still empty here the user will be prompted to select it
    # manually in the UI.  No invalid code ever reaches the form.

    # Countries
    bene_country = _expand_country(str(raw_fields.get("beneficiary_country", "") or "").strip())
    remi_country = _expand_country(str(raw_fields.get("remitter_country", "") or "").strip())
    # Remitter country: default to India when extractor found nothing
    if not remi_country:
        remi_country = "India"
        logger.debug("remitter_country_defaulted_to_india")

    # Excel "Text" column (used by text_remittance_ai_helper classifier)
    excel_text = str(
        excel_data.get("Text") or excel_data.get("text") or ""
    ).strip()

    return {
        # ── Core invoice fields ───────────────────────────────────────────
        "remitter_name":              str(raw_fields.get("remitter_name", "") or "").strip().upper(),
        "remitter_address":           str(raw_fields.get("remitter_address", "") or "").strip(),
        "remitter_country_text":      remi_country,
        "beneficiary_name":           str(raw_fields.get("beneficiary_name", "") or "").strip().upper(),
        "beneficiary_address":        str(raw_fields.get("beneficiary_address", "") or "").strip(),
        "beneficiary_country_text":   bene_country,
        "invoice_number":             str(raw_fields.get("invoice_number", "") or "").strip(),
        "invoice_date_raw":           date_raw,
        "invoice_date_iso":           date_iso,
        "invoice_date_display":       date_display,
        "amount":                     amount_clean,
        "currency_short":             currency,
        # ── Classification (filled by remittance_classifier from _raw_invoice_text) ──
        "nature_of_remittance":       "",
        "purpose_group":              "",
        "purpose_code":               "",
        # ── Classifier inputs ─────────────────────────────────────────────
        "_excel_text":                excel_text,
        "_raw_invoice_text":          raw_text,
        # ── Metadata ─────────────────────────────────────────────────────
        "amount_source":              "local_extractor",
        "requires_review_ai":         False,
        "_extraction_quality":        "",
        "_deterministic_amount_page": "",
        "line_items":                 [],
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def try_local_extraction_from_bytes(
    file_bytes: bytes,
) -> Tuple[Optional[Dict], str, str]:
    """
    Attempt deterministic (no-Gemini) extraction from raw PDF bytes.

    Returns
    -------
    (raw_fields, template_type, raw_text)

    raw_fields   : dict with extractor.py snake_case keys, or None on any failure
    template_type: one of bosch_vietnam / bosch_germany / bosch_sap /
                   bosch_sap_de / generic
    raw_text     : full extracted text (used as _raw_invoice_text for classifier)

    Caller should treat raw_fields=None or template_type="generic" as
    "fall through to Gemini".
    """
    if not _extractor_imported:
        logger.warning("local_extractor_unavailable — falling back to Gemini")
        return None, "generic", ""

    tmp_path: Optional[str] = None
    try:
        # extractor.py requires a file path, not bytes — write to temp file
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        # Extract the native text layer (no OCR).  A single call is used for
        # both the scanned-PDF guard and the subsequent extraction so the file
        # is only read once.
        text, words = extract_pdf_data(tmp_path)

        # Scanned-PDF guard: if the PDF has little or no embedded text it is
        # an image-only scan.  Skip the regex extractors (they require clean
        # text) and let the caller route directly to Gemini vision.
        if len(text.strip()) < _PDF_TEXT_MIN_CHARS:
            logger.info(
                "local_extractor_skipped_scanned_pdf chars=%d "
                "— PDF has no embedded text, routing directly to Gemini vision",
                len(text.strip()),
            )
            return None, "generic", ""

        # Strip embedded hex artefacts (binary object IDs embedded by some PDF
        # generators).  Re-check length in case the PDF contained almost
        # nothing but hex lines.
        text = remove_hex_strings(text)
        if not text.strip():
            logger.info("local_extractor_hex_only_pdf — Gemini image path will run")
            return None, "generic", ""

        # Identify the invoice template
        template_type: str = detect_template(text)
        logger.info("local_extractor_template=%s", template_type)

        if template_type == "generic":
            # generic extractor is minimal and unreliable — always use Gemini
            return None, "generic", text

        # Run the appropriate extractor
        if template_type == "bosch_vietnam":
            raw_fields = bosch_vietnam.extract(text, words)
        elif template_type == "bosch_germany":
            raw_fields = bosch_germany.extract(text, words)
        elif template_type == "bosch_sap_de":
            raw_fields = bosch_sap_de.extract(text, words)
        elif template_type == "bosch_sap":
            raw_fields = bosch_sap.extract(text, words)
        elif template_type == "sap_se":
            raw_fields = sap_se.extract(text, words)
        elif template_type == "syntegon":
            raw_fields = syntegon.extract(text, words)
        else:
            return None, "generic", text

        return raw_fields, template_type, text

    except Exception as exc:
        logger.warning(
            "local_extractor_exception type=%s msg=%s — falling back to Gemini",
            type(exc).__name__,
            exc,
        )
        return None, "generic", ""

    finally:
        # Always remove the temp file even if an exception occurred
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass  # Non-fatal; OS temp-file cleanup will handle it eventually
