import pdfplumber

# Minimum characters per page before OCR fallback is triggered.
# A vector-path PDF yields only the title text (e.g. "Bosch Corporation\nInvoice" = ~25 chars).
# A real text PDF yields hundreds of chars per page.
_OCR_THRESHOLD = 200


def _ocr_pdf(pdf_path):
    """Render pages as images and run Tesseract OCR. Returns (text, [])."""
    try:
        from pdf2image import convert_from_path
        import pytesseract
        import os
        tess_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if os.path.exists(tess_path):
            pytesseract.pytesseract.tesseract_cmd = tess_path
    except ImportError:
        return "", []

    pages = convert_from_path(pdf_path, dpi=200)
    text = ""
    for page_img in pages:
        page_text = pytesseract.image_to_string(page_img)
        if page_text:
            text += page_text + "\n"
    return text, []


def extract_pdf_data(pdf_path):
    text = ""
    words = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text += t + "\n"
            words.extend(page.extract_words(
                x_tolerance=2,
                y_tolerance=2,
                keep_blank_chars=False
            ))

    return text, words


def is_text_pdf(pdf_path, min_chars=_OCR_THRESHOLD):
    """Return True if the PDF has sufficient embedded text (not a scanned image).

    Use this to decide whether to run the local regex extractors.  Scanned
    (image-only) PDFs should bypass the local extractor and go directly to
    Gemini vision, which is faster and more accurate for such files.
    """
    text, _ = extract_pdf_data(pdf_path)
    return len(text.strip()) >= min_chars


def extract_pdf_data_with_ocr_fallback(pdf_path, min_chars=_OCR_THRESHOLD):
    text, words = extract_pdf_data(pdf_path)
    if len(text.strip()) >= min_chars:
        return text, words   # normal path, no OCR needed

    # Fallback: OCR
    print("OCR fallback triggered")
    return _ocr_pdf(pdf_path)


def extract_text(pdf_path):
    text, _ = extract_pdf_data_with_ocr_fallback(pdf_path)
    return text