"""Keep controller identity headers behind the dedicated Authelia proxy path."""

from pathlib import Path

import yaml


def test_compose_isolates_and_sanitizes_proxy_identity() -> None:
    """Only Browser Use ingress services join the dedicated proxy network."""
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    services = compose["services"]
    networks = compose["networks"]
    assert networks["default"]["internal"] is True
    assert networks["browser_use_proxy"]["external"] is True
    assert "traefik_proxy" not in networks
    assert all("ipam" not in network for network in networks.values())

    ingress = {"controller": "browser-use", "novnc": "browser-use-vnc"}
    for name, service in services.items():
        attached = service.get("networks", [])
        assert "traefik_proxy" not in attached
        assert ("browser_use_proxy" in attached) == (name in ingress)
        if isinstance(attached, dict):
            for options in attached.values():
                assert not {"ipv4_address", "ipv6_address"}.intersection(options or {})

    for name, router in ingress.items():
        service = services[name]
        assert set(service["networks"]) == {"browser_use_proxy", "default"}
        assert not service.get("ports")
        labels = dict(label.split("=", 1) for label in service["labels"])
        assert labels["traefik.docker.network"] == "browser_use_proxy"
        middlewares = labels[f"traefik.http.routers.{router}.middlewares"].split(",")
        assert middlewares.index("browser-use-strip-identity@docker") < middlewares.index(
            "authelia@docker"
        )

    labels = dict(label.split("=", 1) for label in services["controller"]["labels"])
    prefix = "traefik.http.middlewares.browser-use-strip-identity.headers.customrequestheaders"
    for header in ("Remote-User", "Remote-Groups", "Remote-Name", "Remote-Email"):
        assert labels[f"{prefix}.{header}"] == ""
