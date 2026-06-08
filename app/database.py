import os

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import declarative_base, sessionmaker

# DATABASE_URL remains supported for deployments that inject a complete secret URL.
# Otherwise use separate values so passwords containing URL-reserved characters are safe.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL:
    if not DATABASE_URL.startswith("postgresql"):
        raise RuntimeError("이 프로젝트는 PostgreSQL 전용으로 구성되었습니다. DATABASE_URL을 postgresql+psycopg2://... 형식으로 설정하세요.")
    database_target = DATABASE_URL
else:
    database_password = os.getenv("DB_PASSWORD", "")
    if not database_password:
        raise RuntimeError("DATABASE_URL 또는 DB_PASSWORD를 설정해야 합니다.")
    database_target = URL.create(
        "postgresql+psycopg2",
        username=os.getenv("DB_USER", "oj"),
        password=database_password,
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "ojdb"),
    )

engine = create_engine(database_target, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
