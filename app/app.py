#!/usr/bin/env python3
import os
import sys
import time
from flask import Flask, jsonify, request, abort
from dotenv import load_dotenv
import subprocess
import shutil
import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.getenv('ENV_PATH') or os.path.join(BASE_DIR, '.env')

# Ensure local app directory is on path for action imports
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if not os.path.exists(ENV_PATH):
    alt_env = '/opt/automation-runner/.env'
    if os.path.exists(alt_env):
        ENV_PATH = alt_env

load_dotenv(ENV_PATH)

API_KEY = os.getenv('API_KEY')

app = Flask(__name__)
app.logger.info('Using ENV_PATH=%s TARGETS_PATH=%s', ENV_PATH, config.TARGETS_PATH)

def authorize(req):
    auth = req.headers.get('Authorization')
    if not auth or not auth.startswith('Bearer '):
        return False
    token = auth.split(' ', 1)[1].strip()
    return API_KEY is not None and token == API_KEY

@app.route('/health', methods=['GET'])
def health():
    return jsonify({ 'status': 'healthy' })

BIN_DIR = os.getenv('BIN_PATH', '')

def _resolve_binary(name):
    if BIN_DIR:
        candidate = os.path.join(BIN_DIR, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    resolved = shutil.which(name)
    if resolved:
        return resolved
    raise RuntimeError(
        f'Binary not found: {name}. Set BIN_PATH or install {name} in your PATH.'
    )

XVFB_BIN = _resolve_binary('Xvfb')
XFREERDP_BIN = _resolve_binary('xfreerdp')
XDOTOOL_BIN = _resolve_binary('xdotool')


def _build_subprocess_env():
    env = os.environ.copy()
    env['HOME'] = '/opt/automation-runner'
    env['XDG_CONFIG_HOME'] = '/opt/automation-runner/.config'
    if BIN_DIR:
        env['PATH'] = BIN_DIR + os.pathsep + env.get('PATH', '')
    return env


def _run(cmd, timeout=None, env=None):
    env = env or _build_subprocess_env()
    if isinstance(cmd, str):
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    else:
        proc = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    if proc.returncode != 0:
        app.logger.warning('_run failed: cmd=%r rc=%s out=%r err=%r', cmd, proc.returncode, out, err)
    return proc.returncode, out, err


def _start_xvfb(display=None):
    def try_start(display_value):
        env = _build_subprocess_env()
        env['DISPLAY'] = display_value
        app.logger.warning('Starting Xvfb: %s display=%s', XVFB_BIN, display_value)
        proc = subprocess.Popen(
            [XVFB_BIN, display_value, '-screen', '0', '1024x768x24'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        time.sleep(1)
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=1)
            raise RuntimeError(f'Xvfb failed to start: rc={proc.returncode} out={out!r} err={err!r}')
        return proc

    if display:
        return try_start(display), display

    for idx in range(99, 110):
        display_value = f':{idx}'
        lock_path = f'/tmp/.X{idx}-lock'
        if os.path.exists(lock_path):
            continue
        try:
            return try_start(display_value), display_value
        except RuntimeError:
            continue

    raise RuntimeError('No free X display found for Xvfb')


def _start_rdp(display, host, user, password):
    env = _build_subprocess_env()
    env['DISPLAY'] = display
    args = [
        XFREERDP_BIN,
        '/title:automation-runner',
        f'/v:{host}',
        f'/u:{user}',
        f'/p:{password}',
        '/cert:ignore',
        '/dynamic-resolution',
        '+clipboard',
        '/audio-mode:0',
        '/log-level:trace',
    ]
    app.logger.warning('Starting RDP connection to host=%s user=%s', host, user)
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    time.sleep(2)
    if proc.poll() is not None:
        out, err = proc.communicate(timeout=1)
        raise RuntimeError(f'xfreerdp failed to start: rc={proc.returncode} out={out!r} err={err!r}')
    return proc


def _find_rdp_window(display, timeout=15):
    env = _build_subprocess_env()
    env['DISPLAY'] = display

    def search_by_title():
        rc, out, err = _run([XDOTOOL_BIN, 'search', '--name', 'automation-runner'], env=env)
        if rc == 0 and out.strip():
            return out.strip().splitlines()[0]
        return None

    def search_by_geometry():
        rc, out, err = _run([XDOTOOL_BIN, 'search', '--all', '--name', '.*'], env=env)
        if rc != 0 or not out.strip():
            return None

        window_ids = [line.strip() for line in out.splitlines() if line.strip()]
        for wid in window_ids:
            rc2, geo_out, geo_err = _run([XDOTOOL_BIN, 'getwindowgeometry', '--shell', wid], env=env)
            if rc2 != 0:
                continue
            geom = {k: v for k, v in (line.split('=', 1) for line in geo_out.splitlines() if '=' in line)}
            if geom.get('X') == '0' and geom.get('Y') == '0' and geom.get('WIDTH') == '1024' and geom.get('HEIGHT') == '768':
                return wid
        return window_ids[0] if window_ids else None

    deadline = time.time() + timeout
    while time.time() < deadline:
        window_id = search_by_title()
        if window_id:
            return window_id

        window_id = search_by_geometry()
        if window_id:
            app.logger.warning('Falling back to RDP window by geometry/first window: %s', window_id)
            return window_id

        time.sleep(0.5)

    raise RuntimeError('Timed out waiting for the RDP window')


def _execute_sequence(payload, seq_keys, typed_command=None):
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

    display = payload.get('display')

    xvfb_proc, display = _start_xvfb(display)
    p = _start_rdp(display, host, user, password)

    # Wait for desktop to appear and the RDP window to be available
    time.sleep(payload.get('wait', 15))
    window_id = _find_rdp_window(display, timeout=15)
    if not window_id:
        raise RuntimeError('Unable to determine RDP window ID')

    env = os.environ.copy()
    env['DISPLAY'] = display
    rc, out, err = _run([XDOTOOL_BIN, 'windowactivate', '--sync', window_id], env=env)
    if rc != 0:
        app.logger.warning('windowactivate failed: rc=%s out=%r err=%r', rc, out, err)

    time.sleep(2)

    for key in seq_keys:
        rc, out, err = _run([XDOTOOL_BIN, 'key', '--window', window_id, '--clearmodifiers', key], env=env)
        if rc != 0:
            app.logger.warning('xdotool key failed: key=%s rc=%s out=%r err=%r', key, rc, out, err)
        time.sleep(1.5)

    if typed_command:
        rc, out, err = _run([XDOTOOL_BIN, 'type', '--window', window_id, '--clearmodifiers', '--delay', '100', typed_command], env=env)
        if rc != 0:
            app.logger.warning('xdotool type failed: cmd=%s rc=%s out=%r err=%r', typed_command, rc, out, err)
        time.sleep(1.5)
        _run([XDOTOOL_BIN, 'key', '--window', window_id, 'Return'], env=env)
        time.sleep(1)

    time.sleep(5)
    try:
        p.terminate()
    except Exception:
        pass
    try:
        xvfb_proc.terminate()
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
        result = _execute_sequence(payload, ['super+r'], 'shutdown /s /t 0')
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
        result = _execute_sequence(payload, ['super+r'], 'shutdown /r /t 0')
        return jsonify({ 'result': result })
    except Exception as e:
        app.logger.exception('Reboot action failed')
        return (f'Action failed: {e}', 500)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
