"""Title helpers shared by scrapers and the matching engine."""
import re
import unicodedata

_YEAR_SUFFIX_RE = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")
_ARTICLE_RE = re.compile(r"^(the|a|an) ")


def split_title_year(title, year=None):
    """'Heat (1995)' -> ('Heat', 1995). An explicit year wins over one in the title."""
    if year is not None:
        year = int(year) if str(year).isdigit() else None
    if not title:
        return title, year
    match = _YEAR_SUFFIX_RE.match(title.strip())
    if match:
        title, year = match.group(1), year or int(match.group(2))
    return title.strip(), year


def norm_title(title):
    """Comparison key: accents/case/punctuation/dashes/leading article/trailing (year) removed.

    Non-Latin titles fall back to a Unicode-aware key instead of collapsing to an empty string.
    """
    text = (title or "").strip()
    text = _YEAR_SUFFIX_RE.sub(r"\1", text)
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower().replace("&", " and ")
    key = re.sub(r"[^a-z0-9]+", " ", folded).strip()
    if not key:
        key = re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()
        return key
    return _ARTICLE_RE.sub("", key)


def clean_id(value):
    """Provider IDs as stripped text, or None when missing/blank/zero."""
    if value is None:
        return None
    value = str(value).strip()
    return value if value and value != "0" else None
