from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.database import Base

DATETIME_COLUMNS = [
    ("users", "submit_banned_until"), ("users", "created_at"),
    ("contests", "start_time"), ("contests", "end_time"), ("contests", "created_at"),
    ("submissions", "created_at"),
    ("contest_questions", "created_at"), ("contest_questions", "answered_at"),
    ("groups", "created_at"), ("group_members", "joined_at"),
    ("messages", "created_at"), ("group_join_requests", "created_at"),
    ("group_problem_sets", "created_at"), ("group_contests", "created_at"),
    ("group_practices", "start_time"), ("group_practices", "end_time"), ("group_practices", "created_at"),
    ("judge_logs", "created_at"),
    ("judge_jobs", "created_at"), ("judge_jobs", "started_at"), ("judge_jobs", "finished_at"),
    ("board_posts", "created_at"), ("board_posts", "updated_at"),
    ("board_comments", "created_at"),
    ("contest_editorials", "created_at"), ("contest_editorials", "updated_at"),
]


POSTGRES_INDEX_MIGRATIONS = [
    "CREATE INDEX IF NOT EXISTS ix_submissions_contest_id_id ON submissions (contest_id, id)",
    "CREATE INDEX IF NOT EXISTS ix_submissions_user_problem_contest ON submissions (user_id, problem_id, contest_id)",
    "CREATE INDEX IF NOT EXISTS ix_submissions_result ON submissions (result)",
    "CREATE INDEX IF NOT EXISTS ix_submissions_judge_status ON submissions (judge_status)",
    "CREATE INDEX IF NOT EXISTS ix_judge_jobs_status_priority_id ON judge_jobs (status, priority, id)",
    "CREATE INDEX IF NOT EXISTS ix_judge_logs_event_created_at ON judge_logs (event, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_contest_problems_contest_order ON contest_problems (contest_id, order_index)",
    "CREATE INDEX IF NOT EXISTS ix_problems_origin_review ON problems (origin_type, review_status)",
    "CREATE INDEX IF NOT EXISTS ix_group_members_user_id ON group_members (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_board_posts_scope_group_display ON board_posts (board_scope, group_id, display_number)",
]

POSTGRES_COLUMN_MIGRATIONS = [
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS force_private_submission BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS is_judge_ready BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS difficulty VARCHAR(50) NOT NULL DEFAULT '미지정'",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS tags VARCHAR(255) NOT NULL DEFAULT ''",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS source VARCHAR(200) NOT NULL DEFAULT ''",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS problem_author VARCHAR(200) NOT NULL DEFAULT ''",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS error_finder VARCHAR(200) NOT NULL DEFAULT ''",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS typo_finder VARCHAR(200) NOT NULL DEFAULT ''",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS allowed_languages VARCHAR(200) NOT NULL DEFAULT 'python,c,cpp,java'",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS origin_type VARCHAR(30) NOT NULL DEFAULT 'regular'",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS origin_group_id INTEGER",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS origin_contest_id INTEGER",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS review_status VARCHAR(30) NOT NULL DEFAULT 'none'",
    "ALTER TABLE problems ADD COLUMN IF NOT EXISTS display_code VARCHAR(50)",
    "ALTER TABLE contests ADD COLUMN IF NOT EXISTS display_number INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE contests ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE contests ADD COLUMN IF NOT EXISTS is_ended BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE contests ADD COLUMN IF NOT EXISTS is_exam_mode BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE contests ADD COLUMN IF NOT EXISTS hide_ranking BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE contests ADD COLUMN IF NOT EXISTS result_display_mode VARCHAR(30) NOT NULL DEFAULT 'full'",
    "ALTER TABLE contests ADD COLUMN IF NOT EXISTS score_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE contest_problems ADD COLUMN IF NOT EXISTS label VARCHAR(10) NOT NULL DEFAULT 'A'",
    "ALTER TABLE contest_problems ADD COLUMN IF NOT EXISTS order_index INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE contest_problems ADD COLUMN IF NOT EXISTS exclude_from_ranking BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE contest_problems ADD COLUMN IF NOT EXISTS score INTEGER NOT NULL DEFAULT 100",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_background_url VARCHAR(500) NOT NULL DEFAULT ''",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR(100) NOT NULL DEFAULT ''",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS student_id VARCHAR(50) NOT NULL DEFAULT ''",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS is_school_group BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS school_group_request_status VARCHAR(30) NOT NULL DEFAULT 'none'",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS school_group_request_reason TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS school_group_request_file_path VARCHAR(500) NOT NULL DEFAULT ''",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS school_group_request_file_name VARCHAR(255) NOT NULL DEFAULT ''",

    "ALTER TABLE group_contests ADD COLUMN IF NOT EXISTS display_number INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE group_contests ADD COLUMN IF NOT EXISTS contest_id INTEGER",
    "ALTER TABLE group_contests ADD COLUMN IF NOT EXISTS title VARCHAR(200) NOT NULL DEFAULT ''",
    "ALTER TABLE group_contests ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE group_practices ADD COLUMN IF NOT EXISTS title VARCHAR(200) NOT NULL DEFAULT ''",
    "ALTER TABLE group_practices ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE group_practices ADD COLUMN IF NOT EXISTS start_time TIMESTAMP WITHOUT TIME ZONE",
    "ALTER TABLE group_practices ADD COLUMN IF NOT EXISTS end_time TIMESTAMP WITHOUT TIME ZONE",
    "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS runtime_ms INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS memory_kb INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS visibility VARCHAR(30) NOT NULL DEFAULT 'private'",
    "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS practice_id INTEGER",
    "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS judge_status VARCHAR(30) NOT NULL DEFAULT 'PENDING'",
    "ALTER TABLE board_posts ADD COLUMN IF NOT EXISTS display_number INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE board_posts ADD COLUMN IF NOT EXISTS board_scope VARCHAR(30) NOT NULL DEFAULT 'site'",
    "ALTER TABLE board_posts ADD COLUMN IF NOT EXISTS board_type VARCHAR(30) NOT NULL DEFAULT 'notice'",
    "ALTER TABLE board_posts ADD COLUMN IF NOT EXISTS group_id INTEGER",
    "ALTER TABLE board_posts ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE board_posts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()",
    "ALTER TABLE board_comments ADD COLUMN IF NOT EXISTS post_id INTEGER",
    "ALTER TABLE board_comments ADD COLUMN IF NOT EXISTS author_id INTEGER",
    "ALTER TABLE board_comments ADD COLUMN IF NOT EXISTS content TEXT NOT NULL DEFAULT ''",
]


KNOWN_TABLE_COLUMNS = {
    "contests": {"id", "display_number", "title", "description", "start_time", "end_time", "is_public", "is_ended", "is_exam_mode", "hide_ranking", "result_display_mode", "score_enabled", "created_at"},
    "group_contests": {"id", "display_number", "group_id", "contest_id", "title", "description", "created_at"},
    "group_practices": {"id", "group_id", "title", "description", "start_time", "end_time", "created_at"},
    "groups": {"id", "name", "description", "owner_id", "is_public", "is_school_group", "school_group_request_status", "school_group_request_reason", "school_group_request_file_path", "school_group_request_file_name", "created_at"},
    "board_posts": {"id", "display_number", "board_scope", "board_type", "group_id", "author_id", "title", "content", "is_pinned", "created_at", "updated_at"},
    "problems": {"id", "title", "description", "input_description", "output_description", "time_limit", "memory_limit", "is_contest_only", "is_public", "force_private_submission", "is_judge_ready", "difficulty", "tags", "source", "problem_author", "error_finder", "typo_finder", "allowed_languages", "origin_type", "origin_group_id", "origin_contest_id", "review_status", "display_code"},
    "judge_jobs": {"id", "submission_id", "job_type", "status", "priority", "attempts", "worker_name", "error_message", "created_at", "started_at", "finished_at"},
}

SERIAL_TABLES = [
    "users", "problems", "contests", "problem_examples", "problem_notes", "problem_hints", "submissions", "contest_questions", "contest_editorials",
    "groups", "messages", "group_join_requests", "group_problem_sets", "group_contests",
    "group_practices", "group_problem_set_problems", "group_practice_problems", "judge_jobs", "judge_logs", "board_posts", "board_comments",
]


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _drop_unknown_not_null_columns(conn, table_name: str) -> None:
    """Keep older development volumes usable.

    During the prototype stage some tables may have had extra NOT NULL columns
    that are no longer present in the SQLAlchemy model.  PostgreSQL will reject
    new inserts if those columns have no default value.  The current app does not
    read those columns, so making only the *unknown* columns nullable is safer
    than failing contest/group-contest creation with a 500 error.
    """
    known = KNOWN_TABLE_COLUMNS.get(table_name, set())
    rows = conn.execute(text(
        "SELECT column_name, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :table_name"
    ), {"table_name": table_name}).fetchall()
    for column_name, is_nullable, column_default in rows:
        if column_name in known or column_name == "id":
            continue
        if is_nullable == "NO" and column_default is None:
            conn.execute(text(
                f"ALTER TABLE {_quote_ident(table_name)} ALTER COLUMN {_quote_ident(column_name)} DROP NOT NULL"
            ))


def _repair_serial_sequence(conn, table_name: str) -> None:
    seq_name = conn.execute(text("SELECT pg_get_serial_sequence(:table_name, 'id')"), {"table_name": table_name}).scalar()
    if not seq_name:
        return
    max_id = conn.execute(text(f"SELECT COALESCE(MAX(id), 0) FROM {_quote_ident(table_name)}")).scalar() or 0
    # setval(..., false) makes the next nextval return max_id + 1.
    conn.execute(text("SELECT setval(:seq_name, :next_value, false)"), {"seq_name": seq_name, "next_value": int(max_id) + 1})


def ensure_postgresql_schema(engine: Engine) -> None:
    """Create and patch the PostgreSQL schema used by the judge.

    The project intentionally uses PostgreSQL only.  This helper keeps older
    Docker volumes usable when columns were added during development.
    """
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for ddl in POSTGRES_COLUMN_MIGRATIONS:
            conn.execute(text(ddl))
        for ddl in POSTGRES_INDEX_MIGRATIONS:
            conn.execute(text(ddl))


        # Older development volumes may have kept group_contests.contest_id as NOT NULL.
        # Empty group contests are intentionally supported, so make the column nullable.
        conn.execute(text("ALTER TABLE group_contests ALTER COLUMN contest_id DROP NOT NULL"))
        conn.execute(text("ALTER TABLE group_contests ALTER COLUMN group_id DROP NOT NULL"))

        for table_name, column_name in DATETIME_COLUMNS:
            dtype = conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table_name AND column_name = :column_name"
            ), {"table_name": table_name, "column_name": column_name}).scalar()
            if dtype == "timestamp with time zone":
                conn.execute(text(
                    f"ALTER TABLE {table_name} ALTER COLUMN {column_name} "
                    "TYPE TIMESTAMP WITHOUT TIME ZONE USING "
                    f"{column_name} AT TIME ZONE 'Asia/Seoul'"
                ))

        for table_name in KNOWN_TABLE_COLUMNS:
            _drop_unknown_not_null_columns(conn, table_name)

        for table_name in SERIAL_TABLES:
            _repair_serial_sequence(conn, table_name)


        # Fill site contest display numbers separately from group contest display numbers.
        conn.execute(text("""
            UPDATE contests c
            SET display_number = ranked.rn
            FROM (
                SELECT c2.id, ROW_NUMBER() OVER (ORDER BY c2.id) AS rn
                FROM contests c2
                WHERE NOT EXISTS (SELECT 1 FROM group_contests gc WHERE gc.contest_id = c2.id)
            ) ranked
            WHERE c.id = ranked.id AND COALESCE(c.display_number, 0) = 0
        """))

        # Fill scoped display numbers so site/group numbers do not share the same visible sequence.
        group_rows = conn.execute(text("SELECT id FROM groups ORDER BY id")).fetchall()
        for row in group_rows:
            conn.execute(text("""
                UPDATE group_contests gc
                SET display_number = ranked.rn
                FROM (
                    SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn
                    FROM group_contests WHERE group_id = :group_id
                ) ranked
                WHERE gc.id = ranked.id AND gc.group_id = :group_id AND COALESCE(gc.display_number, 0) = 0
            """), {"group_id": row[0]})
            conn.execute(text("""
                UPDATE board_posts bp
                SET display_number = ranked.rn
                FROM (
                    SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn
                    FROM board_posts WHERE board_scope = 'group' AND group_id = :group_id
                ) ranked
                WHERE bp.id = ranked.id AND bp.group_id = :group_id AND COALESCE(bp.display_number, 0) = 0
            """), {"group_id": row[0]})
        conn.execute(text("""
            UPDATE board_posts bp
            SET display_number = ranked.rn
            FROM (
                SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn
                FROM board_posts WHERE board_scope = 'site'
            ) ranked
            WHERE bp.id = ranked.id AND bp.board_scope = 'site' AND COALESCE(bp.display_number, 0) = 0
        """))

        # keep labels deterministic for contest rows created before label/order migration
        contest_ids = [row[0] for row in conn.execute(text("SELECT DISTINCT contest_id FROM contest_problems ORDER BY contest_id")).fetchall()]
        for contest_id in contest_ids:
            rows = conn.execute(text(
                "SELECT problem_id FROM contest_problems "
                "WHERE contest_id = :contest_id ORDER BY order_index, problem_id"
            ), {"contest_id": contest_id}).fetchall()
            for index, row in enumerate(rows):
                label = _index_to_label(index)
                conn.execute(text(
                    "UPDATE contest_problems SET label = :label, order_index = :order_index "
                    "WHERE contest_id = :contest_id AND problem_id = :problem_id"
                ), {"label": label, "order_index": index, "contest_id": contest_id, "problem_id": row[0]})


def _index_to_label(index: int) -> str:
    label = ""
    n = index
    while True:
        label = chr(ord("A") + (n % 26)) + label
        n = n // 26 - 1
        if n < 0:
            return label
