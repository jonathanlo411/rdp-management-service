import os
import subprocess
import time
from .. import config


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


def execute(payload=None):
    if payload is None:
        payload = {}

    target_name = payload.get('target')
    if not target_name:
        raise RuntimeError('Payload must include a "target" field')

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

    # Wait for desktop
    time.sleep(payload.get('wait', 12))

    # Reboot sequence: Win+X, U, R
    seq_cmds = [
        f'DISPLAY={display} xdotool key super+x',
        f'DISPLAY={display} xdotool key u',
        f'DISPLAY={display} xdotool key r',
    ]

    for cmd in seq_cmds:
        _run(cmd)
        time.sleep(1)

    time.sleep(5)
    try:
        p.terminate()
    except Exception:
        pass

    return { 'status': 'reboot_sequence_sent', 'target': target_name }
