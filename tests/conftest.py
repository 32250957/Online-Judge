"""Use an isolated test DB; production DATABASE_URL is deliberately never reused."""
import importlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://unused/unused")
os.environ["SESSION_SECRET_KEY"] = "isolated-test-session-key"
from app import database, models, schema

test_url = os.environ.get("OJ_TEST_DATABASE_URL")
if test_url:
    if not test_url.startswith("postgresql"):
        raise RuntimeError("OJ_TEST_DATABASE_URL must refer to a disposable PostgreSQL database")
    test_engine = create_engine(test_url, connect_args={"options": "-c timezone=UTC"})
else:
    test_engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
database.engine = test_engine
database.SessionLocal = sessionmaker(bind=test_engine, autoflush=False)
if test_url:
    main = importlib.import_module("app.main")
else:
    with patch.object(schema, "ensure_postgresql_schema", lambda engine: database.Base.metadata.create_all(engine)):
        main = importlib.import_module("app.main")

main.templates.env.loader.searchpath = [str(Path(__file__).resolve().parents[1] / "app/templates")]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Template and static loading should still resolve to the checked-out source.
    monkeypatch.setattr(main, "BACKUP_DIR", tmp_path / "backups")
    if test_url:
        with test_engine.begin() as conn:
            names = ", ".join(f'"{table.name}"' for table in database.Base.metadata.sorted_tables)
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    else:
        database.Base.metadata.drop_all(test_engine)
        database.Base.metadata.create_all(test_engine)
    main.RANKING_CACHE.clear()
    session = database.SessionLocal()
    yield session
    session.close()


@pytest.fixture
def app_main():
    return main
