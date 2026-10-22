# mediasort – Design

Stand: 2026-10-22

## 1. Ziel

mediasort erkennt Film- und Seriendateien, gleicht sie mit TMDB ab und legt sie
nach einem festen Schema in einer Plex-Bibliothek ab. Bedient wird es über eine
Weboberfläche im Heimnetz. Standardmäßig wird jede Änderung vorher in einer
Prüfliste angezeigt und erst nach Bestätigung ausgeführt. Später kann eine
automatische Ordnerüberwachung sichere Treffer selbstständig einsortieren.

Erfolgskriterien:

- Neue Downloads landen nach einem Klick korrekt benannt in der richtigen Bibliothek.
- Die bestehende Bibliothek (ca. 4.700 Film- und 17.500 Seriendateien) lässt sich
  in Etappen auf das Schema umstellen, inklusive Dublettenbereinigung.
- Es geht nie eine Videodatei verloren, außer der Nutzer bestätigt das Löschen
  einer schlechteren Dublette ausdrücklich.
- Die Plex-Abspielhistorie bleibt erhalten.

## 2. Umgebung

- **Speicher:** UniFi UNAS, btrfs. Alle Medien liegen in einer Freigabe `Medien`
  mit den Ordnern `Downloads`, `Filme_2D`, `Filme_3D`, `Serien`.
- **Plex:** eigene VM auf Proxmox VE mit den Bibliotheken `/media/Filme_2D` und
  `/media/Serien`. `Filme_3D` ist bewusst nicht in Plex eingebunden.
- **mediasort:** eigene kleine VM auf Proxmox VE (Debian, Docker). Bindet `Medien`
  per SMB ebenfalls unter `/media` ein und reicht `/media` unverändert in den
  Container durch. Damit sind die Pfade in Plex und mediasort identisch.
- **Backup:** beide VMs per PBS.

Weil alles in einer Freigabe liegt, ist jedes Verschieben eine Umbenennung auf
dem UNAS und sofort fertig.

## 3. Festgelegte Anforderungen

| Thema | Entscheidung |
|---|---|
| Bedienung | Weboberfläche, Docker, manuell mit Prüfliste; optional automatische Ordnerüberwachung pro Eingangsordner |
| Datenquelle | TMDB (IMDb-IDs werden über TMDB aufgelöst) |
| Sprache | einstellbar, Standard Deutsch, Rückfall Englisch |
| Dateiaktion | Verschieben |
| Ordner | mehrere Eingangsordner, je mit Typ „automatisch“, „Film“, „Serie“ oder „3D“ und eigenem Ziel |
| Untertitel | werden mitverschoben und umbenannt |
| Reste | NFO, Bilder, Texte, Samples usw. werden gelöscht |
| Ursprungsordner | werden gelöscht, außer es liegt noch ein Video über 100 MB darin |
| Staffelordner | `Staffel N` (Bezeichnung einstellbar) |
| Tonspur im Namen | beste deutsche Spur, sonst Standardspur, sonst erste |
| Sprachen im Namen | alle Tonspursprachen als `[DE+EN]` |
| Codec-Schreibweise | knapp, mit HDR/DV-Kennzeichnung |
| Konflikte | bessere Qualität wird vorgeschlagen, Nutzer entscheidet |
| 3D | eigene Bibliothek, Format im Namen |
| ISO | wird einsortiert, ohne technische Angaben |
| Bestandsbibliothek | Modus „Bibliothek prüfen“, in Etappen |
| Plex | gezielter Teilscan nach jedem Auftrag, Warnung bei automatischem Papierkorb-Leeren |
| Zugang | kein Login, nur Heimnetz |

## 4. Architektur

Ein Python-Prozess (FastAPI) in einem Docker-Container. Daten in SQLite unter
`/config`. Ein einziger Hintergrund-Worker führt Scans und Aufträge nacheinander aus.

Technik: Python 3.12, FastAPI, Uvicorn, Jinja2, HTMX, SQLAlchemy 2, httpx,
guessit, babelfish (Sprachcodes), rapidfuzz (Titelähnlichkeit), ffprobe.

### Bausteine

Jeder Baustein ist ein Modul mit klarer Schnittstelle und eigenen Tests.

| Modul | Aufgabe | Hängt ab von |
|---|---|---|
| `scanner` | Findet Videos, ordnet Untertitel zu, erkennt Samples und Reste | Dateisystem |
| `parser` | Zerlegt Datei- und Ordnernamen in Titel, Jahr, Staffel, Folgen, 3D-Format | guessit |
| `tmdb` | TMDB-Suche und -Details mit Cache | httpx, `db` |
| `matcher` | Wählt den besten TMDB-Treffer, vergibt die Sicherheit | `tmdb` |
| `mediainfo` | Liest Auflösung, Codec, HDR, Tonspuren, Sprachen | ffprobe |
| `namer` | Baut den Zielpfad. Reine Funktionen ohne Dateizugriff | – |
| `library` | Index der Bibliotheken für Konflikt- und Dublettenerkennung | `db`, `parser` |
| `planner` | Erzeugt pro Video einen Vorschlag samt Konflikten | alle oben |
| `executor` | Führt bestätigte Vorschläge aus, räumt auf, protokolliert | Dateisystem, `db` |
| `plex` | Liest Bibliotheken und Einstellungen, stößt Teilscans an | httpx |
| `watcher` | Fragt Eingangsordner regelmäßig ab, stößt Scans und automatische Aufträge an | `planner`, `executor` |
| `db` | Datenmodell und Zugriff | SQLAlchemy |
| `web` | Routen, Templates, HTMX-Fragmente | alle |

### Datenfluss

```
Scannen → parser → matcher (tmdb) → mediainfo → namer → planner (+ library)
       → Prüfliste → Nutzer bestätigt / korrigiert / ignoriert
       → executor → Protokoll → plex (Teilscan)
```

## 5. Erkennung und Abgleich

### 5.1 Scannen

- **Videoendungen:** mkv, mp4, m4v, avi, mov, wmv, ts, m2ts, iso
- **Untertitelendungen:** srt, ass, ssa, sub, idx, sup, vtt
- **Übersprungen werden:**
  - Ordner aus der Ausschlussliste (Standard: `incomplete`, `nzbs`, `.deletedByTMM`, `.thumbnails`, `@eaDir`)
  - versteckte Ordner
  - Dateien, die in den letzten 10 Minuten geändert wurden
  - Dateien auf der Ignorierliste
- **Sample:** Video unter 100 MB oder mit `sample` im Namen. Samples erzeugen keinen
  Vorschlag. Kleine Videos ohne `sample` im Namen erscheinen als Hinweis zum Scan.
  Gelöscht wird beim Aufräumen nur, was nach Name ein Sample ist (siehe 7.4).
- **Untertitel zuordnen:** Ein Untertitel gehört zu einem Video, wenn er im selben
  Ordner oder in einem Unterordner `Subs`/`Subtitles` liegt und
  - sein Name mit dem Namen des Videos beginnt, oder
  - in dem Ordner nur ein Video liegt.
- Ordner ohne Video erzeugen keinen Vorschlag und werden nicht angefasst.

### 5.2 Namen zerlegen

- **Deutsche Muster** werden vor guessit in `SxxEyy` übersetzt: `Staffel3`,
  `Staffel 3`, `St3`, `Folge1`, `Folge 01`, `Episode 1`.
- **guessit** liefert Titel, Jahr, Staffel, Folge(n), Sprachhinweise (`German.DL`)
  und 3D-Kennungen.
- **Unbrauchbarer Dateiname** (z. B. `y2x24go2wrcp`, `abc-1080p`): Der Name des
  übergeordneten Ordners wird ausgewertet, die Ergebnisse werden zusammengeführt.
  Das Feld aus dem Dateinamen gewinnt, wenn beide etwas liefern.
- **3D:** `3D` zusammen mit `SBS`, `HSBS`, `OU`, `HOU`, `TAB`, `MVC`, oder
  Eingangsordner vom Typ 3D. Fehlt das Format, bleibt es bei `3D`.

### 5.3 Film oder Serie

1. Hat der Eingangsordner einen festen Typ, gilt dieser.
2. Sonst: Staffel oder Folge erkannt → Serie, ansonsten Film.

### 5.4 Gruppieren

Folgen derselben Serie aus einem Scan bilden eine Gruppe (gleicher normalisierter
Titel). Pro Gruppe gibt es eine TMDB-Suche. Eine Korrektur der Serie gilt für alle
Folgen der Gruppe.

### 5.5 TMDB-Abgleich

- **Suche:** `search/movie` bzw. `search/tv` mit Titel und, falls vorhanden, Jahr.
  Ohne Treffer wird ohne Jahr erneut gesucht.
- **Bewertung je Kandidat:**
  - Titelähnlichkeit (rapidfuzz `token_sort_ratio`, normalisiert) gegen deutschen,
    englischen und Originaltitel. Der höchste Wert zählt.
  - Jahr: gleich oder ±1 gibt einen Bonus, deutliche Abweichung einen Abzug.
  - Popularität entscheidet nur bei Gleichstand.
- **Serien:** Zusätzlich wird geprüft, ob Staffel und Folge bei TMDB existieren.
- **Sicherheit:**
  - 🟢 hoch: Ähnlichkeit ≥ 90, Jahr passt (bzw. kein zweiter Kandidat ≥ 85) und
    bei Serien existiert die Folge
  - 🟡 mittel: Ähnlichkeit ≥ 75 oder mehrere ähnliche Kandidaten
  - 🔴 niedrig: alles darunter oder kein Treffer
- **Cache:** TMDB-Antworten werden 30 Tage in SQLite gespeichert.

### 5.6 Namen und Jahr

- Titel und Folgennamen in der eingestellten Sprache, Rückfall auf Englisch.
- Jahr: bei Filmen das Erscheinungsjahr, bei Serien das Jahr der Erstausstrahlung.
- Mehrfachfolgen: Folgennamen werden mit ` & ` verbunden.
- Fehlt ein Folgenname, entfällt dieser Namensteil.

## 6. Benennung

### 6.1 Schema

Film:

```
<Bibliothek>/<Titel> (<Jahr>)/<Titel> (<Jahr>) <Auflösung> <Codec>[ <HDR>] <Audio>[ 3D <Format>][ [<Sprachen>]].<ext>
```

Serie:

```
<Bibliothek>/<Serie> (<Jahr>)/Staffel <N>/<Serie> - S<SS>E<EE>[-E<EE>][ - <Folgenname>] - <Auflösung>[ [<Sprachen>]].<ext>
```

ISO:

```
<Bibliothek>/<Titel> (<Jahr>)/<Titel> (<Jahr>)[ 3D[ <Format>]].iso
```

Beispiele:

```
Filme_2D/28 Weeks Later (2007)/28 Weeks Later (2007) 1080p h264 DTS [DE+EN].mkv
Filme_2D/Dune (2021)/Dune (2021) 2160p h265 DV TrueHD [DE+EN].mkv
Filme_3D/Avatar - Aufbruch nach Pandora (2009)/Avatar - Aufbruch nach Pandora (2009) 1080p h264 DTS 3D SBS [DE+EN].mkv
Serien/Breaking Bad (2008)/Staffel 3/Breaking Bad - S03E01 - Kein Más - 1080p [DE+EN].mkv
```

- Specials kommen nach `Staffel 0`.
- Staffel- und Folgennummern zweistellig, Staffelordner ohne führende Null.

### 6.2 Auflösung

Vorrangig nach Bildbreite, weil Kinofilme oft beschnitten sind:

| Breite | Ergebnis |
|---|---|
| ≥ 3200 | 2160p |
| ≥ 1800 | 1080p |
| ≥ 1200 | 720p |
| ≥ 900 oder Höhe ≥ 560 | 576p |
| sonst | 480p |

### 6.3 Videocodec und HDR

- Codec: `h264` (avc), `h265` (hevc), `av1`, `vp9`, `mpeg2`, `xvid` (mpeg4); sonst
  der ffprobe-Name in Kleinbuchstaben.
- HDR: Dolby Vision (DOVI-Konfiguration im Stream) → `DV`. Sonst PQ (`smpte2084`)
  oder HLG (`arib-std-b67`) → `HDR`. SDR → kein Zusatz.

### 6.4 Tonspur

1. Kandidaten: alle deutschen Spuren (`ger`, `deu`, `de`). Gibt es keine, die
   Standardspur, sonst die erste Spur.
2. Unter den Kandidaten gewinnt die beste nach dieser Rangfolge:
   TrueHD > DTS (inkl. DTS-HD, DTS:X) > EAC3 > AC3 > FLAC > AAC > Opus > MP3 > Rest.
   Bei Gleichstand gewinnen mehr Kanäle.
3. Schreibweise: `TrueHD`, `DTS`, `EAC3`, `AC3`, `FLAC`, `AAC`, `Opus`, `MP3`;
   sonst der ffprobe-Name in Großbuchstaben.

### 6.5 Sprachen

- Alle Tonspursprachen als zweistellige Kürzel in Großbuchstaben (babelfish),
  Deutsch zuerst, dann alphabetisch, ohne Doppelte, ohne `und`.
- Hat keine Spur eine Sprachkennung, werden die Sprachhinweise aus dem Dateinamen
  verwendet (`German.DL` → `DE+EN`). Ergibt sich nichts, entfällt die Klammer.

### 6.6 Untertitel

```
<Videoname ohne Endung>[.<sprache>][.forced][.<n>].<ext>
```

- Sprache aus dem Untertitelnamen (`ger`, `german`, `deu`, `de`, `eng` …) als
  zweistelliges Kürzel in Kleinbuchstaben.
- `forced` wird erkannt, wenn es im Namen steht.
- Mehrere Untertitel mit gleicher Sprache und Art werden ab 2 durchnummeriert.
- `.idx`/`.sub`-Paare behalten denselben Basisnamen.

### 6.7 Bereinigung

- `:` → ` -`, `/` und `\` → `-`, `? * " < > |` entfernen.
- Mehrfache Leerzeichen zusammenfassen, Punkte und Leerzeichen am Ende entfernen.
- Ein Dateiname darf höchstens 255 Bytes lang sein. Ist er länger, wird der
  Folgenname gekürzt, notfalls der Titel.

## 7. Ausführung, Konflikte, Aufräumen

### 7.1 Vorschlag

Pro Video gibt es einen Vorschlag mit Quelle, Ziel, Untertiteln, Sicherheit,
technischen Angaben, gegebenenfalls Konflikt und Status:
`neu`, `bestätigt`, `ignoriert`, `erledigt`, `fehler`.

Kann ffprobe eine Datei nicht lesen (außer ISO), gibt es keinen Zielpfad. Der
Vorschlag erscheint als Fehler.

### 7.2 Bibliotheksindex und Konflikte

- Der Index kennt pro Bibliothek alle Film- bzw. Serienordner mit Titel, Jahr und,
  sobald bekannt, der TMDB-ID. Liegt eine NFO von tinyMediaManager im Ordner, wird
  die TMDB-ID daraus gelesen. Sonst wird sie beim ersten Bedarf per Abgleich
  ermittelt und gespeichert.
- **Konflikt:** Derselbe Film bzw. dieselbe Folge (gleiche TMDB-ID, bei Serien
  dieselbe Staffel und Folge) liegt bereits in der Bibliothek, egal unter welchem
  Namen. Ebenso, wenn der exakte Zielpfad schon existiert oder ein anderer offener
  Vorschlag (auch aus einem früheren Scan) denselben Film bzw. dieselbe Folge betrifft.
- **Vorhandene Versionen** eines Films sind nur Videos direkt im Filmordner, ab der
  Sample-Grenze, ohne Plex-Extras (`-trailer`, `-featurette` … und Unterordner wie
  `Featurettes`). Filme in verschiedenen Bibliotheken (2D/3D) sind nie Dubletten.
- **Doppelfolgen** werden in beide Richtungen geprüft, damit keine Folge verloren geht:
  - Deckt eine vorhandene Datei Folgen ab, die die neue nicht enthält, wird
    „Neue Version behalten“ nicht angeboten und beim Ausführen abgelehnt.
  - Enthält die neue Datei Folgen, die einer vorhandenen Datei fehlen, wird
    „Vorhandene behalten“ nicht angeboten und beim Ausführen abgelehnt.
  - Trifft beides zu (z. B. neu E01–E02, vorhanden E02–E03), bleiben nur
    „Beide behalten“ und „Überspringen“.
  In all diesen Fällen gibt es keinen Vorschlag. Bei Treffern nur über den Titel
  (Ordner ohne Jahr und ohne TMDB-ID) gibt es ebenfalls keinen Vorschlag.
- Größe und Änderungszeit jeder vorhandenen Version werden gespeichert und vor dem
  Löschen erneut geprüft; hat sie sich verändert, wird nichts gelöscht.
- **Besser** wird in dieser Reihenfolge bewertet: Auflösung → DV vor HDR vor SDR →
  Codec (av1 = h265 > h264 > Rest) → deutsche Tonspur vorhanden →
  Rang der gewählten Tonspur → Dateigröße. Bei ISO-Dateien ohne technische
  Angaben wird kein Vorschlag gemacht, der Nutzer entscheidet.
- **Optionen:** „Bessere behalten“ (Vorschlag; die andere Version wird samt
  Untertiteln gelöscht), „Beide behalten“ (nur möglich, wenn die Zielnamen
  verschieden sind), „Überspringen“.

### 7.3 Ausführen

Bestätigte Vorschläge laufen nacheinander als Hintergrundauftrag. Es läuft immer
nur ein Auftrag. Pro Vorschlag:

1. Prüfen, ob Größe und Änderungszeit der Quelle noch dem Scan entsprechen.
   Wenn nicht: überspringen und als Fehler markieren.
2. Prüfen, ob das Ziel existiert. Wenn ja und kein Konflikt entschieden wurde:
   überspringen. **Es wird nie überschrieben.**
3. Zielordner anlegen, Video verschieben: zuerst `os.rename`. Bei `EXDEV`
   kopieren → Größe vergleichen → Original löschen. Bei einem Fehler wird die
   Teilkopie entfernt, das Original bleibt.
4. Untertitel verschieben.
5. Entschiedene Konflikte umsetzen (unterlegene Version löschen).
6. Ursprungsordner aufräumen (7.4).
7. Jede Aktion protokollieren.

Fehler betreffen nur den einzelnen Vorschlag, der Auftrag läuft weiter.

### 7.4 Aufräumen

- Ursprungsordner ist der Ordner, in dem das Video lag.
- Er wird **samt Inhalt gelöscht**, wenn darin (rekursiv) nichts mehr liegt, was
  verloren gehen könnte. Das Löschen wird verhindert durch:
  - jede Videodatei (breite Liste inkl. mpg, vob, webm …), egal wie groß, außer
    Samples nach Name (Ordner `Sample(s)` oder Dateiname endet auf `sample`),
  - unfertige Downloads (`.part`, `.!qB`, `.crdownload`) und Reste von mediasort,
  - Dateien, die jünger als das Mindestalter sind,
  - unbekannte Dateien ab der Sample-Grenze.
  Sonst bleibt der Ordner unverändert und wird in der Oberfläche markiert.
- **Nie gelöscht werden:** Eingangs- und Bibliotheks-Wurzelordner, Ordner aus der
  Ausschlussliste und deren übergeordnete Ordner.
- Übergeordnete Ordner werden nicht mit gelöscht.
- Im Bibliotheksmodus gilt dasselbe: Ein alter Film-, Serien- oder Staffelordner
  wird nach denselben Regeln gelöscht. NFO-Dateien und Bilder von
  tinyMediaManager gehen dabei verloren. Das ist gewollt, weil Plex sie nicht braucht.

### 7.5 Modus „Bibliothek prüfen“

- Eine Bibliothek wird wie ein Eingangsordner gescannt, Ziel ist sie selbst.
- Wählbarer Ausschnitt: Anfangsbuchstaben (z. B. A–C) oder ein einzelner Ordner.
- Dateien, die schon korrekt heißen und liegen, erzeugen keinen Vorschlag.
- Dubletten erscheinen als Konflikte.
- Vor dem ersten Lauf muss eine Warnung bestätigt werden, falls Plex den
  Papierkorb automatisch leert.

### 7.6 Automatische Ordnerüberwachung

Standardmäßig aus. Wird pro Eingangsordner eingeschaltet, wenn sich mediasort im
manuellen Betrieb bewährt hat.

- **Abfrage statt inotify:** inotify meldet auf SMB-Einbindungen keine Änderungen,
  die andere Rechner (z. B. das Download-Programm) direkt auf dem UNAS machen.
  mediasort sieht deshalb regelmäßig nach, standardmäßig alle 15 Minuten
  (einstellbar). Untersucht werden nur neue oder geänderte Dateien.
- **Stufen pro Eingangsordner:**
  - **Aus** (Standard): nur manuell über „Scannen“.
  - **Nur erkennen:** neue Dateien werden automatisch gescannt und landen in der
    Prüfliste. Ausgeführt wird erst nach Bestätigung.
  - **Automatisch einsortieren:** Vorschläge, die alle Bedingungen unten
    erfüllen, werden direkt ausgeführt. Alle anderen landen in der Prüfliste.
- **Hauptschalter** in den Einstellungen hält die Automatik überall an.
- **Automatisch ausgeführt wird nur, wenn alles zutrifft:**
  - Sicherheit hoch,
  - kein Konflikt, keine Dublette,
  - kein Fehler beim Auslesen,
  - die Datei ist stabil: mindestens so alt wie das Mindestalter (10 Minuten) und
    Größe sowie Änderungszeit seit der letzten Abfrage unverändert.
- **Nie automatisch:** Konflikte und Dubletten sowie der Modus „Bibliothek prüfen“.
- Plex-Teilscan und Aufräumen laufen wie bei manuellen Aufträgen.
- Automatische Aktionen sind im Verlauf als „automatisch“ markiert. Die Übersicht
  zeigt den Zeitpunkt der letzten Abfrage und deren Ergebnis.
- Ein Abfragezyklus wird übersprungen, solange ein anderer Scan oder Auftrag läuft.

## 8. Weboberfläche

Server-gerenderte Seiten (Jinja2) mit HTMX, responsiv, ohne Login.

- **Übersicht:** Eingangsordner mit Anzahl neuer Dateien, Automatik-Stufe und
  „Scannen“, letzte automatische Abfrage, laufender Auftrag mit Fortschritt,
  letzte Aktionen, Warnungen.
- **Prüfliste:** Serien gruppiert nach Serie → Staffel → Folgen, Filme einzeln.
  Pro Zeile alter Name → neuer Pfad, Sicherheit, technische Angaben,
  Konflikt/Fehler. Aktionen: Haken, Korrigieren (TMDB-Suche, TMDB-/IMDb-ID oder
  Link, Staffel/Folge, 3D-Format), Ignorieren. Filter nach Sicherheit, Typ,
  Konflikt, Fehler. Sammelaktionen „Alle sicheren übernehmen“ und
  „Ausgewählte ausführen“. Konflikte zeigen beide Versionen nebeneinander.
- **Bibliothek prüfen:** Bibliothek und Ausschnitt wählen, Scan starten.
- **Verlauf:** durchsuchbares Protokoll (Zeit, Aktion, alter Pfad, neuer Pfad,
  manuell/automatisch).
- **Ignoriert:** Liste der ignorierten Dateien, Ignorieren aufheben.
- **Einstellungen:** TMDB-Schlüssel und Sprache; Ordnerliste (Pfad, Rolle
  Eingang/Bibliothek, Typ, Ziel, Automatik-Stufe); Hauptschalter und
  Abfrageintervall der Automatik; Staffelbezeichnung; Sample-Grenze;
  Ausschlussliste; Mindestalter neuer Dateien; Plex (Adresse, Token,
  Pfadzuordnung).

## 9. Plex-Anbindung

- Optional. Ohne Adresse und Token ist sie aus.
- Liest `/library/sections` (Bibliotheken und deren Pfade) und `/:/prefs`
  (Einstellung `autoEmptyTrash`).
- Pfadzuordnung als Liste von Präfix-Paaren (mediasort-Pfad → Plex-Pfad).
  Vorschlag automatisch anhand gleicher Pfadenden; bei identischen Mounts 1:1.
- Nach jedem Auftrag: `GET /library/sections/{id}/refresh?path=<Ordner>` für jeden
  betroffenen Ordner (Ziel, alter Ort, Ort gelöschter Dubletten), zusammengefasst
  auf die übergeordneten Titelordner.
- Ordner, die zu keiner Plex-Bibliothek gehören (z. B. `Filme_3D`, `Downloads`),
  werden ohne Hinweis übersprungen.
- Ist Plex nicht erreichbar, gibt es einen Hinweis. Der Auftrag gilt trotzdem als
  erfolgreich.

## 10. Datenhaltung

SQLite in `/config/mediasort.db`.

| Tabelle | Inhalt |
|---|---|
| `settings` | Schlüssel/Wert |
| `folders` | Pfad, Rolle, Typ, Ziel-Ordner, Automatik-Stufe |
| `scans` | Zeitpunkt, Ordner, Ausschnitt, Status |
| `items` | Vorschlag: Quelle, Größe, Änderungszeit, Parse-Ergebnis, TMDB-ID, Staffel, Folgen, Sicherheit, Mediainfo, Ziel, Untertitel, Konflikt, Entscheidung, Status, Fehlertext |
| `ignored` | Pfad, Größe, Zeitpunkt |
| `library_index` | Bibliothek, Ordnerpfad, Titel, Jahr, TMDB-ID, Typ |
| `history` | Zeitpunkt, Auftrag, Aktion (verschoben/gelöscht/Ordner gelöscht), alter Pfad, neuer Pfad, automatisch ja/nein |
| `tmdb_cache` | Anfrage-Schlüssel, Antwort, Zeitpunkt |
| `jobs` | Art, Status, Fortschritt, Start, Ende, automatisch ja/nein |
| `file_state` | Pfad, Größe, Änderungszeit, zuletzt gesehen (für die Stabilitätsprüfung der Automatik) |

## 11. Installation

- **VM:** Debian, 2 vCPU, 2 GB RAM, 16 GB Disk, Docker mit Compose-Plugin.
- **Mount:** `Medien` als **eine** CIFS-Einbindung über `/etc/fstab` nach `/media`.
  Getrennte Einbindungen einzelner Unterordner würden jedes Verschieben zwischen
  ihnen zu einer Kopie übers Netz machen. Optionen: `credentials=`, `uid`, `gid`,
  `file_mode=0660`, `dir_mode=0770`, `vers=3.1.1`, `_netdev`, `nofail`.
- **docker-compose.yml:** `/media:/media`, `./config:/config`,
  `PUID`, `PGID`, `TZ=Europe/Berlin`, Port 8080.
- **Image:** Python 3.12 slim mit ffmpeg. Der Prozess läuft als `PUID:PGID`.
  Rechte werden nicht gesetzt (SMB erlaubt kein chown).
- **Repository:** `github.com/maxbrechtel/mediasort`. GitHub Actions führt die
  Tests aus und baut das Image nach `ghcr.io/maxbrechtel/mediasort`.
- README mit Anleitung für VM, Mount, Compose, TMDB-Schlüssel und Plex-Token.

## 12. Tests

- **pytest**, ohne Netzwerkzugriff.
- **parser:** Tabellentests mit echten Namen aus der Bibliothek, u. a.
  `Reacher.S04E05.German.DL.2160p.DV.HDR.WEBRip.AV1-WOTT`, `bm1080-s01e02`,
  `Chernobyl.2019.E05.720p.BluRay.DD5.1.x264-DON`, `Final Destination 5 3D OU.mkv`,
  `Der.König.der.Löwen.(1994).(German).DTS.HD.DL.BluRay.AVC.Remux.-.LeetHD.-.ZED`,
  `Loki S01 GERMAN DL HDR 2160P WEB H265-RiLE`, sowie `Staffel3.Folge1`.
- **namer, Qualitätsvergleich, Tonspurwahl, Auflösung, Sprachen, Bereinigung:**
  Tabellentests der reinen Funktionen.
- **mediainfo:** Kleine Testvideos, im Test mit ffmpeg erzeugt (mehrere
  Tonspuren, Sprachkennungen, Standardspur).
- **tmdb, matcher, plex:** aufgezeichnete Antworten als Fixtures.
- **executor:** temporäre Ordner. Verschieben, simuliertes `EXDEV`, Abbruch beim
  Kopieren, Schutzregel, Konflikte, nie überschreiben, geänderte Quelle.
- **watcher:** Stabilitätsprüfung, Bedingungen für automatisches Ausführen,
  Hauptschalter, Überspringen bei laufendem Auftrag (mit simulierter Uhr).
- **web:** FastAPI-TestClient für die wichtigsten Abläufe (Scan → Prüfliste →
  Bestätigen → Ausführen).
- **Abnahme:** Auf dem UNAS wird per `cp -a --reflink=always` eine Kopie von
  `Downloads` als `Medien/_Test` angelegt und als Eingangsordner mit eigenem
  Testziel verwendet.

## 13. Nicht im Umfang

- Rückgängig-Funktion (das Protokoll enthält alle alten Pfade)
- Absolute Folgennummerierung (Anime)
- Disc-Strukturen (BDMV, VIDEO_TS)
- NFO- und Bild-Erzeugung
- Login und Benutzerverwaltung
- Benachrichtigungen (Mail, Push)
- Andere Datenquellen als TMDB

## 14. Offene Punkte

Keine.
