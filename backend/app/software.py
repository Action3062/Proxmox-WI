"""Software catalog and installation script generation.

Security note: the UI only ever sends *IDs* from this fixed catalog. We map those
IDs to predefined apt packages or vetted install scripts. No user supplied string
is ever interpolated into a shell command, which eliminates command injection from
the package selection feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .models import SoftwarePackage

# Prelude prepended to every apt operation run inside a guest. On VM first boot
# cloud-init and the apt-daily timers may still hold the dpkg lock, so we wait for
# them to finish and let apt wait for the lock instead of failing immediately.
# On LXC (no cloud-init, no running apt) the waits return instantly.
APT_PRELUDE = r"""#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# Stop needrestart from opening an interactive prompt that would hang apt.
export NEEDRESTART_MODE=a
export NEEDRESTART_SUSPEND=1
APT_OPTS="-o DPkg::Lock::Timeout=600"
# On first boot Debian/Ubuntu run apt-daily + unattended-upgrades, which install
# all pending updates and hold the dpkg lock for many minutes. Stop them, give a
# short grace period, then forcefully terminate anything still holding the lock,
# clear stale lock files and repair the package DB. This keeps provisioning fast.
systemctl stop apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
systemctl stop apt-daily.service apt-daily-upgrade.service unattended-upgrades.service >/dev/null 2>&1 || true
for _ in $(seq 1 20); do
  pgrep -x apt >/dev/null 2>&1 || pgrep -x apt-get >/dev/null 2>&1 \
    || pgrep -x dpkg >/dev/null 2>&1 || pgrep -x unattended-upgr >/dev/null 2>&1 || break
  sleep 3
done
# Use exact process-name match (-x), never -f: -f would match our own shell
# (whose arguments contain "apt") and kill the provisioning script itself.
for p in unattended-upgr apt-get apt dpkg; do pkill -9 -x "$p" >/dev/null 2>&1 || true; done
rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock \
      /var/lib/apt/lists/lock /var/cache/apt/archives/lock >/dev/null 2>&1 || true
dpkg --configure -a >/dev/null 2>&1 || true
"""

# Locale / keyboard / timezone presets selectable in the UI.
_LOCALES = {
    "de": {"locale": "de_DE.UTF-8", "keymap": "de", "tz": "Europe/Berlin"},
    "en": {"locale": "en_US.UTF-8", "keymap": "us", "tz": "Etc/UTC"},
}


def _locale_commands(language: str) -> List[str]:
    """Shell commands to set locale, console keymap and timezone in the guest."""
    cfg = _LOCALES.get(language, _LOCALES["de"])
    loc, keymap, tz = cfg["locale"], cfg["keymap"], cfg["tz"]
    return [
        f"echo '==> Konfiguriere Sprache/Tastatur ({language})'",
        f"sed -i 's/^# *{loc} UTF-8/{loc} UTF-8/' /etc/locale.gen 2>/dev/null || true",
        f"grep -q '^{loc}' /etc/locale.gen 2>/dev/null || echo '{loc} UTF-8' >> /etc/locale.gen",
        f"locale-gen {loc} || true",
        f"update-locale LANG={loc} || true",
        f"echo 'KEYMAP={keymap}' > /etc/vconsole.conf",
        f'printf \'XKBLAYOUT="{keymap}"\\nXKBMODEL="pc105"\\n\' > /etc/default/keyboard',
        f"command -v localectl >/dev/null 2>&1 && localectl set-keymap {keymap} 2>/dev/null || true",
        f"command -v timedatectl >/dev/null 2>&1 && timedatectl set-timezone {tz} 2>/dev/null || true",
        f"ln -sf /usr/share/zoneinfo/{tz} /etc/localtime 2>/dev/null || true",
        # Apply the console keymap to the running system (otherwise only on reboot).
        "systemctl restart systemd-vconsole-setup 2>/dev/null || setupcon 2>/dev/null || true",
    ]


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    label: str
    category: str
    description: str = ""
    default: bool = False
    apt: List[str] = field(default_factory=list)  # plain apt packages
    script: str = ""  # extra shell script (runs after apt packages)
    depends: List[str] = field(default_factory=list)  # other catalog IDs


# --- Base packages (preselected, installed via apt) -------------------------
_BASE: List[CatalogEntry] = [
    CatalogEntry("curl", "curl", "base", "HTTP-Client", True, apt=["curl"]),
    CatalogEntry("wget", "wget", "base", "Datei-Downloader", True, apt=["wget"]),
    CatalogEntry("git", "git", "base", "Versionsverwaltung", True, apt=["git"]),
    CatalogEntry("nano", "nano", "base", "Texteditor", True, apt=["nano"]),
    CatalogEntry("htop", "htop", "base", "Prozess-Monitor", True, apt=["htop"]),
    CatalogEntry(
        "openssh-server", "OpenSSH Server", "base", "SSH-Zugang", True,
        apt=["openssh-server"],
    ),
    CatalogEntry("sudo", "sudo", "base", "Rechteverwaltung", True, apt=["sudo"]),
    CatalogEntry(
        "ca-certificates", "ca-certificates", "base", "Root-Zertifikate", True,
        apt=["ca-certificates"],
    ),
    CatalogEntry(
        "apt-transport-https", "apt-transport-https", "base",
        "HTTPS für APT", True, apt=["apt-transport-https"],
    ),
    CatalogEntry("gnupg", "gnupg", "base", "GPG-Schlüsselverwaltung", True, apt=["gnupg"]),
    CatalogEntry(
        "software-properties-common", "software-properties-common", "base",
        "APT-Repository-Verwaltung", True, apt=["software-properties-common"],
    ),
]

# --- Optional extras --------------------------------------------------------
_EXTRA: List[CatalogEntry] = [
    CatalogEntry(
        "docker", "Docker", "container", "Container-Laufzeitumgebung",
        script="curl -fsSL https://get.docker.com | sh",
    ),
    CatalogEntry(
        "docker-compose", "Docker Compose", "container",
        "Compose-Plugin (benötigt Docker)", depends=["docker"],
        # The official Docker install already ships the compose plugin; ensure it.
        script="apt-get install -y docker-compose-plugin || true",
    ),
    CatalogEntry("nginx", "Nginx", "web", "Webserver", apt=["nginx"]),
    CatalogEntry("apache", "Apache", "web", "Webserver", apt=["apache2"]),
    CatalogEntry("mariadb", "MariaDB", "database", "Datenbankserver", apt=["mariadb-server"]),
    CatalogEntry(
        "postgresql", "PostgreSQL", "database", "Datenbankserver",
        apt=["postgresql", "postgresql-contrib"],
    ),
    CatalogEntry(
        "nodejs", "Node.js", "runtime", "JavaScript-Laufzeit (LTS)",
        script=(
            "curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && "
            "apt-get install -y nodejs"
        ),
    ),
    CatalogEntry(
        "python3", "Python 3", "runtime", "Python-Laufzeit",
        apt=["python3", "python3-pip", "python3-venv"],
    ),
    CatalogEntry("php", "PHP", "runtime", "PHP-Laufzeit", apt=["php", "php-cli", "php-fpm"]),
    CatalogEntry("redis", "Redis", "database", "In-Memory-Datenspeicher", apt=["redis-server"]),
    CatalogEntry("ufw", "UFW Firewall", "security", "Unkomplizierte Firewall", apt=["ufw"]),
    CatalogEntry("fail2ban", "Fail2Ban", "security", "Intrusion-Prevention", apt=["fail2ban"]),
    CatalogEntry("cockpit", "Cockpit", "management", "Web-Verwaltungsoberfläche", apt=["cockpit"]),
    CatalogEntry(
        "portainer", "Portainer", "management", "Docker-Web-UI (benötigt Docker)",
        depends=["docker"],
        script=(
            "docker volume create portainer_data && "
            "docker run -d -p 9443:9443 --name portainer --restart=always "
            "-v /var/run/docker.sock:/var/run/docker.sock "
            "-v portainer_data:/data portainer/portainer-ce:latest"
        ),
    ),
]

_ALL: List[CatalogEntry] = _BASE + _EXTRA
_BY_ID: Dict[str, CatalogEntry] = {entry.id: entry for entry in _ALL}


def get_catalog() -> List[SoftwarePackage]:
    """Return the catalog as API models for the frontend."""
    return [
        SoftwarePackage(
            id=e.id,
            label=e.label,
            category=e.category,
            description=e.description,
            default=e.default,
        )
        for e in _ALL
    ]


def valid_ids() -> set:
    return set(_BY_ID.keys())


def default_ids() -> List[str]:
    return [e.id for e in _ALL if e.default]


def _resolve(selected_ids: List[str]) -> List[CatalogEntry]:
    """Resolve selected IDs to catalog entries, adding dependencies, in order."""
    chosen = set(selected_ids)
    # Pull in dependencies (e.g. portainer -> docker).
    changed = True
    while changed:
        changed = False
        for entry_id in list(chosen):
            for dep in _BY_ID[entry_id].depends:
                if dep not in chosen:
                    chosen.add(dep)
                    changed = True
    # Preserve catalog order; install dependencies before dependents.
    ordered = [e for e in _ALL if e.id in chosen]
    ordered.sort(key=lambda e: len(e.depends))  # zero-dependency entries first
    return ordered


def build_install_script(selected_ids: List[str], language: str = "de") -> str:
    """Build a single idempotent bash script that installs the selected software.

    The script is executed inside the guest (LXC via ``pct exec``, VM via the
    guest agent) and is transferred base64-encoded, so its content never touches a
    shell command line on the Proxmox host. It also configures locale/keyboard.
    """
    entries = _resolve(selected_ids)
    # "locales" for locale-gen, "kbd" so the console keymap can actually be
    # loaded (cloud images often lack it -> German keyboard would not apply).
    apt_packages: List[str] = ["locales", "kbd"]
    for entry in entries:
        apt_packages.extend(entry.apt)

    unique = sorted(set(apt_packages))
    lines: List[str] = [
        APT_PRELUDE,
        "echo '==> Aktualisiere Paketlisten'",
        "apt-get $APT_OPTS update",
        f"echo '==> Installiere Pakete: {' '.join(unique)}'",
        "apt-get $APT_OPTS install -y " + " ".join(unique),
    ]

    # Locale/keyboard/timezone (after 'locales' is installed).
    lines.extend(_locale_commands(language))

    for entry in entries:
        if entry.script:
            lines.append(f"echo '==> Einrichtung: {entry.label}'")
            lines.append(entry.script)

    lines.append("echo '==> Software-Installation abgeschlossen'")
    return "\n".join(lines) + "\n"
