# arr-Stack mit SABnzbd hinter ExpressVPN

Diese Anleitung richtet auf Proxmox VE eine VM ein, in der mediasort, SABnzbd,
Prowlarr, Sonarr und Radarr laufen. Der gesamte Internetverkehr dieser Programme
geht über ExpressVPN. Fällt der VPN aus, sperrt ein Killswitch jeden Verkehr nach
außen.

Dateien in diesem Ordner:

- `docker-compose.yml` – alle Container
- `.env.example` – Vorlage für Zugangsdaten und Netz

## 1. Aufgabenteilung

| Was | Wer |
|---|---|
| Usenet-Downloads suchen, laden, entpacken | SABnzbd, gesteuert von Sonarr/Radarr |
| Diese Downloads benennen und in `Serien` bzw. `Filme_2D` ablegen | Sonarr/Radarr |
| Manuelle und DDL-Downloads in `Downloads` | mediasort |
| 3D-Filme | mediasort |
| Bestehende Bibliothek aufräumen, Dubletten | mediasort („Bibliothek prüfen“) |

In der Testphase gilt: **mediasort „Bibliothek prüfen“ nicht auf Ordner anwenden,
die Sonarr oder Radarr verwalten.** Sonst benennen beide Programme dieselben
Dateien abwechselnd um. Sobald feststeht, welches Schema bleibt, wird pro
Bibliothek festgelegt, wer umbenennt.

## 2. Wie der VPN eingebunden ist

```
Internet ◄── ExpressVPN ◄── gluetun ◄── sabnzbd, prowlarr, sonarr, radarr, mediasort
                               │
Heimnetz (Plex, Browser) ◄─────┘  nur das in HOME_SUBNET eingetragene Netz

VM selbst ──► UNAS (SMB), Systemupdates, docker pull   (ohne VPN)
```

- Gluetun baut den VPN auf. Die anderen Container nutzen über
  `network_mode: "service:gluetun"` dessen Netzwerk.
- **Killswitch:** Gluetun lässt Verkehr nach außen nur durch den VPN-Tunnel zu.
  Ist der Tunnel weg, ist auch das Netz der Container weg.
- **Heimnetz:** Nur das Netz in `HOME_SUBNET` ist ohne VPN erreichbar, damit
  Sonarr, Radarr und mediasort Plex benachrichtigen können.
- Die Freigabe `Medien` bindet die VM ein, nicht die Container. Der VPN berührt
  den Zugriff auf den UNAS nicht.
- Weil alle Programme ein gemeinsames Netzwerk haben, werden die Ports nur bei
  `gluetun` veröffentlicht. Untereinander sprechen sich die Programme über
  `localhost` an. SABnzbd läuft auf 8080, mediasort deshalb auf 8090.

## 3. VM anlegen

In Proxmox VE eine neue VM mit Debian:

- 4 vCPU, 4 GB RAM, 32 GB Disk
- Netzwerk im selben Netz wie UNAS und Plex
- Nach der Installation per SSH anmelden und als root weiterarbeiten.

```bash
apt update && apt full-upgrade -y
apt install -y cifs-utils curl
curl -fsSL https://get.docker.com | sh
```

## 4. Freigabe `Medien` einbinden

In der UniFi-Oberfläche dem SMB-Benutzer Lese- und Schreibrechte auf `Medien` geben.

Zugangsdaten ablegen:

```bash
cat > /root/.smb-unas <<'EOF'
username=DEIN_SMB_BENUTZER
password=DEIN_SMB_PASSWORT
EOF
chmod 600 /root/.smb-unas
```

In `/etc/fstab` **eine einzige** Zeile ergänzen (`UNAS` durch Name oder IP
ersetzen):

```
//UNAS/Medien  /media  cifs  credentials=/root/.smb-unas,uid=1000,gid=1000,file_mode=0660,dir_mode=0770,vers=3.1.1,iocharset=utf8,_netdev,nofail  0  0
```

Nicht die Unterordner einzeln einbinden: Dann wäre jedes Verschieben von
`Downloads` in die Bibliothek eine Kopie über das Netz statt einer sofortigen
Umbenennung.

```bash
systemctl daemon-reload
mount -a
ls /media        # Downloads  Filme_2D  Filme_3D  Serien
```

Ordner für SABnzbd anlegen:

```bash
mkdir -p /media/Downloads/usenet/incomplete \
         /media/Downloads/usenet/complete/tv \
         /media/Downloads/usenet/complete/movies
```

## 5. ExpressVPN-Zugangsdaten

Im ExpressVPN-Konto unter **„Manuelle Konfiguration“** → **OpenVPN** stehen ein
eigener Benutzername und ein eigenes Passwort. Diese gehören in `.env`, **nicht**
deine normalen Anmeldedaten.

## 6. Container starten

```bash
mkdir -p /opt/medien && cd /opt/medien
# docker-compose.yml und .env.example aus diesem Ordner hierher kopieren
cp .env.example .env && chmod 600 .env
nano .env        # Zugangsdaten, HOME_SUBNET, PUID/PGID eintragen
```

`PUID`/`PGID` müssen zu `uid`/`gid` in der fstab-Zeile passen.

Den Konfigurationsordner von mediasort vorher anlegen, damit er `PUID:PGID`
gehört (sonst legt Docker ihn als `root` an):

```bash
mkdir -p mediasort && chown 1000:1000 mediasort     # PUID:PGID
```

**mediasort** kommt als fertiges Image aus der GitHub Container Registry
(`ghcr.io/maxbrechtel/mediasort:latest`). Weil das Repository privat ist, einmalig
bei GitHub anmelden (Token mit `read:packages`):

```bash
docker login ghcr.io -u maxbrechtel
```

Im gemeinsamen Gluetun-Netzwerk belegt SABnzbd schon den Port 8080. mediasort
lauscht deshalb über `MEDIASORT_PORT=8090` auf **8090** (so steht es im
`mediasort:`-Block der `docker-compose.yml`); der Gesundheitstest des Images
folgt dieser Einstellung automatisch.

Starten:

```bash
docker compose up -d
docker compose logs -f gluetun     # warten auf "Public IP address is …", Strg+C
```

### VPN und Killswitch prüfen

```bash
curl -s https://ipinfo.io/ip                          # IP der VM (dein Anschluss)
docker exec sabnzbd curl -s https://ipinfo.io/ip      # muss eine andere, die VPN-IP sein

docker compose stop gluetun
docker exec sabnzbd curl -s --max-time 5 https://ipinfo.io/ip || echo "gesperrt – Killswitch greift"
docker compose up -d --force-recreate
```

**Wichtig:** Wird `gluetun` neu gestartet oder aktualisiert, verlieren die
anderen Container ihr Netzwerk, bis sie selbst neu gestartet werden. Deshalb
immer den ganzen Stack neu starten:

```bash
docker compose up -d --force-recreate
```

## 7. SABnzbd einrichten

Aufruf: `http://IP-DER-VM:8080`

1. Assistent: Sprache, dann den **Usenet-Server** deines Anbieters (Host,
   Port 563, SSL an, Benutzer, Passwort, Verbindungen).
2. *Config → Folders*:
   - Temporary Download Folder: `/media/Downloads/usenet/incomplete`
   - Completed Download Folder: `/media/Downloads/usenet/complete`
3. *Config → Categories*: `tv` mit Ordner `tv`, `movies` mit Ordner `movies`.
4. *Config → General*: den **API Key** notieren (für Sonarr/Radarr).
5. Meldet SABnzbd beim Aufruf über einen Hostnamen „Hostname verification
   failed“: unter *Config → Special → host_whitelist* den Hostnamen der VM
   eintragen.

## 8. Prowlarr einrichten

Aufruf: `http://IP-DER-VM:9696`

1. Anmeldung festlegen.
2. *Indexers*: deine Usenet-Indexer hinzufügen.
3. *Settings → Apps*: Sonarr und Radarr hinzufügen.
   - Prowlarr Server: `http://localhost:9696`
   - Sonarr Server: `http://localhost:8989`, API-Key aus Sonarr (*Settings → General*)
   - Radarr Server: `http://localhost:7878`, API-Key aus Radarr

Prowlarr überträgt die Indexer danach selbst an Sonarr und Radarr.

## 9. Sonarr einrichten (Serien)

Aufruf: `http://IP-DER-VM:8989`

1. *Settings → Media Management*:
   - **Rename Episodes:** in der Testphase erst aus lassen, siehe Abschnitt 11
   - Standard Episode Format:
     `{Series Title} - S{season:00}E{episode:00} - {Episode Title} - {Quality Title} {MediaInfo AudioLanguages}`
   - Series Folder Format: `{Series Title} ({Series Year})`
   - Season Folder Format: `Staffel {season}`
   - Multi-Episode Style: **Prefixed Range** (ergibt `S01E01-E02`, wie mediasort)
   - Root Folder: `/media/Serien`
2. *Settings → Download Clients*: SABnzbd, Host `localhost`, Port `8080`,
   API-Key, Kategorie `tv`.
3. *Settings → Connect*: **Plex Media Server**, Host = IP der Plex-VM, Port
   `32400`, „Authenticate with Plex.tv“, „Update Library“ an.

Die Namensvorschau unter *Media Management* zeigt sofort, wie Dateien heißen
werden.

## 10. Radarr einrichten (Filme)

Aufruf: `http://IP-DER-VM:7878`

1. *Settings → UI*: Sprache für Filminformationen auf **Deutsch** stellen, damit
   Titel wie „Stirb langsam“ statt „Die Hard“ verwendet werden.
2. *Settings → Media Management*:
   - **Rename Movies:** in der Testphase erst aus lassen, siehe Abschnitt 11
   - Standard Movie Format:
     `{Movie Title} ({Release Year}) {Quality Title} {MediaInfo VideoCodec} {MediaInfo VideoDynamicRangeType} {MediaInfo AudioCodec} {MediaInfo AudioLanguages}`
   - Movie Folder Format: `{Movie Title} ({Release Year})`
   - Root Folder: `/media/Filme_2D`
3. *Settings → Download Clients*: SABnzbd wie bei Sonarr, Kategorie `movies`.
4. *Settings → Connect*: Plex wie bei Sonarr.

### Unterschiede zum mediasort-Schema

| | mediasort | Sonarr/Radarr |
|---|---|---|
| Auflösung | `1080p` | im Qualitätsnamen, z. B. `Bluray-1080p` |
| Tonspur | beste deutsche Spur | erste Spur der Datei |
| Sprachen | `[DE+EN]`, Deutsch zuerst | Reihenfolge der Datei, z. B. `[EN+DE]` |
| Serientitel | Deutsch (TMDB) | meist Englisch |
| 3D | eigene Bibliothek | nicht unterstützt |

## 11. Bestehende Bibliothek übernehmen

Sonarr (*Series → Library Import*) und Radarr (*Movies → Library Import*) können
die vorhandenen Ordner einlesen.

1. „Rename“ in *Media Management* **aus** lassen.
2. Library Import für `/media/Serien` bzw. `/media/Filme_2D` ausführen und die
   Zuordnungen prüfen.
3. Bei einzelnen Titeln über *Organize* (bzw. *Rename*) die Vorschau ansehen.
4. Erst wenn die Namen gefallen, „Rename“ einschalten.

Vorher in Plex „Papierkorb nach jedem Scan automatisch leeren“ ausschalten und
ein Backup der Plex-VM machen. Das Umbenennen ändert sonst möglicherweise die
Abspielhistorie.

## 12. mediasort neben dem arr-Stack

- In den mediasort-Einstellungen `usenet` auf die **Ausschlussliste** setzen.
  Dann fasst mediasort die Downloads von SABnzbd nie an.
- Eingangsordner bleibt `/media/Downloads` für manuelle und DDL-Downloads.
- mediasort ist unter `http://IP-DER-VM:8090` erreichbar.

## 13. Aktualisieren

```bash
cd /opt/medien
docker compose pull
docker compose up -d --force-recreate
```

## Anhang: VPN für die ganze VM statt nur für die Container

Soll auch die VM selbst (Systemupdates, `docker pull`) nur über den VPN ins
Internet, gibt es die ExpressVPN-App für Linux mit „Network Lock“ als
Killswitch. Dann muss der Zugriff auf das lokale Netz erlaubt sein, sonst
funktionieren die SMB-Einbindung und Plex nicht. Nachteile: Hängt der VPN,
kommen auch keine Updates mehr durch, und die Ausnahmen fürs Heimnetz sind
schwerer zu überprüfen. Für den Schutz der Downloads reicht die Gluetun-Variante.
