"""
Extractor for Syntegon Technology GmbH invoices.
Key layout features:
  - "Number XXXXXXXX" for invoice number (not "Invoice No.")
  - "Invoice date: DD.MM.YYYY"
  - Header line "Syntegon Technology GmbH, PO box 1127, D-71301 Waiblingen" → beneficiary address
  - "Our VAT Reg. No.: DE..." → beneficiary country
  - "Recipient of Supply: <name>,<addr parts>..." → remitter name + address (most reliable)
  - "Final amount: (EUR) 180,637.35" → amount + currency
  - "INDIA" at end of addressee block → remitter_country
"""
import re

_COUNTRY_MAP = {
    "DE": "Germany", "FR": "France", "GB": "UK", "NL": "Netherlands",
    "IT": "Italy", "ES": "Spain", "BE": "Belgium", "AT": "Austria",
    "SE": "Sweden", "PL": "Poland", "CZ": "Czech Republic",
    "HU": "Hungary", "RO": "Romania", "PT": "Portugal", "JP": "Japan",
    "CH": "Switzerland", "DK": "Denmark", "FI": "Finland", "NO": "Norway",
}


def extract(text, words=None):
    data = {}

    # ── Beneficiary name: first non-empty line ────────────────────────────────
    data["beneficiary_name"] = next(
        (l.strip() for l in text.splitlines() if l.strip()), ""
    )

    # ── Beneficiary address: from the header "EntityName, <address>" line ─────
    # e.g. "Syntegon Technology GmbH, PO box 1127, D-71301 Waiblingen"
    m_addr = re.search(
        r"Syntegon\s+Technology\s+GmbH,\s*(.+?)(?:\s+Customer\s*:|$)",
        text, re.IGNORECASE,
    )
    data["beneficiary_address"] = m_addr.group(1).strip() if m_addr else ""

    # ── Beneficiary country: "Our VAT Reg. No.: DE..." ────────────────────────
    m_vat = re.search(
        r"(?:Our\s+)?VAT\s+Reg\.?\s+No\.?\s*:?\s*([A-Z]{2})\d+",
        text, re.IGNORECASE,
    )
    if m_vat:
        data["beneficiary_country"] = _COUNTRY_MAP.get(
            m_vat.group(1).upper(), m_vat.group(1).upper()
        )
    else:
        # Fallback: "Waiblingen" / "Stuttgart" → Germany
        if re.search(r"\b(Waiblingen|Stuttgart|Germany)\b", text, re.IGNORECASE):
            data["beneficiary_country"] = "Germany"
        else:
            data["beneficiary_country"] = ""

    # ── Remitter: from "Recipient of Supply:" line ────────────────────────────
    # Format: "Recipient of Supply: Name,Street,City,Pincode Place"
    # This is the most reliable source — addressee block has interleaved noise.
    data["remitter_country"] = "India"

    m_ros = re.search(r"Recipient\s+of\s+Supply\s*:\s*(.+)", text, re.IGNORECASE)
    if m_ros:
        ros = m_ros.group(1).strip()
        parts = [p.strip() for p in ros.split(",")]

        # Split name from address:
        # Name = accumulate parts that look like a company name
        # Address starts at the first part that has digits or is a bare geography word
        name_parts = []
        addr_parts = []
        name_done = False

        for p in parts:
            if not name_done:
                is_addr_token = (
                    re.search(r"\d", p) or
                    (name_parts and not re.search(
                        r"(Private|Limited|Ltd\.?|Technology|Syntegon|Bosch|GmbH|Inc\.?|Corp\.?)",
                        p, re.IGNORECASE,
                    ))
                )
                if is_addr_token:
                    name_done = True
                    addr_parts.append(p)
                else:
                    name_parts.append(p)
            else:
                addr_parts.append(p)

        data["remitter_name"] = ", ".join(name_parts)
        data["remitter_address"] = ", ".join(addr_parts)
    else:
        # Fallback: parse addressee block ending at INDIA/INDIE
        m_block = re.search(
            r"\n((?:.+\n){1,8}?)"  # up to 8 lines
            r"(?:INDIA|INDIE)\b",
            text, re.IGNORECASE,
        )
        if m_block:
            block_lines = [l.strip() for l in m_block.group(1).splitlines() if l.strip()]
            data["remitter_name"] = block_lines[0] if block_lines else ""
            data["remitter_address"] = ", ".join(block_lines[1:])
        else:
            data["remitter_name"] = ""
            data["remitter_address"] = ""

    # ── Invoice number: "Number XXXXXXXX" ────────────────────────────────────
    m_inv = re.search(r"(?:^|\n)Number\s+([A-Z0-9]+)", text, re.MULTILINE | re.IGNORECASE)
    data["invoice_number"] = m_inv.group(1).strip() if m_inv else ""

    # ── Invoice date: "Invoice date: DD.MM.YYYY" ──────────────────────────────
    m_date = re.search(
        r"Invoice\s+date\s*:\s*(\d{2}[./]\d{2}[./]\d{4})",
        text, re.IGNORECASE,
    )
    data["invoice_date"] = m_date.group(1).strip() if m_date else ""

    # ── Amount & currency: "Final amount: (EUR) 180,637.35" ──────────────────
    m_amt = re.search(
        r"Final\s+amount\s*:\s*\(?(EUR|USD|GBP|JPY|CZK|CHF)\)?\s*([\d,]+\.\d{2})",
        text, re.IGNORECASE,
    )
    if m_amt:
        data["currency"] = m_amt.group(1).upper()
        data["amount_foreign"] = m_amt.group(2).strip()
    else:
        # Fallback: "Sub Total net X.XX EUR"
        m_sub = re.search(
            r"Sub\s+Total\s+net\s+[\d,.]+\s+(EUR|USD|GBP|JPY|CZK|CHF)\s+([\d,]+\.\d{2})",
            text, re.IGNORECASE,
        )
        data["currency"] = m_sub.group(1).upper() if m_sub else ""
        data["amount_foreign"] = m_sub.group(2).strip() if m_sub else ""

    return data
