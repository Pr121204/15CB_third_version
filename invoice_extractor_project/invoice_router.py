
import re

def detect_invoice_type(text):
    if re.search(r"HÓA\s*ĐƠN|VNPT|Việt\s*Nam", text, re.I):
        return "bosch_vietnam"
    if re.search(r"Billing\s*Document|Robert\s*Bosch", text, re.I):
        return "bosch_sap"
    if re.search(r"Syntegon\s+Technology", text, re.IGNORECASE):
        return "syntegon"
    return "generic"
