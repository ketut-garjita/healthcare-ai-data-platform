"""Docker Compose host ports must remain local to the development machine."""

from pathlib import Path

import yaml


def test_all_published_compose_ports_bind_only_to_loopback() -> None:
    compose_path = Path(__file__).parents[2] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text())

    published = [
        port for service in compose["services"].values() for port in service.get("ports", [])
    ]
    assert published, "Compose must expose expected local development services"
    assert all(isinstance(port, str) for port in published)
    assert all(port.split(":", maxsplit=2)[0] == "127.0.0.1" for port in published)
