"""Choose the language of host-generated text."""


def prefers_chinese(accept_language: str) -> bool:
    """Negotiate the two sets of host labels using the browser's language preferences."""
    best = 0.0
    preferred = "en"
    for entry in accept_language.lower().split(","):
        tag, _, weight = entry.strip().partition(";")
        lang = tag.split("-")[0]
        if lang not in ("zh", "en"):
            continue
        try:
            quality = float(weight.strip().removeprefix("q=")) if weight else 1.0
        except ValueError:
            continue
        if best < quality <= 1:
            best, preferred = quality, lang
    return preferred == "zh"
