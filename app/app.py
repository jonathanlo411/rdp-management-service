#!/usr/bin/env python3
import os
import sys
import time
from flask import Flask, jsonify, request, abort
from dotenv import load_dotenv
import subprocess
import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = '/opt/automation-runner/.env'

# Ensure local app directory is on path for action imports
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

load_dotenv(ENV_PATH)

API_KEY = os.getenv('API_KEY')

app = Flask(__name__)

def authorize(req):
    auth = req.headers.get('Authorization')
    if not auth or not auth.startswith('Bearer '):
        return False
    token = auth.split(' ', 1)[1].strip()
    return API_KEY is not None and token == API_KEY

@app.route('/health', methods=['GET'])
def health():
    return jsonify({ 'status': 'healthy' })

def _run(cmd, timeout=None):
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return proc.returncode, out, err


def _start_xvfb(display=':99'):
    subprocess.Popen(f'Xvfb {display} -screen 0 1024x768x24 >/dev/null 2>&1 &', shell=True)


def _start_rdp(display, host, user, password):
    xfreerdp_cmd = (
        f'DISPLAY={display} xfreerdp /v:{host} /u:{user} /p:{password} '
        f'/cert-ignore /dynamic-resolution +clipboard /sound:off /microphone:off'
    )
    return subprocess.Popen(f'{xfreerdp_cmd} >/dev/null 2>&1 &', shell=True)


def _execute_sequence(payload, seq_keys):
    # payload should include 'target' (name) or fallback env vars
    payload = payload or {}
    target_name = payload.get('target')
    if not target_name:
        raise RuntimeError('Payload must include a "target" field')

    # try configured targets first
    tgt = config.get_target(target_name)
    if not tgt:
        host = os.getenv('WINDOWS_HOST')
        user = os.getenv('WINDOWS_USER')
        password = os.getenv('WINDOWS_PASSWORD')
        if not (host and user and password):
            raise RuntimeError(f'Target {target_name} not found and no fallback env vars set')
        tgt = { 'host': host, 'user': user, 'password': password }

    host = tgt.get('host')
    user = tgt.get('user')
    password = tgt.get('password')
    if not (host and user and password):
        raise RuntimeError(f'Target {target_name} missing host/user/password')

    display = payload.get('display', ':99')

    _start_xvfb(display)
    time.sleep(1)
    p = _start_rdp(display, host, user, password)

    # Wait for desktop to appear
    time.sleep(payload.get('wait', 12))

    for key in seq_keys:
        cmd = f'DISPLAY={display} xdotool key {key}'
        _run(cmd)
        time.sleep(1)

    time.sleep(5)
    try:
        p.terminate()
    except Exception:
        pass

    return { 'status': 'sequence_sent', 'target': target_name }


@app.route('/execute/shutdown', methods=['POST'])
def execute_shutdown():
    if not authorize(request):
        return ('Unauthorized', 401)
    payload = request.get_json(silent=True) or {}
    if 'target' not in payload and 'target' in request.args:
        payload['target'] = request.args.get('target')
    try:
        result = _execute_sequence(payload, ['super+x', 'u', 'u'])
        return jsonify({ 'result': result })
    except Exception as e:
        app.logger.exception('Shutdown action failed')
        return (f'Action failed: {e}', 500)


@app.route('/execute/reboot', methods=['POST'])
def execute_reboot():
    if not authorize(request):
        return ('Unauthorized', 401)
    payload = request.get_json(silent=True) or {}
    if 'target' not in payload and 'target' in request.args:
        payload['target'] = request.args.get('target')
    try:
        result = _execute_sequence(payload, ['super+x', 'u', 'r'])
        return jsonify({ 'result': result })
    except Exception as e:
        app.logger.exception('Reboot action failed')
        return (f'Action failed: {e}', 500)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
