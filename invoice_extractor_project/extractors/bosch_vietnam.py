
import re
from text_utils import clean, normalize_company, normalize_address

def extract(text):
    result = {}

    m = re.search(r"Customer\s*Name.*?\n([^\n]+)", text, re.IGNORECASE)
    result["remitter_name"] = normalize_company(m.group(1)) if m else ""

    m = re.search(r"Address.*?\n(.*?)\nIndia", text, re.DOTALL | re.IGNORECASE)
    if m:
        block = m.group(1)
        addr_lines = []
        for l in block.splitlines():
            l = l.strip()
            if not l: 
                continue
            if re.match(r"[A-F0-9]{20,}", l): 
                continue
            if "Tax" in l or "Mã" in l: 
                continue
            addr_lines.append(l)
        result["remitter_address"] = normalize_address(", ".join(addr_lines))
    else:
        result["remitter_address"] = ""

    result["remitter_country"] = "India"

    result["beneficiary_name"] = "Bosch Global Software Technologies Company Limited"

    m = re.search(r"Bosch Global Software Technologies.*?\n(.*?)\nĐT", text, re.DOTALL)
    result["beneficiary_address"] = clean(m.group(1)) if m else ""

    result["beneficiary_country"] = "Vietnam"

    m = re.search(r"Invoice\s*No.*?:\s*([A-Z0-9]+)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"Số\s*hoá\s*đơn.*?:\s*([A-Z0-9]+)", text, re.IGNORECASE)
    result["invoice_number"] = m.group(1) if m else ""

    m = re.search(r"Invoice\s*Date.*?:\s*([\d./]+)", text, re.IGNORECASE)
    result["invoice_date"] = m.group(1) if m else ""

    m = re.search(r"Total\s*Amount.*?:\s*([\d.,]+)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"Tổng\s*tiền\s*thanh\s*toán.*?:\s*([\d.,]+)", text, re.IGNORECASE)
    result["amount_foreign"] = m.group(1) if m else ""

    m = re.search(r"\b(USD|EUR|JPY|GBP)\b", text)
    result["currency"] = m.group(1) if m else ""

    return result
