#!/usr/bin/env bash
set -euo pipefail

# Simple installer for automation-runner
# Usage:
#  - On Proxmox host: curl -fsSL <URL>/install.sh | bash
#  - Inside container: curl -fsSL <URL>/install.sh | bash -s -- inside

INSTALLER_GIT_REPO="https://github.com/jonathanlo411/rdp-management-service.git"
INSTALLER_URL="" # optional: if known, set the script URL here

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

function configure_container(){
  log "Running in-container setup"

  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    xfreerdp2-x11 xvfb xdotool python3 python3-pip git curl ca-certificates procps

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
    python3 -m pip install --upgrade pip
    python3 -m pip install -r /opt/automation-runner/app/requirements.txt
  fi

  # Ensure service user
  if ! id -u automation >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin automation || true
  fi

  # Secrets file
  ENV_FILE=/opt/automation-runner/.env
  if [ ! -f "$ENV_FILE" ]; then
    API_KEY=$(gen_secret)
    cat > "$ENV_FILE" <<EOF
API_KEY=${API_KEY}
# Set your Windows target here
WINDOWS_HOST=
WINDOWS_USER=
WINDOWS_PASSWORD=
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
    "host": "192.168.1.100",
    "user": "myuser",
    "password": "mypassword"
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

  # ensure template available
  if ! pveam available | grep -qa ubuntu-24.04; then
    log "Downloading ubuntu-24.04-template via pveam"
    pveam update
    pveam download local ubuntu-24.04-standard || true
  fi

  TPL=$(pveam available | awk '/ubuntu-24.04-standard/ {print $1; exit}' || true)
  if [ -z "$TPL" ]; then
    log "Unable to find ubuntu-24.04-standard template via pveam. Please ensure a suitable template exists on the Proxmox host."
    exit 1
  fi

  ROOTPW=$(gen_secret)
  # Determine storage for rootfs. Prefer local-lvm, otherwise pick first available storage.
  if [ -z "${VM_STORAGE:-}" ]; then
    if pvesm status 2>/dev/null | awk '{print $1}' | grep -qx "local-lvm"; then
      VM_STORAGE="local-lvm"
    else
      # pick first storage listed by pvesm (skip header)
      VM_STORAGE=$(pvesm status 2>/dev/null | awk 'NR>1 {print $1; exit}' || true)
    fi
  fi
  if [ -z "$VM_STORAGE" ]; then
    log "No storage could be detected for container rootfs. Please specify VM_STORAGE in the script (e.g. local-lvm)."
    exit 1
  fi

  log "Using storage ${VM_STORAGE} for rootfs"
  pct create "$VM_VMID" "$TPL" --hostname "$VM_HOSTNAME" --cores $VM_CORES --memory $VM_MEM --rootfs ${VM_STORAGE}:${VM_DISK} --net0 name=eth0,bridge=vmbr0,ip=dhcp --password "$ROOTPW" || true
  pct start "$VM_VMID"

  log "Waiting for container to start"
  sleep 5

  # Run installer inside the container by invoking the same installer through curl
  if [ -n "${INSTALLER_URL}" ]; then
    log "Bootstrapping inside container via ${INSTALLER_URL}"
    pct exec "$VM_VMID" -- bash -lc "apt-get update && apt-get install -y curl ca-certificates && curl -fsSL ${INSTALLER_URL} | bash -s -- inside"
  else
    log "INSTALLER_URL not set; trying to fetch installer from the host copy"
    # If script is a local file, push it into container and run
    if [ -f "$0" ]; then
      pct push "$VM_VMID" "$0" /tmp/install.sh
      pct exec "$VM_VMID" -- bash -lc "bash /tmp/install.sh inside"
    else
      log "Cannot bootstrap installer inside container: no INSTALLER_URL and installer not a local file"
      exit 1
    fi
  fi
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
