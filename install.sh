#!/usr/bin/env bash
#
# install.sh — install or upgrade the Baton Agent Orchestrator CLI (`jev`).
#
# One-command install (macOS / Linux):
#   curl -fsSL https://raw.githubusercontent.com/Gane2506/baton/main/install.sh | bash
#
# Options, as flags or JEV_* environment variables (flags win):
#   --ref VERSION     install this version (PyPI) or git ref (GitHub)  [JEV_REF]
#   --no-dashboard    skip the web-dashboard extra                      [JEV_NO_DASHBOARD=1]
#   --sudo            system-wide install via sudo (pip only; not recommended) [JEV_SUDO=1]
#   -h, --help        show this help
#
# Examples:
#   curl -fsSL https://raw.githubusercontent.com/Gane2506/baton/main/install.sh | bash -s -- --ref v0.3.0
#   JEV_NO_DASHBOARD=1 curl -fsSL https://raw.githubusercontent.com/Gane2506/baton/main/install.sh | bash
#
# Safe to re-run: installs when missing, upgrades when present. Installs from
# PyPI when the package is published there, otherwise from GitHub. Never uses
# sudo unless --sudo is passed explicitly; prefers pipx, falls back to pip
# --user. After installing, verifies with `jev --version` and `jev doctor`.

set -euo pipefail

REPO="Gane2506/baton"
PKG="baton-agent-orchestrator"
PYPI_URL="https://pypi.org/pypi/${PKG}/json"

REF="${JEV_REF:-}"
NO_DASHBOARD="${JEV_NO_DASHBOARD:-0}"
USE_SUDO="${JEV_SUDO:-0}"

usage() {
  cat <<'EOF'
Usage: install.sh [--ref VERSION] [--no-dashboard] [--sudo] [-h|--help]

Installs or upgrades the `jev` CLI. Safe to re-run.
Environment equivalents: JEV_REF, JEV_NO_DASHBOARD=1, JEV_SUDO=1.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --ref)
      [ $# -ge 2 ] || { echo "error: --ref needs a value" >&2; exit 2; }
      REF="$2"; shift 2 ;;
    --ref=*) REF="${1#*=}"; shift ;;
    --no-dashboard) NO_DASHBOARD=1; shift ;;
    --sudo) USE_SUDO=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

log() { printf '%s\n' "$*"; }
err() { printf 'error: %s\n' "$*" >&2; }
die() { err "$*"; exit 1; }

# --- prerequisites -----------------------------------------------------------
PYBIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PYBIN="$candidate"; break; fi
done
[ -n "$PYBIN" ] || die "no python3 found on PATH. Install Python 3.11+ first: https://www.python.org/downloads/"

PYVER="$("$PYBIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in
  3.1[1-9]*|3.[2-9][0-9]*|[4-9].*) log "Python $PYVER ($PYBIN)" ;;
  *) die "Python $PYVER found, but jev needs Python 3.11+. Install a newer Python: https://www.python.org/downloads/" ;;
esac

INSTALLER=""
if command -v pipx >/dev/null 2>&1; then
  INSTALLER="pipx"
elif "$PYBIN" -m pip --version >/dev/null 2>&1; then
  INSTALLER="pip"
else
  die "neither pipx nor pip found. Install pipx (https://pipx.pypa.io/) or make sure pip is available for $PYBIN."
fi
log "Installer: $INSTALLER"

command -v curl >/dev/null 2>&1 || die "curl not found on PATH; it is needed to check PyPI."
command -v git >/dev/null 2>&1 || log "warning: git not found on PATH; jev needs it to work with repos."

# --- source: PyPI when published, else GitHub --------------------------------
EXTRA=""
[ "$NO_DASHBOARD" = "1" ] || EXTRA="[dashboard]"
if curl -fsSL --max-time 15 "$PYPI_URL" -o /dev/null 2>/dev/null; then
  SPEC="${PKG}${EXTRA}"
  [ -n "$REF" ] && SPEC="${SPEC}==${REF}"
  SOURCE="PyPI"
else
  GREF="${REF:-main}"
  SPEC="${PKG}${EXTRA} @ git+https://github.com/${REPO}@${GREF}"
  SOURCE="GitHub (${REPO}@${GREF})"
fi
log "Source: $SOURCE"

# --- install or upgrade (idempotent) -----------------------------------------
if [ "$INSTALLER" = "pipx" ]; then
  if [ "$USE_SUDO" = "1" ]; then
    log "warning: --sudo is ignored with pipx (pipx always installs into your home directory)."
  fi
  if pipx list --short 2>/dev/null | grep -Eq "^${PKG}( |$)"; then
    log "Upgrading existing pipx install..."
    pipx upgrade "$PKG"
  else
    pipx install "$SPEC"
  fi
  pipx ensurepath >/dev/null 2>&1 || true
else
  if [ "$USE_SUDO" = "1" ]; then
    command -v sudo >/dev/null 2>&1 || die "--sudo passed but sudo is not available."
    log "warning: installing system-wide with sudo (you passed --sudo)."
    sudo "$PYBIN" -m pip install --upgrade "$SPEC"
  else
    "$PYBIN" -m pip install --user --upgrade "$SPEC"
  fi
fi
hash -r 2>/dev/null || true

# --- verify ------------------------------------------------------------------
# `jev` may not be on PATH yet in this shell (pipx ensurepath only affects
# future login shells). The install still counts as a success when the binary
# is verifiable by absolute path — print the PATH line instead of failing.
JEVBIN="$(command -v jev || true)"
if [ -z "$JEVBIN" ]; then
  USERBIN="$("$PYBIN" -c 'import site, os; print(os.path.join(site.getuserbase(), "bin"))')"
  if [ -x "$USERBIN/jev" ]; then
    JEVBIN="$USERBIN/jev"
    log "Note: installed, but \`jev\` is not on PATH yet in this shell."
    log "Add this to your shell profile (~/.zshrc, ~/.bashrc) and restart your shell:"
    log "  export PATH=\"$USERBIN:\$PATH\""
  else
    err "installed, but no \`jev\` binary found at $USERBIN/jev."
    exit 1
  fi
fi
log "OK: $("$JEVBIN" --version)"

# `jev doctor` checks the environment (workers, API keys, config). On a fresh
# machine some checks fail until you configure a worker — that is expected and
# does not fail the install.
if command -v git >/dev/null 2>&1; then
  DOCTOR_DIR="$(mktemp -d)"
  trap 'rm -rf "$DOCTOR_DIR"' EXIT
  git init -q "$DOCTOR_DIR"
  if (cd "$DOCTOR_DIR" && "$JEVBIN" doctor); then
    log "OK: jev doctor passes."
  else
    log "Note: jev doctor reports setup still needed (worker CLIs, API keys)."
    log "That is expected on a fresh machine — see \`jev doctor\` output above."
  fi
else
  log "Note: skipping \`jev doctor\` (git not installed)."
fi

log "Done. Next: cd into a git repo, run \`jev init\`, then \`jev --help\`."

# --- autostart ---------------------------------------------------------------
# Offer the reboot-survival step (`jev dashboard --install-service`) when this
# directory is a git repo — with the user's yes, never silently (deploy Phase 3,
# issue #341). In a piped install (`curl | bash`) stdin is the script, so the
# prompt reads from the tty; without one, print the command to run later
# instead of hanging. A service install failure never fails the install.
if command -v git >/dev/null 2>&1 && REPO_DIR="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  if [ "$REPO_DIR" = "${HOME:-}" ]; then
    log "Next: to start the dashboard at login, run \`jev dashboard --install-service --repo <your-repo>\` after \`jev init\` (it refuses to serve your home directory)."
  elif ( : </dev/tty ) 2>/dev/null; then
    # A tty exists and can be opened (the `curl | bash` case: stdin is the
    # script, but the terminal is still there). The open is attempted in a
    # subshell whose stderr is already /dev/null: /dev/tty exists as a
    # device node even with no controlling terminal, where opening it fails,
    # and a bare `</dev/tty` test would leak that error — `[ -e /dev/tty ]`
    # would wrongly prompt there.
    printf 'Install the dashboard as a service that starts at login for %s? [y/N] ' "$REPO_DIR" >&2
    if read -r REPLY </dev/tty 2>/dev/null; then
      case "$REPLY" in
        [yY]|[yY][eE][sS])
          if "$JEVBIN" dashboard --install-service --repo "$REPO_DIR"; then
            log "OK: dashboard service installed."
          else
            log "Note: service install failed — run \`jev dashboard --install-service --repo $REPO_DIR\` by hand."
          fi
          ;;
      esac
    fi
  else
    log "Next: to start the dashboard at login, run \`jev dashboard --install-service --repo $REPO_DIR\`."
  fi
else
  log "Next: to start the dashboard at login, run \`jev dashboard --install-service --repo <your-repo>\` after \`jev init\`."
fi
