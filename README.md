# Proxmox Web Interface

Ein lokales Webinterface zur Verwaltung und automatisierten Bereitstellung von
**LXC-Containern** (und perspektivisch VMs) auf einem Proxmox-VE-Server im
Heimnetzwerk.

Über eine einfache Oberfläche lassen sich Debian-/Ubuntu-LXC-Container
konfigurieren, per Proxmox-API automatisch ausrollen, mit ausgewählter Software
bestücken und auf verfügbare Updates prüfen.

---

## Inhaltsverzeichnis

1. [Projektziel](#projektziel)
2. [Architektur](#architektur)
3. [Funktionen](#funktionen)
4. [Voraussetzungen auf dem Proxmox-Server](#voraussetzungen-auf-dem-proxmox-server)
5. [Proxmox API-Token erstellen](#proxmox-api-token-erstellen)
6. [Konfiguration der `.env`-Datei](#konfiguration-der-env-datei)
7. [Anwendung lokal starten](#anwendung-lokal-starten)
8. [Deployment per Docker Compose](#deployment-per-docker-compose)
9. [Server-Vorbereitung](#server-vorbereitung)
10. [Deployment per GitHub Actions](#deployment-per-github-actions)
11. [Benötigte GitHub Secrets](#benötigte-github-secrets)
12. [Repository initialisieren und zu GitHub pushen](#repository-initialisieren-und-zu-github-pushen)
13. [Systemd-Alternative](#systemd-alternative)
14. [Sicherheitshinweise](#sicherheitshinweise)
15. [Fehlerbehebung](#fehlerbehebung)
16. [Erweiterbarkeit](#erweiterbarkeit)
17. [Projektstruktur](#projektstruktur)

---

## Projektziel

Das Webinterface soll die wiederkehrende Aufgabe, neue LXC-Container manuell in
Proxmox anzulegen und einzurichten, vereinfachen und automatisieren:

1. Container-Typ, Betriebssystem, Version und Ressourcen über ein Formular wählen.
2. Standard- und Zusatzsoftware auswählen.
3. Auf „Erstellen“ klicken – der Rest läuft automatisch über die Proxmox-API.
4. Nach der Erstellung wird einmalig auf Updates geprüft und das Ergebnis angezeigt.

## Architektur

```
        Browser
           │  HTTP
           ▼
   ┌────────────────┐      ┌──────────────────┐
   │ nginx (Proxy)  │ ───► │ frontend (React) │  (statische SPA)
   │   Port 80      │      └──────────────────┘
   │                │      ┌──────────────────┐      Proxmox VE API (HTTPS)
   │   /api/  ──────┼────► │ backend (FastAPI)│ ───► ┌───────────────┐
   └────────────────┘      │                  │      │  Proxmox-Host  │
                           │  SSH (pct exec)  │ ───► │   (LXC/VMs)    │
                           └──────────────────┘      └───────────────┘
```

- **Backend** (`backend/`): Python **FastAPI**. Spricht die Proxmox-VE-REST-API
  (Container anlegen/starten) und nutzt **SSH + `pct exec`**, um Software im
  Container zu installieren und Updates zu prüfen.
- **Frontend** (`frontend/`): **React + Vite**, als statische Single-Page-App
  von nginx ausgeliefert.
- **Reverse Proxy** (`nginx/`): nginx als einziger öffentlicher Einstiegspunkt,
  leitet `/api/*` an das Backend und alles andere an das Frontend.
- **Deployment**: **Docker Compose**; automatisiertes Rollout über **GitHub
  Actions per SSH** auf den Zielserver.

> **Warum SSH?** Die Proxmox-REST-API kann LXC-Container anlegen und starten,
> bietet aber keine generische „führe Befehl im Container aus“-Funktion. Für die
> Software-Installation und Update-Prüfung verbindet sich das Backend daher per
> SSH mit dem Proxmox-Host und nutzt `pct exec`. Skripte werden base64-kodiert
> übertragen, sodass keine Benutzereingaben ungeprüft in eine Shell gelangen.

## Funktionen

- 🔐 Login-Schutz (JWT) für das Webinterface
- 🧰 Auswahl LXC-Container **oder VM** (cloud-init) · Debian/Ubuntu · Version/Template
- ⚙️ Konfiguration: Hostname, CPU, RAM, Speicher, Bridge, DHCP/statische IP,
  Benutzername, Passwort/SSH-Key, Autostart, Beschreibung
- 📦 Software-Auswahl mit vorausgewählten Standardpaketen + Extras (Docker,
  Nginx, MariaDB, PostgreSQL, Node.js, Portainer, …)
- 🚀 Automatisches Anlegen, Starten und Einrichten über Proxmox
- 🔄 Updates bei der Erstellung installieren (optional, Standard an) +
  „Updates installieren“-Button; **automatische Sicherheitsupdates**
  (unattended-upgrades) optional aktivierbar
- 📊 Live-Statusanzeige (Erstellung → Start → Installation → Update-Prüfung →
  Fertig/Fehler)
- 📝 Serverseitiges Logging mit automatischer Schwärzung sensibler Daten,
  einsehbar im Webinterface

## Voraussetzungen auf dem Proxmox-Server

- Proxmox VE 7 oder 8
- Mindestens ein LXC-Template (Debian/Ubuntu). Verfügbare Templates anzeigen und
  herunterladen:

  ```bash
  pveam update
  pveam available --section system        # Liste verfügbarer Templates
  pveam download local debian-12-standard_12.7-1_amd64.tar.zst
  ```

- Ein Storage für die Container-Root-Disk (z. B. `local-lvm`) und ein Storage
  für Templates (z. B. `local`).
- **SSH-Zugang** zum Proxmox-Host für das Backend (für `pct exec`). Empfohlen:
  eigener Benutzer/Key; im einfachsten Fall `root` mit Key-Authentifizierung.
- Eine Netzwerk-Bridge (z. B. `vmbr0`) mit Internetzugang für die Container
  (damit `apt`/Docker-Installation funktioniert).

### Für VMs: Cloud-Init-Template vorbereiten

VMs werden aus einem **cloud-init-fähigen Template geklont**. Software/Updates
laufen über den **QEMU-Guest-Agent**, daher muss `qemu-guest-agent` im Image
enthalten sein. Einmalige Vorbereitung auf dem Proxmox-Host (Beispiel Debian 12,
Storage `local-zfs`, Template-VMID `9000`):

```bash
# 1. Cloud-Image herunterladen
wget https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2

# 2. qemu-guest-agent ins Image einbauen (wichtig für Software/Updates!)
apt-get install -y libguestfs-tools
virt-customize -a debian-12-genericcloud-amd64.qcow2 \
  --install qemu-guest-agent \
  --run-command 'truncate -s 0 /etc/machine-id'   # eindeutige IP je Klon (DHCP)

# 3. VM anlegen, Disk importieren, cloud-init + Agent aktivieren
qm create 9000 --name debian-12-cloud --memory 1024 --cores 2 --net0 virtio,bridge=vmbr0
qm importdisk 9000 debian-12-genericcloud-amd64.qcow2 local-zfs
qm set 9000 --scsihw virtio-scsi-pci --scsi0 local-zfs:vm-9000-disk-0
qm set 9000 --ide2 local-zfs:cloudinit
qm set 9000 --boot order=scsi0
qm set 9000 --serial0 socket --vga std   # std = nutzbare Web-Konsole (nicht serial0)
qm set 9000 --agent enabled=1

# 4. In ein Template umwandeln
qm template 9000
```

Für Ubuntu analog mit `https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img`.
Das Webinterface zeigt anschließend unter „Virtuelle Maschine“ alle Templates
(VMs mit `template=1`) zur Auswahl an, klont das gewählte, setzt Benutzer/SSH/IP
per cloud-init und installiert Software über den Guest-Agent.

## Proxmox API-Token erstellen

Per Weboberfläche: **Datacenter → Permissions → API Tokens → Add**.

1. *User* wählen (z. B. `root@pam`) und eine *Token ID* vergeben (z. B. `webui`).
2. Für den Anfang **„Privilege Separation“ deaktivieren**, damit der Token die
   Rechte des Benutzers erbt (oder gezielt Rechte vergeben, siehe unten).
3. Das angezeigte **Secret** sofort kopieren – es wird nur einmal angezeigt.

Alternativ per CLI auf dem Proxmox-Host:

```bash
pveum user token add root@pam webui --privsep 0
```

Die resultierende **Token-ID** lautet `root@pam!webui`, dazu gehört das
**Secret** (UUID). Beide kommen in die `.env` (`PROXMOX_TOKEN_ID`,
`PROXMOX_TOKEN_SECRET`).

> **Least Privilege (empfohlen):** Statt voller Root-Rechte kann dem Token-User
> eine Rolle mit `VM.Allocate`, `VM.Config.*`, `VM.PowerMgmt`, `Datastore.*`,
> `Sys.Audit` auf den relevanten Pfaden zugewiesen werden.

## Konfiguration der `.env`-Datei

Die Beispieldatei kopieren und ausfüllen:

```bash
cp .env.example .env
```

Wichtige Werte (vollständige Liste in [`.env.example`](.env.example)):

| Variable | Beschreibung |
| --- | --- |
| `SECRET_KEY` | Signaturschlüssel für JWT (`openssl rand -hex 32`) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Login für das Webinterface |
| `PROXMOX_HOST` | z. B. `https://192.168.1.10:8006` |
| `PROXMOX_NODE` | Node-Name (z. B. `pve`) |
| `PROXMOX_TOKEN_ID` / `PROXMOX_TOKEN_SECRET` | API-Token |
| `PROXMOX_VERIFY_SSL` | `false` bei selbstsigniertem Zertifikat |
| `PROXMOX_SSH_USER` + Key/Passwort | SSH-Zugang für `pct exec` |

> Niemals die `.env` committen – sie steht in `.gitignore`.

## Anwendung lokal starten

### Variante A – Docker Compose (empfohlen)

```bash
cp .env.example .env   # anpassen
docker compose up -d --build
# Webinterface: http://localhost  (bzw. http://localhost:<HTTP_PORT>)
```

### Variante B – Entwicklungsmodus (ohne Docker)

Backend:

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' ../.env | xargs)   # .env laden
uvicorn app.main:app --reload --port 8000
```

Frontend (zweites Terminal):

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173 (proxyt /api -> :8000)
```

## Deployment per Docker Compose

Auf dem Server (siehe [Server-Vorbereitung](#server-vorbereitung)):

```bash
cd /opt/proxmox-webinterface
git pull origin main
test -f .env                # sicherstellen, dass die Konfiguration existiert
docker compose build
docker compose up -d
docker image prune -f
```

Eigenschaften:

- `restart: unless-stopped` → Anwendung startet nach einem Server-Neustart
  automatisch wieder.
- Persistente **Volumes** für Backend-Logs und nginx-Logs.
- Die `.env` liegt außerhalb der Images und wird beim Deployment **nicht**
  überschrieben.

## Server-Vorbereitung

```bash
# 1. Docker + Compose-Plugin installieren (Debian/Ubuntu)
curl -fsSL https://get.docker.com | sh

# 2. Projektverzeichnis anlegen
sudo mkdir -p /opt/proxmox-webinterface
sudo chown "$USER":"$USER" /opt/proxmox-webinterface

# 3. Repository initial klonen
git clone https://github.com/<USER>/<REPO>.git /opt/proxmox-webinterface
cd /opt/proxmox-webinterface

# 4. .env erstellen und befüllen (siehe oben)
cp .env.example .env && nano .env

# 5. Rechte einschränken (enthält Secrets)
chmod 600 .env

# 6. Erststart
docker compose up -d --build
```

**SSH-Key für GitHub Actions einrichten** (Deployment-User):

```bash
# Schlüsselpaar erzeugen (auf einem sicheren Rechner)
ssh-keygen -t ed25519 -f deploy_key -C "github-actions-deploy"

# Öffentlichen Schlüssel auf dem Server hinterlegen
ssh-copy-id -i deploy_key.pub deploy@SERVER     # oder manuell in authorized_keys

# Privaten Schlüssel (deploy_key) als GitHub Secret SERVER_SSH_KEY speichern
```

Empfehlung: einen dedizierten `deploy`-User verwenden und dessen SSH-Zugang in
`~/.ssh/authorized_keys` mit Optionen einschränken (z. B. `from="..."`).

## Deployment per GitHub Actions

Die Datei [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) wird bei
jedem Push auf `main` ausgeführt und:

1. checkt den Code aus,
2. verbindet sich per SSH mit dem Zielserver,
3. wechselt in `DEPLOY_PATH`,
4. aktualisiert den Code (`git pull origin main`),
5. prüft, ob eine `.env` vorhanden ist (bricht sonst ab),
6. baut die Images (`docker compose build`),
7. startet die Container neu (`docker compose up -d`),
8. bereinigt ungenutzte Images (`docker image prune -f`).

Der Workflow läuft ausschließlich für den `main`-Branch (`if: github.ref == ...`).

## Benötigte GitHub Secrets

Unter **Repository → Settings → Secrets and variables → Actions** anlegen:

| Secret | Beschreibung |
| --- | --- |
| `SERVER_HOST` | IP/Hostname des Zielservers |
| `SERVER_USER` | SSH-Benutzer (z. B. `deploy`) |
| `SERVER_SSH_KEY` | Privater SSH-Schlüssel (gesamter Inhalt) |
| `SERVER_PORT` | SSH-Port (z. B. `22`) |
| `DEPLOY_PATH` | Projektpfad auf dem Server (z. B. `/opt/proxmox-webinterface`) |

Secrets stehen nie im Code und werden von GitHub in den Logs maskiert.

## Repository initialisieren und zu GitHub pushen

```bash
# Falls noch kein Repo vorhanden:
git init
git add .
git commit -m "Initiale Version: Proxmox Web Interface"

# Remote anlegen (auf GitHub vorher ein leeres Repository erstellen)
git remote add origin https://github.com/<USER>/<REPO>.git
git branch -M main
git push -u origin main
```

Sensible Dateien (`.env`, Schlüssel) werden durch [`.gitignore`](.gitignore)
ausgeschlossen. Vor dem ersten Push prüfen:

```bash
git status        # es darf KEINE .env oder *.key auftauchen
```

## Systemd-Alternative

Die bevorzugte Variante ist Docker Compose. Ohne Docker kann die Anwendung über
`systemd` betrieben werden. Beispiel für das Backend:

```ini
# /etc/systemd/system/proxmox-wi-backend.service
[Unit]
Description=Proxmox Web Interface Backend
After=network.target

[Service]
User=proxmoxwi
WorkingDirectory=/opt/proxmox-webinterface/backend
EnvironmentFile=/opt/proxmox-webinterface/.env
ExecStart=/opt/proxmox-webinterface/backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
# Frontend einmalig bauen und z. B. mit nginx ausliefern:
cd frontend && npm install && npm run build   # Ergebnis in frontend/dist
sudo systemctl enable --now proxmox-wi-backend
```

Das gebaute Frontend (`frontend/dist`) wird von einem nginx ausgeliefert, der
`/api` an `127.0.0.1:8000` weiterreicht (siehe `nginx/default.conf` als Vorlage).

## Sicherheitshinweise

- **Keine hartcodierten Zugangsdaten** – alles über `.env`/Umgebungsvariablen.
- **JWT** mit konfigurierbarer Laufzeit; Secret aus `SECRET_KEY`.
- **Passwörter** werden mit **bcrypt** gehasht; Klartext nie gespeichert.
- **API-Token** und SSH-Schlüssel verlassen niemals das Backend und werden nicht
  ans Frontend ausgegeben.
- **Logging-Schwärzung**: Passwörter, Tokens und SSH-Keys werden vor dem
  Schreiben automatisch durch `***REDACTED***` ersetzt.
- **Eingabevalidierung** für Hostname, IP (CIDR), CPU/RAM/Speicher und
  Paketnamen (nur IDs aus festem Katalog → keine Shell-Injection).
- **Kein ungeprüftes Zusammensetzen von Shell-Befehlen**: Skripte werden
  base64-kodiert per `pct exec ... bash -s` ausgeführt.
- **CORS** ist auf die konfigurierten Origins beschränkt.
- Reverse Proxy als einziger Einstiegspunkt; Backend/Frontend sind nicht direkt
  exponiert.
- Empfehlung für Produktion: TLS am Reverse Proxy terminieren (z. B. Let's
  Encrypt) und den SSH-Deploy-User auf das Projektverzeichnis einschränken.

## Fehlerbehebung

| Symptom | Mögliche Ursache / Lösung |
| --- | --- |
| Login schlägt fehl | `ADMIN_PASSWORD`/`ADMIN_PASSWORD_HASH` gesetzt? Backend-Logs prüfen. |
| „Proxmox-Server nicht erreichbar“ | `PROXMOX_HOST` korrekt? Netzwerk/Firewall? |
| „Authentifizierung bei Proxmox fehlgeschlagen“ | `PROXMOX_TOKEN_ID`/`SECRET` prüfen, Token-Rechte. |
| TLS-Fehler zur Proxmox-API | `PROXMOX_VERIFY_SSL=false` bei selbstsigniertem Zertifikat. |
| Keine Templates in der Auswahl | Templates fehlen → `pveam download ...`; Template-Storage prüfen. |
| Software-Installation/Update schlägt fehl | SSH-Zugang (`PROXMOX_SSH_*`) prüfen; Container braucht Internetzugang. |
| „Container reagiert nicht (pct exec)“ | Container gestartet? SSH-User darf `pct` ausführen (root/sudo)? |
| Deployment bricht mit „.env fehlt“ ab | `.env` im `DEPLOY_PATH` auf dem Server anlegen. |

Logs ansehen:

```bash
docker compose logs -f backend      # Backend-Logs
docker compose logs -f nginx        # Reverse-Proxy-Logs
# oder im Webinterface unter „Server-Logs“
```

## Erweiterbarkeit

Der Code ist so strukturiert, dass folgende Punkte ergänzt werden können:

- **VM-Erstellung** (`type="vm"` ist im Datenmodell bereits vorgesehen)
- Mehrere **Proxmox-Nodes** und **Storage-Ziele** (Node-/Storage-Auswahl bereits
  über die API verfügbar)
- **Template-/ISO-Verwaltung**
- **Benutzerverwaltung mit Rollen** (`require_role`-Dependency + User-Store als
  Grundgerüst vorhanden)
- **Geplante Updates**, **Snapshots**, **Backups**, **Netzwerkprofile**
- Persistente Job-Historie (aktuell In-Memory) z. B. via Redis/DB

## Projektstruktur

```text
.
├── backend/
│   ├── app/
│   │   ├── main.py             # FastAPI-App, CORS, Router, Exception-Handler
│   │   ├── config.py           # Einstellungen aus .env/Umgebung
│   │   ├── auth.py             # JWT, Passwort-Hashing, Auth-Dependencies
│   │   ├── proxmox_client.py   # Async-Client für die Proxmox-VE-API
│   │   ├── ssh_client.py       # SSH/pct-exec-Wrapper (Software, Updates)
│   │   ├── software.py         # Software-Katalog + Install-Skripte
│   │   ├── tasks.py            # Job-Management + Bereitstellungs-Workflow
│   │   ├── models.py           # Pydantic-Modelle + Validierung
│   │   ├── logging_config.py   # Logging + Schwärzung sensibler Daten
│   │   └── routers/            # auth, proxmox, containers, logs
│   ├── tests/                  # Unit-Tests
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── src/                    # React-Komponenten (Login, Formular, Status, …)
│   ├── package.json
│   ├── vite.config.js
│   ├── nginx.conf              # SPA-Auslieferung im Frontend-Container
│   └── Dockerfile
├── nginx/
│   └── default.conf            # Reverse Proxy (/api -> Backend, / -> Frontend)
├── .github/workflows/
│   ├── deploy.yml              # Deployment per SSH bei Push auf main
│   └── ci.yml                  # Build & Tests
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

---

## Ziel-Ablauf

1. Entwickler ändert Code lokal und pusht nach `main`.
2. GitHub Actions verbindet sich per SSH mit dem Server.
3. Der Server zieht den aktuellen Code und baut/startet die Container neu.
4. Das Webinterface ist aktualisiert verfügbar.
5. Nutzer konfiguriert im Webinterface einen Debian-/Ubuntu-LXC.
6. Proxmox erstellt und startet den Container automatisch.
7. Die ausgewählte Software wird installiert.
8. Updates werden einmal geprüft und angezeigt.
