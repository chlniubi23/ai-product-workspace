from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parents[1]


class Settings(BaseSettings):
    app_env: str = "development"
    app_secret_key: str = "replace-with-a-strong-secret"
    api_cors_origins: str = "http://localhost:3000"
    database_url: str = "sqlite:///./data/app.db"
    # SQLite fallback is intentionally opt-in.  A failed MySQL connection must
    # be visible to operators instead of silently creating a second database.
    allow_sqlite_fallback: bool = False
    data_root: str = "./data"
    max_upload_size_mb: int = Field(default=50, ge=1)
    max_rows_per_dataset: int = Field(default=100000, ge=1)
    max_columns_per_dataset: int = Field(default=50, ge=1)
    jwt_access_token_minutes: int = Field(default=60, ge=5)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_seconds: int = Field(default=60, ge=1)
    deepseek_max_retries: int = Field(default=2, ge=0)

    model_config = SettingsConfigDict(
        env_file=(API_ROOT / ".env", REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.api_cors_origins.split(",") if origin.strip()]

    @property
    def data_path(self) -> Path:
        path = Path(self.data_root).expanduser()
        if not path.is_absolute():
            # Runtime paths must not depend on the shell directory used to
            # launch Uvicorn. Otherwise a restart can silently select another
            # SQLite database and make existing credentials appear invalid.
            path = API_ROOT / path
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        for child in ("uploads", "processed", "exports"):
            (path / child).mkdir(parents=True, exist_ok=True)
        return path

    @property
    def resolved_database_url(self) -> str:
        """Return a stable URL for local SQLite files.

        SQLAlchemy resolves relative SQLite paths against ``Path.cwd()``. The
        API is commonly launched both from the repository root and from
        ``apps/api``, so anchor relative database paths to the API directory.
        Network database URLs and SQLite in-memory/file URIs are unchanged.
        """

        url = make_url(self.database_url)
        if url.get_backend_name() != "sqlite" or not url.database:
            return self.database_url
        if url.database == ":memory:" or url.database.startswith("file:"):
            return self.database_url

        database_path = Path(url.database).expanduser()
        if not database_path.is_absolute():
            database_path = API_ROOT / database_path
        database_path = database_path.resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        return url.set(database=database_path.as_posix()).render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
