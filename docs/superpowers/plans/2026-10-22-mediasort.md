# mediasort Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eine Weboberfläche in Docker, die Film- und Seriendateien erkennt, mit TMDB abgleicht, nach festem Schema benennt, in Plex-Bibliotheken einsortiert und optional Eingangsordner automatisch überwacht.

**Architecture:** Ein FastAPI-Prozess mit Jinja2/HTMX-Oberfläche und SQLite unter `/config`. Fachlogik in kleinen Modulen (parser, mediainfo, quality, namer, scanner, tmdb, matcher, library, planner, executor, plex, watcher); die meisten sind reine Funktionen. Ein einziger Hintergrund-Worker führt Scans und Aufträge nacheinander aus.

**Tech Stack:** Python ≥ 3.12, FastAPI, Uvicorn, Jinja2, HTMX 2.0.4 (lokal ausgeliefert), SQLAlchemy 2, httpx, guessit, babelfish, rapidfuzz, ffprobe/ffmpeg, pytest.

**Spec:** `docs/superpowers/specs/2026-10-22-mediasort-design.md`

## Global Constraints

- Python `>=3.12`; Paket im `src/`-Layout: `src/mediasort/`.
- Oberfläche, Fehlermeldungen und Protokolltexte auf Deutsch; Bezeichner im Code englisch.
- Tests laufen ohne Netzwerk. TMDB und Plex werden in Tests über `httpx.MockTransport` bedient.
- **Nie überschreiben:** Keine Funktion darf eine existierende Datei ersetzen. Unterlegene Dubletten werden nur nach ausdrücklicher Entscheidung gelöscht.
- Standardwerte (in `Settings`): Sprache `de-DE`, Staffelbezeichnung `Staffel`, Sample-Grenze `104_857_600` Bytes (100 MB), Mindestalter `10` Minuten, Abfrageintervall `15` Minuten, Ausschlussliste `["incomplete", "nzbs", ".deletedByTMM", ".thumbnails", "@eaDir"]`, Automatik-Hauptschalter aus, TMDB-Cache `30` Tage.
- Videoendungen: `mkv mp4 m4v avi mov wmv ts m2ts iso`. Untertitelendungen: `srt ass ssa sub idx sup vtt`.
- Sicherheit: hoch ≥ 90, mittel ≥ 75, Konkurrenzschwelle 85 (Spec 5.5).
- Zielnamen werden in Unicode-NFC geschrieben.
- Container: Port `8080`, Konfiguration `/config`, Medien `/media` (identisch zur Plex-VM), läuft als `user: "${PUID}:${PGID}"`, setzt keine Dateirechte.
- Jeder Commit endet mit den Zeilen `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` und `Claude-Session: https://claude.ai/code/session_01D5LXVrW6fLF9XJNAcGdsX1`.

## Review Focus

1. **Umlaute in NFD-Form** (z. B. `König` von einem Mac auf die Freigabe geschrieben): Vergleiche, Index-Suche und Zielnamen müssen NFC und NFD als gleich behandeln. → Tests in Task 4 (`test_target_names_are_nfc`) und Task 9 (`test_finds_nfd_folder`).
2. **Zwei Releases derselben Folge im selben Scan** (z. B. `Reacher.S04E05` in AV1 und h265, beide 2160p) ergeben denselben Zielpfad: Der zweite Vorschlag muss ein Konflikt sein, kein stilles Überschreiben. → Test in Task 10 (`test_same_target_in_batch_becomes_conflict`).
3. **SMB-Einbindung fehlt** (`nofail`, `/media` leer): Scan und Auftrag müssen abbrechen, statt Bibliotheksordner auf der lokalen VM-Platte anzulegen. → Test in Task 11 (`test_missing_library_root_aborts_without_creating`).
4. **TMDB nicht erreichbar oder 429:** Der Scan darf nicht abstürzen; betroffene Vorschläge bekommen Sicherheit niedrig und einen Fehlertext. → Tests in Task 7 (`test_retries_once_on_429`) und Task 10 (`test_tmdb_down_marks_items`).
5. **Serienfolge in einer Film-Bibliothek** (`Chernobyl.2019.E05…` liegt in `Filme_2D`, Ordnertyp fest „Film“): Der Vorschlag darf nicht als Film einsortiert werden, sondern bekommt Sicherheit niedrig mit Hinweis „sieht aus wie eine Serienfolge“. → Test in Task 10 (`test_episode_in_movie_folder_is_flagged`).

---

## Dateistruktur

```
pyproject.toml
Dockerfile
docker-compose.yml
.github/workflows/ci.yml
README.md
src/mediasort/
  __init__.py
  models.py        # Dataclasses und Enums, die alle Module teilen
  langs.py         # Sprachcodes → zweistellig
  quality.py       # Labels (Auflösung, Codec, HDR, Audio), Tonspurwahl, Sprachen-Tag, Qualitätsvergleich
  mediainfo.py     # ffprobe aufrufen und in MediaInfo übersetzen
  parser.py        # Datei-/Ordnernamen → ParsedName
  namer.py         # Bereinigung, Zielpfade, Untertitelnamen
  scanner.py       # Dateisystem → VideoFile-Liste
  db.py            # SQLAlchemy-Modelle, Session, Cache
  settings.py      # Settings-Dataclass laden/speichern
  tmdb.py          # TMDB-Client
  matcher.py       # Kandidaten bewerten → Match
  library.py       # Bibliotheksindex, NFO, vorhandene Folgen
  planner.py       # Scan → Vorschläge (Items) inkl. Konflikte
  executor.py      # Vorschläge ausführen, aufräumen, protokollieren
  plex.py          # Plex-Client, Pfadzuordnung, Teilscans
  jobs.py          # Einzelner Hintergrund-Worker
  watcher.py       # Automatische Ordnerüberwachung
  web/
    app.py         # create_app(), Abhängigkeiten
    routes_main.py      # Übersicht, Scan starten, Auftragsstatus
    routes_review.py    # Prüfliste, Korrektur, Ignorieren, Ausführen, Konflikte
    routes_admin.py     # Bibliothek prüfen, Verlauf, Ignoriert, Einstellungen
    templates/*.html
    static/htmx.min.js, static/style.css
tests/
  conftest.py
  test_*.py        # je Modul
  fixtures/tmdb/*.json, fixtures/plex/*.json, fixtures/ffprobe/*.json
```

---

### Task 1: Projektgerüst, gemeinsame Typen, Sprachcodes

**Files:**
- Create: `pyproject.toml`, `src/mediasort/__init__.py`, `src/mediasort/models.py`, `src/mediasort/langs.py`, `tests/conftest.py`, `tests/test_langs.py`

**Interfaces:**
- Produces (`models.py`, alle `@dataclass(frozen=True)` bzw. `StrEnum`):
  - `MediaKind(StrEnum)`: `MOVIE="movie"`, `EPISODE="episode"`
  - `FolderType(StrEnum)`: `AUTO="auto"`, `MOVIE="movie"`, `SERIES="series"`, `MOVIE3D="3d"`
  - `Confidence(StrEnum)`: `HIGH="hoch"`, `MEDIUM="mittel"`, `LOW="niedrig"`
  - `ItemStatus(StrEnum)`: `NEW="new"`, `CONFIRMED="confirmed"`, `IGNORED="ignored"`, `DONE="done"`, `ERROR="error"`
  - `AutoLevel(StrEnum)`: `OFF="off"`, `DETECT="detect"`, `AUTO="auto"`
  - `ParsedName(kind: MediaKind|None, title: str|None, year: int|None, season: int|None, episodes: tuple[int, ...], languages: tuple[str, ...], is_3d: bool, format_3d: str|None, is_sample: bool)`
  - `AudioStream(codec: str, profile: str|None, channels: int, language: str|None, default: bool)` – `language` zweistellig klein oder `None`
  - `MediaInfo(width: int, height: int, video_codec: str, hdr: str|None, audio: tuple[AudioStream, ...])` – `video_codec` roher ffprobe-Name, `hdr` in `{"DV","HDR",None}`
  - `Subtitle(path: Path, language: str|None, forced: bool)`
  - `VideoFile(path: Path, size: int, mtime: float, root: Path, subtitles: tuple[Subtitle, ...])`
  - `Candidate(tmdb_id: int, kind: MediaKind, title: str, original_title: str, english_title: str|None, year: int|None, popularity: float)`
  - `Match(candidate: Candidate|None, confidence: Confidence, alternatives: tuple[Candidate, ...], score: float)`
- Produces (`langs.py`): `to_alpha2(code: str) -> str | None`

- [ ] **Step 1: `pyproject.toml` anlegen**

Abhängigkeiten: `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`, `sqlalchemy>=2`, `httpx`, `guessit`, `babelfish`, `rapidfuzz`. Dev-Extra `test`: `pytest`, `pytest-timeout`. `[tool.pytest.ini_options] testpaths=["tests"]`, `timeout=60`. Entry point `mediasort = "mediasort.web.app:main"`.

- [ ] **Step 2: Failing test `tests/test_langs.py`**

```python
@pytest.mark.parametrize("code,expected", [
    ("ger", "de"), ("deu", "de"), ("de", "de"), ("German", "de"), ("german", "de"),
    ("eng", "en"), ("en", "en"), ("fre", "fr"), ("und", None), ("", None), ("xx1", None),
])
def test_to_alpha2(code, expected):
    assert to_alpha2(code) == expected
```

- [ ] **Step 3: Run** `pip install -e '.[test]' && pytest tests/test_langs.py -v` → FAIL (ImportError)

- [ ] **Step 4: Implement `models.py` und `to_alpha2`** – babelfish: der Reihe nach `Language.fromalpha3b`, `Language(code)` (alpha3t), `Language.fromalpha2`, `Language.fromname(code.title())` versuchen; `und`/`mul`/`zxx` → `None`.

- [ ] **Step 5: Run** `pytest -v` → PASS

- [ ] **Step 6: Commit** `git add -A && git commit -m "Projektgerüst, gemeinsame Typen, Sprachcodes"`

---

### Task 2: Qualität – Labels, Tonspurwahl, Sprachen, Vergleich

**Files:**
- Create: `src/mediasort/quality.py`, `tests/test_quality.py`

**Interfaces:**
- Consumes: `AudioStream`, `MediaInfo`, `to_alpha2`
- Produces:
  - `resolution_label(width: int, height: int) -> str`
  - `video_codec_label(codec: str) -> str`
  - `audio_codec_label(codec: str, profile: str | None) -> str`
  - `audio_rank(stream: AudioStream) -> tuple[int, int]` (höher = besser)
  - `pick_audio(streams: Sequence[AudioStream]) -> AudioStream | None`
  - `languages_tag(streams: Sequence[AudioStream], fallback: Sequence[str]) -> str` → `"[DE+EN]"` oder `""`
  - `quality_key(info: MediaInfo | None, size: int) -> tuple | None` (`None` bei fehlender MediaInfo)
  - `better(a: tuple[MediaInfo|None, int], b: tuple[MediaInfo|None, int]) -> Literal["a", "b"] | None`

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.parametrize("w,h,label", [
    (3840, 2160, "2160p"), (3840, 1600, "2160p"), (1920, 800, "1080p"), (1920, 1080, "1080p"),
    (1280, 720, "720p"), (1280, 536, "720p"), (1024, 576, "576p"), (720, 576, "576p"), (720, 480, "480p"),
])
def test_resolution_label(w, h, label): ...

@pytest.mark.parametrize("codec,label", [("h264","h264"),("hevc","h265"),("av1","av1"),("vp9","vp9"),
    ("mpeg2video","mpeg2"),("mpeg4","xvid"),("vc1","vc1")])
def test_video_codec_label(codec, label): ...

@pytest.mark.parametrize("codec,profile,label", [("truehd",None,"TrueHD"),("dts","DTS-HD MA","DTS"),
    ("dts",None,"DTS"),("eac3",None,"EAC3"),("ac3",None,"AC3"),("flac",None,"FLAC"),("aac","LC","AAC"),
    ("opus",None,"Opus"),("mp3",None,"MP3"),("pcm_s16le",None,"PCM_S16LE")])
def test_audio_codec_label(codec, profile, label): ...

def test_pick_audio_prefers_best_german():
    s = [A("truehd", "en", 8, default=True), A("ac3", "de", 6), A("dts", "de", 6)]
    assert pick_audio(s).codec == "dts"

def test_pick_audio_more_channels_wins_tie():
    s = [A("ac3", "de", 2), A("ac3", "de", 6)]
    assert pick_audio(s).channels == 6

def test_pick_audio_without_german_takes_default_then_first():
    assert pick_audio([A("aac","en",2), A("ac3","fr",6,default=True)]).language == "fr"
    assert pick_audio([A("aac","en",2), A("ac3","fr",6)]).language == "en"

def test_languages_tag():
    assert languages_tag([A("ac3","en",6), A("dts","de",6), A("ac3","de",2), A("aac",None,2)], ()) == "[DE+EN]"
    assert languages_tag([A("aac",None,2)], ("de","en")) == "[DE+EN]"
    assert languages_tag([A("aac",None,2)], ()) == ""
    assert languages_tag([A("aac","fr",2), A("aac","en",2)], ()) == "[EN+FR]"

def test_better_prefers_resolution_then_hdr_then_codec():
    uhd = MI(3840, 2160, "hevc", None, [A("ac3","de",6)])
    fhd_dv = MI(1920, 1080, "hevc", "DV", [A("truehd","de",8)])
    assert better((uhd, 10), (fhd_dv, 99)) == "a"
    dv = MI(3840, 2160, "hevc", "DV", [A("ac3","de",6)])
    assert better((uhd, 10), (dv, 10)) == "b"
    av1 = MI(3840, 2160, "av1", None, [A("ac3","de",6)])
    assert better((av1, 10), (uhd, 10)) is None      # av1 und h265 gleichwertig
    assert better((av1, 10), (uhd, 11)) == "b"       # dann entscheidet die Größe

def test_better_german_audio_beats_no_german():
    de = MI(1920,1080,"h264",None,[A("ac3","de",6)]); en = MI(1920,1080,"h264",None,[A("truehd","en",8)])
    assert better((de, 1), (en, 2)) == "a"

def test_better_unknown_info_gives_no_suggestion():
    assert better((None, 5), (MI(1920,1080,"h264",None,[]), 1)) is None
```

(`A` und `MI` sind kleine Fabrik-Helfer in `tests/conftest.py`.)

- [ ] **Step 2: Run** `pytest tests/test_quality.py -v` → FAIL

- [ ] **Step 3: Implement** nach Spec 6.2–6.5 und 7.2:
  - Auflösung: Breite ≥3200 → 2160p, ≥1800 → 1080p, ≥1200 → 720p, Breite ≥900 oder Höhe ≥560 → 576p, sonst 480p.
  - Audio-Rang: TrueHD 8, DTS 7, EAC3 6, AC3 5, FLAC 4, AAC 3, Opus 2, MP3 1, sonst 0; zweites Kriterium Kanäle.
  - `quality_key`: `(Auflösungsstufe, hdr_stufe[DV=2,HDR=1,None=0], codec_stufe[av1=h265=2,h264=1,sonst 0], hat_deutsch, audio_rank(pick_audio), size)`. `better` vergleicht die Tupel; gleich → `None`; fehlende MediaInfo → `None`.
  - `languages_tag`: Deutsch zuerst, Rest alphabetisch, Großbuchstaben, `+`-verbunden; wenn kein Stream eine Sprache hat, `fallback` verwenden.

- [ ] **Step 4: Run** → PASS

- [ ] **Step 5: Commit** `"Qualitätslabels, Tonspurwahl, Qualitätsvergleich"`

---

### Task 3: mediainfo – ffprobe auslesen

**Files:**
- Create: `src/mediasort/mediainfo.py`, `tests/test_mediainfo.py`, `tests/fixtures/ffprobe/dv_truehd.json`, `tests/fixtures/ffprobe/hdr10.json`, `tests/fixtures/ffprobe/sdr_multi.json`

**Interfaces:**
- Consumes: `MediaInfo`, `AudioStream`, `to_alpha2`
- Produces:
  - `class ProbeError(Exception)`
  - `run_ffprobe(path: Path, timeout: float = 60) -> dict` – `ffprobe -v error -print_format json -show_streams -show_format <path>`; Exit-Code ≠ 0 oder Timeout → `ProbeError` mit deutscher Meldung
  - `parse_probe(data: dict) -> MediaInfo` – erster Videostream ohne `disposition.attached_pic`; kein Videostream → `ProbeError`
  - `probe(path: Path) -> MediaInfo | None` – `.iso` → `None` ohne ffprobe-Aufruf, sonst `parse_probe(run_ffprobe(path))`

- [ ] **Step 1: Fixtures anlegen** – drei gekürzte ffprobe-JSONs:
  - `dv_truehd.json`: hevc 3840×1608, `side_data_list: [{"side_data_type": "DOVI configuration record"}]`, `color_transfer: smpte2084`; Audio truehd/ger/default, ac3/eng.
  - `hdr10.json`: hevc 3840×2160, `color_transfer: smpte2084`, Audio eac3/ger.
  - `sdr_multi.json`: h264 1920×800, Audio ac3 `tags.language: "eng"` default, dts `profile: "DTS-HD MA"` `tags.language: "ger"`, aac ohne Sprache; zusätzlich ein mjpeg-Stream mit `disposition.attached_pic: 1`.

- [ ] **Step 2: Failing tests**

```python
def test_parse_dv(): mi = parse_probe(load("dv_truehd")); assert (mi.width, mi.hdr, mi.video_codec) == (3840, "DV", "hevc")
def test_parse_hdr10(): assert parse_probe(load("hdr10")).hdr == "HDR"
def test_parse_hlg(): d = load("hdr10"); d["streams"][0]["color_transfer"] = "arib-std-b67"; assert parse_probe(d).hdr == "HDR"
def test_parse_sdr_multi_audio():
    mi = parse_probe(load("sdr_multi"))
    assert mi.hdr is None and mi.width == 1920
    assert [(a.codec, a.language, a.default) for a in mi.audio] == [("ac3","en",True), ("dts","de",False), ("aac",None,False)]
    assert mi.audio[1].profile == "DTS-HD MA"
def test_iso_is_not_probed(tmp_path): p = tmp_path / "x.iso"; p.write_bytes(b"0"); assert probe(p) is None
def test_broken_file_raises(tmp_path): p = tmp_path / "x.mkv"; p.write_bytes(b"kaputt"); pytest.raises(ProbeError, probe, p)
def test_timeout_raises(monkeypatch, tmp_path): # subprocess.run wirft TimeoutExpired → ProbeError
    ...
def test_real_file_roundtrip(tmp_path):
    # ffmpeg erzeugt 1 s Testvideo 1920x800 mpeg4 + ac3 (ger, default) + aac (eng)
    ...
    mi = probe(out)
    assert mi.video_codec == "mpeg4" and [a.language for a in mi.audio] == ["de", "en"] and mi.audio[0].default
```

Für `test_real_file_roundtrip`: `ffmpeg -f lavfi -i testsrc=size=1920x800:duration=1 -f lavfi -i sine=duration=1 -f lavfi -i sine=duration=1 -map 0 -map 1 -map 2 -c:v mpeg4 -c:a:0 ac3 -c:a:1 aac -metadata:s:a:0 language=ger -metadata:s:a:1 language=eng -disposition:a:0 default -disposition:a:1 0 out.mkv`; Test wird mit `pytest.mark.skipif(shutil.which("ffmpeg") is None)` markiert.

- [ ] **Step 3: Run** → FAIL
- [ ] **Step 4: Implement** – HDR nach Spec 6.3; Sprache über `to_alpha2(tags.language)`; Kanäle aus `channels` (fehlt → 0).
- [ ] **Step 5: Run** → PASS
- [ ] **Step 6: Commit** `"mediainfo über ffprobe"`

---

### Task 4: namer – Bereinigung, Zielpfade, Untertitel

**Files:**
- Create: `src/mediasort/namer.py`, `tests/test_namer.py`

**Interfaces:**
- Consumes: `quality.*`, `MediaInfo`, `Subtitle`
- Produces:
  - `sanitize(name: str) -> str`
  - `movie_relpath(title: str, year: int | None, ext: str, info: MediaInfo | None, fallback_langs: Sequence[str] = (), format_3d: str | None = None, is_3d: bool = False) -> PurePosixPath`
  - `episode_relpath(show: str, year: int | None, season: int, episodes: Sequence[int], episode_titles: Sequence[str], ext: str, info: MediaInfo | None, fallback_langs: Sequence[str] = (), season_label: str = "Staffel") -> PurePosixPath`
  - `subtitle_targets(video_target: Path, subs: Sequence[Subtitle]) -> list[tuple[Subtitle, Path]]`
  - `MAX_NAME_BYTES = 255`

- [ ] **Step 1: Failing tests**

```python
def test_sanitize():
    assert sanitize("Mission: Impossible") == "Mission - Impossible"
    assert sanitize('AC/DC: Live? "Best" <1>|*') == "AC-DC - Live Best 1"
    assert sanitize("Titel...  ") == "Titel"
    assert sanitize("A   B") == "A B"

def test_movie_relpath():
    info = MI(1920, 800, "h264", None, [A("dts","de",6), A("ac3","en",6)])
    assert str(movie_relpath("28 Weeks Later", 2007, "mkv", info)) == \
        "28 Weeks Later (2007)/28 Weeks Later (2007) 1080p h264 DTS [DE+EN].mkv"

def test_movie_relpath_dv_and_3d():
    info = MI(1920, 1080, "h264", None, [A("dts","de",6), A("ac3","en",6)])
    assert movie_relpath("Avatar - Aufbruch nach Pandora", 2009, "mkv", info, is_3d=True, format_3d="SBS").name == \
        "Avatar - Aufbruch nach Pandora (2009) 1080p h264 DTS 3D SBS [DE+EN].mkv"
    dv = MI(3840, 1600, "hevc", "DV", [A("truehd","de",8), A("ac3","en",6)])
    assert movie_relpath("Dune", 2021, "mkv", dv).name == "Dune (2021) 2160p h265 DV TrueHD [DE+EN].mkv"

def test_iso_without_info():
    assert str(movie_relpath("Star Wars - Das Erwachen der Macht", 2015, "iso", None, is_3d=True)) == \
        "Star Wars - Das Erwachen der Macht (2015)/Star Wars - Das Erwachen der Macht (2015) 3D.iso"

def test_episode_relpath():
    info = MI(1920, 1080, "h264", None, [A("ac3","de",6), A("ac3","en",6)])
    assert str(episode_relpath("Breaking Bad", 2008, 3, [1], ["Kein Más"], "mkv", info)) == \
        "Breaking Bad (2008)/Staffel 3/Breaking Bad - S03E01 - Kein Más - 1080p [DE+EN].mkv"

def test_episode_multi_and_missing_title():
    info = MI(1280, 720, "h264", None, [A("ac3","de",2)])
    assert episode_relpath("Breaking Bad", 2008, 1, [1, 2], ["Pilot", "Die Katze ist im Sack"], "mkv", info).name == \
        "Breaking Bad - S01E01-E02 - Pilot & Die Katze ist im Sack - 720p [DE].mkv"
    assert episode_relpath("X", 2020, 0, [5], [""], "mkv", info).name == "X - S00E05 - 720p [DE].mkv"
    assert episode_relpath("X", 2020, 0, [5], [""], "mkv", info).parent.name == "Staffel 0"

def test_long_names_fit_255_bytes():
    p = episode_relpath("Serie", 2020, 1, [1], ["ä" * 300], "mkv", MI(1920,1080,"h264",None,[]))
    assert len(p.name.encode()) <= 255 and p.name.endswith(" - 1080p.mkv")

def test_target_names_are_nfc():
    nfd = unicodedata.normalize("NFD", "König der Löwen")
    p = movie_relpath(nfd, 1994, "mkv", MI(1920,1080,"h264",None,[]))
    assert unicodedata.is_normalized("NFC", str(p))

def test_subtitle_targets(tmp_path):
    v = Path("/lib/X (2020)/X (2020) 1080p h264 AC3 [DE].mkv")
    subs = [Subtitle(Path("/in/a.ger.srt"), "de", False), Subtitle(Path("/in/b.ger.srt"), "de", False),
            Subtitle(Path("/in/c.forced.eng.srt"), "en", True), Subtitle(Path("/in/d.srt"), None, False),
            Subtitle(Path("/in/e.idx"), "de", False), Subtitle(Path("/in/e.sub"), "de", False)]
    names = [t.name for _, t in subtitle_targets(v, subs)]
    assert names == ["X (2020) 1080p h264 AC3 [DE].de.srt", "X (2020) 1080p h264 AC3 [DE].de.2.srt",
                     "X (2020) 1080p h264 AC3 [DE].en.forced.srt", "X (2020) 1080p h264 AC3 [DE].srt",
                     "X (2020) 1080p h264 AC3 [DE].de.idx", "X (2020) 1080p h264 AC3 [DE].de.sub"]
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** – alle Namensteile mit `sanitize` und `unicodedata.normalize("NFC")`; ohne MediaInfo nur Titel/Jahr (+3D); ohne Jahr entfällt ` (Jahr)`. Kürzen: zuerst Folgenname, dann Titel im Dateinamen, jeweils am Zeichen, bis UTF-8-Länge ≤ 255. Untertitel-Zählung pro `(Sprache, forced, Endung)`; `.idx`/`.sub` mit gleichem Stamm zählen gemeinsam.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `"Zielnamen nach Schema"`

---

### Task 5: parser – Datei- und Ordnernamen

**Files:**
- Create: `src/mediasort/parser.py`, `tests/test_parser.py`

**Interfaces:**
- Consumes: `ParsedName`, `MediaKind`, `to_alpha2`
- Produces:
  - `normalize_german(name: str) -> str`
  - `parse_name(filename: str, parent: str | None = None) -> ParsedName`
  - `is_garbage_title(title: str | None) -> bool`
  - `normalize_title(title: str) -> str` (für Gruppierung und Vergleiche: NFKD, ohne Akzente, casefold, nur `[a-z0-9 ]`, Leerzeichen zusammengefasst)

- [ ] **Step 1: Failing tests** – Tabellentest `(filename, parent) → erwartete Felder`:

| Eingabe | kind | title | year | season | episodes | weitere |
|---|---|---|---|---|---|---|
| `Reacher.S04E05.German.DL.2160p.DV.HDR.WEBRip.AV1-WOTT.mkv` | episode | Reacher | – | 4 | (5,) | languages `("de","en")` |
| `Babylon.Berlin.UHD.Staffel3.Folge1.mkv` | episode | Babylon Berlin | – | 3 | (1,) | |
| `Serie X Staffel 2 Folge 10.mkv` | episode | Serie X | – | 2 | (10,) | |
| `Chernobyl.2019.E05.720p.BluRay.DD5.1.x264-DON.mkv` | episode | Chernobyl | 2019 | 1 | (5,) | |
| `Breaking.Bad.S01E01E02.German.720p.mkv` | episode | Breaking Bad | – | 1 | (1, 2) | languages `("de",)` |
| `bm1080-s01e02.mkv` | episode | bm1080 | – | 1 | (2,) | |
| `Final Destination 5 3D OU.mkv` | movie | Final Destination 5 | – | – | () | is_3d, format_3d `"OU"` |
| `Avatar.2009.3D.HSBS.1080p.mkv` | movie | Avatar | 2009 | | | format_3d `"HSBS"` |
| `Avatar.2009.3D.MVC.mkv` | movie | Avatar | 2009 | | | format_3d `"MVC"` |
| `Final Destination 4 3D.mkv` | movie | Final Destination 4 | | | | is_3d, format_3d `None` |
| `Star Wars - Das Erwachen der Macht (2015) 3D.iso` | movie | Star Wars Das Erwachen der Macht | 2015 | | | is_3d |
| `die.purpurnen.fluesse.2000.german.dl.2160p.uhd.bluray.x265-endstation.sample.mkv` | movie | die purpurnen fluesse | 2000 | | | is_sample |
| `y2x24go2wrcp.mkv`, parent `The Little Things 2021 German AC3D DL 2160p UHD BluRay DV HDR HEVC Remux-QfG DDL - NIMA4K org` | movie | The Little Things | 2021 | | | languages `("de","en")` |
| `The.Continental.S01E02.mkv`, parent `The Continental Aus der Welt von John Wick 2023 S01 German EAC3 DL …` | episode | The Continental | 2023 | 1 | (2,) | languages `("de","en")` |
| `matrix.mkv`, parent `The.Matrix.1999.German.DL.1080p.BluRay.x264` | movie | matrix | 1999 | | | languages `("de","en")` |
| `Breaking.Bad.S01E01.mkv`, parent `Unrelated.Show.2015.German` | episode | Breaking Bad | – | 1 | (1,) | Ordner-Jahr nicht übernommen |

Zusätzlich:
```python
def test_is_garbage_title():
    assert is_garbage_title("y2x24go2wrcp") and is_garbage_title(None) and is_garbage_title("ab")
    assert not is_garbage_title("Reacher") and not is_garbage_title("M3GAN") and not is_garbage_title("300")
def test_normalize_title():
    assert normalize_title("Der König der Löwen") == normalize_title("der.konig.der.lowen".replace(".", " "))
    assert normalize_title(unicodedata.normalize("NFD", "Löwen")) == normalize_title("Löwen")
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement**
  - `normalize_german`: Regex (case-insensitiv, an Wortgrenzen) `Staffel\s*0*(\d{1,2})` bzw. `St\.?\s*0*(\d{1,2})` und `(?:Folge|Episode)\s*0*(\d{1,3})`; der Staffel-Treffer wird an seiner Stelle durch `S{ss}E{ee}` ersetzt, der Folgen-Treffer entfernt; nur Staffel → `S{ss}`.
  - guessit mit `{"type": ...}` nicht vorgeben. `alternative_title` wird mit Leerzeichen an `title` gehängt; `title` als Liste → erstes Element.
  - Jahr als Staffel: hat guessit `season == year` und `season >= 1900`, dann `season = 1`. Folge ohne Staffel → `season = 1`.
  - Sprachen: guessit `language` (`de`, `mul` …) → zweistellig; `mul` bedeutet bei vorhandenem `de` zusätzlich `en`; `mul` selbst entfällt.
  - 3D: guessit `other` enthält `3D`; Format: `Half Side By Side`→`HSBS`, `Side By Side`→`SBS`, `Half Over Under`→`HOU`, `Over Under`→`OU`; zusätzlich Regex `\b(TAB|MVC)\b` im Namen.
  - Sample: `other` enthält `Sample` oder Name enthält `sample` (case-insensitiv).
  - Ordner: `parent` wird immer geparst, wenn angegeben. Ist der Datei-Titel Müll oder fehlt → Titel, Jahr und Sprachen aus dem Ordner (Staffel/Folge aus der Datei, falls vorhanden). Sonst werden Jahr und Sprachen aus dem Ordner nur übernommen, wenn sie in der Datei fehlen **und** der normalisierte Ordnertitel mit dem normalisierten Dateititel beginnt oder gleich ist.
  - `is_garbage_title`: `None`, kürzer als 3 Zeichen, oder `^[a-z0-9]{8,}$` mit mindestens 2 Ziffern und keinem Leerzeichen.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `"Parser für Datei- und Ordnernamen"`

---

### Task 6: scanner und Datenbank

**Files:**
- Create: `src/mediasort/scanner.py`, `src/mediasort/db.py`, `src/mediasort/settings.py`, `tests/test_scanner.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: `VideoFile`, `Subtitle`, `to_alpha2`, `parse_name` (nur für Untertitelsprache), `FolderType`, `AutoLevel`, `ItemStatus`
- Produces (`scanner.py`):
  - `VIDEO_EXT: frozenset[str]`, `SUB_EXT: frozenset[str]`
  - `@dataclass ScanResult(videos: list[VideoFile], samples: list[Path], too_new: list[Path])`
  - `scan(root: Path, *, exclude: Sequence[str], sample_bytes: int, min_age_s: float, now: float, ignored: Container[str] = (), top_filter: Callable[[str], bool] | None = None) -> ScanResult`
  - `subtitle_from_path(path: Path) -> Subtitle`
  - `letter_range(first: str, last: str) -> Callable[[str], bool]` (vergleicht den ersten Buchstaben des obersten Eintrags unter `root`, Ziffern zählen als `0`)
- Produces (`db.py`, SQLAlchemy 2 Declarative):
  - Tabellen nach Spec 10: `Setting(key, value_json)`, `Folder(id, path, role["input"|"library"], media_type, movie_target_id, series_target_id, movie3d_target_id, auto_level)`, `Scan(id, folder_id, started, finished, subset, status, auto)`, `Item(id, scan_id, folder_id, source, size, mtime, kind, parsed_json, group_key, tmdb_id, title, year, season, episodes_json, episode_titles_json, confidence, alternatives_json, mediainfo_json, target, subtitles_json, conflict_json, decision, status, error, note, auto)`, `Ignored(path, size, created)`, `LibraryEntry(id, library_id, path, title, year, tmdb_id, kind)`, `History(id, ts, job_id, action, old_path, new_path, auto)`, `TmdbCache(key, value_json, fetched)`, `Job(id, kind, status, progress, total, started, finished, auto, message)`, `FileState(path, size, mtime, last_seen)`
  - `make_engine(db_path: Path) -> Engine`, `init_db(engine) -> sessionmaker[Session]`
  - `class DbCache: __init__(sessionmaker, ttl_days: int = 30); get(key: str) -> Any | None; put(key: str, value: Any) -> None`
- Produces (`settings.py`): `@dataclass Settings` mit genau diesen Feldern: `tmdb_key: str = ""`, `language: str = "de-DE"`, `season_label: str = "Staffel"`, `sample_bytes: int = 104_857_600`, `min_age_minutes: int = 10`, `exclude: list[str]` (Standardliste), `cache_days: int = 30`, `plex_url: str = ""`, `plex_token: str = ""`, `plex_path_map: list[tuple[str, str]] = []`, `auto_enabled: bool = False`, `poll_minutes: int = 15`, `confirmed_trash_warning: bool = False`; `load_settings(session) -> Settings`, `save_settings(session, s: Settings) -> None`.

- [ ] **Step 1: Failing tests `test_scanner.py`** (Hilfsfunktion `mk(path, size, age_s)` legt Sparse-Dateien mit `os.truncate` an und setzt `mtime`)

```python
def test_finds_videos_and_subtitles(tmp_path):
    mk("Rel/Movie.2020.mkv", 2*GB); mk("Rel/Movie.2020.ger.srt", 1); mk("Rel/Subs/eng.forced.srt", 1); mk("Rel/movie.nfo", 1)
    r = scan(tmp_path, **DEFAULTS)
    assert [v.path.name for v in r.videos] == ["Movie.2020.mkv"]
    assert sorted((s.language, s.forced) for s in r.videos[0].subtitles) == [("de", False), ("en", True)]

def test_subtitles_need_matching_prefix_when_folder_has_several_videos(tmp_path):
    mk("S/A.S01E01.mkv", GB); mk("S/A.S01E02.mkv", GB); mk("S/A.S01E01.de.srt", 1); mk("S/other.srt", 1)
    v = {x.path.name: x for x in scan(tmp_path, **DEFAULTS).videos}
    assert [s.path.name for s in v["A.S01E01.mkv"].subtitles] == ["A.S01E01.de.srt"] and v["A.S01E02.mkv"].subtitles == ()

def test_samples_excluded_and_too_new_skipped(tmp_path):
    mk("R/x.sample.mkv", 300*MB); mk("R/small.mkv", 50*MB); mk("R/new.mkv", GB, age_s=60)
    r = scan(tmp_path, **DEFAULTS)
    assert r.videos == [] and len(r.samples) == 2 and [p.name for p in r.too_new] == ["new.mkv"]

def test_excluded_hidden_and_ignored(tmp_path):
    mk("incomplete/a.mkv", GB); mk(".hidden/b.mkv", GB); mk("ok/c.mkv", GB); mk("ok/d.mkv", GB)
    r = scan(tmp_path, **DEFAULTS, ignored={str(tmp_path / "ok/d.mkv")})
    assert [v.path.name for v in r.videos] == ["c.mkv"]

def test_letter_range(tmp_path):
    mk("Avatar (2009)/a.mkv", GB); mk("Dune (2021)/d.mkv", GB); mk("300 (2007)/t.mkv", GB)
    names = {v.path.name for v in scan(tmp_path, **DEFAULTS, top_filter=letter_range("0", "C")).videos}
    assert names == {"a.mkv", "t.mkv"}
```

- [ ] **Step 2: Failing tests `test_db.py`**

```python
def test_settings_roundtrip(session):
    s = load_settings(session); assert s.language == "de-DE" and s.sample_bytes == 104_857_600 and s.season_label == "Staffel"
    s.tmdb_key = "k"; s.plex_path_map = [("/media", "/media")]; save_settings(session, s)
    assert load_settings(session).plex_path_map == [("/media", "/media")]

def test_cache_ttl(sessionmaker_, monkeypatch):
    c = DbCache(sessionmaker_, ttl_days=30); c.put("k", {"a": 1}); assert c.get("k") == {"a": 1}
    # Zeit um 31 Tage vorstellen → None
```

- [ ] **Step 3: Run** → FAIL
- [ ] **Step 4: Implement** – `scan` mit `os.walk`, Ausschluss per Ordnername (exakt) und führendem Punkt; Untertitelregeln nach Spec 5.1; `subtitle_from_path`: Sprache aus den Namensteilen (Punkte/Unterstriche/Leerzeichen getrennt, letzte zuerst) über `to_alpha2`, `forced` wenn ein Teil `forced` ist.
- [ ] **Step 5: Run** → PASS
- [ ] **Step 6: Commit** `"Scanner, Datenbank, Einstellungen"`

---

### Task 7: TMDB-Client

**Files:**
- Create: `src/mediasort/tmdb.py`, `tests/test_tmdb.py`, `tests/fixtures/tmdb/*.json`

**Interfaces:**
- Consumes: `Candidate`, `MediaKind`, `DbCache`
- Produces:
  - `class TmdbError(Exception)`
  - `class TmdbClient(api_key: str, language: str = "de-DE", cache: DbCache | None = None, transport: httpx.BaseTransport | None = None)`
    - `search(kind: MediaKind, query: str, year: int | None) -> list[Candidate]`
    - `details(kind: MediaKind, tmdb_id: int) -> Candidate`
    - `episode_titles(tv_id: int, season: int) -> dict[int, str]`
    - `find_imdb(imdb_id: str) -> Candidate | None`
    - `parse_reference(text: str) -> tuple[str, str] | None` (statisch: `"tmdb:movie:603"`, `https://www.themoviedb.org/tv/1396-…` → `("tv","1396")`, `tt0133093` oder IMDb-Link → `("imdb","tt0133093")`)

- [ ] **Step 1: Fixtures** – aufgezeichnete Antworten für `search/movie` (Matrix, de-DE und en-US), `search/tv` (Breaking Bad), `tv/1396`, `tv/1396/season/3` (de-DE mit einem leeren `name` und einem `"Episode 5"`; en-US mit Namen), `find/tt0133093`.

- [ ] **Step 2: Failing tests**

```python
def test_search_merges_english_title(client):
    c = client.search(MediaKind.MOVIE, "Matrix", 1999)[0]
    assert (c.tmdb_id, c.title, c.english_title, c.year) == (603, "Matrix", "The Matrix", 1999)

def test_search_without_year_retry(client_router):  # Suche mit Jahr leer → zweite Suche ohne Jahr
    ...

def test_episode_titles_fall_back_to_english(client):
    t = client.episode_titles(1396, 3)
    assert t[1] == "Kein Más" and t[5] == "Mas"  # leer bzw. "Episode 5" → englischer Name

def test_cache_avoids_second_request(client, calls): client.details(MediaKind.TV, 1396); client.details(MediaKind.TV, 1396); assert calls["tv/1396"] == 1

def test_retries_once_on_429(): # erst 429 mit Retry-After: 0, dann 200 → Ergebnis; zweimal 429 → TmdbError
    ...

def test_network_error_raises_tmdb_error(): ...  # httpx.ConnectError → TmdbError("TMDB nicht erreichbar")

def test_bearer_token_for_long_keys(): ...  # Schlüssel > 40 Zeichen → Header Authorization: Bearer, sonst Query api_key

@pytest.mark.parametrize("text,ref", [("https://www.themoviedb.org/tv/1396-breaking-bad", ("tv","1396")),
    ("https://www.imdb.com/title/tt0133093/", ("imdb","tt0133093")), ("tt0133093", ("imdb","tt0133093")),
    ("tmdb:movie:603", ("movie","603")), ("Matrix", None)])
def test_parse_reference(text, ref): ...
```

- [ ] **Step 3: Run** → FAIL
- [ ] **Step 4: Implement** – Basis `https://api.themoviedb.org/3`; Timeout 15 s; Suche je einmal mit `language` und `en-US`, nach ID zusammengeführt; `Candidate.year` aus `release_date` bzw. `first_air_date`. Folgennamen gelten als fehlend, wenn leer oder `^(Episode|Folge) \d+$`. Cache-Schlüssel = Pfad + sortierte Parameter ohne Schlüssel.
- [ ] **Step 5: Run** → PASS
- [ ] **Step 6: Commit** `"TMDB-Client mit Cache"`

---

### Task 8: matcher – Kandidaten bewerten

**Files:**
- Create: `src/mediasort/matcher.py`, `tests/test_matcher.py`

**Interfaces:**
- Consumes: `ParsedName`, `Candidate`, `Match`, `Confidence`, `normalize_title`
- Produces:
  - `title_similarity(query: str, cand: Candidate) -> float` (max über `title`, `english_title`, `original_title`; rapidfuzz `token_sort_ratio` auf `normalize_title`)
  - `match(parsed: ParsedName, candidates: Sequence[Candidate], episode_exists: bool | None = None) -> Match`

- [ ] **Step 1: Failing tests**

```python
M = lambda id, t, y, pop=1.0, en=None, orig=None: Candidate(id, MediaKind.MOVIE, t, orig or t, en, y, pop)

def test_exact_title_and_year_is_high():
    assert match(P("Matrix", 1999), [M(603, "Matrix", 1999, en="The Matrix")]).confidence is Confidence.HIGH

def test_english_title_matches():
    m = match(P("The Matrix", 1999), [M(603, "Matrix", 1999, en="The Matrix")]); assert m.candidate.tmdb_id == 603

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
    assert match(P("Reacher", None, kind=EP), [TV(108978, "Reacher", 2022)], episode_exists=False).confidence is not Confidence.HIGH

def test_nothing_found_is_low():
    m = match(P("y2x24go2wrcp", None), []); assert m.candidate is None and m.confidence is Confidence.LOW
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** – Punkte = Ähnlichkeit + 10 bei Jahresabstand ≤ 1, − 20 bei Abstand > 1 (nur wenn beide Jahre bekannt); Sortierung nach Punkten, dann Popularität. Sicherheit nach Spec 5.5 mit den Schwellen aus „Global Constraints“; „Jahr passt“ heißt Abstand ≤ 1; ohne Jahr in der Datei ist hoch nur möglich, wenn kein zweiter Kandidat ≥ 85 Ähnlichkeit hat. `alternatives` = die nächsten bis zu 5 Kandidaten.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `"Abgleich und Sicherheit"`

---

### Task 9: library – Bibliotheksindex

**Files:**
- Create: `src/mediasort/library.py`, `tests/test_library.py`

**Interfaces:**
- Consumes: `LibraryEntry`, `parse_name`, `normalize_title`, `title_similarity`, `Candidate`
- Produces:
  - `read_nfo_tmdb_id(folder: Path) -> int | None` – sucht `*.nfo` direkt im Ordner, liest `<uniqueid type="tmdb">` oder `<tmdbid>`
  - `parse_folder_name(name: str) -> tuple[str, int | None]` – `"Titel (2009)"` → `("Titel", 2009)`
  - `class LibraryIndex(session_factory, library_id: int, root: Path)`
    - `refresh() -> int` – oberste Ordner einlesen (Titel, Jahr, NFO-ID), Anzahl zurück
    - `find(cand: Candidate) -> list[Path]` – Ordner mit gleicher TMDB-ID oder (ohne ID) Titelähnlichkeit ≥ 90 zu einem der Kandidatentitel und Jahresabstand ≤ 1
    - `remember(folder: Path, tmdb_id: int) -> None`
  - `episode_files(show_dir: Path, season: int, episodes: Sequence[int]) -> list[Path]` – alle Videos im Serienordner (rekursiv), deren geparste Staffel/Folgen sich mit den gesuchten überschneiden
  - `videos_in(folder: Path) -> list[Path]`

- [ ] **Step 1: Failing tests**

```python
def test_nfo_id(tmp_path): (tmp_path/"movie.nfo").write_text('<movie><uniqueid type="tmdb">603</uniqueid></movie>'); assert read_nfo_tmdb_id(tmp_path) == 603
def test_nfo_tmdbid_tag(tmp_path): (tmp_path/"x.nfo").write_text("<movie><tmdbid>841</tmdbid></movie>"); assert read_nfo_tmdb_id(tmp_path) == 841
def test_parse_folder_name(): assert parse_folder_name("Jede Sekunde zählt - The Guardian (2006)") == ("Jede Sekunde zählt - The Guardian", 2006) and parse_folder_name("Greenland") == ("Greenland", None)

def test_find_by_id_and_by_title(index_with):  # zwei Ordner: "Matrix (1999)" mit NFO 603, "Jede Sekunde zaehlt The Guardian (2006)" ohne NFO
    assert index.find(M(603, "Matrix", 1999))[0].name == "Matrix (1999)"
    hits = index.find(M(4643, "Jede Sekunde zählt - The Guardian", 2006))
    assert [h.name for h in hits] == ["Jede Sekunde zaehlt The Guardian (2006)"]

def test_finds_nfd_folder(tmp_path):  # Ordnername in NFD wird für NFC-Kandidat gefunden
    ...

def test_episode_files(tmp_path):
    for n in ["Season 4/Reacher.S04E05.mkv", "Staffel 4/Reacher - S04E05-E06 - X.mkv", "Staffel 4/Reacher - S04E07.mkv"]: mk(n)
    assert len(episode_files(tmp_path, 4, [6])) == 1 and len(episode_files(tmp_path, 4, [5])) == 2
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** – Index liegt in `LibraryEntry`; `refresh` ersetzt die Einträge der Bibliothek, behält aber bekannte `tmdb_id`s zu unverändertem Pfad.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `"Bibliotheksindex"`

---

### Task 10: planner – Vorschläge erzeugen

**Files:**
- Create: `src/mediasort/planner.py`, `tests/test_planner.py`

Hinweis: Pfade wie `/media/Filme_2D/...` in den Tests stehen für die Test-Wurzel (`env.root`), nicht wörtlich für `/media`.

**Interfaces:**
- Consumes: `scan`, `parse_name`, `TmdbClient`, `match`, `probe`, `movie_relpath`, `episode_relpath`, `subtitle_targets`, `LibraryIndex`, `episode_files`, `better`, `Settings`, `Folder`, `Item`
- Produces:
  - `@dataclass PlanContext(session_factory, settings: Settings, tmdb: TmdbClient, probe: Callable[[Path], MediaInfo | None], now: Callable[[], float])`
  - `class RootMissing(Exception)`
  - `plan_folder(ctx: PlanContext, folder: Folder, *, subset: Callable[[str], bool] | None = None, auto: bool = False) -> int` – legt einen `Scan` an, schreibt `Item`s, gibt die Scan-ID zurück
  - `replan_item(ctx: PlanContext, item_id: int, *, tmdb_ref: tuple[str, str] | None = None, season: int | None = None, episodes: Sequence[int] | None = None, format_3d: str | None = None) -> None` – Korrektur; `tmdb_ref` aus `TmdbClient.parse_reference` (`("imdb", id)` → `find_imdb`, sonst `details`); bei Serien wird die TMDB-Zuordnung auf alle `NEW`-Items derselben `group_key` im selben Scan übertragen
  - `target_root(folder: Folder, kind: MediaKind, is_3d: bool, folders: dict[int, Folder]) -> Path`

- [ ] **Step 1: Failing tests** – mit `tmp_path`-Ordnerbaum, Fake-`probe` (gibt feste `MediaInfo` je Dateiname zurück) und TMDB über `MockTransport`-Fixtures.

```python
def test_movie_in_downloads_gets_target(env):
    env.file("Downloads/The.Matrix.1999.German.DL.1080p.BluRay.x264/matrix.mkv")
    it = env.plan("Downloads").items()[0]
    assert it.target == "/media/Filme_2D/Matrix (1999)/Matrix (1999) 1080p h264 DTS [DE+EN].mkv"
    assert it.confidence == "hoch" and it.status == "new"

def test_episodes_grouped_one_search(env):
    for e in (1, 2, 3): env.file(f"Downloads/Reacher.S04E0{e}.German.DL.1080P.WEBRip.h265-WOTT/x.mkv")
    items = env.plan("Downloads").items()
    assert len({i.group_key for i in items}) == 1 and env.calls("search/tv") == 2  # de + en
    assert items[0].target.startswith("/media/Serien/Reacher (2022)/Staffel 4/Reacher - S04E01 - ")

def test_3d_goes_to_3d_library(env):
    env.file("Downloads/Avatar.2009.3D.HSBS.1080p/a.mkv")
    assert env.plan("Downloads").items()[0].target.startswith("/media/Filme_3D/")

def test_existing_movie_is_conflict_with_suggestion(env):
    env.file("Filme_2D/Matrix (1999)/Matrix (1999) 720p h264 AC3 [DE].mkv", info=MI(1280,720,"h264",None,[A("ac3","de",6)]))
    env.file("Downloads/The.Matrix.1999.2160p/m.mkv", info=MI(3840,2160,"hevc","DV",[A("truehd","de",8)]))
    it = env.plan("Downloads").items()[0]
    assert it.conflict["existing"] == ["/media/Filme_2D/Matrix (1999)/Matrix (1999) 720p h264 AC3 [DE].mkv"]
    assert it.conflict["suggestion"] == "keep_new"

def test_same_target_in_batch_becomes_conflict(env):
    env.file("Downloads/Reacher.S04E05.German.DL.2160p.DV.HDR.WEBRip.AV1-WOTT/a.mkv", info=UHD_DE)
    env.file("Downloads/Reacher.S04E05.German.DL.HDR.2160p.WEB.h265-W4K/b.mkv", info=UHD_DE)
    items = env.plan("Downloads").items()
    assert sum(1 for i in items if i.conflict) >= 1

def test_already_correct_file_in_library_mode_has_no_item(env):
    env.file("Filme_2D/Matrix (1999)/Matrix (1999) 1080p h264 DTS [DE+EN].mkv")
    assert env.plan("Filme_2D").items() == []

def test_episode_in_movie_folder_is_flagged(env):
    env.file("Filme_2D/Chernobyl.2019.E05.720p.BluRay.DD5.1.x264-DON.mkv")
    it = env.plan("Filme_2D").items()[0]
    assert it.confidence == "niedrig" and "Serienfolge" in it.note

def test_unreadable_file_is_error(env):
    env.file("Downloads/Kaputt.2020/k.mkv", info=ProbeError("ffprobe konnte die Datei nicht lesen"))
    it = env.plan("Downloads").items()[0]
    assert it.status == "error" and it.target is None and "ffprobe" in it.error

def test_tmdb_down_marks_items(env_tmdb_down):
    env.file("Downloads/The.Matrix.1999/m.mkv")
    it = env.plan("Downloads").items()[0]
    assert it.confidence == "niedrig" and "TMDB" in it.error and it.status == "new"

def test_missing_root_raises(env):
    shutil.rmtree(env.root / "Filme_2D")
    with pytest.raises(RootMissing): env.plan("Downloads")

def test_replan_with_tmdb_id_updates_group(env): ...  # Korrektur einer Folge setzt Serie für alle drei Folgen

def test_ignored_and_already_planned_files_skipped(env): ...  # zweiter Scan erzeugt keine Dubletten-Items für noch offene Vorschläge
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** – Ablauf pro Videodatei: parsen → Art bestimmen (Spec 5.3; fester Typ „Film“ aber Staffel/Folge erkannt → Sicherheit niedrig, Notiz „sieht aus wie eine Serienfolge“; fester Typ „3D“ → `is_3d=True`) → gruppieren (`group_key = f"{kind}:{normalize_title(title)}:{year or ''}"`) → je Gruppe einmal `search` + `match` (bei Serien `episode_exists` aus `episode_titles`) → `probe` → Zielpfad über `target_root` + namer → Konflikte: `LibraryIndex.find` (Filme: Videos in gefundenen Ordnern; Serien: `episode_files`), exakter Zielpfad existiert, oder Zielpfad bereits von einem anderen Item dieses Scans belegt → `conflict_json = {"existing": [...], "suggestion": "keep_new"|"keep_existing"|None}` über `better`. Library-Modus: `target == source` → kein Item; die eigene Quelldatei zählt nie als „existing“, ein zweiter Ordner mit derselben TMDB-ID dagegen schon (Dublette). Vor dem Scan: Wurzel des Ordners und alle Zielbibliotheken müssen existieren, sonst `RootMissing`. Offene (`NEW`/`CONFIRMED`) Items mit gleicher Quelle werden nicht erneut angelegt. TMDB-Fehler werden pro Gruppe gefangen.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `"Planner: Vorschläge, Gruppen, Konflikte"`

---

### Task 11: executor – Ausführen und Aufräumen

**Files:**
- Create: `src/mediasort/executor.py`, `tests/test_executor.py`

**Interfaces:**
- Consumes: `Item`, `History`, `Folder`, `Settings`, `videos_in`
- Produces:
  - `move_file(src: Path, dst: Path) -> None` – `FileExistsError` wenn `dst` existiert; `os.rename`, bei `OSError.errno == EXDEV` kopieren (`shutil.copyfile` in `dst.with_suffix(dst.suffix + ".mediasort-tmp")`), Größe vergleichen, umbenennen, Quelle löschen; bei Fehler Teilkopie entfernen
  - `cleanup_source(folder: Path, *, protected: Collection[Path], exclude: Sequence[str], sample_bytes: int) -> bool` – löscht `folder` rekursiv, wenn kein Video > `sample_bytes` mehr darin liegt und `folder` weder in `protected` noch ein Ausschlussordner bzw. dessen Vorfahr ist; `True` wenn gelöscht
  - `@dataclass ExecutionReport(done: int, failed: int, touched: set[Path])`
  - `execute(session_factory, item_ids: Sequence[int], *, settings: Settings, job_id: int, auto: bool = False, progress: Callable[[int, int], None] = lambda d, t: None) -> ExecutionReport`

- [ ] **Step 1: Failing tests**

```python
def test_move_same_fs(tmp_path): ...  # Datei ist am Ziel, Quelle weg
def test_move_never_overwrites(tmp_path): ...  # dst existiert → FileExistsError, beide Dateien unverändert
def test_move_exdev_fallback(tmp_path, monkeypatch):  # os.rename wirft beim ersten Aufruf OSError(EXDEV) → kopiert, Quelle weg
    ...
def test_move_exdev_copy_failure_keeps_source(tmp_path, monkeypatch): ...  # copyfile wirft → kein .mediasort-tmp, Quelle da
def test_cleanup_deletes_folder_with_leftovers(tmp_path): ...  # nfo, jpg, sample.mkv (300 MB sparse) → Ordner weg
def test_cleanup_keeps_folder_with_big_video(tmp_path): ...
def test_cleanup_never_deletes_roots_or_excluded(tmp_path): ...  # protected root, "incomplete" und dessen Eltern bleiben

def test_execute_moves_video_and_subs_and_logs(env):
    item = env.item(source=".../Rel/m.mkv", target=".../Filme_2D/M (2020)/M (2020) 1080p h264 AC3 [DE].mkv", subs=[("Rel/m.ger.srt", "... [DE].de.srt")])
    r = execute(env.sf, [item.id], settings=env.settings, job_id=1)
    assert r.done == 1 and not (env.root / "Downloads/Rel").exists()
    assert [h.action for h in env.history()] == ["moved", "moved", "folder_deleted"]

def test_execute_skips_changed_source(env): ...  # Größe weicht ab → status error, Fehlertext „seit dem Scan verändert“
def test_execute_keep_new_deletes_old_version_and_subs(env): ...  # decision keep_new → alte Datei + .srt gelöscht, History "deleted"
def test_execute_keep_existing_deletes_new_source(env): ...
def test_execute_continues_after_error(env): ...  # zwei Items, erstes schlägt fehl → zweites erledigt, report 1/1
def test_missing_library_root_aborts_without_creating(env):
    shutil.rmtree(env.root / "Filme_2D")
    r = execute(env.sf, [env.item_to_filme_2d().id], settings=env.settings, job_id=1)
    assert r.failed == 1 and not (env.root / "Filme_2D").exists()
def test_touched_contains_target_and_source_dirs(env): ...
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** – Reihenfolge Spec 7.3. Zielbibliothek = oberster Ordner des Zielpfads unter `/media` gemäß `Folder`-Tabelle; existiert er nicht → Item-Fehler „Bibliotheksordner fehlt – ist die Freigabe eingebunden?“, nichts anlegen. `keep_both` nur ausführbar, wenn Zielpfad frei. History-Aktionen: `moved`, `deleted`, `folder_deleted`. `protected` = alle `Folder.path` plus deren Eltern.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `"Executor: verschieben, Konflikte, aufräumen, protokollieren"`

---

### Task 12: Plex-Anbindung

**Files:**
- Create: `src/mediasort/plex.py`, `tests/test_plex.py`, `tests/fixtures/plex/sections.json`, `tests/fixtures/plex/prefs.json`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) PlexSection(key: str, title: str, type: str, locations: tuple[str, ...])`
  - `class PlexError(Exception)`
  - `class PlexClient(base_url: str, token: str, transport: httpx.BaseTransport | None = None)`: `sections() -> list[PlexSection]`, `auto_empty_trash() -> bool`, `refresh(section_key: str, path: str) -> None`
  - `map_path(path: Path, mapping: Sequence[tuple[str, str]]) -> str` – längstes passendes Präfix, sonst unverändert
  - `suggest_mapping(local_roots: Sequence[Path], sections: Sequence[PlexSection]) -> list[tuple[str, str]]` – Paare über gleichen letzten Pfadteil; identische Pfade → `(p, p)`
  - `refresh_touched(client: PlexClient, touched: Iterable[Path], mapping: Sequence[tuple[str, str]], library_roots: Sequence[Path]) -> list[str]` – reduziert jeden Pfad auf den Titelordner (erste Ebene unter der Bibliothekswurzel), ruft `refresh` für Abschnitte, deren `locations` den Plex-Pfad enthalten; Pfade ohne Abschnitt werden übersprungen; gibt Hinweise (deutsch) bei `PlexError` zurück

- [ ] **Step 1: Failing tests**

```python
def test_sections(client): s = client.sections(); assert [(x.key, x.locations) for x in s] == [("1", ("/media/Filme_2D",)), ("2", ("/media/Serien",))]
def test_auto_empty_trash(client): assert client.auto_empty_trash() is True
def test_token_header_sent(client, seen): client.sections(); assert seen[0].headers["X-Plex-Token"] == "tok"
def test_map_path(): assert map_path(Path("/data/Filme_2D/X"), [("/data", "/media")]) == "/media/Filme_2D/X"
def test_suggest_mapping_identical(): assert suggest_mapping([Path("/media/Filme_2D")], SECTIONS) == [("/media/Filme_2D", "/media/Filme_2D")]
def test_refresh_touched(client, seen):
    msgs = refresh_touched(client, [Path("/media/Filme_2D/Matrix (1999)/x.mkv"), Path("/media/Filme_2D/Matrix (1999)"),
                                    Path("/media/Filme_3D/Avatar (2009)"), Path("/media/Downloads/Rel")],
                           [("/media", "/media")], [Path("/media/Filme_2D"), Path("/media/Filme_3D")])
    assert msgs == [] and refreshed(seen) == [("1", "/media/Filme_2D/Matrix (1999)")]
def test_unreachable_gives_hint(): ...  # ConnectError → ["Plex nicht erreichbar: …"]
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** – JSON über `Accept: application/json`; `sections` aus `MediaContainer.Directory[].Location[].path`; Präferenz `autoEmptyTrash` aus `MediaContainer.Setting[]`; Refresh `GET /library/sections/{key}/refresh?path=…`; Timeout 10 s.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `"Plex-Anbindung"`

---

### Task 13: Hintergrund-Worker und Ordnerüberwachung

**Files:**
- Create: `src/mediasort/jobs.py`, `src/mediasort/watcher.py`, `tests/test_jobs.py`, `tests/test_watcher.py`

**Interfaces:**
- Consumes: `Job`, `FileState`, `Item`, `Folder`, `plan_folder`, `execute`, `refresh_touched`, `Settings`
- Produces (`jobs.py`):
  - `class JobRunner(session_factory)`: `start() -> None`, `stop() -> None`, `submit(kind: str, fn: Callable[[int, Callable[[int, int], None]], str | None], *, auto: bool = False) -> int | None` (gibt `None` zurück, wenn schon ein Auftrag läuft oder wartet), `busy() -> bool`, `wait_idle(timeout: float) -> bool`
  - Ein Thread; `fn(job_id, progress)` liefert eine Abschlussmeldung; Ausnahmen setzen `Job.status="error"` mit Meldung.
- Produces (`watcher.py`):
  - `is_stable(path: str, size: int, mtime: float, now: float, min_age_s: float, state: FileState | None) -> bool` – Alter ≥ Mindestalter und `state` mit gleicher Größe/mtime
  - `auto_eligible(item: Item) -> bool` – Sicherheit hoch, kein Konflikt, Status `new`, kein Fehler, Ziel gesetzt
  - `class Watcher(session_factory, runner: JobRunner, plan: Callable[[Folder, bool], int], run_items: Callable[[list[int], bool], None], now: Callable[[], float])`: `tick() -> str` (Ergebnistext für die Übersicht)

- [ ] **Step 1: Failing tests**

```python
def test_runner_runs_one_at_a_time(runner): a = runner.submit("x", slow); assert runner.submit("y", fast) is None; runner.wait_idle(5)
def test_runner_records_error(runner, sf): jid = runner.submit("x", boom); runner.wait_idle(5); assert job(sf, jid).status == "error"

def test_is_stable():
    assert not is_stable("a", 10, 1000, now=1300, min_age_s=600, state=None)
    assert not is_stable("a", 10, 1000, now=2000, min_age_s=600, state=None)          # erstes Sehen
    assert is_stable("a", 10, 1000, now=2000, min_age_s=600, state=FS("a", 10, 1000))
    assert not is_stable("a", 11, 1000, now=2000, min_age_s=600, state=FS("a", 10, 1000))

def test_auto_eligible(): ...  # hoch+ohne Konflikt → True; mittel, Konflikt, error, ohne Ziel → False

def test_tick_master_switch_off_does_nothing(w): assert w.tick() == "Automatik ist ausgeschaltet"
def test_tick_detect_plans_but_does_not_execute(w): ...  # Ordner auf DETECT → plan aufgerufen, run_items nicht
def test_tick_auto_executes_only_eligible_and_stable(w): ...  # zwei Items, nur eins stabil+hoch → run_items([id], True)
def test_tick_skipped_when_busy(w): ...  # runner.busy() → "übersprungen: Auftrag läuft"
def test_library_folders_never_automatic(w): ...
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** – `tick`: Hauptschalter prüfen; für jeden Eingangsordner mit `auto_level != off` `plan(folder, True)`; danach `FileState` für alle Quellen der neuen Items aktualisieren; bei `AUTO` die `auto_eligible`- und `is_stable`-Items über `run_items(ids, True)` ausführen. Der Planner überspringt ohnehin zu junge Dateien (Mindestalter). Das periodische Aufrufen von `tick` alle `poll_minutes` übernimmt `web/app.py` (Task 14).
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `"Hintergrund-Worker und Ordnerüberwachung"`

---

### Task 14: Web – App, Übersicht, Einstellungen

**Files:**
- Create: `src/mediasort/web/__init__.py`, `web/app.py`, `web/routes_main.py`, `web/routes_admin.py` (nur Einstellungen in diesem Task), `web/templates/base.html`, `overview.html`, `settings.html`, `web/static/htmx.min.js` (htmx 2.0.4 von `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js` herunterladen), `web/static/style.css`, `tests/test_web_main.py`

**Interfaces:**
- Consumes: alle vorherigen Module
- Produces:
  - `create_app(config_dir: Path, *, tmdb_transport: httpx.BaseTransport | None = None, plex_transport: httpx.BaseTransport | None = None, probe: Callable[[Path], MediaInfo | None] = mediainfo.probe, start_background: bool = True) -> FastAPI`
  - `main() -> None` – `uvicorn.run(create_app(Path(os.environ.get("MEDIASORT_CONFIG", "/config"))), host="0.0.0.0", port=8080)`
  - Routen: `GET /` (Übersicht), `POST /scan/{folder_id}`, `GET /jobs/current` (HTMX-Fragment, pollt alle 2 s solange aktiv), `GET /settings`, `POST /settings`, `POST /settings/folders`, `POST /settings/folders/{id}/delete`, `POST /settings/plex/test`
  - App-Zustand (`app.state`): `sf`, `runner`, `watcher`, Fabriken für `TmdbClient`/`PlexClient` aus aktuellen Settings
  - Beim Start immer `runner.start()`; nur mit `start_background=True` zusätzlich ein asyncio-Task, der alle `poll_minutes` `watcher.tick()` im Runner-Thread anstößt

- [ ] **Step 1: Failing tests** (FastAPI `TestClient`, `start_background=False`)

```python
def test_overview_shows_folders_and_warnings(client):
    r = client.get("/"); assert r.status_code == 200
    assert "TMDB-Schlüssel fehlt" in r.text and "Downloads" in r.text

def test_plex_trash_warning(client_with_plex):  # prefs autoEmptyTrash=true
    assert "Papierkorb automatisch" in client_with_plex.get("/").text

def test_settings_save_and_folder_crud(client):
    client.post("/settings", data={"tmdb_key": "k", "language": "de-DE", "season_label": "Staffel", ...})
    client.post("/settings/folders", data={"path": "/x/Downloads", "role": "input", "media_type": "auto", "auto_level": "off", ...})
    assert "/x/Downloads" in client.get("/settings").text

def test_scan_starts_job_and_reports(client, media_tree):
    r = client.post(f"/scan/{downloads_id}"); assert r.status_code in (200, 303)
    client.app.state.runner.wait_idle(10)  # Runner läuft auch mit start_background=False
    assert "Vorschläge" in client.get("/jobs/current").text

def test_plex_test_button_suggests_mapping(client_with_plex): ...  # zeigt "/media/Filme_2D → /media/Filme_2D"
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** – Jinja2-Templates, deutsche Texte; Layout mit Navigation (Übersicht, Prüfliste, Bibliothek prüfen, Verlauf, Ignoriert, Einstellungen); responsives CSS (eine Spalte unter 700 px). Warnungen: TMDB-Schlüssel fehlt; Plex leert Papierkorb automatisch; Bibliotheksordner fehlt (Freigabe nicht eingebunden). Übersicht zeigt pro Eingangsordner Anzahl offener Vorschläge, Automatik-Stufe und letzte Abfrage.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `"Weboberfläche: Übersicht und Einstellungen"`

---

### Task 15: Web – Prüfliste, Korrektur, Ausführen

**Files:**
- Create: `web/routes_review.py`, `web/templates/review.html`, `web/templates/_item_row.html`, `web/templates/_correct.html`, `web/templates/_conflict.html`, `tests/test_web_review.py`

**Interfaces:**
- Consumes: `replan_item`, `execute`, `refresh_touched`, `TmdbClient.search`, `TmdbClient.parse_reference`, `JobRunner.submit`
- Produces: Routen
  - `GET /review?confidence=&kind=&only=conflicts|errors` – gruppiert: Serien nach Serie → Staffel → Folgen, Filme einzeln
  - `POST /items/{id}/toggle` – `new` ↔ `confirmed` (HTMX, liefert Zeile)
  - `POST /items/confirm-safe` – alle `new` mit Sicherheit hoch und ohne Konflikt → `confirmed`
  - `GET /items/{id}/correct` – Formular (Suche, ID/Link, Staffel/Folge, 3D-Format)
  - `GET /items/{id}/search?q=` – TMDB-Treffer als Liste
  - `POST /items/{id}/correct` – ruft `replan_item`
  - `POST /items/{id}/ignore` – Item `ignored` + Eintrag in `Ignored`
  - `POST /items/{id}/decision` – `keep_new|keep_existing|keep_both|skip` und `confirmed`
  - `POST /execute` – startet Auftrag mit allen `confirmed`; danach `refresh_touched`, wenn Plex konfiguriert

- [ ] **Step 1: Failing tests**

```python
def test_review_groups_series(client, planned):  # 3 Reacher-Folgen + 1 Film
    t = client.get("/review").text; assert t.count("Staffel 4") == 1 and "Matrix (1999)" in t
def test_toggle_and_confirm_safe(client, planned): ...
def test_conflict_shows_both_versions_and_suggestion(client, planned_conflict): ...  # beide Pfade, "Bessere behalten" vorausgewählt
def test_correct_by_imdb_link(client, planned): ...  # POST mit "https://www.imdb.com/title/tt0133093/" → Ziel "Matrix (1999)/…"
def test_ignore_persists_across_scans(client, planned): ...
def test_execute_runs_and_triggers_plex(client_with_plex, planned): ...  # Datei verschoben, Plex-Refresh für "/media/Filme_2D/Matrix (1999)"
def test_execute_without_confirmed_items_is_noop(client): ...
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** – Ein Konflikt ohne Entscheidung blockiert das Bestätigen (Knopf deaktiviert, Hinweis „Bitte zuerst entscheiden“). Fehler-Items sind nicht bestätigbar. Zeile zeigt alter Name → neuer Pfad, Sicherheits-Plakette 🟢🟡🔴, Auflösung/Codec/HDR/Audio/Sprachen, Notiz und Fehler.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `"Weboberfläche: Prüfliste, Korrektur, Ausführen"`

---

### Task 16: Web – Bibliothek prüfen, Verlauf, Ignoriert

**Files:**
- Modify: `web/routes_admin.py`
- Create: `web/templates/library.html`, `history.html`, `ignored.html`, `tests/test_web_admin.py`

**Interfaces:**
- Consumes: `plan_folder` (mit `subset`), `letter_range`, `LibraryIndex.refresh`, `History`, `Ignored`, `PlexClient.auto_empty_trash`
- Produces: Routen
  - `GET /library`, `POST /library/scan` (Felder `folder_id`, `from_letter`, `to_letter` oder `subfolder`, `confirm_trash_warning`)
  - `GET /history?q=&auto=`
  - `GET /ignored`, `POST /ignored/{path_id}/remove`

- [ ] **Step 1: Failing tests**

```python
def test_library_scan_requires_trash_confirmation(client_with_plex):  # autoEmptyTrash=true, ohne Bestätigung → 400 + Hinweistext
    ...
def test_library_scan_letter_range(client, library_tree): ...  # nur A–C erzeugt Items
def test_library_scan_refreshes_index(client, library_tree): ...  # LibraryEntry-Anzahl > 0 nach Scan
def test_history_search_and_auto_filter(client, history_rows): ...
def test_unignore(client, ignored_row): ...  # Eintrag weg, nächster Scan findet Datei wieder
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** – die Bestätigung der Papierkorb-Warnung wird in `Settings.confirmed_trash_warning` gespeichert; danach wird nicht erneut gefragt. Meldet Plex `autoEmptyTrash=false`, ist keine Bestätigung nötig.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `"Weboberfläche: Bibliothek prüfen, Verlauf, Ignoriert"`

---

### Task 17: Docker, CI, Anleitung

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.github/workflows/ci.yml`, `README.md`

- [ ] **Step 1: `Dockerfile`** – `FROM python:3.12-slim`; `apt-get install -y --no-install-recommends ffmpeg`; `pip install --no-cache-dir .`; `ENV MEDIASORT_CONFIG=/config`; `EXPOSE 8080`; `CMD ["mediasort"]`; `HEALTHCHECK` über `python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"` (Route `GET /health` → `"ok"` in `routes_main.py` ergänzen, mit Test).

- [ ] **Step 2: `docker-compose.yml`**

```yaml
services:
  mediasort:
    image: ghcr.io/maxbrechtel/mediasort:latest
    build: .
    user: "${PUID:-1000}:${PGID:-1000}"
    environment:
      TZ: Europe/Berlin
    ports: ["8080:8080"]
    volumes:
      - ./config:/config
      - /media:/media
    restart: unless-stopped
```

- [ ] **Step 3: `ci.yml`** – bei Push/PR: Ubuntu, `apt-get install ffmpeg`, `pip install -e '.[test]'`, `pytest -q`; bei Push auf `main` zusätzlich `docker/login-action` (GHCR, `GITHUB_TOKEN`), `docker/build-push-action` mit Tags `latest` und `sha`. `permissions: packages: write`.

- [ ] **Step 4: `README.md`** – auf Deutsch: Zweck; VM anlegen (Debian, 2 vCPU, 2 GB RAM, 16 GB); Docker installieren; `/etc/fstab`-Zeile `//UNAS/Medien /media cifs credentials=/root/.smb-unas,uid=1000,gid=1000,file_mode=0660,dir_mode=0770,vers=3.1.1,_netdev,nofail 0 0` mit Hinweis „eine einzige Einbindung“; `docker compose up -d`; TMDB-Schlüssel beantragen; Plex-Token finden; Ordner anlegen (Downloads=Eingang/auto, Filme_2D/Serien/Filme_3D=Bibliothek); Abnahme mit `cp -a --reflink=always` nach `Medien/_Test`; Plex-Hinweis „Papierkorb automatisch leeren“ ausschalten.

- [ ] **Step 5: Verifizieren** – `docker build -t mediasort:test .` und `docker run --rm -d -p 8080:8080 -v $PWD/tmpcfg:/config mediasort:test`, dann `curl -fsS localhost:8080/health` → `ok`; falls Docker in der Umgebung nicht verfügbar ist, das im Commit-Text vermerken und nur `pytest -q` laufen lassen.

- [ ] **Step 6: Gesamtlauf** `pytest -q` → alle Tests grün

- [ ] **Step 7: Commit** `"Docker, CI und Installationsanleitung"`
