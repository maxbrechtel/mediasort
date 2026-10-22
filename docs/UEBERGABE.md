# Übergabe: Stand und nächste Schritte

Stand: 2026-10-22. Diese Datei fasst zusammen, was in der ersten Arbeitssitzung entschieden
und gebaut wurde, damit eine neue Sitzung nahtlos weitermachen kann.

## 1. Was fertig ist

- **mediasort** ist vollständig umgesetzt und auf `main`:
  - Weboberfläche (Übersicht, Prüfliste, Bibliothek prüfen, Verlauf, Ignoriert, Einstellungen)
  - Erkennung über guessit, deutsche Muster und Release-Ordner, Abgleich mit TMDB, ffprobe
  - Benennung nach Schema, sicheres Verschieben und Aufräumen, Plex-Teilscan
  - automatische Ordnerüberwachung (standardmäßig aus)
- **Tests:** 585 bestanden, einer übersprungen (läuft nur ohne Root).
- **CI** auf GitHub ist grün. Das Image `ghcr.io/maxbrechtel/mediasort:latest` ist gebaut.
- **Dokumente:**
  - Spec: `docs/superpowers/specs/2026-10-22-mediasort-design.md` (verbindlich)
  - Plan: `docs/superpowers/plans/2026-10-22-mediasort.md`
  - Installation von mediasort allein: `README.md`
  - Gemeinsame Einrichtung mit arr-Stack und VPN: `docs/arr-stack/README.md`, dazu
    `docs/arr-stack/docker-compose.yml` und `.env.example`

## 2. Umgebung des Nutzers

- **Proxmox VE** mit PBS als Backup-Ziel.
- **Plex** läuft in einer eigenen VM. Seine Bibliotheken sind `/media/Filme_2D` und
  `/media/Serien`. `Filme_3D` ist bewusst **nicht** in Plex eingebunden.
- **UniFi UNAS** mit btrfs:
  - Die früheren Freigaben `Filme_2D`, `Filme_3D`, `Serien` und `Downloads` wurden per
    Reflink in die neue Freigabe **`Medien`** kopiert, mit denselben Unterordnern.
  - Pfad auf dem UNAS: `/volume/7006f584-f689-4596-b752-a55a1953d57d/.srv/.unifi-drive/Medien/.data/`
  - Größen: Filme_2D ca. 13 TB (ca. 4.700 Dateien), Serien ca. 7,7 TB (ca. 17.500), Filme_3D
    ca. 420 GB (43, ohne Jahr, `3D SBS`/`3D OU`, eine `.iso`), Downloads ca. 1,1 TB.
- **Bestand:** Die Bibliothek wurde früher mit tinyMediaManager gepflegt. Es gibt also NFO-Dateien,
  `.deletedByTMM`-Ordner und einige Dubletten bzw. lose Dateien.
- **Download-Programm:** Bisher keines. Geplant ist **SABnzbd** mit Usenet.
- **VPN:** ExpressVPN über **Gluetun**. Nur die Container laufen über den VPN, mit Killswitch.
  Die VM selbst geht direkt ins Netz.

## 3. Offen beim Nutzer (vor der Einrichtung bestätigen)

1. **Alte UNAS-Freigaben:** Sind `Filme_2D`, `Filme_3D`, `Serien` und `Downloads` als einzelne
   Freigaben schon gelöscht? Das sollte erst geschehen, wenn alles auf `Medien` umgestellt ist.
2. **Plex-VM:** Ist sie schon auf die neue Freigabe umgestellt? Vereinbart ist Variante 2:
   `//UNAS/Medien/Filme_2D` → `/media/Filme_2D` und `//UNAS/Medien/Serien` → `/media/Serien`.
   Die Pfade bleiben gleich, Plex merkt den Umzug nicht. Vorher Plex stoppen bzw. „Papierkorb
   automatisch leeren“ aus.
3. Werte, die für die Einrichtung noch fehlen:
   - IP der neuen VM
   - IP des UNAS
   - IP der Plex-VM
   - Heimnetz (für `HOME_SUBNET`)
   - SMB-Benutzer

## 4. Nächste Schritte: Einrichtung

Grundlage ist `docs/arr-stack/README.md`, Abschnitte 3 bis 13.

1. **Neue VM `medien` in Proxmox:**
   - Debian 13, 4 vCPU, 4 GB RAM, 32 GB Disk, gleiche Bridge wie die Plex-VM
   - QEMU-Agent an, feste IP
   - Nur SSH-Server und Standard-Werkzeuge installieren
2. **Freigabe einbinden:** `Medien` als **eine** CIFS-Einbindung unter `/media`, per fstab,
   Zugangsdaten in `/root/.smb-unas`. Den Container erst starten, wenn die Einbindung steht.
3. **Docker** installieren und Ordner für SABnzbd anlegen: `/media/Downloads/usenet/...`
4. **Stack starten** in `/opt/medien` mit `docker-compose.yml` und `.env` aus `docs/arr-stack`:
   - Die ExpressVPN-OpenVPN-Zugangsdaten trägt der Nutzer selbst ein.
   - `docker login ghcr.io -u maxbrechtel` mit einem Token mit `read:packages`, weil das
     Repository privat ist.
   - VPN und Killswitch prüfen, wie in Abschnitt 6 der Anleitung beschrieben.
5. **SABnzbd** auf 8080, **Prowlarr** auf 9696, **Sonarr** auf 8989, **Radarr** auf 7878
   einrichten. Beim Library Import „Rename“ zunächst aus lassen.
6. **mediasort** auf 8090 einrichten (`MEDIASORT_PORT=8090`):
   - TMDB-Schlüssel und Plex (Adresse, Token) eintragen.
   - Ordner anlegen: `Downloads` als Eingang mit Typ „automatisch“ und den Zielen. `Filme_2D`,
     `Serien` und `Filme_3D` als Bibliothek.
   - `usenet` auf die Ausschlussliste setzen.
7. **Probelauf vor dem echten Einsatz:**
   - Auf dem UNAS per `cp -a --reflink=always` eine Testkopie nach `Medien/_Test` anlegen.
   - Darin sollten vorkommen: kleine Folgen, `.mpg`-Dateien, ein Extras-Ordner, ein
     3D-Release, eine Doppelfolge und eine Umbenennung, die nur die Schreibweise ändert.
   - Als eigenen Eingangsordner mit Testzielen verwenden. Die Automatik bleibt aus.
8. **Erst danach „Bibliothek prüfen“:**
   - Vorher Plex-VM per PBS sichern (Modus „Stop“) und „Papierkorb automatisch leeren“ aus.
   - In Etappen nach Anfangsbuchstaben vorgehen.
   - In der Testphase nicht auf Bibliotheken anwenden, die Sonarr oder Radarr verwalten.

## 5. Arbeitsregeln (Wünsche des Nutzers)

- **Sprache:** Kommunikation auf Deutsch.
- **Passwörter:** SMB, ExpressVPN, TMDB-Schlüssel und Plex-Token trägt der Nutzer selbst ein.
  Sie nicht im Chat abfragen.
- **Rechte:** Auf dem Proxmox-Host und dem UNAS nicht eigenmächtig als root arbeiten, nur
  gemeinsam mit dem Nutzer. Auf der Medien-VM selbst ist das unkritisch.
- **Plex:** Die Abspielhistorie soll erhalten bleiben. Deshalb Pfade unverändert lassen,
  „Papierkorb automatisch leeren“ aus und Backup vorher.
- **Sonarr/Radarr:** Sie sollen zunächst testweise selbst umbenennen. Danach wird entschieden,
  welches Schema bleibt.
- **Automatik in mediasort:** Erst einschalten, wenn der manuelle Betrieb überzeugt.

## 6. Bekannte Restpunkte in mediasort

Keiner davon gefährdet Dateien.

- **Reverse-Proxy:** Hinter einem Proxy, der den Host-Header umschreibt, lehnt die CSRF-Prüfung
  Formulare mit 403 ab. Ohne Proxy spielt das keine Rolle.
- **Liegen gebliebene Ordner:** Release-Ordner mit großen `.rar`- oder `.par2`-Resten werden
  bewusst nicht gelöscht.
- **Doppelfolge gegen Einzeldateien:** Trifft eine neue Doppelfolge auf vorhandene
  Einzeldateien, wird „Vorhandene behalten“ konservativ gesperrt. Die neue Datei muss man dann
  bei Bedarf von Hand löschen.
- **Plex-Editionen:** `{edition-…}`-Dateien im Filmordner zählen als Versionen. Sie werden in der
  Konfliktansicht angezeigt.
- **Ohne Migration:** Das Datenbankschema hat keine Migrationen. Das ist vor dem ersten
  Einsatz unproblematisch.
