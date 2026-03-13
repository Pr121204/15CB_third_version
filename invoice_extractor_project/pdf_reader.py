import pdfplumber
from utils.ocr_utils import ocr_pdf

def extract_pdf_data(pdf_path):
    text = ""
    words = []
    
    # 1. Try extracting text and words using pdfplumber
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
            
    # 2. Fallback to OCR if text layer is missing or too small
    if not text or len(text.strip()) < 10:
        print("OCR fallback triggered")
        text = ocr_pdf(pdf_path)
        
    return text, words

def extract_text(pdf_path):
    text, _ = extract_pdf_data(pdf_path)
    return text
