import pytest

from mediasort.langs import to_alpha2


@pytest.mark.parametrize("code,expected", [
    ("ger", "de"), ("deu", "de"), ("de", "de"), ("German", "de"), ("german", "de"),
    ("eng", "en"), ("en", "en"), ("fre", "fr"), ("und", None), ("", None), ("xx1", None),
])
def test_to_alpha2(code, expected):
    assert to_alpha2(code) == expected
