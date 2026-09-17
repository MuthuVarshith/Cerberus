def truncate(text: str, width: int, suffix: str = "...") -> str:
    """Shorten text to at most `width` characters, ending with `suffix` when shortened."""
    if width < 0:
        raise ValueError("width must be non-negative")
    if len(text) <= width:
        return text
    if width <= len(suffix):
        return suffix[:width]
    return text[: width - len(suffix)] + suffix
