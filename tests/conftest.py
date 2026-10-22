"""Gemeinsame Test-Helfer (aus Tests importierbar: ``from conftest import A, MI``)."""

import pytest

from mediasort.db import init_db, make_engine
from mediasort.models import AudioStream, MediaInfo


def A(codec, language, channels, default=False, profile=None) -> AudioStream:
    return AudioStream(codec=codec, profile=profile, channels=channels, language=language, default=default)


def MI(width, height, video_codec, hdr, audio_list) -> MediaInfo:
    return MediaInfo(width=width, height=height, video_codec=video_codec, hdr=hdr, audio=tuple(audio_list))


@pytest.fixture
def sessionmaker_(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    yield init_db(engine)
    engine.dispose()


@pytest.fixture
def session(sessionmaker_):
    with sessionmaker_() as s:
        yield s
