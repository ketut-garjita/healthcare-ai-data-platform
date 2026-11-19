import psycopg
from fastapi import FastAPI, HTTPException

from healthcare_platform.config import settings

app = FastAPI(
    title="Healthcare AI Data Platform API",
    version="0.1.0",
    description="Development API for the Healthcare AI Data Platform portfolio project.",
)


@app.get("/health", tags=["Operations"])
def health() -> dict[str, str]:
    # Liveness probe: confirms that the API process is running.
    return {"status": "ok", "service": "healthcare-api"}


@app.get("/ready", tags=["Operations"])
def readiness() -> dict[str, str]:
    # Readiness probe: checks connectivity to PostgreSQL.
    try:
        with psycopg.connect(settings.database_url, connect_timeout=3) as connection:
            connection.execute("SELECT 1")
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="Database is not ready") from exc

    return {"status": "ready", "database": "connected"}
