#!/usr/bin/env python3
"""
automation-runner — Flask API that triggers RDP-based automation on Windows targets.
Rebuilt to mirror the working shell sequence exactly.
"""

import os
import subprocess
import time
import shutil
import json
import logging

from flask import Flask, jsonify, request
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(BASE_DIR, '..'))  # /opt/automation-runner

ENV_PATH = os.getenv('ENV_PATH', os.path.join(ROOT_DIR, '.env'))
TARGETS_PATH = os.getenv('TARGETS_PATH', os.path.join(ROOT_DIR, 'targets.json'))

load_dotenv(ENV_PATH)

API_KEY = os.getenv('API_KEY')

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Flask(__name__)
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')
log = app.logger


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def authorize(req):
    auth = req.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return False
    return API_KEY is not None and auth.split(' ', 1)[1].strip() == API_KEY


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def load_targets():
    if not os.path.exists(TARGETS_PATH):
        return {}
    with open(TARGETS_PATH) as f:
        return json.load(f)


def get_target(name):
    targets = load_targets()
    return targets.get(name)


# ---------------------------------------------------------------------------
# Core: run a shell command, capturing output, with a timeout
# ---------------------------------------------------------------------------

def run(cmd, timeout=30, env=None):
    """Run a command (list or string). Returns (returncode, stdout, stderr)."""
    if isinstance(cmd, list):
        log.debug('RUN: %s', ' '.join(str(c) for c in cmd))
    else:
        log.debug('RUN: %s', cmd)

    # Safety net: if caller forgot to pass env, build a minimal one with DISPLAY
    # so xdotool never sees DISPLAY=(null)
    if env is None:
        env = os.environ.copy()

    log.debug('  DISPLAY=%s', env.get('DISPLAY', '(not set)'))

    result = subprocess.run(
        cmd,
        shell=isinstance(cmd, str),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        log.warning('  rc=%s stdout=%r stderr=%r', result.returncode, result.stdout[:200], result.stderr[:200])
    else:
        log.debug('  rc=0 stdout=%r', result.stdout[:200])
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Find a free X display slot
# ---------------------------------------------------------------------------

def find_free_display(start=99, end=120):
    for n in range(start, end):
        if not os.path.exists(f'/tmp/.X{n}-lock'):
            return f':{n}'
    raise RuntimeError('No free X display slot found')


# ---------------------------------------------------------------------------
# The core sequence — mirrors the working shell script exactly
# ---------------------------------------------------------------------------

def rdp_sequence(host, user, password, key_sequence, typed_command=None, wait=5):
    """
    1. Start Xvfb on a free display
    2. Launch xfreerdp
    3. Wait for the desktop to appear
    4. Find the RDP window by title
    5. Send key sequence + optional typed command
    6. Tear everything down

    All subprocess calls receive the same explicit env with DISPLAY set,
    matching the manual shell script approach.
    """

    display = find_free_display()
    log.info('Using display %s', display)

    xvfb_bin     = shutil.which('Xvfb')
    xfreerdp_bin = shutil.which('xfreerdp')
    xdotool_bin  = shutil.which('xdotool')
    xdpyinfo_bin = shutil.which('xdpyinfo')

    for name, path in [('Xvfb', xvfb_bin), ('xfreerdp', xfreerdp_bin), ('xdotool', xdotool_bin)]:
        if not path:
            raise RuntimeError(f'{name} not found in PATH')

    # Build env once — every subprocess gets this exact dict.
    # Also stamp DISPLAY onto os.environ so that any code path that
    # accidentally omits env= still gets the right display.
    env = os.environ.copy()
    env['DISPLAY'] = display
    env['HOME'] = ROOT_DIR
    env['XDG_CONFIG_HOME'] = os.path.join(ROOT_DIR, '.config')
    os.environ['DISPLAY'] = display   # belt-and-suspenders

    log.info('env DISPLAY=%s HOME=%s', env['DISPLAY'], env['HOME'])

    # ------------------------------------------------------------------
    # Step 1: Start Xvfb
    # ------------------------------------------------------------------
    log.info('Starting Xvfb on %s', display)
    xvfb_proc = subprocess.Popen(
        [xvfb_bin, display, '-screen', '0', '1024x768x24', '-ac'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    time.sleep(2)
    if xvfb_proc.poll() is not None:
        raise RuntimeError(f'Xvfb failed to start (rc={xvfb_proc.returncode})')

    # Verify Xvfb is actually accepting connections before we go further
    if xdpyinfo_bin:
        rc, _, _ = run([xdpyinfo_bin, '-display', display], timeout=5, env=env)
        if rc != 0:
            raise RuntimeError(f'Xvfb started but display {display} is not accepting connections')
        log.info('Xvfb display %s verified with xdpyinfo', display)

    try:
        # ------------------------------------------------------------------
        # Step 2: Launch xfreerdp
        # ------------------------------------------------------------------
        log.info('Starting xfreerdp -> %s@%s', user, host)
        rdp_cmd = [
            xfreerdp_bin,
            '/title:automation-runner',
            f'/v:{host}',
            f'/u:{user}',
            f'/p:{password}',
            '/cert:ignore',
            '/dynamic-resolution',
            '+clipboard',
            '/audio-mode:0',
        ]
        rdp_proc = subprocess.Popen(
            rdp_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        time.sleep(2)
        if rdp_proc.poll() is not None:
            raise RuntimeError(f'xfreerdp exited immediately (rc={rdp_proc.returncode})')

        try:
            # ------------------------------------------------------------------
            # Step 3: Wait for the Windows desktop to load
            # ------------------------------------------------------------------
            log.info('Waiting %ss for Windows desktop...', wait)
            time.sleep(wait)

            # ------------------------------------------------------------------
            # Step 4: Find the RDP window
            # ------------------------------------------------------------------
            log.info('Searching for RDP window by title "automation-runner"')
            wid = None
            deadline = time.time() + 15
            while time.time() < deadline:
                rc, out, _ = run([xdotool_bin, 'search', '--name', 'automation-runner'], env=env)
                if rc == 0 and out.strip():
                    wid = out.strip().splitlines()[0]
                    break
                time.sleep(1)

            if not wid:
                # Fallback: grab whatever window exists
                rc, out, _ = run([xdotool_bin, 'search', '--name', '.*'], env=env)
                if rc == 0 and out.strip():
                    wid = out.strip().splitlines()[0]
                    log.warning('Title search failed; falling back to first window: %s', wid)

            if not wid:
                raise RuntimeError('Could not find any X window to interact with')

            # Log window name so we know exactly what we're targeting
            rc, name_out, _ = run([xdotool_bin, 'getwindowname', wid], env=env)
            log.info('Targeting window id=%s name=%r', wid, name_out.strip())

            # ------------------------------------------------------------------
            # Step 5: Activate window and send input
            # ------------------------------------------------------------------
            log.info('Activating window %s', wid)
            run([xdotool_bin, 'windowactivate', '--sync', wid], timeout=10, env=env)
            time.sleep(1)

            for key in key_sequence:
                log.info('Sending key: %s', key)
                run([xdotool_bin, 'key', '--window', wid, '--clearmodifiers', key], env=env)
                time.sleep(1)

            if typed_command:
                log.info('Typing command: %s', typed_command)
                run([xdotool_bin, 'type', '--window', wid, '--clearmodifiers', '--delay', '100', typed_command], env=env)
                time.sleep(1)
                run([xdotool_bin, 'key', '--window', wid, 'Return'], env=env)
                time.sleep(1)

            log.info('Sequence complete')
            time.sleep(3)

        finally:
            log.info('Terminating xfreerdp (pid=%s)', rdp_proc.pid)
            rdp_proc.terminate()
            try:
                rdp_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rdp_proc.kill()

    finally:
        log.info('Terminating Xvfb (pid=%s)', xvfb_proc.pid)
        xvfb_proc.terminate()
        try:
            xvfb_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            xvfb_proc.kill()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'})


def _handle_action(key_sequence, typed_command=None):
    if not authorize(request):
        return 'Unauthorized', 401

    payload = request.get_json(silent=True) or {}

    target_name = payload.get('target') or request.args.get('target')
    if not target_name:
        return 'Missing "target" in request body', 400

    tgt = get_target(target_name)
    if not tgt:
        return f'Target "{target_name}" not found in {TARGETS_PATH}', 404

    host     = tgt.get('host')
    user     = tgt.get('user')
    password = tgt.get('password')
    if not (host and user and password):
        return f'Target "{target_name}" is missing host, user, or password', 500

    wait = int(payload.get('wait', 15))

    try:
        rdp_sequence(host, user, password, key_sequence, typed_command, wait=wait)
        return jsonify({'status': 'ok', 'target': target_name, 'action': typed_command or key_sequence})
    except Exception as e:
        log.exception('Action failed')
        return f'Action failed: {e}', 500


@app.route('/execute/shutdown', methods=['POST'])
def execute_shutdown():
    return _handle_action(['super+r'], 'shutdown /s /t 0')


@app.route('/execute/reboot', methods=['POST'])
def execute_reboot():
    return _handle_action(['super+r'], 'shutdown /r /t 0')


# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=False)