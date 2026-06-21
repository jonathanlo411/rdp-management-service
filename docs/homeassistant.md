Home Assistant integration example

Add to your `configuration.yaml`:

rest_command:
  gaming_pc_shutdown:
    url: "http://<automation-runner-ip>:5000/execute/shutdown"
    method: POST
    headers:
      Authorization: "Bearer !secret automation_runner_key"
    # Example body to pick the target from targets.json
    payload: '{"target":"gaming_pc"}'

Store the secret in `secrets.yaml`:

automation_runner_key: YOUR_API_KEY_FROM__ENV
