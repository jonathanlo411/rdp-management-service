# API Reference

All execution endpoints will require the API key from the environment variable.

## Health Check (`/health`)
Check to see if the service is up.

```bash
curl -X GET http://<automation-runner-ip>:5000/health
```

## Shutdown (`/execute/shutdown`)
Turns off the target PC.

```bash
# Shutdown target named 'gaming_pc'
curl -X POST -H "Authorization: Bearer <API_KEY>" -H "Content-Type: application/json" \
  -d '{"target":"gaming_pc"}' http://<automation-runner-ip>:5000/execute/shutdown
```

## Reboot (`/execute/reboot`)
Restarts the target PC.

```bash
# Reboot target named 'office_pc'
curl -X POST -H "Authorization: Bearer <API_KEY>" -H "Content-Type: application/json" \
  -d '{"target":"office_pc"}' http://<automation-runner-ip>:5000/execute/reboot
```
