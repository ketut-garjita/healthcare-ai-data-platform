from fastapi.testclient import TestClient

import healthcare_platform.main as main_module
from healthcare_platform.main import app

client = TestClient(app)


def test_readiness_returns_503_when_database_is_unavailable(monkeypatch) -> None:
    def fail_connect(*args, **kwargs):
        raise main_module.psycopg.OperationalError("database unavailable")

    monkeypatch.setattr(main_module.psycopg, "connect", fail_connect)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == "Database is not ready"
