import re
import unicodedata


def slugify(text: str, separator: str = "-") -> str:
    """Lowercase ASCII slug: accents stripped, runs of non-alphanumerics collapsed to one separator."""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[a-z0-9]+", normalized.lower())
    return separator.join(words)
