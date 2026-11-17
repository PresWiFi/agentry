import pytest
from fastapi import HTTPException

from agentry import auth, db
from agentry.config import settings


def test_group_mapping(monkeypatch):
    db.conn()
    monkeypatch.setattr(settings, "oidc_admin_groups", ["it-admins"])
    monkeypatch.setattr(settings, "oidc_reviewer_groups", ["helpdesk"])
    monkeypatch.setattr(settings, "oidc_default_role", "none")
    u = auth.upsert_sso_user({"iss": "https://idp", "sub": "a1", "email": "a@x.com", "groups": ["helpdesk"]})
    assert u["role"] == "reviewer"
    u = auth.upsert_sso_user({"iss": "https://idp", "sub": "a1", "email": "a@x.com", "groups": ["it-admins", "helpdesk"]})
    assert u["role"] == "admin"
    with pytest.raises(HTTPException) as e:
        auth.upsert_sso_user({"iss": "https://idp", "sub": "a2", "email": "b@x.com", "groups": ["sales"]})
    assert e.value.status_code == 403


def test_allowed_domains(monkeypatch):
    db.conn()
    monkeypatch.setattr(settings, "oidc_allowed_domains", ["corp.com"])
    monkeypatch.setattr(settings, "oidc_default_role", "viewer")
    with pytest.raises(HTTPException):
        auth.upsert_sso_user({"iss": "https://idp", "sub": "z", "email": "x@gmail.com"})
    assert auth.upsert_sso_user({"iss": "https://idp", "sub": "y", "email": "x@corp.com"})["role"] == "viewer"
