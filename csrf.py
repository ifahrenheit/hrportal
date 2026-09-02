import secrets
from flask import session, request


def get_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(16)
    return session['_csrf_token']


def validate_csrf():
    token = session.get('_csrf_token')
    submitted = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token', '')
    return bool(token) and secrets.compare_digest(token, submitted)
