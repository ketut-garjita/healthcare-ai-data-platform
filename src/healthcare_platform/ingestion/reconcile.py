"""Command-line recovery for stale FHIR ingestion attempts."""

import argparse
import json
import sys

from healthcare_platform.config import settings
from healthcare_platform.ingestion.fhir import FhirIngestionService
from healthcare_platform.storage.silo import SiloObjectStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--max-retries", type=int, default=5)
    args = parser.parse_args()
    store = SiloObjectStore(
        settings.silo_endpoint,
        settings.silo_access_key,
        settings.silo_secret_key,
        settings.silo_bucket_raw,
    )
    service = FhirIngestionService(settings.database_url, store, settings.fhir_base_url)
    try:
        results = service.reconcile_stale_attempts(args.limit, args.max_retries)
    except Exception:
        print("FHIR reconciliation could not complete", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(results, separators=(",", ":")))


if __name__ == "__main__":
    main()
