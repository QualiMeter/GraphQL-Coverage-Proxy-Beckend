import os


def database_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://coverage:coverage_dev@localhost:5432/coverage",
    )


def cors_origins() -> list[str]:
    value = os.environ.get("CORS_ORIGINS", "http://localhost:5173")
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def target_graphql_url() -> str:
    return os.environ.get("TARGET_GRAPHQL_URL", "").strip()
