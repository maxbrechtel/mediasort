from mediasort.matcher import match, title_similarity
from mediasort.models import Candidate, Confidence, MediaKind, ParsedName

EP = MediaKind.EPISODE


def P(title, year, kind=MediaKind.MOVIE):
    return ParsedName(kind, title, year, None, (), (), False, None, False)


def TV(id, title, year, pop=1.0):
    return Candidate(id, EP, title, title, None, year, pop)


M = lambda id, t, y, pop=1.0, en=None, orig=None: Candidate(id, MediaKind.MOVIE, t, orig or t, en, y, pop)


def test_exact_title_and_year_is_high():
    assert match(P("Matrix", 1999), [M(603, "Matrix", 1999, en="The Matrix")]).confidence is Confidence.HIGH


def test_english_title_matches():
    m = match(P("The Matrix", 1999), [M(603, "Matrix", 1999, en="The Matrix")])
    assert m.candidate.tmdb_id == 603


def test_year_decides_between_remakes():
    m = match(P("Dune", 2021), [M(841, "Dune", 1984, pop=50), M(438631, "Dune", 2021, pop=40)])
    assert m.candidate.tmdb_id == 438631 and m.confidence is Confidence.HIGH


def test_no_year_and_two_similar_candidates_is_medium():
    m = match(P("Dune", None), [M(841, "Dune", 1984, pop=50), M(438631, "Dune", 2021, pop=40)])
    assert m.confidence is Confidence.MEDIUM and len(m.alternatives) == 1


def test_partial_title_is_not_high():  # "M3GAN" vs "M3GAN 2.0" (Ähnlichkeit ≈ 71), Jahr passt
    m = match(P("M3GAN", 2025), [M(505642, "M3GAN", 2022), M(1071585, "M3GAN 2.0", 2025)])
    assert m.candidate.tmdb_id == 1071585 and m.confidence is not Confidence.HIGH


def test_missing_episode_downgrades():
    m = match(P("Reacher", None, kind=EP), [TV(108978, "Reacher", 2022)], episode_exists=False)
    assert m.confidence is not Confidence.HIGH


def test_nothing_found_is_low():
    m = match(P("y2x24go2wrcp", None), [])
    assert m.candidate is None and m.confidence is Confidence.LOW
    assert m.score == 0.0 and m.alternatives == ()


def test_no_year_single_candidate_is_high():
    assert match(P("Matrix", None), [M(603, "Matrix", 1999)]).confidence is Confidence.HIGH


def test_year_distance_one_is_ok_two_is_not():
    assert match(P("Matrix", 2000), [M(603, "Matrix", 1999)]).confidence is Confidence.HIGH
    assert match(P("Matrix", 2001), [M(603, "Matrix", 1999)]).confidence is Confidence.MEDIUM


def test_missing_candidate_year_is_not_high():
    assert match(P("Matrix", 1999), [M(603, "Matrix", None)]).confidence is Confidence.MEDIUM


def test_episode_exists_true_or_unknown_keeps_high():
    cands = [TV(108978, "Reacher", 2022)]
    assert match(P("Reacher", 2022, kind=EP), cands, episode_exists=True).confidence is Confidence.HIGH
    assert match(P("Reacher", 2022, kind=EP), cands, episode_exists=None).confidence is Confidence.HIGH


def test_missing_episode_caps_at_medium_not_below():
    m = match(P("Reacher", 2022, kind=EP), [TV(108978, "Reacher", 2022)], episode_exists=False)
    assert m.confidence is Confidence.MEDIUM


def test_poor_similarity_is_low():
    m = match(P("Completely Different", 2000), [M(1, "Matrix", 2000)])
    assert m.confidence is Confidence.LOW and m.candidate.tmdb_id == 1


def test_popularity_breaks_ties():
    m = match(P("Dune", 2021), [M(1, "Dune", 2021, pop=1), M(2, "Dune", 2021, pop=9)])
    assert m.candidate.tmdb_id == 2


def test_alternatives_capped_at_five_and_exclude_chosen():
    cands = [M(i, "Dune", 2021, pop=float(i)) for i in range(1, 9)]
    m = match(P("Dune", 2021), cands)
    assert len(m.alternatives) == 5
    assert m.candidate not in m.alternatives
    assert [a.tmdb_id for a in m.alternatives] == [7, 6, 5, 4, 3]


def test_score_is_similarity_plus_year_bonus_or_malus():
    assert match(P("Dune", 2021), [M(1, "Dune", 2021)]).score == 110.0
    assert match(P("Dune", 2021), [M(1, "Dune", 1984)]).score == 80.0
    assert match(P("Dune", None), [M(1, "Dune", 1984)]).score == 100.0


def test_title_similarity_uses_best_title_and_normalizes():
    c = M(1, "Das Boot", 1981, en="The Boat", orig="Das Boot")
    assert title_similarity("das  BOOT!", c) == 100
    assert title_similarity("The Boat", c) == 100
    assert title_similarity("Amélie", M(2, "Amelie", 2001)) == 100


def test_title_similarity_empty_is_zero():
    assert title_similarity("", M(1, "Dune", 2021)) == 0
    assert title_similarity(None, M(1, "Dune", 2021)) == 0  # type: ignore[arg-type]
    assert title_similarity("Dune", M(1, "", 2021, orig="")) == 0


def test_parsed_without_title_is_low():
    m = match(P(None, 2021), [M(1, "Dune", 2021)])
    assert m.confidence is Confidence.LOW

