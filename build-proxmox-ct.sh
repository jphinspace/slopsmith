#!/usr/bin/env bash
# =============================================================================
# build-proxmox-ct.sh  –  Build a Proxmox LXC rootfs from WSL2 (no lxc-start)
#
# Run this from your project root (where rscli/, server.py, etc. live).
#
# Usage:
#   sudo bash build-proxmox-ct.sh [TARGETARCH] [OUTPUT_NAME]
#
# Examples:
#   sudo bash build-proxmox-ct.sh amd64 rocksmith-ct
#   sudo bash build-proxmox-ct.sh arm64 rocksmith-ct
#
# Prerequisites (install in WSL):
#   sudo apt install debootstrap systemd-container tar zstd curl unzip git
#
# On Proxmox, after transfer:
#   pct restore <VMID> rocksmith-ct.tar.zst --storage local-lvm --rootfs 8 --unprivileged 1
# =============================================================================

set -euo pipefail

TARGETARCH="${1:-amd64}"
OUTPUT_NAME="${2:-rocksmith-ct}"

# debootstrap requires a real Linux filesystem (ext4/tmpfs) – it creates
# device nodes that NTFS/FUSE mounts (/mnt/c, /mnt/d …) cannot represent.
# We build everything under /tmp (tmpfs) and copy the final tarball back.
PROJECT_DIR="$(pwd)"          # may be on /mnt/d – that's fine for source files
BUILD_BASE="/tmp/proxmox-ct-build"
ROOTFS="${BUILD_BASE}/rootfs"
mkdir -p "$BUILD_BASE"

PYTHON_VERSION="3.13"
DOTNET_CHANNEL="10.0"
VGMSTREAM_URL="https://github.com/vgmstream/vgmstream/releases/download/r2083/vgmstream-linux-cli.zip"

APP_DIR="/app"
RSCLI_DIR="/opt/rscli"
DLC_DIR="/dlc"
CONFIG_DIR="/config"
ROCKSMITH_DIR="/rocksmith"
ROCKSMITH_SRC_DIC="/mnt/z/Steam/steamapps/common/Rocksmith2014"

# Coloured logging
info() { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()   { echo -e "\033[1;32m[OK]\033[0m    $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die()  { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"

case "$TARGETARCH" in
  arm64) RID="linux-arm64" ; DEBIAN_ARCH="arm64" ;;
  amd64) RID="linux-x64"   ; DEBIAN_ARCH="amd64" ;;
  *)     RID="linux-x64"   ; DEBIAN_ARCH="amd64" ;;
esac

# Confirm required tools
for cmd in debootstrap systemd-nspawn curl unzip git tar zstd; do
  command -v "$cmd" &>/dev/null || die "'$cmd' not found. Run: sudo apt install debootstrap systemd-container curl unzip git zstd"
done

# =============================================================================
# Helper: run a command inside the rootfs via systemd-nspawn
# --pipe keeps stdin/stdout connected; --quiet suppresses nspawn chatter.
# =============================================================================
r() {
  systemd-nspawn \
    --quiet \
    --directory="$ROOTFS" \
    --bind-ro=/etc/resolv.conf:/etc/resolv.conf \
    -- bash -c "$*"
}

# =============================================================================
# 1. Bootstrap a minimal Debian Trixie rootfs
# =============================================================================
info "Bootstrapping Debian Trixie (${DEBIAN_ARCH}) rootfs at ${ROOTFS} …"
if [[ -d "$ROOTFS" ]]; then
  warn "Existing rootfs found at ${ROOTFS} – remove it to rebuild from scratch."
  read -rp "    Delete and rebuild? [y/N] " yn
  [[ "$yn" =~ ^[Yy]$ ]] && rm -rf "$ROOTFS" || die "Aborting."
fi

debootstrap \
  --arch="$DEBIAN_ARCH" \
  --include=ca-certificates,curl,gnupg \
  trixie \
  "$ROOTFS" \
  http://deb.debian.org/debian

ok "Bootstrap complete."

# Bind /proc and /sys so apt and dotnet-install work inside nspawn
# (systemd-nspawn does this automatically; just ensuring resolv.conf is live)
echo "nameserver 1.1.1.1" > "$ROOTFS/etc/resolv.conf"

# =============================================================================
# 2. System packages  (mirrors Stage 2 apt block)
# =============================================================================
#info "Configuring apt sources (bookworm + backports) …"
#cat > "${ROOTFS}/etc/apt/sources.list" <<EOF
#deb http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware
#deb http://deb.debian.org/debian bookworm-backports main contrib non-free non-free-firmware
#deb http://security.debian.org/debian-security bookworm-security main contrib non-free
#EOF

info "Installing system packages …"
r "apt-get update -qq && apt-get install -y --no-install-recommends \
    python${PYTHON_VERSION} python3-pip python3-venv \
    ffmpeg \
    fluidsynth \
    fluid-soundfont-gm \
    libsndfile1 \
    curl \
    unzip \
    megatools \
    git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*"
ok "System packages installed."

# =============================================================================
# 3. Install .NET  (needed to build AND run RsCli – no SDK in final Docker
#    stage, but in an LXC the runtime must be present)
# =============================================================================
info "Installing .NET ${DOTNET_CHANNEL} runtime + SDK …"
r "curl -sL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
    && chmod +x /tmp/dotnet-install.sh \
    && DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
       DOTNET_CLI_TELEMETRY_OPTOUT=1 \
       /tmp/dotnet-install.sh --channel ${DOTNET_CHANNEL} \
           --install-dir /usr/share/dotnet \
    && ln -sf /usr/share/dotnet/dotnet /usr/local/bin/dotnet \
    && rm /tmp/dotnet-install.sh"
ok ".NET installed."

# =============================================================================
# 4. Build RsCli inside the rootfs
# =============================================================================
info "Cloning Rocksmith2014.NET into rootfs (host-side) …"
rm -rf "${ROOTFS}/opt/rs2014"
git clone --depth 1 https://github.com/iminashi/Rocksmith2014.NET.git "${ROOTFS}/opt/rs2014"
 
info "Copying rscli sources …"
[[ -f "${PROJECT_DIR}/rscli/RsCli.fsproj" ]] || die "rscli/RsCli.fsproj not found."
[[ -f "${PROJECT_DIR}/rscli/Program.fs"   ]] || die "rscli/Program.fs not found."
mkdir -p "${ROOTFS}/opt/rs2014/tools/RsCli"
cp "${PROJECT_DIR}/rscli/RsCli.fsproj" "${ROOTFS}/opt/rs2014/tools/RsCli/"
cp "${PROJECT_DIR}/rscli/Program.fs"   "${ROOTFS}/opt/rs2014/tools/RsCli/"
 
info "Patching Directory.Build.props (host-side) …"
PROPS=$(find "${ROOTFS}/opt/rs2014" -name "Directory.Build.props" | head -1)
if [[ -z "$PROPS" ]]; then
  warn "Directory.Build.props not found – skipping NuGetAudit patch"
else
  info "  Patching: ${PROPS#$ROOTFS}"
  sed -i 's|</PropertyGroup>|<NuGetAudit>false</NuGetAudit></PropertyGroup>|' "$PROPS"
fi
 
# Compute the path as seen inside the container (strip the host rootfs prefix)
FSPROJ_HOST=$(find "${ROOTFS}/opt/rs2014/tools/RsCli" -name "*.fsproj" 2>/dev/null | head -1)
[[ -n "$FSPROJ_HOST" ]] || die "RsCli.fsproj not found under ${ROOTFS}/opt/rs2014/tools/RsCli"
FSPROJ_INNER="${FSPROJ_HOST#$ROOTFS}"
FSPROJ_DIR_INNER="$(dirname "$FSPROJ_INNER")"
info "  Building project at (container path): ${FSPROJ_DIR_INNER}"
 
r "export DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
   export DOTNET_CLI_TELEMETRY_OPTOUT=1
   cd '${FSPROJ_DIR_INNER}'
   dotnet publish -c Release -r '${RID}' --self-contained -o '${RSCLI_DIR}'"
 
# Clean up build artifacts to keep the image lean
rm -rf "${ROOTFS}/opt/rs2014" "${ROOTFS}/root/.nuget" "${ROOTFS}/root/.dotnet/toolResolverCache"
ok "RsCli built → ${RSCLI_DIR}"
 
# Tip: remove the SDK after build to save ~300 MB (runtime stays):
rm -rf "${ROOTFS}/usr/share/dotnet/sdk"
 
# =============================================================================
# 5. vgmstream-cli
# =============================================================================
info "Installing vgmstream-cli …"
r "curl -sL '${VGMSTREAM_URL}' -o /tmp/vgm.zip \
    && unzip -o /tmp/vgm.zip -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/vgmstream-cli \
    && rm /tmp/vgm.zip"
ok "vgmstream-cli installed."

# =============================================================================
# 6. Python application
# =============================================================================
info "Setting up Python application …"
mkdir -p \
  "${ROOTFS}${APP_DIR}/lib" \
  "${ROOTFS}${APP_DIR}/static" \
  "${ROOTFS}${APP_DIR}/plugins"

for d in lib static plugins; do
  [[ -d "$d" ]] && cp -r "${d}/." "${ROOTFS}${APP_DIR}/${d}/" \
    && info "  Copied ${d}/" || warn "  Local '${d}/' not found – skipping."
done

for f in requirements.txt server.py VERSION main.py; do
  [[ -f "$f" ]] && cp "$f" "${ROOTFS}${APP_DIR}/" \
    && info "  Copied ${f}" || warn "  '${f}' not found – skipping."
done

info "Installing Python dependencies …"
r "pip install --no-cache-dir --break-system-packages -r ${APP_DIR}/requirements.txt"
ok "Python dependencies installed."

# =============================================================================
# 7. Data directories + assets
# =============================================================================
info "Populating data directories …"
mkdir -p "${ROOTFS}${CONFIG_DIR}" "${ROOTFS}${DLC_DIR}" "${ROOTFS}${ROCKSMITH_DIR}"

[[ -d "config" ]] && cp -r config/. "${ROOTFS}${CONFIG_DIR}/" \
  && info "  Copied config/" || warn "  config/ not found."

if compgen -G "${ROCKSMITH_SRC_DIC}/dlc/*_p.psarc" &>/dev/null; then
  cp "${ROCKSMITH_SRC_DIC}"/dlc/*_p.psarc "${ROOTFS}${DLC_DIR}/"
  info "  Copied DLC psarc files."
else
  warn "  No *_p.psarc files found – copy them into ${DLC_DIR} on Proxmox."
fi

[[ -f "${ROCKSMITH_SRC_DIC}/songs.psarc" ]] && cp "${ROCKSMITH_SRC_DIC}/songs.psarc" "${ROOTFS}${ROCKSMITH_DIR}/" \
  && info "  Copied songs.psarc" || warn "  songs.psarc not found."

# =============================================================================
# 8. Environment variables
# =============================================================================
info "Writing /etc/environment …"
cat > "${ROOTFS}/etc/environment" <<EOF
PYTHONPATH=${APP_DIR}/lib:${APP_DIR}
RSCLI_PATH=${RSCLI_DIR}/RsCli
DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
DLC_DIR=${DLC_DIR}
CONFIG_DIR=${CONFIG_DIR}
EOF

# =============================================================================
# 9. systemd service for uvicorn
# =============================================================================
info "Installing rocksmith-server.service …"
mkdir -p "${ROOTFS}/etc/systemd/system"
cat > "${ROOTFS}/etc/systemd/system/rocksmith-server.service" <<EOF
[Unit]
Description=Rocksmith uvicorn server
After=network.target

[Service]
WorkingDirectory=${APP_DIR}
EnvironmentFile=/etc/environment
ExecStart=python3 main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Enable by symlinking (avoids running systemctl inside nspawn)
mkdir -p "${ROOTFS}/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/rocksmith-server.service \
       "${ROOTFS}/etc/systemd/system/multi-user.target.wants/rocksmith-server.service"
ok "Service enabled."

# =============================================================================
# 10. Proxmox-specific tweaks
# =============================================================================
info "Applying Proxmox CT compatibility tweaks …"

# (a) Ensure a working /etc/hostname and /etc/hosts
echo "rocksmith" > "${ROOTFS}/etc/hostname"
cat > "${ROOTFS}/etc/hosts" <<EOF
127.0.0.1   localhost
127.0.1.1   rocksmith
::1         localhost ip6-localhost ip6-loopback
EOF

# (b) Clear machine-id so Proxmox generates a fresh one on first boot
# A pre-filled machine-id can cause network/systemd conflicts across clones.
echo -n > "${ROOTFS}/etc/machine-id"
[[ -f "${ROOTFS}/var/lib/dbus/machine-id" ]] && echo -n > "${ROOTFS}/var/lib/dbus/machine-id"

# (c) DHCP networking via systemd-networkd (Proxmox expects this for unprivileged CTs)
mkdir -p "${ROOTFS}/etc/systemd/network"
cat > "${ROOTFS}/etc/systemd/network/20-eth0.network" <<EOF
[Match]
Name=eth0

[Network]
DHCP=yes
EOF

# Enable via symlinks on the host – systemctl inside nspawn needs a running
# init which WSL doesn't provide.

for svc in systemd-networkd systemd-resolved; do
  mkdir -p "${ROOTFS}/etc/systemd/system/multi-user.target.wants"
  ln -sf "/lib/systemd/system/${svc}.service"          "${ROOTFS}/etc/systemd/system/multi-user.target.wants/${svc}.service" 2>/dev/null || true
done

# (d) Fix resolv.conf to use systemd-resolved stub
rm -f "${ROOTFS}/etc/resolv.conf"
ln -sf /run/systemd/resolve/stub-resolv.conf "${ROOTFS}/etc/resolv.conf"

# (e) Ensure correct permissions on key dirs
chown -R 0:0 "${ROOTFS}${APP_DIR}" "${ROOTFS}${RSCLI_DIR}" \
              "${ROOTFS}${CONFIG_DIR}" "${ROOTFS}${DLC_DIR}" \
              "${ROOTFS}${ROCKSMITH_DIR}"

ok "Proxmox tweaks applied."

# =============================================================================
# 11. Package as a Proxmox-importable .tar.zst
# =============================================================================
OUTPUT_FILE="${OUTPUT_NAME}.tar.zst"
info "Creating ${OUTPUT_FILE} …"

# Proxmox pct restore expects a plain rootfs tarball (no ./rootfs/ prefix).
tar \
  --numeric-owner \
  --xattrs \
  --acls \
  -C "$ROOTFS" \
  -c . \
  | zstd -T0 -9 > "$OUTPUT_FILE"

ok "Template ready: $(pwd)/${OUTPUT_FILE}  ($(du -sh "$OUTPUT_FILE" | cut -f1))"

# =============================================================================
# Done
# =============================================================================
cat <<DONE

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Build complete!

  Transfer to Proxmox:
    scp ${OUTPUT_FILE} root@<proxmox-host>:/var/lib/vz/template/cache/

  Import on Proxmox (pick an unused VMID, e.g. 200):
    pct restore 200 /var/lib/vz/template/cache/${OUTPUT_FILE} \\
        --storage local-lvm \\
        --rootfs 8 \\
        --memory 2048 \\
        --cores 2 \\
        --net0 name=eth0,bridge=vmbr0,ip=dhcp \\
        --unprivileged 1 \\
        --start 1

  Then check the server:
    pct exec 200 -- systemctl status rocksmith-server
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DONE
