"""Network policy: localhost (tailscale serve) → all, LAN → UI only, otherwise 403."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def enforce(monkeypatch, settings):
    monkeypatch.setattr(settings, "enforce_client_networks", True)
    monkeypatch.setattr(settings, "lan_ui_networks", "192.168.1.0/24")
    return settings


def get(ip: str, path: str, **headers):
    with TestClient(app, client=(ip, 50000)) as c:
        return c.get(path, headers=headers, follow_redirects=False)


def test_localhost_gets_everything(enforce):
    assert get("127.0.0.1", "/login").status_code == 200
    assert get("127.0.0.1", "/api/v1/agents/me").status_code == 503  # Setup mode, but not blocked.
    assert get("::1", "/login").status_code == 200


def test_lan_gets_ui_but_not_api(enforce):
    assert get("192.168.1.50", "/login").status_code == 200
    assert get("192.168.1.50", "/healthz").status_code == 503  # Reachable; only the DB is missing.
    r = get("192.168.1.50", "/api/v1/agents/me")
    assert r.status_code == 403 and r.json()["error"]["code"] == "lan_api_forbidden"


@pytest.mark.parametrize("ip", ["172.17.0.2", "100.91.1.5", "10.0.0.8", "8.8.8.8", "testclient"])
def test_other_networks_blocked(enforce, ip):
    """Docker bridges, direct Tailnet IP bypassing tailscale serve, and other networks."""
    assert get(ip, "/login").status_code == 403


def test_lan_disabled_by_default(enforce, monkeypatch):
    monkeypatch.setattr(enforce, "lan_ui_networks", "")
    assert get("192.168.1.50", "/login").status_code == 403


def test_forged_tailscale_header_from_lan_ignored(enforce, monkeypatch):
    monkeypatch.setattr(enforce, "ui_allowed_logins", "admin@test")
    r = get("192.168.1.50", "/admin/database", **{"Tailscale-User-Login": "admin@test"})
    assert r.status_code == 303 and r.headers["location"] == "/login"


# --- API_NETWORKS (agents in the LAN or on a Docker host without Tailscale) ---------------

@pytest.fixture
def api_lan(enforce, monkeypatch):
    monkeypatch.setattr(enforce, "api_networks", "192.168.1.0/28")
    return enforce


def test_api_network_gets_ui_and_api(api_lan):
    assert get("192.168.1.5", "/login").status_code == 200
    assert get("192.168.1.5", "/api/v1/agents/me").status_code == 503  # Setup mode, but not blocked.


def test_api_networks_empty_by_default(enforce):
    assert enforce.api_networks == ""
    assert get("192.168.1.5", "/api/v1/agents/me").json()["error"]["code"] == "lan_api_forbidden"


def test_lan_outside_api_networks_keeps_ui_only(api_lan):
    r = get("192.168.1.50", "/api/v1/agents/me")
    assert r.status_code == 403 and r.json()["error"]["code"] == "lan_api_forbidden"
    assert get("10.0.0.8", "/api/v1/agents/me").status_code == 403


def test_api_network_without_lan_ui(api_lan, monkeypatch):
    monkeypatch.setattr(api_lan, "lan_ui_networks", "")
    assert get("192.168.1.5", "/api/v1/agents/me").status_code == 503
    assert get("192.168.1.50", "/login").status_code == 403


def test_api_network_is_not_trusted(api_lan, monkeypatch):
    """API access does not extend to the Tailscale header or git credentials."""
    from starlette.requests import Request

    from app.api.agents import _secure_channel

    monkeypatch.setattr(api_lan, "ui_allowed_logins", "admin@test")
    r = get("192.168.1.5", "/admin/database", **{"Tailscale-User-Login": "admin@test"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert api_lan.is_api_peer("192.168.1.5") and not api_lan.is_trusted_peer("192.168.1.5")
    request = Request({"type": "http", "client": ("192.168.1.5", 50000), "headers": []})
    assert _secure_channel(request) is False


@pytest.mark.parametrize("host,allowed", [("192.168.1.1", True), ("192.168.1.15", True), ("192.168.1.16", False),
                                          ("127.0.0.1", False), ("testclient", False), ("", False)])
def test_is_api_peer(api_lan, host, allowed):
    assert api_lan.is_api_peer(host) is allowed


@pytest.mark.parametrize("field", ["api_networks", "lan_ui_networks", "trusted_peer_networks"])
@pytest.mark.parametrize("value", ["192.168.1.0/33", "not-a-network", "0.0.0.0/0", "10.0.0.0/8, ::/0"])
def test_invalid_networks_rejected_at_startup(field, value):
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_networks_parsed_from_environment(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("API_NETWORKS", "192.168.1.0/24, 10.0.0.5")
    s = Settings(_env_file=None)
    assert [str(n) for n in s.api_client_networks] == ["192.168.1.0/24", "10.0.0.5/32"]
    assert s.is_api_peer("10.0.0.5") and not s.is_api_peer("10.0.0.6")
