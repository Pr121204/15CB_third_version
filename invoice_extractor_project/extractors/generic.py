
import re
from text_utils import clean

def extract(text):
    result = {}
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    result["beneficiary_name"] = lines[0] if lines else ""
    result["beneficiary_address"] = ", ".join(lines[1:3]) if len(lines) > 2 else ""

    m = re.search(r"\b(Germany|India|France|Japan|Vietnam)\b", text)
    result["beneficiary_country"] = m.group(1) if m else ""

    result["remitter_name"] = ""
    result["remitter_address"] = ""
    result["remitter_country"] = "India"

    m = re.search(r"Invoice\s*(?:No\.?|#)\s*[:\s]+([A-Z0-9]+)", text)
    result["invoice_number"] = m.group(1) if m else ""

    m = re.search(r"(\d{2}[./]\d{2}[./]\d{4})", text)
    result["invoice_date"] = m.group(1) if m else ""

    m = re.search(r"([\d,.]+)\s*(USD|EUR|GBP)", text)
    if m:
        result["amount_foreign"] = m.group(1)
        result["currency"] = m.group(2)
    else:
        result["amount_foreign"] = ""
        result["currency"] = ""

    return result
