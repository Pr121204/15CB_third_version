"""
Extractor for the SAP-format invoices (e.g. Robert Bosch, spol. s r. o. / Czech Republic).
These have:
  - Heavily compressed / space-stripped text from the PDF extractor
  - "Billing Document" instead of "Invoice No."
  - "Billing Date" instead of "Date Invoice"
  - "Total value" instead of "Invoice amount"
  - European number format: 289.500,00 CZK (dot=thousands, comma=decimal)
  - "INDIE" (Czech for India) as country marker
  - "Bill to Party Address" block for remitter info
"""
import re
from text_utils import detect_country

_COUNTRY_MAP = {
    "DE": "Germany", "FR": "France", "GB": "UK", "NL": "Netherlands",
    "IT": "Italy", "ES": "Spain", "BE": "Belgium", "AT": "Austria",
    "SE": "Sweden", "PL": "Poland", "CZ": "Czech Republic",
    "HU": "Hungary", "RO": "Romania", "PT": "Portugal", "JP": "Japan",
}

_INDIA_TOKENS = {"INDIE", "INDIA", "IN"}

# Legal suffixes to strip when normalising beneficiary name
_NAME_STRIP = re.compile(
    r"\s*[,\s]+"
    r"(?:GmbH|SAS|S\.A\.S\.|spol\.\s*s\s*r\.\s*o\.|Ltd\.| "
    r"SE|AG|NV|BV|S\.A\.|S\.p\.A\.|Oy|AB|AS|ApS|Inc\.|LLC|"
    r"France|Deutschland|Japan|Czech|Polska).*$",
    re.IGNORECASE,
)


def _normalize_name(raw):
    """Strip legal-form suffix and country qualifiers from entity name."""
    if re.search(r"Bosch\s+Corporation", raw, re.IGNORECASE):
        return "Bosch Corporation"
    # Strip parenthesised country qualifier e.g. "(France)" in "Robert Bosch (France) S.A.S."
    name = re.sub(r"\s*\([^)]+\).*$", "", raw).strip()
    # Also strip remaining legal suffix if paren-strip left one
    name = _NAME_STRIP.sub("", name).strip().rstrip(",").strip()
    return name if name else raw


def _decompress_text(s):
    """Insert spaces in PDF-compressed text: 'RobertaBosche2678' -> 'Roberta Bosche 2678'."""
    # Space before digit following a letter
    s = re.sub(r"([A-Za-z\u00C0-\u024F])(\d)", r"\1 \2", s)
    # Space before uppercase following lowercase (CamelCase)
    s = re.sub(
        r"([a-z\u00E0-\u024F])([A-Z\u00C0-\u00DE])",
        r"\1 \2", s
    )
    return s


def _normalize_amount(s):
    """Convert European '289.500,00' → '289,500.00'; leave US format unchanged."""
    s = s.strip()
    # European: digits, dot-separated thousands, comma decimal e.g. 289.500,00
    if re.match(r"^\d{1,3}(\.\d{3})*,\d{2}$", s):
        integer_str = s.rsplit(",", 1)[0].replace(".", "")
        decimal_str = s.rsplit(",", 1)[1]
        # Reformat with comma thousands separator
        integer_val = int(integer_str)
        return f"{integer_val:,}.{decimal_str}"
    return s


def extract(text, words=None):
    data = {}

    # ── Beneficiary name ──────────────────────────────────────────────────────
    raw_name = next(
        (l.strip() for l in text.splitlines() if l.strip()), ""
    )
    data["beneficiary_name"] = _normalize_name(raw_name)

    # ── Beneficiary country: VAT ID (compressed or spaced) ───────────────────
    m_vat = re.search(
        r"(?:Our\s*VAT\s*ID|OurVATID)\s*:?\s*([A-Z]{2})\d+",
        text, re.IGNORECASE,
    )
    vat_prefix = m_vat.group(1).upper() if m_vat else ""
    data["beneficiary_country"] = _COUNTRY_MAP.get(vat_prefix, vat_prefix)

    # ── Beneficiary address: RegisteredOffice line ────────────────────────────
    # Compressed: "RegisteredOffice RobertBosch,spol.sr.o.,RobertaBosche2678,37004 CeskeBudejovice"
    # Spaced:     "Registered Office Robert Bosch, spol. s r.o., Roberta Bosche 2678, ..."
    m_ro = re.search(
        r"(?:Registered\s*Office|RegisteredOffice)\s+(.+)",
        text, re.IGNORECASE,
    )
    if m_ro:
        ro = m_ro.group(1).strip()
        # Drop entity name (up to second comma) to get pure address
        parts = ro.split(",")
        if len(parts) >= 3:
            # "Robert Bosch, spol. s r.o., Roberta Bosche 2678, 370 04 ..."
            # Skip first 1-2 parts that are the entity name
            # Find first part that looks like a street (has digits)
            addr_parts = []
            skip_done = False
            for p in parts:
                p = p.strip()
                if not skip_done:
                    if re.search(r"\d", p):
                        skip_done = True
                        addr_parts.append(p)
                else:
                    # Stop at "registered in" clause
                    if re.search(r"registered\s+in|district\s+court", p, re.IGNORECASE):
                        break
                    addr_parts.append(p)
            if addr_parts:
                addr = ", ".join(addr_parts).strip()
                # Drop "registered..." clause (compressed or spaced)
                addr = re.sub(r",?\s*registered.*$", "", addr, flags=re.IGNORECASE).strip()
                # Fix compressed text: "RobertaBosche2678" -> "Roberta Bosche 2678"
                addr = _decompress_text(addr)
                data["beneficiary_address"] = addr
            else:
                data["beneficiary_address"] = ro
        else:
            data["beneficiary_address"] = ro
    else:
        data["beneficiary_address"] = ""

    # ── Remitter name ─────────────────────────────────────────────────────────
    # data["remitter_country"] = "India"  # Replaced by dynamic detection in address step
    m_name = re.search(r"Bosch\s*Ltd\.", text, re.IGNORECASE)
    data["remitter_name"] = "Bosch Ltd." if m_name else ""

    # ── Remitter address ──────────────────────────────────────────────────────
    #
    # Priority order:
    #   1. "Payer BoschLtd.HosurRoad,AdugodiBANGALORE,Karnataka560030,INDIE"
    #      This is the entity that actually remits payment — most reliable source.
    #   2. Bill to Party block (multi-line, may be ship-to not payer)
    #   3. SoldtoParty inline line fallback
    #
    lines = text.splitlines()
    remitter_addr = ""

    # Strategy 1: Inline "Payer BoschLtd.<address>,INDIE" line
    # This appears near the bottom summary block of SAP invoices.
    # Skip the short "Payer XXXXXXXX" header line (no address content).
    m_payer_line = re.search(
        r"(?m)^Payer\s+(?:Bosch\s*Ltd\.|BoschLtd\.)([^\n]+?)(?:,\s*(?:INDIE|INDIA)|\s+(?:INDIE|INDIA))\s*$",
        text, re.IGNORECASE,
    )
    if m_payer_line:
        raw = m_payer_line.group(1).strip().rstrip(",")
        dec = _decompress_text(raw)
        dec = re.sub(r"\s*,\s*", ", ", dec).strip()
        # Extract trailing 6-digit pincode with optional state prefix
        mp = re.search(r",?\s*([\w][\w\s]*?)\s+(\d{6})\s*$", dec)
        if mp:
            state = mp.group(1).strip()
            pin = mp.group(2)
            addr_body = dec[:mp.start()].strip().rstrip(",")
            remitter_addr = f"{addr_body}, {state} - {pin}"
        else:
            remitter_addr = dec

    # Strategy 2: Bill to Party multi-line block
    if not remitter_addr:
        bill_start = None
        for i, line in enumerate(lines):
            if re.search(r"Bill\s*to\s*Part", line, re.IGNORECASE):
                bill_start = i
                break

        if bill_start is not None:
            addr_parts = []
            collecting = False
            for j in range(bill_start, min(bill_start + 15, len(lines))):
                l = lines[j].strip()
                if re.search(r"Bosch\s*Ltd\.", l, re.IGNORECASE):
                    collecting = True
                    continue
                if not collecting:
                    continue
                if re.search(r"(SoldtoParty|Sold\s+to\s+Party|ShiptoParty|Ship\s+to\s+Party)", l, re.IGNORECASE):
                    break
                if re.search(r"(TaxFulfillment|Tax\s+Fulfillment|BillingDate|Billing\s+Date)", l, re.IGNORECASE):
                    continue
                if not l:
                    continue
                m_pc = re.search(r"(\d{5,6})[,\s]*(INDIE|INDIA|IN)", l, re.IGNORECASE)
                if m_pc:
                    addr_parts.append(m_pc.group(1))
                    break
                l = re.sub(r"POBox(\d+)", r"PO Box ", l)
                addr_parts.append(l)

            if addr_parts:
                if len(addr_parts) >= 2:
                    pincode = addr_parts[-1]
                    city = addr_parts[-2].title()
                    street_parts = addr_parts[:-2]
                    street = ", ".join(street_parts)
                    remitter_addr = (street + ", " if street else "") + city + " - " + pincode
                else:
                    remitter_addr = ", ".join(addr_parts)

    # Strategy 3: SoldtoParty inline fallback
    if not remitter_addr:
        m_sold = re.search(
            r"(?:SoldtoParty|Sold\s+to\s+Party)\s+(?:Bosch\s*Ltd\.|BoschLtd\.)"
            r"((?:PO\s*Box\s*|POBox)\d+)([A-Z]+)(\d{6})",
            text, re.IGNORECASE,
        )
        if m_sold:
            street = re.sub(r"POBox(\d+)", r"PO Box ", m_sold.group(1)).strip()
            city = m_sold.group(2).strip().title()
            pincode = m_sold.group(3).strip()
            remitter_addr = f"{street}, {city} - {pincode}"

    data["remitter_address"] = remitter_addr
    data["remitter_country"] = detect_country(remitter_addr, default="India")

    # ── Invoice number: "Billing Document XXXXXXXXX" ─────────────────────────
    m_inv = re.search(
        r"(?:Billing\s*Document|BillingDocument)\s+(\d+)",
        text, re.IGNORECASE,
    )
    data["invoice_number"] = m_inv.group(1).strip() if m_inv else ""

    # ── Invoice date: "Billing Date DD.MM.YYYY" ───────────────────────────────
    m_date = re.search(
        r"(?:Billing\s*Date|BillingDate)\s+(\d{2}\.\d{2}\.\d{4})",
        text, re.IGNORECASE,
    )
    data["invoice_date"] = m_date.group(1).strip() if m_date else ""

    # ── Amount & currency: "Total value 289.500,00 CZK" ─────────────────────
    m_amt = re.search(
        r"(?:Total\s*value|Totalvalue)\s+([\d.,]+)\s*(CZK|EUR|USD|JPY|GBP)",
        text, re.IGNORECASE,
    )
    if m_amt:
        data["currency"] = m_amt.group(2).upper()
        data["amount_foreign"] = _normalize_amount(m_amt.group(1))
    else:
        data["amount_foreign"] = ""
        data["currency"] = ""

    return data









# """
# Extractor for the SAP-format invoices (e.g. Robert Bosch, spol. s r. o. / Czech Republic).
# These have:
#   - Heavily compressed / space-stripped text from the PDF extractor
#   - "Billing Document" instead of "Invoice No."
#   - "Billing Date" instead of "Date Invoice"
#   - "Total value" instead of "Invoice amount"
#   - European number format: 289.500,00 CZK (dot=thousands, comma=decimal)
#   - "INDIE" (Czech for India) as country marker
#   - "Bill to Party Address" block for remitter info
# """
# import re

# _COUNTRY_MAP = {
#     "DE": "Germany", "FR": "France", "GB": "UK", "NL": "Netherlands",
#     "IT": "Italy", "ES": "Spain", "BE": "Belgium", "AT": "Austria",
#     "SE": "Sweden", "PL": "Poland", "CZ": "Czech Republic",
#     "HU": "Hungary", "RO": "Romania", "PT": "Portugal", "JP": "Japan",
# }

# _INDIA_TOKENS = {"INDIE", "INDIA", "IN"}

# # Legal suffixes to strip when normalising beneficiary name
# _NAME_STRIP = re.compile(
#     r"\s*[,\s]+"
#     r"(?:GmbH|SAS|S\.A\.S\.|spol\.\s*s\s*r\.\s*o\.|Ltd\.| "
#     r"SE|AG|NV|BV|S\.A\.|S\.p\.A\.|Oy|AB|AS|ApS|Inc\.|LLC|"
#     r"France|Deutschland|Japan|Czech|Polska).*$",
#     re.IGNORECASE,
# )


# def _normalize_name(raw):
#     """Strip legal-form suffix and country qualifiers from entity name."""
#     if re.search(r"Bosch\s+Corporation", raw, re.IGNORECASE):
#         return "Bosch Corporation"
#     # Strip parenthesised country qualifier e.g. "(France)" in "Robert Bosch (France) S.A.S."
#     name = re.sub(r"\s*\([^)]+\).*$", "", raw).strip()
#     # Also strip remaining legal suffix if paren-strip left one
#     name = _NAME_STRIP.sub("", name).strip().rstrip(",").strip()
#     return name if name else raw


# def _decompress_text(s):
#     """Insert spaces in PDF-compressed text: 'RobertaBosche2678' -> 'Roberta Bosche 2678'."""
#     # Space before digit following a letter
#     s = re.sub(r"([A-Za-z\u00C0-\u024F])(\d)", r"\1 \2", s)
#     # Space before uppercase following lowercase (CamelCase)
#     s = re.sub(
#         r"([a-z\u00E0-\u024F])([A-Z\u00C0-\u00DE])",
#         r"\1 \2", s
#     )
#     return s


# def _normalize_amount(s):
#     """Convert European '289.500,00' → '289,500.00'; leave US format unchanged."""
#     s = s.strip()
#     # European: digits, dot-separated thousands, comma decimal e.g. 289.500,00
#     if re.match(r"^\d{1,3}(\.\d{3})*,\d{2}$", s):
#         integer_str = s.rsplit(",", 1)[0].replace(".", "")
#         decimal_str = s.rsplit(",", 1)[1]
#         # Reformat with comma thousands separator
#         integer_val = int(integer_str)
#         return f"{integer_val:,}.{decimal_str}"
#     return s


# def extract(text, words=None):
#     data = {}

#     # ── Beneficiary name ──────────────────────────────────────────────────────
#     raw_name = next(
#         (l.strip() for l in text.splitlines() if l.strip()), ""
#     )
#     data["beneficiary_name"] = _normalize_name(raw_name)

#     # ── Beneficiary country: VAT ID (compressed or spaced) ───────────────────
#     m_vat = re.search(
#         r"(?:Our\s*VAT\s*ID|OurVATID)\s*:?\s*([A-Z]{2})\d+",
#         text, re.IGNORECASE,
#     )
#     vat_prefix = m_vat.group(1).upper() if m_vat else ""
#     data["beneficiary_country"] = _COUNTRY_MAP.get(vat_prefix, vat_prefix)

#     # ── Beneficiary address: RegisteredOffice line ────────────────────────────
#     # Compressed: "RegisteredOffice RobertBosch,spol.sr.o.,RobertaBosche2678,37004 CeskeBudejovice"
#     # Spaced:     "Registered Office Robert Bosch, spol. s r.o., Roberta Bosche 2678, ..."
#     m_ro = re.search(
#         r"(?:Registered\s*Office|RegisteredOffice)\s+(.+)",
#         text, re.IGNORECASE,
#     )
#     if m_ro:
#         ro = m_ro.group(1).strip()
#         # Drop entity name (up to second comma) to get pure address
#         parts = ro.split(",")
#         if len(parts) >= 3:
#             # "Robert Bosch, spol. s r.o., Roberta Bosche 2678, 370 04 ..."
#             # Skip first 1-2 parts that are the entity name
#             # Find first part that looks like a street (has digits)
#             addr_parts = []
#             skip_done = False
#             for p in parts:
#                 p = p.strip()
#                 if not skip_done:
#                     if re.search(r"\d", p):
#                         skip_done = True
#                         addr_parts.append(p)
#                 else:
#                     # Stop at "registered in" clause
#                     if re.search(r"registered\s+in|district\s+court", p, re.IGNORECASE):
#                         break
#                     addr_parts.append(p)
#             if addr_parts:
#                 addr = ", ".join(addr_parts).strip()
#                 # Drop "registered..." clause (compressed or spaced)
#                 addr = re.sub(r",?\s*registered.*$", "", addr, flags=re.IGNORECASE).strip()
#                 # Fix compressed text: "RobertaBosche2678" -> "Roberta Bosche 2678"
#                 addr = _decompress_text(addr)
#                 data["beneficiary_address"] = addr
#             else:
#                 data["beneficiary_address"] = ro
#         else:
#             data["beneficiary_address"] = ro
#     else:
#         data["beneficiary_address"] = ""

#     # ── Remitter name ─────────────────────────────────────────────────────────
#     data["remitter_country"] = "India"
#     m_name = re.search(r"Bosch\s*Ltd\.", text, re.IGNORECASE)
#     data["remitter_name"] = "Bosch Ltd." if m_name else ""

#     # ── Remitter address: "Bill to Party" block or "Sold to Party" line ───────
#     #
#     # Compressed block pattern:
#     #   Bill to PartyAddress ... Payer XXXXXXXX
#     #   BoschLtd.
#     #   TaxFulfillmentdate DD.MM.YYYY
#     #   POBox3000 / HosurRoad... 
#     #   TIRUNELVELI / BANGALORE...
#     #   627352,INDIE
#     #
#     lines = text.splitlines()
#     remitter_addr = ""

#     # Find "Bill to Party" section start
#     bill_start = None
#     for i, line in enumerate(lines):
#         if re.search(r"Bill\s*to\s*Part", line, re.IGNORECASE):
#             bill_start = i
#             break

#     if bill_start is not None:
#         addr_parts = []
#         collecting = False
#         for j in range(bill_start, min(bill_start + 15, len(lines))):
#             l = lines[j].strip()
#             # Start collecting after "Bosch Ltd." line
#             if re.search(r"Bosch\s*Ltd\.", l, re.IGNORECASE):
#                 collecting = True
#                 continue
#             if not collecting:
#                 continue
#             # Stop conditions
#             if re.search(r"(SoldtoParty|Sold\s+to\s+Party|ShiptoParty|Ship\s+to\s+Party)", l, re.IGNORECASE):
#                 break
#             # Skip date/tax lines
#             if re.search(r"(TaxFulfillment|Tax\s+Fulfillment|BillingDate|Billing\s+Date)", l, re.IGNORECASE):
#                 continue
#             if not l:
#                 continue
#             # Check for country marker + pincode
#             m_pc = re.search(r"(\d{5,6})[,\s]*(INDIE|INDIA|IN\b)", l, re.IGNORECASE)
#             if m_pc:
#                 addr_parts.append(m_pc.group(1))  # just the pincode
#                 break
#             # Normalize compressed text (e.g. "POBox3000" → "PO Box 3000")
#             l = re.sub(r"POBox(\d+)", r"PO Box \1", l)
#             addr_parts.append(l)

#         if addr_parts:
#             # Last element is the pincode; second-to-last is city
#             if len(addr_parts) >= 2:
#                 pincode = addr_parts[-1]
#                 city = addr_parts[-2].title()
#                 street_parts = addr_parts[:-2]
#                 street = ", ".join(street_parts)
#                 remitter_addr = (street + ", " if street else "") + city + " - " + pincode
#             else:
#                 remitter_addr = ", ".join(addr_parts)

#     if not remitter_addr:
#         # Fallback: parse "SoldtoParty BoschLtd.POBox3000TIRUNELVELI627352,INDIE"
#         m_sold = re.search(
#             r"(?:SoldtoParty|Sold\s+to\s+Party)\s+(?:Bosch\s*Ltd\.|BoschLtd\.)"
#             r"((?:PO\s*Box\s*|POBox)\d+)([A-Z]+)(\d{6})",
#             text, re.IGNORECASE,
#         )
#         if m_sold:
#             street = re.sub(r"POBox(\d+)", r"PO Box \1", m_sold.group(1)).strip()
#             city = m_sold.group(2).strip().title()
#             pincode = m_sold.group(3).strip()
#             remitter_addr = f"{street}, {city} - {pincode}"

#     data["remitter_address"] = remitter_addr

#     # ── Invoice number: "Billing Document XXXXXXXXX" ─────────────────────────
#     m_inv = re.search(
#         r"(?:Billing\s*Document|BillingDocument)\s+(\d+)",
#         text, re.IGNORECASE,
#     )
#     data["invoice_number"] = m_inv.group(1).strip() if m_inv else ""

#     # ── Invoice date: "Billing Date DD.MM.YYYY" ───────────────────────────────
#     m_date = re.search(
#         r"(?:Billing\s*Date|BillingDate)\s+(\d{2}\.\d{2}\.\d{4})",
#         text, re.IGNORECASE,
#     )
#     data["invoice_date"] = m_date.group(1).strip() if m_date else ""

#     # ── Amount & currency: "Total value 289.500,00 CZK" ─────────────────────
#     m_amt = re.search(
#         r"(?:Total\s*value|Totalvalue)\s+([\d.,]+)\s*(CZK|EUR|USD|JPY|GBP)",
#         text, re.IGNORECASE,
#     )
#     if m_amt:
#         data["currency"] = m_amt.group(2).upper()
#         data["amount_foreign"] = _normalize_amount(m_amt.group(1))
#     else:
#         data["amount_foreign"] = ""
#         data["currency"] = ""

#     return data