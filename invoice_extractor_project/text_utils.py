
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


def normalize_address(text):

    # space between lowercase and uppercase
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)

    # space between letters and numbers
    text = re.sub(r"([a-zA-Z])(\d)", r"\1 \2", text)

    # space between numbers and letters
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)

    # remove extra commas
    text = re.sub(r"\s*,\s*", ", ", text)

    return text.strip()


def remove_hex_strings(text):
    lines = []
    for l in text.splitlines():
        if re.match(r"[A-F0-9]{20,}", l):
            continue
        lines.append(l)
    return "\n".join(lines)
