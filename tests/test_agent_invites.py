from flask import Flask

import register as registration


class _Users:
    def __init__(self):
        self.inserted = None

    def find_one(self, query, projection=None):
        if "username" in query:
            return None
        return {"_id": registration.ObjectId("507f1f77bcf86cd799439011"), "role": "manager", "name": "Manager", "branch": "Accra"}

    def insert_one(self, user):
        self.inserted = user


def _app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    registration.bcrypt.init_app(app)
    return app


def test_signed_invite_resolves_manager(monkeypatch):
    users = _Users()
    monkeypatch.setattr(registration, "users_collection", users)
    app = _app()
    with app.app_context():
        token = registration._invite_serializer().dumps("507f1f77bcf86cd799439011")
        manager = registration._invite_manager(token)
    assert manager["name"] == "Manager"


def test_invited_agent_is_linked_to_manager_and_forced_branch(monkeypatch):
    users = _Users()
    monkeypatch.setattr(registration, "users_collection", users)
    app = _app()
    manager_id = registration.ObjectId("507f1f77bcf86cd799439011")
    with app.app_context():
        registration._create_agent(
            {"username": "newagent", "password": "secret1", "name": "New Agent", "branch": "Forged", "status": "Inactive"},
            manager_id,
            forced_branch="Accra",
        )
    assert users.inserted["manager_id"] == manager_id
    assert users.inserted["branch"] == "Accra"
    assert users.inserted["status"] == "Active"
