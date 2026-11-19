"""Runnable local MLLP listener for HL7 v2 ingestion."""

import asyncio
import logging
from collections.abc import Mapping

from healthcare_platform.config import settings
from healthcare_platform.ingestion.hl7 import Hl7IngestionService
from healthcare_platform.integrations.hl7.ack import build_ack
from healthcare_platform.integrations.hl7.mllp import MllpServer
from healthcare_platform.storage.silo import SiloObjectStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def ack_code_for_result(result: Mapping[str, object]) -> str:
    outcome = result.get("outcome")
    if outcome in {"accepted", "unchanged"}:
        return "AA"
    if outcome in {"conflict", "rejected"}:
        return "AR"
    return "AE"


def _service() -> Hl7IngestionService:
    store = SiloObjectStore(
        settings.silo_endpoint,
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    return Hl7IngestionService(
        settings.database_url,
        store,
        settings.hl7_source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        settings.hl7_reconciliation_max_retries,
    )


async def run() -> None:
    service = _service()

    async def handle(payload: bytes) -> bytes:
        result = await asyncio.to_thread(service.ingest, payload)
        outcome = result.get("outcome")
        logger.info(
            "HL7 message processed",
            extra={
                "status": result.get("status"),
                "outcome": outcome,
                "attempt_id": result.get("attempt_id"),
            },
        )
        return build_ack(
            payload, ack_code_for_result(result), encoding=settings.hl7_message_encoding
        )

    server = MllpServer(
        handle,
        host=settings.hl7_mllp_bind_host,
        port=settings.hl7_mllp_port,
        max_message_bytes=settings.hl7_message_max_bytes,
        connection_timeout=settings.hl7_connection_timeout_seconds,
        read_timeout=settings.hl7_read_timeout_seconds,
        max_connections=settings.hl7_max_connections,
    )
    await server.start()
    logger.info(
        "HL7 MLLP listener started",
        extra={"host": settings.hl7_mllp_bind_host, "port": settings.hl7_mllp_port},
    )
    try:
        await server.serve_forever()
    finally:
        await server.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
