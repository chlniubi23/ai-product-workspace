import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.schema import CreateColumn

from .config import settings

DATABASE_URL = settings.resolved_database_url
logger = logging.getLogger(__name__)


def _quote_name(engine: Engine, name: str) -> str:
    return engine.dialect.identifier_preparer.quote(name)


def _repair_missing_columns(target_engine: Engine) -> None:
    """Add columns that exist in the models but not in a pre-existing table.

    ``create_all`` only creates missing tables; it never touches existing ones.
    A database created before a migration therefore boots fine and then fails
    on the first INSERT referencing the new column.  This safety net keeps a
    development database aligned with the models without hand-running
    migrations.  Columns are added as their model definition (type, default,
    nullability); foreign keys are intentionally omitted -- referential
    integrity for dev data matters less than booting at all.
    """

    from . import models  # noqa: F401  (populate Base.metadata)

    inspector = inspect(target_engine)
    added: list[str] = []
    with target_engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = str(CreateColumn(column).compile(dialect=target_engine.dialect))
                connection.execute(text(f"ALTER TABLE {_quote_name(target_engine, table.name)} ADD COLUMN {ddl}"))
                added.append(f"{table.name}.{column.name}")
    if added:
        logger.warning("Added missing columns to existing tables: %s", ", ".join(added))


def _build_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    database_engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    if database_url.startswith("sqlite"):
        @event.listens_for(database_engine, "connect")
        def _sqlite_fk(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return database_engine


# Keep the configured connector separate from the active connector.  When an
# operator explicitly opts into a temporary SQLite fallback, ``init_db`` must
# still be able to retry the original MySQL engine after it is repaired.
configured_engine = _build_engine(DATABASE_URL)
engine = configured_engine
USING_FALLBACK_SQLITE = False
# The exception is kept as a short, redacted string for readiness diagnostics.
# It is deliberately not exposed in API responses because connector errors may
# include credentials or local filesystem paths.
DATABASE_CONNECTION_ERROR: str | None = None


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    global USING_FALLBACK_SQLITE, DATABASE_CONNECTION_ERROR, engine
    from . import models  # noqa: F401

    try:
        # Connect before DDL so an invalid URL/credential is detected during
        # startup rather than on the first user request.
        with configured_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        Base.metadata.create_all(bind=configured_engine)
        _repair_missing_columns(configured_engine)
        engine = configured_engine
        SessionLocal.configure(bind=configured_engine)
        DATABASE_CONNECTION_ERROR = None
        USING_FALLBACK_SQLITE = False
    except Exception as exc:
        DATABASE_CONNECTION_ERROR = f"{exc.__class__.__name__}: {str(exc)[:240]}"
        # SQLite is a valid explicitly configured backend and is never called a
        # fallback.  For network databases, fallback requires an explicit opt-in
        # so a typo cannot silently split data across two stores.
        if DATABASE_URL.startswith("sqlite") or not settings.allow_sqlite_fallback:
            engine = configured_engine
            SessionLocal.configure(bind=configured_engine)
            USING_FALLBACK_SQLITE = False
            safe_url = configured_engine.url.render_as_string(hide_password=True)
            logger.error(
                "DATABASE NOT READY: unable to connect to %s (%s). "
                "Set ALLOW_SQLITE_FALLBACK=true only for disposable local work.",
                safe_url,
                DATABASE_CONNECTION_ERROR,
            )
            if settings.app_env.lower() in {"production", "prod"}:
                raise
            # Development/test servers stay up so /health/ready can report a
            # truthful 503 and the operator can fix the connection in place.
            return
        fallback_url = f"sqlite:///{(settings.data_path / 'app.db').as_posix()}"
        fallback_engine = _build_engine(fallback_url)
        Base.metadata.create_all(bind=fallback_engine)
        SessionLocal.configure(bind=fallback_engine)
        engine = fallback_engine
        USING_FALLBACK_SQLITE = True
        logger.warning(
            "!!! DATABASE FALLBACK ENABLED !!! configured database is unavailable; "
            "using disposable SQLite at %s. This instance is NOT ready for normal use.",
            settings.data_path / "app.db",
        )


def database_ready() -> bool:
    """Return whether the configured database is reachable and not a fallback."""

    if USING_FALLBACK_SQLITE:
        return False
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
