def camel_to_snake(name: str) -> str:
    """Convert camelCase or PascalCase to snake_case, keeping acronyms together (HTTPServer -> http_server)."""
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            prev_lower = name[i - 1].islower()
            next_lower = i + 1 < len(name) and name[i + 1].islower()
            if prev_lower or next_lower:
                out.append("_")
        out.append(ch.lower())
    return "".join(out)
