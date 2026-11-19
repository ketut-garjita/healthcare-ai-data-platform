"""S3-compatible raw object storage adapter for PGSTY Silo."""

import threading
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError


class SiloObjectStore:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str) -> None:
        self.bucket = bucket
        self._bucket_ready = False
        self._bucket_lock = threading.Lock()
        if "://" not in endpoint:
            endpoint = f"http://{endpoint}"
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}),
        )

    def ensure_bucket(self) -> None:
        with self._bucket_lock:
            if self._bucket_ready:
                return
            try:
                self._client.head_bucket(Bucket=self.bucket)
            except ClientError as exc:
                if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                    raise
                self._client.create_bucket(Bucket=self.bucket)
            self._bucket_ready = True

    def put_raw(self, key: str, payload: bytes, content_hash: str) -> None:
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload,
                ContentType="application/fhir+json",
                Metadata={"sha256": content_hash},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"NoSuchBucket", "NotFound"} or status == 404:
                with self._bucket_lock:
                    self._bucket_ready = False
            raise
