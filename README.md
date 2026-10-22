# mediasort

mediasort erkennt Film- und Seriendateien, gleicht sie mit TMDB ab und legt sie
nach einem festen Schema in einer Plex-Bibliothek ab. Bedient wird es über eine
Weboberfläche im Heimnetz. Jede Änderung erscheint zuerst in einer Prüfliste und
wird erst nach Bestätigung ausgeführt.

> **Gemeinsamer Betrieb mit SABnzbd, Prowlarr, Sonarr und Radarr hinter ExpressVPN
> (Gluetun)?** Dann nicht dieser Anleitung folgen, sondern
> **[docs/arr-stack/README.md](docs/arr-stack/README.md)**. Dort laufen alle
> Programme in einer VM, mediasort ist mit eingerichtet.

## Funktionen

- Dateien in einem Eingangsordner erkennen, Namen zerlegen, mit TMDB abgleichen
  (auch per TMDB-/IMDb-ID oder Link korrigierbar).
- Einheitliches Namensschema mit Auflösung, Codec, HDR, bester deutscher Tonspur
  und Sprachen (`[DE+EN]`); Untertitel werden mitverschoben.
- Prüfliste mit Sicherheit, Konflikt- und Dublettenerkennung. Die bessere Version
  wird vorgeschlagen, entschieden wird von Hand.
- 3D-Filme in eigener Bibliothek, ISO-Abbilder werden einsortiert.
- Modus „Bibliothek prüfen“ für die bestehende Bibliothek, in Etappen.
- Aufräumen: Reste (NFO, Bilder, Samples) und leere Ursprungsordner verschwinden.
- Gezielter Plex-Teilscan nach jedem Auftrag, Warnung beim automatischen
  Papierkorb-Leeren.
- Optional eine automatische Ordnerüberwachung pro Eingangsordner (standardmäßig aus).
- Verlauf aller Aktionen, Liste ignorierter Dateien.

Es gibt keine Anmeldung. mediasort gehört nur ins Heimnetz.

## Installation (eigenständig)

### 1. VM anlegen

In Proxmox VE eine VM mit Debian anlegen: 2 vCPU, 2 GB RAM, 16 GB Disk, im selben
Netz wie UNAS und Plex. Danach als root weiterarbeiten:

```bash
apt update && apt full-upgrade -y
apt install -y cifs-utils curl
curl -fsSL https://get.docker.com | sh      # installiert Docker mit Compose-Plugin
```

### 2. Freigabe `Medien` einbinden

Dem SMB-Benutzer in der UniFi-Oberfläche Lese- und Schreibrechte auf `Medien` geben
und die Zugangsdaten ablegen:

```bash
cat > /root/.smb-unas <<'EOF'
username=DEIN_SMB_BENUTZER
password=DEIN_SMB_PASSWORT
EOF
chmod 600 /root/.smb-unas
```

In `/etc/fstab` **eine einzige** Zeile ergänzen (`UNAS` durch Name oder IP ersetzen):

```
//UNAS/Medien  /media  cifs  credentials=/root/.smb-unas,uid=1000,gid=1000,file_mode=0660,dir_mode=0770,vers=3.1.1,_netdev,nofail  0  0
```

**Wichtig: eine einzige Einbindung.** Nicht `Downloads`, `Filme_2D` und die anderen
Ordner einzeln einbinden. Sonst wäre jedes Verschieben dazwischen eine Kopie über
das Netz statt einer sofortigen Umbenennung auf dem UNAS.

```bash
systemctl daemon-reload
mount -a
ls /media        # Downloads  Filme_2D  Filme_3D  Serien
```

`uid`/`gid` (hier 1000) sind die Kennung, unter der mediasort läuft. Sie müssen zu
`PUID`/`PGID` unten passen.

### 3. Image holen und starten

Das Repository ist privat, daher liegt auch das Image privat in der GitHub Container
Registry. Einmalig anmelden, mit einem GitHub-Token (Personal Access Token, classic)
mit dem Recht `read:packages`:

```bash
docker login ghcr.io -u maxbrechtel
```

Danach die Compose-Datei holen und starten:

```bash
mkdir -p /opt/mediasort/config && cd /opt/mediasort
chown 1000:1000 config                # PUID:PGID, der Container schreibt dort
# docker-compose.yml aus diesem Repository hierher kopieren
printf 'PUID=1000\nPGID=1000\n' > .env
docker compose up -d
```

Der Ordner `config` muss vorher existieren und `PUID:PGID` gehören (sonst legt
Docker ihn als `root` an und mediasort kann nicht schreiben). Er enthält die
Datenbank und die Einstellungen und kommt in das VM-Backup.

Die Oberfläche läuft auf `http://IP-DER-VM:8080`. Soll sie auf einem anderen Port
laufen, `MEDIASORT_PORT=8090` in die Datei `.env` schreiben.

## Erste Schritte

### TMDB-Schlüssel

1. Auf <https://www.themoviedb.org> ein kostenloses Konto anlegen.
2. Unter *Einstellungen → API* einen Schlüssel beantragen (privater Gebrauch).
3. Den **API-Schlüssel** (oder das „API Read Access Token“) in mediasort unter
   *Einstellungen → TMDB* eintragen. Solange er fehlt, zeigt die Übersicht eine
   Warnung.

### Plex-Token (optional)

Mit Plex-Adresse und Token stößt mediasort nach jedem Auftrag einen Teilscan an.

1. In der Plex-Weboberfläche irgendeinen Titel öffnen, im Menü **„Info anzeigen“**
   bzw. **„Get Info“** → **„XML anzeigen“** wählen.
2. In der Adresszeile des neuen Tabs steht am Ende `X-Plex-Token=…`. Dieser Wert
   ist der Token.
3. In mediasort unter *Einstellungen → Plex* Adresse (`http://IP-DER-PLEX-VM:32400`)
   und Token eintragen. Die Pfadzuordnung bleibt leer, wenn Plex dieselben Pfade
   (`/media/...`) sieht.

### Ordner anlegen

Unter *Einstellungen → Ordner hinzufügen* zuerst die **Bibliotheken**, dann den
**Eingang** eintragen (die Ziele des Eingangs werden aus den Bibliotheken gewählt):

| Pfad | Rolle | Typ | Ziele |
|---|---|---|---|
| `/media/Filme_2D` | Bibliothek | | |
| `/media/Serien` | Bibliothek | | |
| `/media/Filme_3D` | Bibliothek | | |
| `/media/Downloads` | Eingang | automatisch | Filme: `/media/Filme_2D`, Serien: `/media/Serien`, 3D-Filme: `/media/Filme_3D` |

`Downloads` ist der **Eingang** mit Typ „automatisch“ (mediasort entscheidet pro
Datei, ob Film oder Serie) und den drei Zielordnern. `Filme_2D`, `Serien` und
`Filme_3D` sind **Bibliotheken**. `Filme_3D` ist bewusst nicht in Plex eingebunden.

## Abnahme mit einer Kopie

Bevor mediasort an die echte Bibliothek geht, an einer Kopie üben. Auf btrfs
legt `--reflink=always` die Kopie sofort an und braucht keinen zusätzlichen Platz
(das geht nur innerhalb derselben Freigabe, deshalb liegt der Testordner in
`Medien`):

```bash
mkdir -p /media/_Test/Eingang
cp -a --reflink=always "/media/Downloads/ein-testordner" /media/_Test/Eingang/
```

Dann in den Einstellungen Test-Bibliotheken `/media/_Test/Filme` und
`/media/_Test/Serien` sowie den Test-Eingang `/media/_Test/Eingang` (Typ
„automatisch“, Ziele auf diese Bibliotheken) anlegen, scannen und die Prüfliste durchsehen. Erst
wenn Namen und Ablageorte stimmen, den echten Eingang `Downloads` verwenden. Zum
Schluss den Ordner `/media/_Test` samt Test-Einträgen in den Einstellungen löschen.

## Plex: Papierkorb nicht automatisch leeren

**Vor dem ersten Lauf in Plex ausschalten:** *Einstellungen → Bibliothek →
„Papierkorb nach jedem Scan automatisch leeren“.*

Verschiebt mediasort Dateien, fehlen sie für einen Augenblick an ihrem alten Ort.
Leert Plex den Papierkorb automatisch, verschwindet der Titel samt Abspielhistorie
aus Plex. mediasort warnt in der Übersicht, wenn die Einstellung noch an ist. Für
den Modus „Bibliothek prüfen“ ist zusätzlich ein Backup der Plex-VM sinnvoll.

## Automatik

Die automatische Ordnerüberwachung ist **standardmäßig aus**. Sie wird erst
eingeschaltet, wenn sich der manuelle Betrieb bewährt hat.

1. *Einstellungen → Automatik:* „Automatik eingeschaltet (Hauptschalter)“ ankreuzen und das Abfrageintervall
   prüfen (Standard 15 Minuten).
2. *Einstellungen → Ordner → Bearbeiten* beim Eingangsordner die Stufe unter „Automatik“ wählen:
   - **Aus** – nur manuell über „Scannen“.
   - **Nur erkennen** – neue Dateien landen automatisch in der Prüfliste,
     ausgeführt wird erst nach Bestätigung.
   - **Automatisch einsortieren** – sichere Treffer ohne Konflikt und mit stabiler
     Datei (Mindestalter 10 Minuten) werden direkt ausgeführt, alles andere
     bleibt in der Prüfliste.
3. Im *Verlauf* sind automatische Aktionen als „automatisch“ markiert.

Konflikte, Dubletten und der Modus „Bibliothek prüfen“ laufen nie automatisch.
Empfehlung: zuerst „Nur erkennen“, später „Automatisch einsortieren“.

## Aktualisieren

```bash
cd /opt/mediasort
docker compose pull
docker compose up -d
```

Neue Versionen baut GitHub Actions bei jedem Push auf `main` nach
`ghcr.io/maxbrechtel/mediasort:latest`. Einstellungen und Datenbank bleiben im
Ordner `config` erhalten.

## Entwicklung

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[test]'      # ffmpeg (ffprobe) muss installiert sein
pytest -q -W error
```

Das Image lässt sich lokal mit `docker build -t mediasort:test .` bauen. Die
Compose-Datei dieses Repositories baut es mit `docker compose build` selbst.
