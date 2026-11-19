from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = (
        "postgresql://healthcare_app:change_me_local_only@localhost:5432/healthcare"
    )
    fhir_base_url: str = "http://localhost:8080/fhir"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
