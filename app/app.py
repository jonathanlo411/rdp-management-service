#!/usr/bin/env python3
import os
import sys
import time
from flask import Flask, jsonify, request, abort
from dotenv import load_dotenv
import importlib

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

@app.route('/execute/<action>', methods=['POST'])
def execute(action):
    if not authorize(request):
        return ('Unauthorized', 401)

    # load action module dynamically from actions package
    try:
        mod = importlib.import_module(f'actions.{action}')
    except Exception:
        return (f'Action not found: {action}', 404)

    if not hasattr(mod, 'execute'):
        return (f'Action {action} has no execute() function', 500)

    # parse json payload if any
    payload = request.get_json(silent=True) or {}
    # allow target via query string too
    if 'target' not in payload and 'target' in request.args:
        payload['target'] = request.args.get('target')

    try:
        result = mod.execute(payload)
        return jsonify({ 'result': result })
    except Exception as e:
        app.logger.exception('Action execution failed')
        return (f'Action failed: {e}', 500)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
