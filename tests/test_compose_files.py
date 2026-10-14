"""Compose files: the Tailscale sidecar keeps the loopback trust model (docs/docker.md)."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> dict:
    yaml = pytest.importorskip("yaml")

    class Loader(yaml.SafeLoader):
        pass

    # Compose merge tags (`!reset`, `!override`): keep the value, drop the tag.
    Loader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_yaml_seq(node)
                                 if isinstance(node, yaml.SequenceNode) else
                                 loader.construct_mapping(node) if isinstance(node, yaml.MappingNode) else
                                 loader.construct_scalar(node))
    return yaml.load((ROOT / name).read_text(), Loader=Loader)


def test_sidecar_shares_namespace_with_app():
    services = _load("compose.tailscale.yaml")["services"]
    app = services["taskbrigade"]
    assert app["network_mode"] == "service:tailscale"
    assert not app["ports"] and not app["networks"]  # Reset: network_mode excludes both.
    sidecar = services["tailscale"]
    assert sidecar["networks"] == ["taskbrigade"]  # Reaches MariaDB on the stack network.
    assert sidecar["ports"] == _load("compose.yaml")["services"]["taskbrigade"]["ports"]
    assert sidecar["environment"]["TS_SERVE_CONFIG"] == "/config/serve.json"
    assert any(v.endswith(":/config/serve.json:ro") for v in sidecar["volumes"])


def test_serve_config_proxies_to_loopback_without_funnel():
    serve = json.loads((ROOT / "deploy/docker/tailscale-serve.json").read_text())
    assert serve["TCP"] == {"443": {"HTTPS": True}}
    (site, web), = serve["Web"].items()
    assert web["Handlers"]["/"]["Proxy"] == "http://127.0.0.1:8090"
    assert serve["AllowFunnel"] == {site: False}


def test_compose_trusts_only_the_gateway():
    env = _load("compose.yaml")["services"]["taskbrigade"]["environment"]
    assert env["TRUSTED_PEER_NETWORKS"].endswith(":-172.31.250.1/32}")
    assert "API_NETWORKS" not in env  # Opt-in through .env only.
