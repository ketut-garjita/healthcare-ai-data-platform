from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_upgrade_migrations_match_fresh_initialization_sql() -> None:
    migrations = ROOT / "infrastructure/postgres/migrations"
    initialization = ROOT / "infrastructure/postgres/init"
    for migration in migrations.glob("*.sql"):
        init_script = initialization / migration.name
        assert init_script.exists(), f"missing fresh initialization SQL for {migration.name}"
        assert init_script.read_bytes() == migration.read_bytes()


def test_redundant_stale_index_is_removed_by_new_forward_migration() -> None:
    migration = ROOT / "infrastructure/postgres/migrations/007_remove_redundant_stale_index.sql"
    sql = migration.read_text()
    assert "DROP INDEX IF EXISTS idx_fhir_ingestion_attempts_stale" in sql
    assert "idx_fhir_ingestion_attempts_retryable" not in sql
