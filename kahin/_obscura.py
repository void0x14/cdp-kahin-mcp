"""_obscura.py — Obscura binary discovery and distro-aware installation.

Obscura (https://github.com/h4ckf0r0day/obscura) is the Rust headless browser
that powers the shadow engine. It ships as prebuilt tarballs/zips on GitHub
Releases (lean and -stealth variants). There are no deb/rpm/snap/flatpak/
appimage/npm/cargo/brew builds; the only third-party packaging is the AUR
package `obscura-browser` (frequently out-of-date, builds from source).

Install cascade (first hit wins):

  1. OBSCURA_PATH env var or an existing `obscura` binary on PATH
  2. managed dir (~/.local/share/kahin/obscura, %LOCALAPPDATA%\\kahin\\obscura)
  3. Linux: native package manager (apt / dnf / zypper / pacman)
  4. Arch-like distros: AUR helper (paru/yay) — skipped when the package is
     flagged out-of-date on AUR, because the GitHub prebuilt is newer and
     needs no toolchain
  5. Linux: flatpak (appstream id), AppImage in ~/Applications
  6. GitHub Releases prebuilt download (official channel, always available)

Total failure raises a RuntimeError with an actionable message; it never
silently falls back to another browser.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

REPO = "h4ckf0r0day/obscura"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_URL = f"https://github.com/{REPO}/releases/download"
LATEST_REDIRECT = f"https://github.com/{REPO}/releases/latest/download"
AUR_RPC = "https://aur.archlinux.org/rpc/v5/info?arg[]=obscura-browser"
AUR_PACKAGE = "obscura-browser"
BINARIES = {"obscura", "obscura-worker", "obscura.exe", "obscura-worker.exe"}
DEFAULT_INSTALL_TIMEOUT = 600.0

SYSTEM = platform.system().lower()


def managed_dir() -> Path:
    """Per-user directory where kahin keeps its own Obscura installs."""
    override = os.environ.get("KAHIN_OBSCURA_DIR")
    if override:
        return Path(override).expanduser()
    if SYSTEM == "windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(base) / "kahin" / "obscura"
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "kahin" / "obscura"


def _binary_name() -> str:
    return "obscura.exe" if SYSTEM == "windows" else "obscura"


def _find_in_dir(dir_path: Path) -> str | None:
    """Return the newest usable binary inside a managed version dir."""
    if not dir_path.is_dir():
        return None
    candidate: Path | None = None
    candidate_mtime = 0.0
    for version_dir in dir_path.iterdir():
        if not version_dir.is_dir():
            continue
        bin_path = version_dir / _binary_name()
        if bin_path.is_file() and os.access(bin_path, os.X_OK if SYSTEM != "windows" else os.R_OK):
            mtime = bin_path.stat().st_mtime
            if mtime > candidate_mtime:
                candidate, candidate_mtime = bin_path, mtime
    return str(candidate) if candidate else None


def find_obscura() -> str | None:
    """Locate an existing Obscura binary without installing anything."""
    env_path = os.environ.get("OBSCURA_PATH")
    if env_path:
        p = Path(env_path).expanduser()
        if p.is_file():
            return str(p)
    found = shutil.which("obscura") or shutil.which("obscura.exe")
    if found:
        return found
    return _find_in_dir(managed_dir())


def ensure_obscura(stealth: bool = False, timeout: float = DEFAULT_INSTALL_TIMEOUT) -> str:
    """Return a working Obscura binary, installing it if necessary."""
    existing = find_obscura()
    if existing:
        return existing
    return install_obscura(stealth=stealth, timeout=timeout)


# --------------------------------------------------------------------------- #
# Platform detection
# --------------------------------------------------------------------------- #

def _detect_distro() -> str:
    """Return distro id; '' on Windows/macOS or when /etc/os-release is absent."""
    os_release = Path("/etc/os-release")
    if not os_release.is_file():
        return ""
    fields: dict[str, str] = {}
    for line in os_release.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip().strip('"')
    return fields.get("ID", "").lower()


def _is_arch_like() -> bool:
    os_release = Path("/etc/os-release")
    if not os_release.is_file():
        return False
    text = os_release.read_text(encoding="utf-8", errors="replace").lower()
    return "id=arch" in text.replace(" ", "") or "id_like=arch" in text.replace(" ", "")


def asset_name(stealth: bool = False) -> str:
    """Map platform + architecture to the GitHub release asset name."""
    machine = platform.machine().lower()
    arch_map = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
        "x86": "x86_64",  # 32-bit systems fall back to the x86_64 build
        "i386": "x86_64",
        "i686": "x86_64",
    }
    arch = arch_map.get(machine)
    if arch is None:
        raise RuntimeError(
            f"Unsupported architecture '{machine}' for Obscura. "
            "Supported: x86_64, aarch64 (arm64)."
        )
    suffix = "-stealth" if stealth else ""
    if SYSTEM == "windows":
        if arch != "x86_64":
            raise RuntimeError(f"Obscura has no Windows build for architecture '{machine}'.")
        return f"obscura-x86_64-windows{suffix}.zip"
    if SYSTEM == "linux":
        return f"obscura-{arch}-linux{suffix}.tar.gz"
    if SYSTEM == "darwin":
        return f"obscura-{arch}-macos{suffix}.tar.gz"
    raise RuntimeError(
        f"Unsupported platform '{SYSTEM}' for Obscura. Supported: linux, darwin, windows."
    )


# --------------------------------------------------------------------------- #
# GitHub Releases (official channel)
# --------------------------------------------------------------------------- #

def _http_json(url: str, timeout: float = 15.0) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": "kahin-mcp", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("HTTP request to %s failed: %s", url, e)
        return None


def _latest_release_tag() -> str | None:
    data = _http_json(LATEST_API)
    if data is None:
        return None
    tag = data.get("tag_name")
    return str(tag) if tag else None


def _download(url: str, dest: Path, timeout: float) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "kahin-mcp"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def _install_from_archive(archive: Path, dest_dir: Path) -> Path:
    """Extract the Obscura binaries into dest_dir. Returns the main binary path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                name = Path(member).name
                if name in BINARIES:
                    with zf.open(member) as src, open(dest_dir / name, "wb") as out:
                        shutil.copyfileobj(src, out)
    else:
        with tarfile.open(archive, mode="r:*") as tf:
            for member in tf.getmembers():
                if member.isfile() and Path(member.name).name in BINARIES:
                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    with src, open(dest_dir / Path(member.name).name, "wb") as out:
                        shutil.copyfileobj(src, out)
    binary = dest_dir / _binary_name()
    if not binary.is_file():
        raise RuntimeError(f"Downloaded archive did not contain a {_binary_name()} binary.")
    if SYSTEM != "windows":
        binary.chmod(binary.stat().st_mode | 0o755)
        worker = dest_dir / "obscura-worker"
        if worker.is_file():
            worker.chmod(worker.stat().st_mode | 0o755)
    return binary


def _verify_binary(binary: Path, timeout: float = 15.0) -> str:
    """Run `obscura --version`; return its output or raise a helpful error."""
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Obscura binary at {binary} hung on --version.") from None
    except OSError as e:
        raise RuntimeError(f"Obscura binary at {binary} is not executable: {e}") from None
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        hint = ""
        if "GLIBC" in stderr:
            hint = (
                " Your system's glibc is too old (Obscura requires glibc >= 2.35). "
                "Install from source or use a newer distribution."
            )
        raise RuntimeError(
            f"Obscura binary at {binary} failed to run: {stderr or proc.stdout or 'unknown error'}.{hint}"
        )
    return (proc.stdout or proc.stderr or "").strip()


def _install_from_github(timeout: float, stealth: bool = False) -> Path:
    """Download the prebuilt asset from GitHub Releases and install it."""
    name = asset_name(stealth=stealth)
    tag = _latest_release_tag()
    if tag:
        url = f"{DOWNLOAD_URL}/{tag}/{name}"
        version_dir = managed_dir() / tag
    else:
        url = f"{LATEST_REDIRECT}/{name}"
        version_dir = managed_dir() / "latest"
    logger.info("Downloading Obscura from %s", url)
    with tempfile.TemporaryDirectory(prefix="kahin-obscura-") as tmp:
        archive = Path(tmp) / name
        try:
            _download(url, archive, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Failed to download Obscura from {url}: {e}") from None
        binary = _install_from_archive(archive, version_dir)
    _verify_binary(binary, timeout=min(timeout, 15.0))
    logger.info("Obscura %s installed at %s", tag or "latest", binary)
    return binary


# --------------------------------------------------------------------------- #
# Linux native package managers
# --------------------------------------------------------------------------- #

def _run(cmd: list[str], timeout: float, allow_fail: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("command %s failed: %s", cmd, e)
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _apt_install(timeout: float) -> Path | None:
    probe = _run(["apt-cache", "search", "obscura"], timeout=min(timeout, 30.0))
    if probe.returncode != 0 or "obscura" not in probe.stdout:
        return None
    result = _run(["sudo", "-n", "apt-get", "install", "-y", "obscura"], timeout=timeout)
    if result.returncode != 0:
        logger.warning("apt install failed (needs root?): %s", result.stderr.strip())
        return None
    return _install_via_path("obscura")


def _dnf_install(timeout: float) -> Path | None:
    probe = _run(["dnf", "list", "obscura"], timeout=min(timeout, 30.0))
    if probe.returncode != 0:
        return None
    result = _run(["sudo", "-n", "dnf", "install", "-y", "obscura"], timeout=timeout)
    if result.returncode != 0:
        logger.warning("dnf install failed (needs root?): %s", result.stderr.strip())
        return None
    return _install_via_path("obscura")


def _zypper_install(timeout: float) -> Path | None:
    probe = _run(["zypper", "se", "-x", "obscura"], timeout=min(timeout, 30.0))
    if probe.returncode != 0:
        return None
    result = _run(["sudo", "-n", "zypper", "--non-interactive", "install", "obscura"], timeout=timeout)
    if result.returncode != 0:
        logger.warning("zypper install failed (needs root?): %s", result.stderr.strip())
        return None
    return _install_via_path("obscura")


def _pacman_install(timeout: float) -> Path | None:
    probe = _run(["pacman", "-Ss", "obscura"], timeout=min(timeout, 30.0))
    if probe.returncode != 0 or not any(
        line.startswith(("extra/obscura", "community/obscura", "core/obscura"))
        for line in probe.stdout.splitlines()
    ):
        return None
    result = _run(["sudo", "-n", "pacman", "-S", "--noconfirm", "obscura"], timeout=timeout)
    if result.returncode != 0:
        logger.warning("pacman install failed (needs root?): %s", result.stderr.strip())
        return None
    return _install_via_path("obscura")


def _install_via_path(name: str) -> Path | None:
    found = shutil.which(name)
    return Path(found) if found else None


def _native_pm_install(timeout: float) -> Path | None:
    """Try the distro's native package manager. Returns binary path or None."""
    distro = _detect_distro()
    logger.info("Detected distro: %r", distro)
    if shutil.which("apt-cache") and (p := _apt_install(timeout)):
        return p
    if shutil.which("dnf") and (p := _dnf_install(timeout)):
        return p
    if shutil.which("zypper") and (p := _zypper_install(timeout)):
        return p
    if shutil.which("pacman") and (p := _pacman_install(timeout)):
        return p
    return None


# --------------------------------------------------------------------------- #
# AUR (Arch)
# --------------------------------------------------------------------------- #

def _aur_package_current() -> bool:
    """True when the AUR package exists and is not flagged out-of-date."""
    data = _http_json(AUR_RPC, timeout=10.0)
    if not data or data.get("resultcount", 0) == 0:
        return False
    info = data.get("results", [{}])[0]
    if info.get("OutOfDate"):
        logger.info("AUR package %s is out-of-date (AUR %s); skipping", AUR_PACKAGE, info.get("Version"))
        return False
    return True


def _aur_install(timeout: float) -> Path | None:
    if not _is_arch_like():
        return None
    if not _aur_package_current():
        return None
    helper = shutil.which("paru") or shutil.which("yay")
    if not helper:
        logger.info("AUR package exists but no AUR helper (paru/yay) on PATH; skipping")
        return None
    logger.info("Installing %s via %s", AUR_PACKAGE, helper)
    result = _run([helper, "-S", "--noconfirm", AUR_PACKAGE], timeout=timeout)
    if result.returncode != 0:
        logger.warning("AUR install failed: %s", result.stderr.strip())
        return None
    return _install_via_path("obscura")


# --------------------------------------------------------------------------- #
# Flatpak / AppImage
# --------------------------------------------------------------------------- #

def _flatpak_install(timeout: float) -> Path | None:
    if SYSTEM != "linux" or not shutil.which("flatpak"):
        return None
    probe = _run(["flatpak", "search", "obscura"], timeout=min(timeout, 30.0))
    if probe.returncode != 0 or not any("obscura" in line for line in probe.stdout.splitlines()):
        return None
    result = _run(["flatpak", "install", "--noninteractive", "flathub", "io.github.h4ckf0r0day.obscura"], timeout=timeout)
    if result.returncode != 0:
        logger.warning("flatpak install failed: %s", result.stderr.strip())
        return None
    return _install_via_path("obscura")


def _appimage_install() -> Path | None:
    if SYSTEM != "linux":
        return None
    for base in (Path.home() / "Applications", Path.home() / "AppImages"):
        if not base.is_dir():
            continue
        for candidate in base.iterdir():
            if candidate.is_file() and candidate.name.lower().startswith("obscura") and candidate.name.lower().endswith(".appimage"):
                candidate.chmod(candidate.stat().st_mode | 0o755)
                return candidate
    return None


# --------------------------------------------------------------------------- #
# Public install entry point
# --------------------------------------------------------------------------- #

def install_obscura(stealth: bool = False, timeout: float = DEFAULT_INSTALL_TIMEOUT) -> str:
    """Run the install cascade; raise a clear RuntimeError on total failure."""
    steps: list[tuple[str, Callable[..., Path | None]]]
    if SYSTEM == "windows":
        steps = [("GitHub Releases", _install_from_github)]
    else:
        steps = [
            ("native package manager", _native_pm_install),
            ("AUR", _aur_install),
            ("flatpak", _flatpak_install),
            ("AppImage", _appimage_install),
            ("GitHub Releases", _install_from_github),
        ]

    for name, step in steps:
        logger.info("Obscura install step: %s", name)
        try:
            result = step(timeout) if name != "AppImage" else step()
        except Exception as e:  # noqa: BLE001
            logger.warning("install step %s failed: %s", name, e)
            continue
        if result:
            binary = str(result)
            logger.info("Obscura installed via %s at %s", name, binary)
            return binary

    raise RuntimeError(
        "Could not find or install the Obscura browser binary. Tried: "
        + ", ".join(n for n, _ in steps)
        + ". Install it manually (https://github.com/h4ckf0r0day/obscura) or set OBSCURA_PATH."
    )
