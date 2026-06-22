# Installation

There are a couple of flavors of how to install it, though the easiest is to use the provided installation script.

## One-liner installer:

```bash
curl -fsSL https://raw.githubusercontent.com/jonathanlo411/rdp-management-service/refs/heads/main/install.sh | bash
```


## Run installer inside an existing Ubuntu container:

```bash
curl -fsSL https://raw.githubusercontent.com/jonathanlo411/rdp-management-service/refs/heads/main/install.sh | bash -s -- inside
```

## Manual Proxmox `pct` example to create an LXC (change `101` to a free VMID):

```bash
pct create 101 ubuntu-24.04-standard --hostname automation-runner --cores 1 --memory 512 \
  --rootfs local:8 --net0 name=eth0,bridge=vmbr0,ip=dhcp --password "changeme"
pct start 101

# bootstrap the installer inside the LXC
pct exec 101 -- bash -lc "apt-get update && apt-get install -y curl && curl -fsSL https://raw.githubusercontent.com/jonathanlo411/rdp-management-service/refs/heads/main/install.sh | bash -s -- inside"
```
