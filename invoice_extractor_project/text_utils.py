
import re

def clean(text):
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[,:;]+$", "", text)
    return text

# def normalize_company(name):
#     name = re.sub(r"BoschLtd", "Bosch Ltd.", name)
#     name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
#     return name

def normalize_company(name):

    name = name.strip()

    # Fix BoschLtd
    name = re.sub(r"Bosch\s*Ltd\.?", "Bosch Ltd.", name, flags=re.I)

    # Remove duplicate punctuation
    name = re.sub(r"\.+$", ".", name)

    return name


# def normalize_address(text):

#     # Insert space between lowercase and uppercase
#     text = re.sub(r"([a-zà-ỹ])([A-ZÀ-Ỹ])", r"\1 \2", text)

#     # Insert space between letters and numbers
#     text = re.sub(r"([A-Za-zÀ-Ỹà-ỹ])(\d)", r"\1 \2", text)

#     # Insert space between numbers and letters
#     text = re.sub(r"(\d)([A-Za-zÀ-Ỹà-ỹ])", r"\1 \2", text)

#     # Fix Vietnamese common words
#     text = text.replace("Thànhphố", "Thành phố")
#     text = text.replace("ViệtNam", "Việt Nam")
#     text = text.replace("phốLiễu", "phố Liễu")

#     # Clean commas
#     text = re.sub(r"\s*,\s*", ", ", text)

#     return text.strip()

def normalize_address(text):
    if not text:
        return ""

    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)

    text = text.replace("LeDuan", "Le Duan")

    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip(", ")

def clean_value(v):
    if not v:
        return ""
    return v.strip().strip(":")

def remove_hex(text):
    if not text:
        return ""
    return re.sub(r"\b[A-F0-9]{20,}\b", "", text)

def validate_amount(v):
    if not v:
        return ""
    # Ensure it looks like a number with optional dots/commas
    if not re.match(r"^[0-9.,]+$", v):
        return ""
    return v

def remove_hex_strings(text):
    lines = []
    for l in text.splitlines():
        if re.match(r"[A-F0-9]{20,}", l):
            continue
        lines.append(l)
    return "\n".join(lines)
