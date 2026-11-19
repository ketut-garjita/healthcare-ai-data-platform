"""Retry durable interrupted HL7 ingestion attempts."""

import argparse
import logging

from healthcare_platform.config import settings
from healthcare_platform.ingestion.hl7 import Hl7IngestionService
from healthcare_platform.storage.silo import SiloObjectStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 1000:
        parser.error("--limit must be between 1 and 1000")
    logging.basicConfig(level=logging.INFO)
    store = SiloObjectStore(
        settings.silo_endpoint,
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = Hl7IngestionService(
        settings.database_url,
        store,
        settings.hl7_source_id,
        settings.hl7_supported_versions,
        settings.hl7_message_encoding,
        settings.hl7_reconciliation_max_retries,
    )
    recovered = service.reconcile_stale_attempts(args.limit)
    logging.info("HL7 reconciliation completed", extra={"attempts_retried": recovered})


if __name__ == "__main__":
    main()
