import re, json, os
from pdf_reader import extract_pdf_data_with_ocr_fallback
from invoice_router import detect_invoice_type
from text_utils import remove_hex_strings, normalize_address
from extractors import bosch_vietnam, bosch_germany, bosch_sap, bosch_sap_de, generic, sap_se


def detect_template(text):
    if re.search(r"H[OÓ]A\s*D[OƠÓÒÔ]N\s*GIA\s*TRI\s*GIA\s*TANG", re.sub(r"[^A-Z ]", "", text.upper())):
        return "bosch_vietnam"
    if re.search(r"Billing\s*Document", text, re.IGNORECASE):
        # GmbH letterhead SAP (OCR): GERMANY in header + Gross value/Grossvalue total
        header = "\n".join(text.splitlines()[:15])
        if re.search(r"\bGERMANY\b", header) and re.search(r"Gross\s*value", text, re.IGNORECASE):
            return "bosch_sap_de"
        return "bosch_sap"
    
    # SAP SE invoices (may be wrapped in SRN Payment Request)
    if re.search(r"SAP\s+SE|Payee\s+Name.*SAP|Invoice\s+No\.\s+\d{7,12}", text, re.IGNORECASE):
        if re.search(r"Bosch\s+Global\s+Software|SRN\s+Payment\s+Request|SAP\s+Signavio", text, re.IGNORECASE):
            return "sap_se"

    if re.search(r"Robert\s+Bosch", text, re.IGNORECASE):
        return "bosch_germany"
    if re.search(r"Bosch\s+Corporation", text, re.IGNORECASE):
        return "bosch_germany"
    if re.search(r"Bosch\s+Powertrain", text, re.IGNORECASE):
        return "bosch_germany"
    # Fallback for other Bosch entities in first few lines
    first_lines = "\n".join(text.splitlines()[:5])
    if re.search(r"^Bosch\b", first_lines, re.IGNORECASE | re.MULTILINE):
        return "bosch_germany"
    return "generic"


def process_pdf(path):
    text, words = extract_pdf_data_with_ocr_fallback(path)
    text = remove_hex_strings(text)

    print(f"--- Extracted text ---")
    print(text)
    print("--- End of extracted text ---")

    inv_type = detect_template(text)
    print(f"Detected template type: {inv_type}")

    if inv_type == "bosch_vietnam":
        data = bosch_vietnam.extract(text, words)
    elif inv_type == "bosch_germany":
        data = bosch_germany.extract(text, words)
    elif inv_type == "bosch_sap_de":
        data = bosch_sap_de.extract(text, words)
    elif inv_type == "bosch_sap":
        data = bosch_sap.extract(text, words)
    elif inv_type == "sap_se":
        data = sap_se.extract(text, words)
    else:
        try:
            data = generic.extract(text, words)
        except TypeError:
            data = generic.extract(text)

    data["file"] = os.path.basename(path)
    return data


def process_file(path):
    if path.lower().endswith(".pdf"):
        return [process_pdf(path)]
    raise ValueError("Only PDF supported")


if __name__ == "__main__":
    INPUT_PATH = r"C:/Users/HP/Downloads/fwdrequesting15cbfordcin/9027584389.pdf"
    results = process_file(INPUT_PATH)
    print(json.dumps(results, indent=2, ensure_ascii=False))








# import re, json, os
# from pdf_reader import extract_pdf_data
# from invoice_router import detect_invoice_type
# from text_utils import remove_hex_strings, normalize_address
# from extractors import bosch_vietnam, bosch_germany, bosch_sap, bosch_sap_de, generic


# def detect_template(text):
#     if "HÓA ĐƠN GIÁ TRỊ GIA TĂNG" in text:
#         return "bosch_vietnam"
#     if re.search(r"Billing\s*Document", text, re.IGNORECASE):
#         # GmbH letterhead SAP (OCR): has "GERMANY" in header + "Gross value" total
#         header = "\n".join(text.splitlines()[:15])
#         if re.search(r"\bGERMANY\b", header) and re.search(r"Gross\s+value", text, re.IGNORECASE):
#             return "bosch_sap_de"
#         return "bosch_sap"
#     # All Robert Bosch entity variants (GmbH, France SAS, spol. s r.o., etc.)
#     if re.search(r"Robert\s+Bosch", text, re.IGNORECASE):
#         return "bosch_germany"
#     # Non-Robert-Bosch Bosch entities (e.g. Bosch Corporation, Japan)
#     if re.search(r"Bosch\s+Corporation", text, re.IGNORECASE):
#         return "bosch_germany"
#     # Bosch subsidiaries with Invoice No./Date Invoice labels but no "Robert Bosch" in text
#     # e.g. "Bosch Technology Licensing Administration GmbH"
#     first_lines = "\n".join(text.splitlines()[:4])
#     if re.search(r"^Bosch\s+\w", first_lines, re.IGNORECASE | re.MULTILINE):
#         if re.search(r"Invoice\s+No\.?\s*:", text, re.IGNORECASE):
#             return "bosch_germany"
#     return "generic"


# def process_pdf(path):
#     text, words = extract_pdf_data(path)
#     text = remove_hex_strings(text)

#     print(f"--- Extracted text ---")
#     print(text)
#     print("--- End of extracted text ---")

#     inv_type = detect_template(text)

#     if inv_type == "bosch_vietnam":
#         data = bosch_vietnam.extract(text, words)
#     elif inv_type == "bosch_germany":
#         data = bosch_germany.extract(text, words)
#     elif inv_type == "bosch_sap_de":
#         data = bosch_sap_de.extract(text, words)
#     elif inv_type == "bosch_sap":
#         data = bosch_sap.extract(text, words)
#     else:
#         try:
#             data = generic.extract(text, words)
#         except TypeError:
#             data = generic.extract(text)

#     data["file"] = os.path.basename(path)
#     return data


# def process_file(path):
#     if path.lower().endswith(".pdf"):
#         return [process_pdf(path)]
#     raise ValueError("Only PDF supported")


# if __name__ == "__main__":
#     INPUT_PATH = r"C:/Users/HP/Downloads/fwdrequesting15cbforrbin_citibank/7135427187.pdf"
#     results = process_file(INPUT_PATH)
#     print(json.dumps(results, indent=2, ensure_ascii=False))