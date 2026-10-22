"""Einstellungen: ein Feld pro Zeile in der Tabelle ``settings``."""

from dataclasses import dataclass, field, fields

from sqlalchemy import select
from sqlalchemy.orm import Session

from mediasort.db import Setting


@dataclass
class Settings:
    tmdb_key: str = ""
    language: str = "de-DE"
    season_label: str = "Staffel"
    sample_bytes: int = 104_857_600
    min_age_minutes: int = 10
    exclude: list[str] = field(
        default_factory=lambda: ["incomplete", "nzbs", ".deletedByTMM", ".thumbnails", "@eaDir"]
    )
    cache_days: int = 30
    plex_url: str = ""
    plex_token: str = ""
    plex_path_map: list[tuple[str, str]] = field(default_factory=list)
    auto_enabled: bool = False
    poll_minutes: int = 15
    confirmed_trash_warning: bool = False


def load_settings(session: Session) -> Settings:
    stored = {row.key: row.value_json for row in session.scalars(select(Setting))}
    values = {f.name: stored[f.name] for f in fields(Settings) if f.name in stored}
    if "plex_path_map" in values:
        values["plex_path_map"] = [tuple(pair) for pair in values["plex_path_map"]]
    return Settings(**values)


def save_settings(session: Session, s: Settings) -> None:
    for f in fields(Settings):
        session.merge(Setting(key=f.name, value_json=getattr(s, f.name)))
    session.commit()
