import re
from typing import Any, Dict, Sequence


_CID_TOKEN_RE = re.compile(r"\(cid:\d+\)")
_REPLACEMENT_CHAR_RE = re.compile("\ufffd")


def assess_pdf_text_quality(pages_text: Sequence[str]) -> Dict[str, Any]:
    """Summarize whether a native PDF text layer looks usable.

    Some PDFs expose a long embedded text layer that is clearly corrupt
    (for example repeated ``(cid:123)`` tokens from a broken font map).
    Those files should be routed to image/OCR extraction even when the raw
    character count is high.
    """
    pages = [str(page or "") for page in pages_text if str(page or "").strip()]
    if not pages:
        return {
            "usable": False,
            "reason": "empty",
            "pages": 0,
            "total_chars": 0,
            "total_lines": 0,
            "cid_tokens": 0,
            "cid_lines": 0,
            "bad_line_ratio": 0.0,
            "max_page_bad_line_ratio": 0.0,
            "replacement_chars": 0,
        }

    total_chars = sum(len(page) for page in pages)
    cid_tokens = 0
    cid_lines = 0
    total_lines = 0
    max_page_bad_line_ratio = 0.0

    for page in pages:
        lines = [line for line in page.splitlines() if line.strip()]
        page_total_lines = len(lines)
        page_cid_lines = sum(1 for line in lines if _CID_TOKEN_RE.search(line))
        total_lines += page_total_lines
        cid_lines += page_cid_lines
        cid_tokens += len(_CID_TOKEN_RE.findall(page))
        if page_total_lines:
            max_page_bad_line_ratio = max(
                max_page_bad_line_ratio,
                page_cid_lines / page_total_lines,
            )

    bad_line_ratio = cid_lines / total_lines if total_lines else 0.0
    replacement_chars = len(_REPLACEMENT_CHAR_RE.findall("".join(pages)))

    usable = True
    reason = "ok"

    # Broken text layers typically contain many cid tokens across a meaningful
    # share of lines, even when the total character count is high.
    if cid_tokens >= 20 and (bad_line_ratio >= 0.25 or max_page_bad_line_ratio >= 0.5):
        usable = False
        reason = "cid_noise"
    elif replacement_chars >= 20:
        usable = False
        reason = "replacement_chars"

    return {
        "usable": usable,
        "reason": reason,
        "pages": len(pages),
        "total_chars": total_chars,
        "total_lines": total_lines,
        "cid_tokens": cid_tokens,
        "cid_lines": cid_lines,
        "bad_line_ratio": bad_line_ratio,
        "max_page_bad_line_ratio": max_page_bad_line_ratio,
        "replacement_chars": replacement_chars,
    }
