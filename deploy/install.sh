#!/usr/bin/env bash
#
# nightstand installer for pi-bedroom. Run as root by the snaphouse bootstrap on
# first install, and safe to re-run: every step is idempotent. After this the
# nightstand repo owns everything display-specific on the Pi (SPI/I2C, the inky
# driver, the service, the self-update timer, and art sync).
#
# Python is managed with uv only: no pip, no apt python3-* libraries. uv is a
# single static binary pinned here with its sha256, verified against the pin
# rather than a digest fetched from the same origin as the download.
#
# No em dashes, no identifying data: nothing here carries a name, address or home
# location, and the only coordinates the app ever uses live in the untracked
# /etc/nightstand/local.toml that this script seeds from the example.

set -euo pipefail

REPO_URL="https://github.com/doyled-it/nightstand.git"
REPO_DIR="/opt/nightstand"
CONFIG_DIR="/etc/nightstand"
STATE_ROOT="/var/lib/nightstand"
BOOT_CONFIG="/boot/firmware/config.txt"
SYSTEMD_DIR="/etc/systemd/system"

# uv release pinned to match snaphouse (vars/all.yml). Bump the version and both
# checksums together from the release's uv-<arch>-unknown-linux-gnu.tar.gz.sha256.
UV_VERSION="0.13.7"
UV_SHA256_X86_64="c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83"
UV_SHA256_AARCH64="71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc"

reboot_required=0

log() {
  printf '==> %s\n' "$*"
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh must run as root" >&2
    exit 1
  fi
}

enable_interfaces() {
  log "Enabling SPI and I2C"
  raspi-config nonint do_spi 0
  raspi-config nonint do_i2c 0

  # Pimoroni's Inky Impression needs a second SPI chip-select on the Pi 5. Add
  # the overlay once; a change here needs a reboot to take effect.
  if [ -f "$BOOT_CONFIG" ]; then
    if ! grep -q '^dtoverlay=spi0-0cs' "$BOOT_CONFIG"; then
      log "Adding dtoverlay=spi0-0cs to $BOOT_CONFIG"
      printf '\n# Added by nightstand install.sh for the Inky Impression.\ndtoverlay=spi0-0cs\n' \
        >>"$BOOT_CONFIG"
      reboot_required=1
    fi
  else
    log "WARNING: $BOOT_CONFIG not found; skipping dtoverlay"
  fi
}

create_user() {
  if ! id -u nightstand >/dev/null 2>&1; then
    log "Creating the nightstand system user"
    useradd --system --home-dir "$STATE_ROOT" --shell /usr/sbin/nologin nightstand
  fi
  for grp in spi i2c gpio; do
    if getent group "$grp" >/dev/null 2>&1; then
      usermod -aG "$grp" nightstand
    fi
  done
}

install_uv() {
  local arch sha target
  arch="$(uname -m)"
  case "$arch" in
    x86_64) sha="$UV_SHA256_X86_64"; target="x86_64" ;;
    aarch64) sha="$UV_SHA256_AARCH64"; target="aarch64" ;;
    *) echo "unsupported architecture: $arch" >&2; exit 1 ;;
  esac

  if command -v uv >/dev/null 2>&1 && [ "$(uv --version | awk '{print $2}')" = "$UV_VERSION" ]; then
    log "uv $UV_VERSION already installed"
    return
  fi

  log "Installing uv $UV_VERSION ($target)"
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  local url="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${target}-unknown-linux-gnu.tar.gz"
  curl -fsSL "$url" -o "$tmp/uv.tar.gz"
  echo "$sha  $tmp/uv.tar.gz" | sha256sum -c -
  tar -xzf "$tmp/uv.tar.gz" -C /usr/local/bin --strip-components=1
}

sync_repo() {
  if [ -d "$REPO_DIR/.git" ]; then
    log "Updating $REPO_DIR"
    git -C "$REPO_DIR" fetch --quiet origin main
    git -C "$REPO_DIR" checkout --quiet origin/main
  else
    log "Cloning nightstand into $REPO_DIR"
    git clone --quiet "$REPO_URL" "$REPO_DIR"
  fi
  log "Syncing the Python environment"
  ( cd "$REPO_DIR" && uv sync --extra pi --frozen )
}

seed_config() {
  mkdir -p "$CONFIG_DIR"
  if [ ! -f "$CONFIG_DIR/local.toml" ]; then
    log "Seeding $CONFIG_DIR/local.toml from the example"
    cp "$REPO_DIR/config.example.toml" "$CONFIG_DIR/local.toml"
  fi
}

make_dirs() {
  log "Creating the working directories under $STATE_ROOT"
  for sub in art cache state public; do
    mkdir -p "$STATE_ROOT/$sub"
  done
  chown -R nightstand:nightstand "$STATE_ROOT"
}

install_units() {
  log "Installing the systemd units"
  cp "$REPO_DIR"/deploy/systemd/*.service "$SYSTEMD_DIR/"
  cp "$REPO_DIR"/deploy/systemd/*.timer "$SYSTEMD_DIR/"
  systemctl daemon-reload
  systemctl enable --now nightstand.service
  systemctl enable --now nightstand-update.timer
  systemctl enable --now nightstand-art-sync.timer
}

main() {
  require_root
  enable_interfaces
  create_user
  install_uv
  sync_repo
  seed_config
  make_dirs
  install_units
  log "nightstand install complete"
  if [ "$reboot_required" -eq 1 ]; then
    log "A reboot is required for the SPI overlay to take effect."
  fi
}

main "$@"
