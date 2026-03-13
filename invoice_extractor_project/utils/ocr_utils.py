import os
import shutil
import platform
import pytesseract
from pdf2image import convert_from_path

# Explicit fallback for Windows if PATH not configured
DEFAULT_WINDOWS_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

if platform.system() == "Windows":
    TESSERACT_PATH = (
        os.getenv("TESSERACT_PATH")
        or shutil.which("tesseract")
        or DEFAULT_WINDOWS_PATH
    )
else:
    TESSERACT_PATH = (
        os.getenv("TESSERACT_PATH")
        or shutil.which("tesseract")
        or "/usr/bin/tesseract"
    )

# Point pytesseract to the detected binary
pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

def ocr_pdf(pdf_path):
    """
    Convert PDF pages to images and perform OCR using Tesseract.
    """
    try:
        images = convert_from_path(pdf_path)
        text = ""
        for img in images:
            page_text = pytesseract.image_to_string(img)
            text += page_text + "\n"
        return text
    except Exception as e:
        print(f"OCR Error: {e}")
        return ""
