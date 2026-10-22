"""Kandidaten bewerten und die Sicherheit des besten Treffers bestimmen (Spec 5.5)."""

from collections.abc import Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from mediasort.models import Candidate, Confidence, Match, ParsedName
from mediasort.parser import normalize_title

HIGH_SIMILARITY = 90
MEDIUM_SIMILARITY = 75
COMPETITOR_SIMILARITY = 85
YEAR_TOLERANCE = 1
YEAR_BONUS = 10
YEAR_MALUS = 20
MAX_ALTERNATIVES = 5


@dataclass(frozen=True)
class _Scored:
    candidate: Candidate
    similarity: float
    points: float
    year_ok: bool


def title_similarity(query: str, cand: Candidate) -> float:
    """Höchste Ähnlichkeit (0..100) zwischen ``query`` und deutschem, englischem oder Originaltitel."""
    q = normalize_title(query) if query else ""
    if not q:
        return 0.0
    best = 0.0
    for title in (cand.title, cand.english_title, cand.original_title):
        t = normalize_title(title) if title else ""
        if t:
            best = max(best, fuzz.token_sort_ratio(q, t))
    return best


def _score(parsed: ParsedName, cand: Candidate) -> _Scored:
    similarity = title_similarity(parsed.title or "", cand)
    points = similarity
    year_ok = False
    if parsed.year is not None and cand.year is not None:
        if abs(parsed.year - cand.year) <= YEAR_TOLERANCE:
            points += YEAR_BONUS
            year_ok = True
        else:
            points -= YEAR_MALUS
    return _Scored(cand, similarity, points, year_ok)


def _confidence(
    parsed: ParsedName,
    best: _Scored,
    rivals: Sequence[_Scored],
    episode_exists: bool | None,
) -> Confidence:
    competing = [r for r in rivals if r.similarity >= COMPETITOR_SIMILARITY]
    no_year_is_fine = parsed.year is None and not competing
    high = best.similarity >= HIGH_SIMILARITY and (best.year_ok or no_year_is_fine)
    if high and episode_exists is not False:
        return Confidence.HIGH
    # „Mehrere ähnliche Kandidaten ohne Jahr“ ist hier schon durch ≥ 75 abgedeckt: Ohne
    # entscheidendes Jahr hat der Beste stets die höchste Ähnlichkeit, also ≥ 85.
    if best.similarity >= MEDIUM_SIMILARITY:
        return Confidence.MEDIUM
    return Confidence.LOW


def match(
    parsed: ParsedName,
    candidates: Sequence[Candidate],
    episode_exists: bool | None = None,
) -> Match:
    """Bester Kandidat mit Sicherheit; ``episode_exists=False`` begrenzt auf höchstens „mittel“."""
    scored = sorted(
        (_score(parsed, c) for c in candidates),
        key=lambda s: (s.points, s.candidate.popularity),
        reverse=True,
    )
    if not scored:
        return Match(None, Confidence.LOW, (), 0.0)
    best, rivals = scored[0], scored[1:]
    return Match(
        best.candidate,
        _confidence(parsed, best, rivals, episode_exists),
        tuple(r.candidate for r in rivals[:MAX_ALTERNATIVES]),
        best.points,
    )
