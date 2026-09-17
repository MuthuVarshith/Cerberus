# Reproduction test previously hardcoded in main.py for the VoteVault export_votes bug.
# Requires a VoteVault checkout (app.py, models.py); that repository is not part of Cerberus.
import pytest
from app import app
from models import db


def test_export_votes_returns_200():
    with app.app_context():
        db.create_all()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['is_admin'] = True
    resp = client.get('/export_votes')
    assert resp.status_code == 200, f'Expected 200 OK, got {resp.status_code}'
