import os
import json

TARGETS_PATH = os.getenv('TARGETS_PATH', '/opt/automation-runner/targets.json')

def load_targets():
    if not os.path.exists(TARGETS_PATH):
        return {}
    try:
        with open(TARGETS_PATH, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}

def get_target(name):
    targets = load_targets()
    return targets.get(name)
