# automation-runner

Automated RDP-based runner designed to be deployed inside a Proxmox LXC. Provides a small Flask REST API for Home Assistant to trigger actions such as a Windows shutdown via automated RDP.

See `docs/homeassistant.md` for Home Assistant integration examples.

Quick deploy examples

1) Run the installer one-liner (after hosting this repo and updating `install.sh` with your repo URL):

```bash
curl -fsSL jonathanlo411/rdp-management-service/install.sh | bash
```

2) If you prefer to create the LXC manually on a Proxmox host, example `pct` command:

```bash
# choose a free VMID (e.g. 101) and an available ubuntu-24.04 template on the host
pct create 101 ubuntu-24.04-standard --hostname automation-runner --cores 1 --memory 512 \
	--rootfs local:8 --net0 name=eth0,bridge=vmbr0,ip=dhcp --password "changeme"
pct start 101

# then run the installer inside the container (replace 101 with your vmid)
pct exec 101 -- bash -lc "apt-get update && apt-get install -y curl && curl -fsSL https://your.repo.url/install.sh | bash -s -- inside"
```

3) After installation, edit `/opt/automation-runner/.env` and `/opt/automation-runner/targets.json` to add secrets and targets.

