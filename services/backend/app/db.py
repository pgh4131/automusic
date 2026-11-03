from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from .models import Base, Production, Song, SongVersion


def backfill_song_versions(engine):
    """Preserve each pre-versioning production as V1 of its own song, once only."""
    insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
    with engine.begin() as connection:
        legacy = connection.execute(select(
            Production.id, Production.title, Production.created_at, Production.updated_at,
        ).outerjoin(SongVersion, Production.id == SongVersion.production_id).where(
            SongVersion.production_id.is_(None),
        )).mappings().all()
        for row in legacy:
            # Deterministic IDs and conflict handling also permit repeated startup.
            connection.execute(insert(Song).values(
                id=row["id"], title=row["title"], next_version=2,
                created_at=row["created_at"], updated_at=row["updated_at"],
            ).on_conflict_do_nothing(index_elements=["id"]))
            connection.execute(insert(SongVersion).values(
                production_id=row["id"], song_id=row["id"], version_no=1,
                parent_production_id=None,
            ).on_conflict_do_nothing(index_elements=["production_id"]))


def make_database(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {"connect_args": {"check_same_thread": False, "timeout": 15}} if settings.database_url.startswith("sqlite") else {}
    engine = create_engine(settings.database_url, **kwargs)
    if settings.database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def sqlite_pragmas(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    backfill_song_versions(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)
