Examples and quick commands

1) One-liner installer:

```bash
curl -fsSL https://raw.githubusercontent.com/jonathanlo411/rdp-management-service/refs/heads/main/install.sh | bash
```

2) Run installer inside an existing Ubuntu container:

```bash
curl -fsSL https://raw.githubusercontent.com/jonathanlo411/rdp-management-service/refs/heads/main/install.sh | bash -s -- inside
```

3) Manual Proxmox `pct` example to create an LXC (change `101` to a free VMID):

```bash
pct create 101 ubuntu-24.04-standard --hostname automation-runner --cores 1 --memory 512 \
  --rootfs local:8 --net0 name=eth0,bridge=vmbr0,ip=dhcp --password "changeme"
pct start 101

# bootstrap the installer inside the LXC
pct exec 101 -- bash -lc "apt-get update && apt-get install -y curl && curl -fsSL https://raw.githubusercontent.com/jonathanlo411/rdp-management-service/refs/heads/main/install.sh | bash -s -- inside"
```

4) Triggering a shutdown or reboot from a machine (use the API key from `/opt/automation-runner/.env`):

```bash
# Shutdown target named 'gaming_pc'
curl -X POST -H "Authorization: Bearer <API_KEY>" -H "Content-Type: application/json" \
  -d '{"target":"gaming_pc"}' http://<automation-runner-ip>:5000/execute/shutdown

# Reboot target named 'office_pc'
curl -X POST -H "Authorization: Bearer <API_KEY>" -H "Content-Type: application/json" \
  -d '{"target":"office_pc"}' http://<automation-runner-ip>:5000/execute/reboot
```

5) Edit targets list:

```bash
sudo nano /opt/automation-runner/targets.json

# Example format:
{
  "gaming_pc": { "host": "192.168.1.100", "user": "myuser", "password": "mypassword" },
  "office_pc": { "host": "192.168.1.101", "user": "admin", "password": "secret" }
}
```
