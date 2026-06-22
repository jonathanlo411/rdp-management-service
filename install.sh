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

function gen_secret(){
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    python3 -c 'import secrets; print(secrets.token_hex(24))'
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
  apt-get update -qq
  apt-get install -y --no-install-recommends \
    freerdp2-x11 xvfb xdotool python3 python3-pip python3-venv git curl ca-certificates procps x11-utils

  mkdir -p /opt/automation-runner

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
    log "Repo already exists, pulling latest"
    (cd /opt/automation-runner && git pull --ff-only || true)
  fi

  if [ -f /opt/automation-runner/app/requirements.txt ]; then
    if [ ! -d /opt/automation-runner/.venv ]; then
      python3 -m venv /opt/automation-runner/.venv
    fi
    /opt/automation-runner/.venv/bin/python -m pip install --upgrade pip -q
    /opt/automation-runner/.venv/bin/python -m pip install -r /opt/automation-runner/app/requirements.txt -q
  fi

  mkdir -p /opt/automation-runner/.config/freerdp

  # Secrets file
  ENV_FILE=/opt/automation-runner/.env
  if [ ! -f "$ENV_FILE" ]; then
    API_KEY=$(gen_secret)
    cat > "$ENV_FILE" <<EOF
API_KEY=${API_KEY}
EOF
    chmod 600 "$ENV_FILE"
    log "Generated ${ENV_FILE} with new API_KEY"
  else
    log "Using existing ${ENV_FILE}"
  fi

  # Sample targets.json
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
    chmod 600 "$TARGETS_JSON"
    log "Created sample ${TARGETS_JSON} — edit before use"
  else
    log "Using existing ${TARGETS_JSON}"
  fi

  # Install systemd service
  SERVICE_SRC=/opt/automation-runner/app/automation-runner.service
  if [ -f "$SERVICE_SRC" ]; then
    cp "$SERVICE_SRC" /etc/systemd/system/automation-runner.service
    systemctl daemon-reload
    systemctl enable automation-runner.service
    systemctl restart automation-runner.service || true
    log "Service installed and started"
  else
    log "WARNING: service file not found at ${SERVICE_SRC} — starting app directly in background"
    pkill -f "app/app.py" 2>/dev/null || true
    nohup /opt/automation-runner/.venv/bin/python /opt/automation-runner/app/app.py \
      > /var/log/automation-runner.log 2>&1 &
    log "App started (pid=$!), logs at /var/log/automation-runner.log"
  fi

  # Health check
  sleep 3
  for i in {1..20}; do
    if curl -fsS http://127.0.0.1:5000/health >/dev/null 2>&1; then
      log "Health endpoint OK"
      break
    else
      log "Waiting for service to become healthy... ($i/20)"
    fi
    sleep 1
  done
}

function find_storage_for_template(){
  if pvesm status 2>/dev/null | awk 'NR>1 && $1 == "local" {print $1; exit}'; then
    return 0
  fi
  pvesm status 2>/dev/null | awk 'NR>1 && $2 == "dir" {print $1; exit}'
}

function find_storage_for_rootfs(){
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

  if [ -z "${VM_VMID}" ]; then
    if command -v pvesh >/dev/null 2>&1; then
      VM_VMID=$(pvesh get /cluster/nextid 2>/dev/null || true)
    fi
    VM_VMID=${VM_VMID:-101}
  fi

  log "Creating LXC vmid=${VM_VMID} hostname=${VM_HOSTNAME}"

  pveam update >/dev/null 2>&1 || true
  TPL=$(pveam available | awk '/ubuntu-24\.04-standard|ubuntu-24\.04/ { print $2; exit }' || true)
  if [ -z "$TPL" ]; then
    log "ubuntu-24.04 template not found"
    pveam available | grep -i ubuntu || true
    exit 1
  fi

  TPL_STORAGE=${TPL_STORAGE:-$(find_storage_for_template)}
  if [ -z "$TPL_STORAGE" ]; then
    log "Unable to find template storage. Run 'pvesm status'."
    exit 1
  fi

  pveam download "$TPL_STORAGE" "$TPL" >/dev/null 2>&1 || true
  OSTPL="${TPL_STORAGE}:vztmpl/${TPL}"
  log "Using ostemplate ${OSTPL}"

  ROOTPW=$(gen_secret)

  VM_STORAGE=${VM_STORAGE:-$(find_storage_for_rootfs)}
  if [ -z "$VM_STORAGE" ]; then
    log "Unable to find rootfs storage. Run 'pvesm status'."
    exit 1
  fi

  STORAGE_TYPE=$(pvesm status 2>/dev/null | awk -v s="$VM_STORAGE" '$1 == s {print $2}')
  if [[ "$STORAGE_TYPE" =~ ^(lvmthin|zfspool|btrfs)$ ]]; then
    ROOTFS_SPEC="${VM_DISK//G/}"
  else
    ROOTFS_SPEC="${VM_DISK}G"
  fi

  log "Using storage ${VM_STORAGE} (type=${STORAGE_TYPE})"
  pct create "$VM_VMID" "$OSTPL" \
    --hostname "$VM_HOSTNAME" \
    --cores "$VM_CORES" \
    --memory "$VM_MEM" \
    --rootfs "${VM_STORAGE}:${ROOTFS_SPEC}" \
    --net0 name=eth0,bridge=vmbr0,ip=dhcp \
    --password "$ROOTPW"
  pct start "$VM_VMID"

  # Clean up template tarball
  if [ "$TPL_STORAGE" = "local" ] && [ -f "/var/lib/vz/template/cache/${TPL}" ]; then
    rm -f "/var/lib/vz/template/cache/${TPL}"
    log "Removed template tarball from host"
  fi

  log "Waiting for container to boot..."
  sleep 5

  INSTALLER_FETCH_URL="$(derive_installer_url)"
  log "Bootstrapping inside container via ${INSTALLER_FETCH_URL}"
  pct exec "$VM_VMID" -- bash -lc \
    "apt-get update -qq && apt-get install -y curl ca-certificates && curl -fsSL ${INSTALLER_FETCH_URL} | bash -s -- inside"
}

# ---------- entrypoint ----------

if grep -qa container=lxc /proc/1/environ 2>/dev/null || [ -f /.dockerenv ] || [ -f /run/systemd/container ]; then
  configure_container
  exit 0
fi

if command -v pct >/dev/null 2>&1; then
  create_lxc
  exit 0
fi

log "No pct found and not inside a container."
log "To install inside an existing container:"
log "  curl -fsSL $(derive_installer_url) | bash -s -- inside"