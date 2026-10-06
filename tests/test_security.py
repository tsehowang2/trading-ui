import os
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import app as web
import db


def test_csrf_blocks_unprotected_writes(client):
    web.app.config['SECURITY_DISABLED_FOR_TESTS'] = False
    assert client.post('/api/profiles', json={'name': 'Unsafe'}).status_code == 403
    assert client.get('/settings').status_code == 200
    with client.session_transaction() as session:
        token = session['csrf_token']
    response = client.post('/api/profiles', json={'name': 'Safe'}, headers={'X-CSRF-Token': token})
    assert response.status_code == 201


def test_authentication_and_login(client, monkeypatch):
    web.app.config['SECURITY_DISABLED_FOR_TESTS'] = False
    monkeypatch.setenv('APP_PASSWORD_HASH', generate_password_hash('fixture-only-password'))
    assert client.get('/api/profiles').status_code == 401
    assert client.get('/login').status_code == 200
    with client.session_transaction() as session:
        token = session['csrf_token']
    response = client.post('/login', data={'password': 'fixture-only-password', 'csrf_token': token})
    assert response.status_code == 302
    assert client.get('/api/profiles').status_code == 200


def test_configured_deployment_fails_closed(client, monkeypatch):
    web.app.config['SECURITY_DISABLED_FOR_TESTS'] = False
    monkeypatch.delenv('APP_PASSWORD_HASH', raising=False)
    monkeypatch.setattr(db, 'DATABASE_URL', 'configured-but-not-connected')
    assert client.get('/api/profiles').status_code == 503


def test_database_read_error_is_not_empty_profiles(client, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_URL', 'configured-but-not-connected')
    with patch.object(db, 'get_connection', side_effect=RuntimeError('offline')):
        response = client.get('/api/profiles')
    assert response.status_code == 503
    assert 'error' in response.get_json()