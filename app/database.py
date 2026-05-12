import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# PostgreSQL only.  docker-compose provides this value automatically.
# For manual local execution, run PostgreSQL first and set DATABASE_URL if needed.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://oj:ojpass@localhost:5432/ojdb",
)

if not DATABASE_URL.startswith("postgresql"):
    raise RuntimeError("이 프로젝트는 PostgreSQL 전용으로 구성되었습니다. DATABASE_URL을 postgresql+psycopg2://... 형식으로 설정하세요.")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
