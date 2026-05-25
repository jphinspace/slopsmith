#!/usr/bin/env bash
# shellcheck shell=bash
# =============================================================================
# build-proxmox-ct.sh  –  Build a Proxmox LXC rootfs from WSL2 (no lxc-start)
#
# Run this from your project root (where rscli/, server.py, etc. live).
#
# Usage:
#   sudo bash build-proxmox-ct.sh [TARGETARCH] [OUTPUT_NAME]
#
# Examples:
#   sudo bash build-proxmox-ct.sh amd64 slopsmith-ct
#   sudo bash build-proxmox-ct.sh arm64 slopsmith-ct
#
# Environment variables:
#   SONG_SRC_DIR        Path to the song source root, containing both
#                       dlc/*_p.psarc and songs.psarc (default: /mnt/z/songs).
#   SKIP_HASH_CHECK=1   Bypass SHA256 verification — for unpinned hashes OR
#                       to override mismatches when an upstream artifact rolls
#                       (e.g. dot.net/v1/dotnet-install.sh). Use with caution.
#   KEEP_BUILD_DIR=1    Retain ${BUILD_BASE} after a successful build
#   FORCE_REBUILD=1     Delete an existing rootfs without prompting (for CI)
#
# Prerequisites (install in WSL):
#   sudo apt install debootstrap systemd-container tar zstd curl unzip git
#
# On Proxmox, after transfer:
#   pct restore <VMID> slopsmith-ct.tar.zst --storage local-lvm --rootfs 8 --unprivileged 1
# =============================================================================

set -euo pipefail

TARGETARCH="${1:-amd64}"
OUTPUT_NAME="${2:-slopsmith-ct}"

# OUTPUT_NAME is a positional arg that flows into BUILD_BASE (interpolated into
# `mkdir -p` / `rm -rf` paths) and into the final tarball name. Reject anything
# outside a safe filename charset so an input like `../../etc` can't escape
# /tmp or shape the tarball path.
if [[ ! "$OUTPUT_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "[ERROR] OUTPUT_NAME must match ^[A-Za-z0-9._-]+\$ (got: '${OUTPUT_NAME}')" >&2
  exit 1
fi

# debootstrap requires a real Linux filesystem (ext4/tmpfs/etc.) — it creates
# device nodes that NTFS/FUSE mounts (/mnt/c, /mnt/d …) cannot represent.
# We default the build dir to /tmp (a Linux fs on every WSL2 setup we've
# seen, even when /tmp isn't strictly tmpfs) and copy the final tarball back.
PROJECT_DIR="$(pwd)"          # may be on /mnt/d – that's fine for source files
# Namespace BUILD_BASE by OUTPUT_NAME + TARGETARCH so concurrent invocations
# (or stale leftovers from a prior build of a different artifact) don't
# collide on /tmp/proxmox-ct-build/rootfs. BUILD_BASE can still be overridden
# via the environment for users who want a known, reusable path.
BUILD_BASE="${BUILD_BASE:-/tmp/proxmox-ct-build-${OUTPUT_NAME}-${TARGETARCH}}"
# Safety net: BUILD_BASE feeds rm -rf in the cleanup trap, the rebuild flow,
# and (indirectly) the rootfs build. Refuse obviously dangerous values up
# front so a stray BUILD_BASE=/ or BUILD_BASE='' can never `rm -rf` the host.
case "$BUILD_BASE" in
  ""|/|//|/.*|.|./*|../*) echo "[ERROR] Refusing dangerous BUILD_BASE='${BUILD_BASE}'." >&2; exit 1 ;;
esac
if [[ "${BUILD_BASE}" != /* ]]; then
  echo "[ERROR] BUILD_BASE must be an absolute path (got: '${BUILD_BASE}')." >&2
  exit 1
fi

# Normalize so that things like /tmp/../etc resolve against the real prefix
# check below — without this, a path-traversal payload would slip past the
# /tmp/* match and the cleanup branches could rm-rf an unintended host path.
BUILD_BASE=$(realpath -m -- "$BUILD_BASE")
ROOTFS="${BUILD_BASE}/rootfs"

if (( ${#BUILD_BASE} < 6 )); then
  echo "[ERROR] BUILD_BASE='${BUILD_BASE}' is too short — refusing for safety." >&2
  exit 1
fi
if [[ "${BUILD_BASE}" != /tmp/* && "${I_KNOW_WHAT_IM_DOING:-0}" != "1" ]]; then
  echo "[ERROR] BUILD_BASE='${BUILD_BASE}' resolves outside /tmp." >&2
  echo "        Re-run with I_KNOW_WHAT_IM_DOING=1 to use a non-/tmp path." >&2
  exit 1
fi

mkdir -p "$BUILD_BASE"

DOTNET_CHANNEL="10.0"
VGMSTREAM_URL="https://github.com/vgmstream/vgmstream/releases/download/r2083/vgmstream-linux-cli.zip"
# Pin Rocksmith2014.NET to a specific commit so RsCli builds are reproducible.
# Bump this when intentionally pulling upstream changes.
RS2014_NET_REPO="https://github.com/iminashi/Rocksmith2014.NET.git"
RS2014_NET_COMMIT="b87c9a3afd31c40ade9685a9244e718e7581c0cb"
# Supply-chain hashes — regenerate with:
#   curl -fsSL <URL> | sha256sum
# Set SKIP_HASH_CHECK=1 to bypass verification (e.g. when Microsoft rolls
# dotnet-install.sh and the pinned hash hasn't been refreshed yet).
VGMSTREAM_SHA256="7fc17225b7a49b8f1e7850f6cc5bdaf73c35e81ee5774bb12211ebc85188a4ff"
# dot.net/v1/dotnet-install.sh is a rolling URL; refresh this hash whenever
# Microsoft updates the installer (the build will abort with a clear mismatch).
DOTNET_INSTALL_SHA256="102a6849303713f15462bb28eb10593bf874bbeec17122e0522f10a3b57ce442"

APP_DIR="/app"
VENV_DIR="/opt/app-venv"
RSCLI_DIR="/opt/rscli"
DLC_DIR="/dlc"
CONFIG_DIR="/config"
SONG_ARCHIVE_DIR="/songs"
# SONG_SRC_DIR points at the source ROOT (the directory containing dlc/).
SONG_SRC_DIR="${SONG_SRC_DIR:-/mnt/z/songs}"
SVC_USER="slopsmith"

# Coloured logging
info() { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()   { echo -e "\033[1;32m[OK]\033[0m    $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die()  { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; exit 1; }

cleanup() {
  local rc=$?
  if [[ $rc -ne 0 && -d "${BUILD_BASE:-}" ]]; then
    warn "Build failed (exit $rc). Partial rootfs left at ${BUILD_BASE} for inspection."
    # Use printf directly: warn() pipes through `echo -e`, which would
    # re-interpret the backslash escapes that `printf %q` emits and
    # silently break the suggested cleanup command.
    printf "\033[1;33m[WARN]\033[0m  Run: sudo rm -rf %q\n" "${BUILD_BASE}"
  elif [[ $rc -eq 0 && -d "${BUILD_BASE:-}" && "${KEEP_BUILD_DIR:-0}" != "1" ]]; then
    info "Removing build directory ${BUILD_BASE} (set KEEP_BUILD_DIR=1 to retain)."
    rm -rf "${BUILD_BASE}"
  fi
}
trap cleanup EXIT

# Verify a downloaded file against a pinned SHA256 hash.
# Skips verification when the expected hash is empty (not yet pinned).
verify_sha256() {
  local file="$1" expected="$2" label="${3:-$1}"
  if [[ -z "$expected" ]]; then
    if [[ "${SKIP_HASH_CHECK:-0}" != "1" ]]; then
      die "No SHA256 pinned for ${label}. Pin the hash or set SKIP_HASH_CHECK=1 to proceed."
    fi
    warn "No SHA256 pinned for ${label} — skipping verification (SKIP_HASH_CHECK=1)."
    return 0
  fi
  local actual
  actual=$(sha256sum "$file" | awk '{print $1}')
  if [[ "$actual" != "$expected" ]]; then
    if [[ "${SKIP_HASH_CHECK:-0}" == "1" ]]; then
      warn "SHA256 mismatch for ${label} (expected ${expected}, got ${actual}) — continuing because SKIP_HASH_CHECK=1."
      return 0
    fi
    die "SHA256 mismatch for ${label}:\n" \
        "       expected: ${expected}\n" \
        "       got:      ${actual}\n" \
        "       Refresh the pinned hash, or set SKIP_HASH_CHECK=1 to bypass."
  fi
  ok "SHA256 verified for ${label}."
}

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"

case "$TARGETARCH" in
  arm64) RID="linux-arm64" ; DEBIAN_ARCH="arm64" ;;
  amd64) RID="linux-x64"   ; DEBIAN_ARCH="amd64" ;;
  *)     die "Unsupported TARGETARCH: ${TARGETARCH}. Expected: amd64 | arm64" ;;
esac

# arm64 cross-builds require qemu-user-static + a registered binfmt handler.
# Checking the binary alone isn't enough — without a registered+enabled handler
# debootstrap/nspawn fail later with "exec format error" after significant
# wasted setup time.
if [[ "$TARGETARCH" == "arm64" && "$(uname -m)" != "aarch64" ]]; then
  if ! command -v qemu-aarch64-static &>/dev/null; then
    die "arm64 builds on a non-arm64 host require qemu-user-static.\n" \
        "       Install with: sudo apt install qemu-user-static binfmt-support\n" \
        "       Then re-run this script."
  fi
  binfmt_reg=""
  for f in /proc/sys/fs/binfmt_misc/qemu-aarch64 \
           /proc/sys/fs/binfmt_misc/qemu-aarch64-static; do
    [[ -f "$f" ]] && grep -q '^enabled' "$f" 2>/dev/null && { binfmt_reg="$f"; break; }
  done
  if [[ -z "$binfmt_reg" ]]; then
    die "arm64 binfmt handler not registered or not enabled.\n" \
        "       Register with: sudo apt install qemu-user-static binfmt-support\n" \
        "       Or:           docker run --rm --privileged multiarch/qemu-user-static --reset -p yes\n" \
        "       Then verify:  grep ^enabled /proc/sys/fs/binfmt_misc/qemu-aarch64*"
  fi
fi

# Confirm required tools
for cmd in debootstrap systemd-nspawn curl unzip git tar zstd; do
  command -v "$cmd" &>/dev/null || die "'$cmd' not found. Run: sudo apt install debootstrap systemd-container curl unzip git tar zstd"
done

# =============================================================================
# Helper: run a command inside the rootfs via systemd-nspawn
# --quiet suppresses nspawn chatter so apt/dotnet output is the only
# thing we see during the build. The host's /etc/resolv.conf is
# bind-mounted read-only so DNS works inside nspawn.
# =============================================================================
r() {
  systemd-nspawn \
    --quiet \
    --directory="$ROOTFS" \
    --bind-ro=/etc/resolv.conf:/etc/resolv.conf \
    -- bash -c "set -e; $1"
}

# =============================================================================
# 1. Bootstrap a minimal Debian Trixie rootfs
# =============================================================================
info "Bootstrapping Debian Trixie (${DEBIAN_ARCH}) rootfs at ${ROOTFS} …"
if [[ -d "$ROOTFS" ]]; then
  if [[ "${FORCE_REBUILD:-0}" == "1" ]]; then
    info "FORCE_REBUILD=1 — removing existing rootfs at ${ROOTFS}."
    rm -rf "$ROOTFS" || die "Failed to remove existing rootfs at ${ROOTFS}."
  elif [[ -t 0 ]]; then
    warn "Existing rootfs found at ${ROOTFS} – remove it to rebuild from scratch."
    read -rp "    Delete and rebuild? [y/N] " yn
    if [[ "$yn" =~ ^[Yy]$ ]]; then
      rm -rf "$ROOTFS" || die "Failed to remove existing rootfs at ${ROOTFS}."
    else
      die "Aborting."
    fi
  else
    die "Existing rootfs at ${ROOTFS}; rerun with FORCE_REBUILD=1 to overwrite."
  fi
fi

debootstrap \
  --arch="$DEBIAN_ARCH" \
  --include=ca-certificates,curl,gnupg \
  trixie \
  "$ROOTFS" \
  https://deb.debian.org/debian

ok "Bootstrap complete."

# DNS during the build is supplied by the host's /etc/resolv.conf, which
# r() bind-mounts read-only into nspawn. The rootfs's own /etc/resolv.conf
# gets replaced with a systemd-resolved stub symlink in step 10(d).

# =============================================================================
# 2. System packages  (mirrors Stage 2 apt block)
# =============================================================================
info "Installing system packages …"
# systemd-sysv + systemd-resolved are explicit because the final container
# enables systemd-networkd/systemd-resolved units in step 10 and rewrites
# /etc/resolv.conf to the resolved stub — a minimal debootstrap does not
# guarantee these binaries on its own, which would yield broken DNS in the
# imported CT.
r "apt-get update -qq && apt-get install -y --no-install-recommends \
    systemd-sysv systemd-resolved \
    python3 python3-pip python3-venv \
    ffmpeg \
    fluidsynth \
    fluid-soundfont-gm \
    libsndfile1 \
    curl \
    unzip \
    megatools \
    && apt-get clean && rm -rf /var/lib/apt/lists/*"
ok "System packages installed."

# =============================================================================
# 3. Install .NET  (build-time only — RsCli is published with --self-contained,
#    so the entire /usr/share/dotnet tree is removed after build to save ~700MB
#    and shrink the runtime attack surface, mirroring the Dockerfile's slim
#    final stage)
# =============================================================================
info "Installing .NET ${DOTNET_CHANNEL} SDK (build-only, removed after publish) …"
r "curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh"
verify_sha256 "${ROOTFS}/tmp/dotnet-install.sh" "${DOTNET_INSTALL_SHA256}" "dotnet-install.sh"
r "chmod +x /tmp/dotnet-install.sh \
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
info "Cloning Rocksmith2014.NET @ ${RS2014_NET_COMMIT:0:12} (host-side) …"
mkdir -p "${ROOTFS}/opt"  # /opt is FHS-required, but be explicit
rm -rf "${ROOTFS}/opt/rs2014"
git clone --no-checkout --filter=blob:none "${RS2014_NET_REPO}" "${ROOTFS}/opt/rs2014"
git -C "${ROOTFS}/opt/rs2014" checkout --quiet "${RS2014_NET_COMMIT}"
 
info "Copying rscli sources …"
[[ -f "${PROJECT_DIR}/rscli/RsCli.fsproj" ]] || die "rscli/RsCli.fsproj not found."
[[ -f "${PROJECT_DIR}/rscli/Program.fs"   ]] || die "rscli/Program.fs not found."
mkdir -p "${ROOTFS}/opt/rs2014/tools/RsCli"
cp "${PROJECT_DIR}/rscli/RsCli.fsproj" "${ROOTFS}/opt/rs2014/tools/RsCli/"
cp "${PROJECT_DIR}/rscli/Program.fs"   "${ROOTFS}/opt/rs2014/tools/RsCli/"
 
# NuGetAudit=false: Rocksmith2014.NET pins older NuGet dependencies that
# trigger audit warnings.  We don't ship the SDK in the final image — only
# the self-contained publish output — so these warnings are noise during a
# build-time-only step.  Re-enable if you upgrade the upstream project.
info "Patching Directory.Build.props (host-side) …"
PROPS=$(find "${ROOTFS}/opt/rs2014" -name "Directory.Build.props" | head -1)
if [[ -z "$PROPS" ]]; then
  warn "Directory.Build.props not found – skipping NuGetAudit patch"
else
  info "  Patching: ${PROPS#"$ROOTFS"}"
  sed -i 's|</PropertyGroup>|<NuGetAudit>false</NuGetAudit></PropertyGroup>|' "$PROPS"
fi
 
# Compute the path as seen inside the container (strip the host rootfs prefix)
FSPROJ_HOST=$(find "${ROOTFS}/opt/rs2014/tools/RsCli" -name "*.fsproj" 2>/dev/null | head -1)
[[ -n "$FSPROJ_HOST" ]] || die "RsCli.fsproj not found under ${ROOTFS}/opt/rs2014/tools/RsCli"
FSPROJ_INNER="${FSPROJ_HOST#"$ROOTFS"}"
FSPROJ_DIR_INNER="$(dirname "$FSPROJ_INNER")"
info "  Building project at (container path): ${FSPROJ_DIR_INNER}"
 
r "export DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
    && export DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    && cd '${FSPROJ_DIR_INNER}' \
    && dotnet publish -c Release -r '${RID}' --self-contained -o '${RSCLI_DIR}'"
 
# Clean up build artifacts to keep the image lean. RsCli is self-contained
# (its publish output bundles its own .NET runtime under ${RSCLI_DIR}), so
# the system-wide /usr/share/dotnet tree is build-only and gets dropped.
rm -rf "${ROOTFS}/opt/rs2014" \
       "${ROOTFS}/root/.nuget" \
       "${ROOTFS}/root/.dotnet" \
       "${ROOTFS}/usr/share/dotnet" \
       "${ROOTFS}/usr/local/bin/dotnet"
ok "RsCli built → ${RSCLI_DIR}"

# =============================================================================
# 5. vgmstream-cli
# =============================================================================
info "Installing vgmstream-cli …"
r "curl -fSL '${VGMSTREAM_URL}' -o /tmp/vgm.zip"
verify_sha256 "${ROOTFS}/tmp/vgm.zip" "${VGMSTREAM_SHA256}" "vgmstream-linux-cli.zip"
r "unzip -o /tmp/vgm.zip -d /usr/local/bin/ \
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
  if [[ -d "$d" ]]; then
    cp -r "${d}/." "${ROOTFS}${APP_DIR}/${d}/"
    info "  Copied ${d}/"
  else
    # main.py imports logging_setup from lib/; server.py imports plugins.
    # Without these the rootfs boots and the service crashes immediately.
    if [[ "$d" == "lib" || "$d" == "plugins" ]]; then
      die "  '${d}/' not found — required for the service to import."
    fi
    warn "  Local '${d}/' not found – skipping."
  fi
done

for f in requirements.txt server.py VERSION main.py; do
  if [[ -f "$f" ]]; then
    cp "$f" "${ROOTFS}${APP_DIR}/"
    info "  Copied ${f}"
  else
    # main.py imports `server:app`, so without server.py the service unit
    # would boot but fail on first request — make it fail-fast at build.
    if [[ "$f" == "requirements.txt" || "$f" == "main.py" || "$f" == "server.py" ]]; then
      die "  '${f}' not found — required for the service to start."
    fi
    warn "  '${f}' not found – skipping."
  fi
done

info "Creating Python venv and installing dependencies …"
r "python3 -m venv ${VENV_DIR} \
    && ${VENV_DIR}/bin/pip install --no-cache-dir -r ${APP_DIR}/requirements.txt"
ok "Python venv + dependencies installed."

# =============================================================================
# 7. Data directories + assets
# =============================================================================
info "Populating data directories …"
mkdir -p "${ROOTFS}${CONFIG_DIR}" "${ROOTFS}${DLC_DIR}" "${ROOTFS}${SONG_ARCHIVE_DIR}"

if [[ -d "config" ]]; then
  cp -r config/. "${ROOTFS}${CONFIG_DIR}/"
  info "  Copied config/"
else
  warn "  config/ not found."
fi

if compgen -G "${SONG_SRC_DIR}/dlc/*_p.psarc" &>/dev/null; then
  cp "${SONG_SRC_DIR}"/dlc/*_p.psarc "${ROOTFS}${DLC_DIR}/"
  info "  Copied DLC psarc files."
else
  warn "  No *_p.psarc files found – copy them into ${DLC_DIR} on Proxmox."
fi

if [[ -f "${SONG_SRC_DIR}/songs.psarc" ]]; then
  cp "${SONG_SRC_DIR}/songs.psarc" "${ROOTFS}${SONG_ARCHIVE_DIR}/"
  info "  Copied songs.psarc"
else
  warn "  songs.psarc not found."
fi

# =============================================================================
# 8. Environment variables
# =============================================================================
info "Writing /etc/environment …"
cat > "${ROOTFS}/etc/environment" <<EOF
PATH=${VENV_DIR}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
PYTHONPATH=${APP_DIR}/lib:${APP_DIR}
RSCLI_PATH=${RSCLI_DIR}/RsCli
DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
DLC_DIR=${DLC_DIR}
CONFIG_DIR=${CONFIG_DIR}
EOF

# =============================================================================
# 9. systemd service for uvicorn
# =============================================================================
info "Creating service user '${SVC_USER}' …"
r "useradd --system --home-dir ${APP_DIR} --shell /usr/sbin/nologin ${SVC_USER}"
ok "User '${SVC_USER}' created."

info "Installing slopsmith-server.service …"
mkdir -p "${ROOTFS}/etc/systemd/system"
cat > "${ROOTFS}/etc/systemd/system/slopsmith-server.service" <<EOF
[Unit]
Description=Slopsmith uvicorn server
After=network.target

[Service]
User=${SVC_USER}
# Default port (8000) is non-privileged; uncomment the next line only if
# you set PORT<1024 in /etc/environment so the unit can bind it.
# AmbientCapabilities=CAP_NET_BIND_SERVICE
WorkingDirectory=${APP_DIR}
EnvironmentFile=/etc/environment
ExecStart=${VENV_DIR}/bin/python3 main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Enable by symlinking (avoids running systemctl inside nspawn)
mkdir -p "${ROOTFS}/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/slopsmith-server.service \
       "${ROOTFS}/etc/systemd/system/multi-user.target.wants/slopsmith-server.service"
ok "Service enabled."

# =============================================================================
# 10. Proxmox-specific tweaks
# =============================================================================
info "Applying Proxmox CT compatibility tweaks …"

# (a) Ensure a working /etc/hostname and /etc/hosts.
# Use OUTPUT_NAME (already validated to a safe filename charset) so the
# template's identity matches the artifact name. This is just a sane
# fallback — `pct restore --hostname …` (or the Proxmox UI) will overwrite
# /etc/hostname when the CT is created from this template.
DEFAULT_HOSTNAME="${OUTPUT_NAME//_/-}"  # underscores aren't valid in hostnames
echo "${DEFAULT_HOSTNAME}" > "${ROOTFS}/etc/hostname"
cat > "${ROOTFS}/etc/hosts" <<EOF
127.0.0.1   localhost
127.0.1.1   ${DEFAULT_HOSTNAME}
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

mkdir -p "${ROOTFS}/etc/systemd/system/multi-user.target.wants"
for svc in systemd-networkd systemd-resolved; do
  unit_src=""
  for d in /lib/systemd/system /usr/lib/systemd/system; do
    if [[ -e "${ROOTFS}${d}/${svc}.service" ]]; then
      unit_src="${d}/${svc}.service"
      break
    fi
  done
  if [[ -z "$unit_src" ]]; then
    die "${svc}.service unit not found in rootfs — DNS/networking would be broken in the imported CT."
  fi
  ln -sf "${unit_src}" "${ROOTFS}/etc/systemd/system/multi-user.target.wants/${svc}.service"
done

# (d) Ensure correct permissions on key dirs.
# -h preserves symlinks: a Python venv keeps /opt/app-venv/bin/python3 as a
# symlink to /usr/bin/python3, and a plain `chown -R` would chase it and
# rewrite the system interpreter's ownership inside the rootfs.
SVC_UID="$(r "id -u ${SVC_USER}")"
SVC_GID="$(r "id -g ${SVC_USER}")"
chown -hR "${SVC_UID}:${SVC_GID}" \
              "${ROOTFS}${APP_DIR}" "${ROOTFS}${CONFIG_DIR}" \
              "${ROOTFS}${DLC_DIR}" "${ROOTFS}${VENV_DIR}"
chown -hR 0:0 "${ROOTFS}${RSCLI_DIR}" \
              "${ROOTFS}${SONG_ARCHIVE_DIR}"

# (e) Fix resolv.conf to use the systemd-resolved stub. MUST run after the
# last r() invocation: r() bind-mounts the host /etc/resolv.conf onto the
# rootfs path, and systemd-nspawn follows symlinks when resolving the bind
# target — pointing /etc/resolv.conf at /run/systemd/resolve/stub-resolv.conf
# before that would make subsequent r() calls try to bind onto a path that
# doesn't exist during the build.
rm -f "${ROOTFS}/etc/resolv.conf"
ln -sf /run/systemd/resolve/stub-resolv.conf "${ROOTFS}/etc/resolv.conf"

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
    pct exec 200 -- systemctl status slopsmith-server
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DONE
