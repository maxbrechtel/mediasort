"""Sprachcodes: beliebige Schreibweisen auf zweistellige Kleinbuchstaben-Codes abbilden."""

from babelfish import Language
from babelfish.exceptions import Error as BabelfishError

_UNDETERMINED = {"und", "mul", "zxx"}


def to_alpha2(code: str) -> str | None:
    """Wandelt alpha3 (b/t), alpha2 oder englischen Namen in einen alpha2-Code (klein).

    Unbekannte oder unbestimmte Codes (``und``, ``mul``, ``zxx``) ergeben ``None``.
    """
    code = code.strip()
    if not code or code.lower() in _UNDETERMINED:
        return None
    lowered = code.lower()
    attempts = (
        lambda: Language.fromalpha3b(lowered),
        lambda: Language(lowered),
        lambda: Language.fromalpha2(lowered),
        lambda: Language.fromname(code.title()),
    )
    for attempt in attempts:
        try:
            alpha2 = attempt().alpha2
        except (BabelfishError, ValueError):
            continue
        if alpha2:
            return alpha2.lower()
    return None
