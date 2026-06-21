#!/usr/bin/env bash
set -euo pipefail

# Simple installer for automation-runner
# Usage:
#  - On Proxmox host: curl -fsSL <URL>/install.sh | bash
#  - Inside container: curl -fsSL <URL>/install.sh | bash -s -- inside

INSTALLER_GIT_REPO="https://github.com/jonathanlo411/rdp-management-service.git"

VM_HOSTNAME="automation-runner"
VM_VMID="" # auto if empty
VM_MEM=512
VM_CORES=1
VM_DISK=8

function log(){ echo "[install] $*"; }

function ensure_inside_container(){
  # Detect if running inside LXC/container
  if grep -qa container=lxc /proc/1/environ 2>/dev/null || [ -f /.dockerenv ] || [ -f /run/systemd/container ]; then
    return 0
  fi
  return 1
}

function gen_secret(){
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    python3 -c 'import secrets,sys; print(secrets.token_hex(24))'
  fi
}

function derive_installer_url(){
  if [[ "${INSTALLER_GIT_REPO}" =~ ^https://github.com/([^/]+)/([^/.]+)(\.git)?$ ]]; then
    local owner=${BASH_REMATCH[1]}
    local repo=${BASH_REMATCH[2]}
    echo "https://raw.githubusercontent.com/${owner}/${repo}/main/install.sh"
    return
  fi
  if [[ "${INSTALLER_GIT_REPO}" =~ ^git@github.com:([^/]+)/([^/.]+)\.git$ ]]; then
    local owner=${BASH_REMATCH[1]}
    local repo=${BASH_REMATCH[2]}
    echo "https://raw.githubusercontent.com/${owner}/${repo}/main/install.sh"
    return
  fi
}

function configure_container(){
  log "Running in-container setup"

  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    freerdp2-x11 xvfb xdotool python3 python3-pip python3-venv git curl ca-certificates procps

  mkdir -p /opt/automation-runner
  chown root:root /opt/automation-runner

  if [ ! -d /opt/automation-runner/.git ]; then
    if [ -n "${INSTALLER_GIT_REPO}" ]; then
      log "Cloning repo ${INSTALLER_GIT_REPO} into /opt/automation-runner"
      rm -rf /opt/automation-runner/*
      git clone --depth 1 "${INSTALLER_GIT_REPO}" /opt/automation-runner
    else
      log "No INSTALLER_GIT_REPO set. Cannot fetch application files."
      exit 1
    fi
  else
    (cd /opt/automation-runner && git pull --ff-only || true)
  fi

  if [ -f /opt/automation-runner/app/requirements.txt ]; then
    if [ ! -d /opt/automation-runner/.venv ]; then
      python3 -m venv /opt/automation-runner/.venv
      chown -R automation:automation /opt/automation-runner/.venv || true
    fi
    /opt/automation-runner/.venv/bin/python -m pip install --upgrade pip
    /opt/automation-runner/.venv/bin/python -m pip install -r /opt/automation-runner/app/requirements.txt
  fi

  # Ensure service user
  if ! id -u automation >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin automation || true
  fi

  mkdir -p /opt/automation-runner/.config/freerdp
  chown -R automation:automation /opt/automation-runner/.config || true

  # Secrets file
  ENV_FILE=/opt/automation-runner/.env
  if [ ! -f "$ENV_FILE" ]; then
    API_KEY=$(gen_secret)
    cat > "$ENV_FILE" <<EOF
API_KEY=${API_KEY}
EOF
    chown automation:automation "$ENV_FILE" || true
    chmod 600 "$ENV_FILE" || true
    log "Generated ${ENV_FILE} with new API_KEY"
  else
    log "Using existing ${ENV_FILE}"
  fi

  # Create sample targets.json for multi-target support
  TARGETS_JSON=/opt/automation-runner/targets.json
  if [ ! -f "$TARGETS_JSON" ]; then
    cat > "$TARGETS_JSON" <<EOF
{
  "gaming_pc": {
    "host": "<WINDOWS_HOST>",
    "user": "<WINDOWS_USER>",
    "password": "<WINDOWS_PASSWORD>"
  }
}
EOF
    chown automation:automation "$TARGETS_JSON" || true
    chmod 600 "$TARGETS_JSON" || true
    log "Created sample ${TARGETS_JSON} (edit to add targets)"
  else
    log "Using existing ${TARGETS_JSON}"
  fi

  # Install systemd service
  if [ -f /opt/automation-runner/app/automation-runner.service ]; then
    cp /opt/automation-runner/app/automation-runner.service /etc/systemd/system/automation-runner.service
    systemctl daemon-reload
    systemctl enable --now automation-runner.service || systemctl.restart automation-runner.service || true
  else
    log "service file not found in repo: /opt/automation-runner/app/automation-runner.service"
  fi

  # Health check
  sleep 2
  if curl -fsS --connect-timeout 3 http://127.0.0.1:5000/health >/dev/null 2>&1; then
    log "Health endpoint OK"
  else
    log "Health check failed. Check service logs: journalctl -u automation-runner.service -n 200 --no-pager"
  fi
}

function find_storage_for_template(){
  # Prefer a dir-backed storage for templates, typically 'local'
  if pvesm status 2>/dev/null | awk 'NR>1 && $1 == "local" {print $1; exit}'; then
    return 0
  fi
  pvesm status 2>/dev/null | awk 'NR>1 && $2 == "dir" {print $1; exit}'
}

function find_storage_for_rootfs(){
  # Prefer local-lvm for LXC rootfs if available, otherwise choose another block storage
  if pvesm status 2>/dev/null | awk 'NR>1 && $1 == "local-lvm" {exit 0} END {exit 1}'; then
    echo "local-lvm"
    return 0
  fi
  pvesm status 2>/dev/null | awk 'NR>1 && ($2 == "lvmthin" || $2 == "zfspool" || $2 == "btrfs") {print $1; exit}'
}

function create_lxc(){
  if ! command -v pct >/dev/null 2>&1; then
    log "pct command not found. Are you on a Proxmox host?"
    exit 1
  fi

  # choose vmid
  if [ -z "${VM_VMID}" ]; then
    if command -v pvesh >/dev/null 2>&1; then
      VM_VMID=$(pvesh get /cluster/nextid 2>/dev/null || true)
    fi
    VM_VMID=${VM_VMID:-101}
  fi

  log "Creating LXC with vmid=${VM_VMID} hostname=${VM_HOSTNAME}"

  pveam update >/dev/null 2>&1 || true
  TPL=$(pveam available | awk '/ubuntu-24\.04-standard|ubuntu-24\.04/ { print $2; exit }' || true)
  if [ -z "$TPL" ]; then
    log "ubuntu-24.04 template not found in pveam available list"
    pveam available | grep -i ubuntu || true
    exit 1
  fi

  TPL_STORAGE=${TPL_STORAGE:-$(find_storage_for_template)}
  if [ -z "$TPL_STORAGE" ]; then
    log "Unable to find a storage that supports vzdtmpl. Run 'pvesm status' and choose an appropriate storage."
    pvesm status
    exit 1
  fi

  if ! pveam download "$TPL_STORAGE" "$TPL" >/dev/null 2>&1; then
    log "Failed to download template ${TPL} into storage ${TPL_STORAGE}."
    pveam available | grep -i ubuntu || true
    exit 1
  fi

  OSTPL="${TPL_STORAGE}:vztmpl/${TPL}"
  log "Using ostemplate ${OSTPL}"

  ROOTPW=$(gen_secret)

  VM_STORAGE=${VM_STORAGE:-$(find_storage_for_rootfs)}
  if [ -z "$VM_STORAGE" ]; then
    log "Unable to find a storage for rootfs. Run 'pvesm status' and choose an appropriate storage for containers."
    pvesm status
    exit 1
  fi

  STORAGE_TYPE=$(pvesm status 2>/dev/null | awk -v s="$VM_STORAGE" '$1 == s {print $2}')
  ROOTFS_SPEC=""
  if [[ "$STORAGE_TYPE" =~ ^(lvmthin|zfspool|btrfs)$ ]]; then
    if [[ "$VM_DISK" =~ ^([0-9]+)G$ ]] || [[ "$VM_DISK" =~ ^([0-9]+)g$ ]] || [[ "$VM_DISK" =~ ^([0-9]+)$ ]]; then
      ROOTFS_SPEC="${BASH_REMATCH[1]}"
    else
      ROOTFS_SPEC="$VM_DISK"
    fi
  else
    if [[ "$VM_DISK" =~ ^[0-9]+$ ]]; then
      ROOTFS_SPEC="${VM_DISK}G"
    else
      ROOTFS_SPEC="$VM_DISK"
    fi
  fi

  log "Using storage ${VM_STORAGE} for rootfs (type=${STORAGE_TYPE}, spec=${ROOTFS_SPEC})"
  pct create "$VM_VMID" "$OSTPL" --hostname "$VM_HOSTNAME" --cores $VM_CORES --memory $VM_MEM --rootfs ${VM_STORAGE}:${ROOTFS_SPEC} --net0 name=eth0,bridge=vmbr0,ip=dhcp --password "$ROOTPW"
  pct start "$VM_VMID"

  # clean up host template archive to avoid leaving tarballs behind
  if [ "$TPL_STORAGE" = "local" ] && [ -f "/var/lib/vz/template/cache/${TPL}" ]; then
    rm -f "/var/lib/vz/template/cache/${TPL}"
    log "Removed downloaded template /var/lib/vz/template/cache/${TPL} from host"
  fi

  log "Waiting for container to start"
  sleep 5

  INSTALLER_FETCH_URL="$(derive_installer_url)"
  log "Bootstrapping inside container via ${INSTALLER_FETCH_URL}"
  pct exec "$VM_VMID" -- bash -lc "apt-get update && apt-get install -y curl ca-certificates && curl -fsSL ${INSTALLER_FETCH_URL} | bash -s -- inside"
}

if grep -qa container=lxc /proc/1/environ 2>/dev/null || [ -f /.dockerenv ] || [ -f /run/systemd/container ]; then
  configure_container
  exit 0
fi

# Not inside container: try to create LXC
if command -v pct >/dev/null 2>&1; then
  create_lxc
  exit 0
fi

log "No pct found and not inside container. You can still run this script inside a target Ubuntu container to install the application."
echo "To run inside a container (one-time):"
echo "  curl -fsSL <installer-url>/install.sh | bash -s -- inside"
