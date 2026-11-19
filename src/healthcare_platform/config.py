from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "postgresql://healthcare_app:change_me_local_only@localhost:5432/healthcare"
    fhir_base_url: str = "http://localhost:8080/fhir"
    fhir_timeout_seconds: float = 10.0
    fhir_max_response_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    fhir_capability_cache_seconds: float = Field(default=60.0, ge=0.0, le=3600.0)
    fhir_search_max_pages: int = Field(default=10, ge=1, le=100)
    fhir_api_bearer_token: SecretStr | None = None
    hl7_source_id: str = "local-hl7-mllp"
    hl7_supported_versions: tuple[str, ...] = ("2.5.1",)
    hl7_mllp_bind_host: str = "127.0.0.1"
    hl7_mllp_port: int = Field(default=2575, ge=1, le=65535)
    hl7_message_max_bytes: int = Field(default=1024 * 1024, ge=1024, le=10 * 1024 * 1024)
    hl7_connection_timeout_seconds: float = Field(default=10.0, gt=0.0, le=300.0)
    hl7_read_timeout_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
    hl7_max_connections: int = Field(default=20, ge=1, le=500)
    hl7_reconciliation_max_retries: int = Field(default=3, ge=1, le=10)
    hl7_message_encoding: str = "utf-8"
    silo_endpoint: str = Field(
        default="http://localhost:9000",
        validation_alias=AliasChoices("SILO_ENDPOINT", "MINIO_ENDPOINT"),
    )
    silo_access_key: str = Field(
        default="local_minio_admin",
        validation_alias=AliasChoices("MINIO_ROOT_USER", "SILO_ACCESS_KEY"),
    )
    silo_secret_key: str = Field(
        default="change_me_minio_local_only",
        validation_alias=AliasChoices("MINIO_ROOT_PASSWORD", "SILO_SECRET_KEY"),
    )
    silo_bucket_raw: str = Field(
        default="healthcare-raw",
        validation_alias=AliasChoices("SILO_BUCKET_RAW", "MINIO_BUCKET_RAW"),
    )

    @field_validator("fhir_api_bearer_token", mode="before")
    @classmethod
    def empty_bearer_token_is_unconfigured(cls, value: object) -> object:
        return None if value == "" else value

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
