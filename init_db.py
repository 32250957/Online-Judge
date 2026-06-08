from __future__ import annotations

import json
import os
from pathlib import Path

from app.database import SessionLocal, engine
from app.models import User, Problem
from app.schema import ensure_postgresql_schema
from app.security import hash_password

ensure_postgresql_schema(engine)

db = SessionLocal()
try:
    bootstrap_username = os.getenv("OJ_BOOTSTRAP_ADMIN_USERNAME", "").strip()
    bootstrap_password = os.getenv("OJ_BOOTSTRAP_ADMIN_PASSWORD", "")
    if bool(bootstrap_username) != bool(bootstrap_password):
        raise RuntimeError("OJ_BOOTSTRAP_ADMIN_USERNAME and OJ_BOOTSTRAP_ADMIN_PASSWORD must be set together")
    if bootstrap_username:
        if len(bootstrap_password) < 12:
            raise RuntimeError("OJ_BOOTSTRAP_ADMIN_PASSWORD must be at least 12 characters")
        admin = db.query(User).filter(User.username == bootstrap_username).first()
        if admin is None:
            db.add(User(
                username=bootstrap_username,
                password_hash=hash_password(bootstrap_password),
                is_admin=True,
                must_change_password=True,
            ))
            print(f"Bootstrap admin account created: {bootstrap_username} (password change required)")

    problems_path = Path("problems")
    if problems_path.exists():
        for problem_dir in problems_path.iterdir():
            meta_file = problem_dir / "meta.json"
            if not meta_file.exists():
                continue

            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            problem = db.query(Problem).filter(Problem.id == meta["id"]).first()

            if problem is None:
                problem = Problem(
                    id=meta["id"], title=meta["title"], description=meta["description"],
                    input_description=meta["input_description"], output_description=meta["output_description"],
                    time_limit=meta["time_limit"], memory_limit=meta["memory_limit"], is_contest_only=False,
                    is_public=True, is_judge_ready=True,
                    allowed_languages=meta.get("allowed_languages", "python,c,cpp,java"),
                )
                db.add(problem)
            else:
                problem.title = meta["title"]
                problem.description = meta["description"]
                problem.input_description = meta["input_description"]
                problem.output_description = meta["output_description"]
                problem.time_limit = meta["time_limit"]
                problem.memory_limit = meta["memory_limit"]
                problem.allowed_languages = meta.get("allowed_languages", problem.allowed_languages or "python,c,cpp,java")
                if problem.is_public is None:
                    problem.is_public = True
                if problem.is_judge_ready is None:
                    problem.is_judge_ready = True

    db.commit()
finally:
    db.close()

print("PostgreSQL database initialized.")
