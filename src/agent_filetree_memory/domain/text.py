"""Exact edits shared by persistence adapters; never fuzzy-match private text."""

from .errors import EditConflict, QuotaExceeded


def replace_text(
    content: str, old_text: str, new_text: str, *, replace_all: bool, max_bytes: int
) -> str:
    if not old_text:
        raise ValueError("old_text must be non-empty")
    count = content.count(old_text)
    if count == 0:
        raise EditConflict(
            "old_text was not found; read the document and copy exact text, including whitespace"
        )
    if count > 1 and not replace_all:
        raise EditConflict(
            "old_text is ambiguous; include surrounding text or set replace_all=true"
        )
    if old_text == new_text:
        raise EditConflict("old_text and new_text are identical; no edit was made")
    replacements = count if replace_all else 1
    size = len(content.encode("utf-8")) + replacements * (
        len(new_text.encode("utf-8")) - len(old_text.encode("utf-8"))
    )
    if size > max_bytes:
        raise QuotaExceeded("edited document exceeds the content limit")
    return content.replace(old_text, new_text, replacements)
