# automation-runner

Automated RDP-based runner designed to be deployed inside a Proxmox LXC. Provides a small Flask REST API for Home Assistant to trigger actions such as a Windows shutdown via automated RDP.

See `/docs` for example integrations on how to call the service as well as how to integrate with HomeAssistant.

## Quickstart
1) Run the installer one-liner in your Proxmox shell:
```bash
curl -fsSL https://raw.githubusercontent.com/jonathanlo411/rdp-management-service/refs/heads/main/install.sh | bash
```
2) After installation, shell into your LXC via `pct enter <ID>` and edit `/opt/automation-runner/targets.json` to add your PC.
3. Get your API key via `cat /opt/automation-runner/.env`.