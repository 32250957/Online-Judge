"""Basic regression/health checks for the OJ project.

Run inside the web container after docker compose up:
    docker compose exec web python tools/regression_check.py

This script checks DB connectivity, important columns, problem test files,
and a few high-risk links touched by recent group/contest/admin patches.
"""
from pathlib import Path
import sys

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal  # noqa: E402
from app.models import Problem, ContestProblem, Submission, GroupContest  # noqa: E402


def problem_dir(problem_id: int) -> Path:
    return ROOT / "problems" / str(problem_id)


def get_columns(db, table_name: str) -> set[str]:
    try:
        rows = db.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = :table_name
        """), {"table_name": table_name}).fetchall()
        if rows:
            return {row[0] for row in rows}
    except Exception:
        pass
    rows = db.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return {row[1] for row in rows}


def main() -> int:
    db = SessionLocal()
    warnings: list[str] = []
    try:
        db.execute(text("SELECT 1"))
        expected = {
            "problems": ["origin_type", "origin_group_id", "origin_contest_id", "review_status", "display_code"],
            "groups": ["is_school_group", "school_group_request_status", "school_group_request_file_path", "school_group_request_file_name"],
            "contests": ["display_number", "is_ended"],
            "group_contests": ["display_number", "group_id", "contest_id"],
            "board_posts": ["display_number", "board_scope", "group_id"],
        }
        for table, columns in expected.items():
            existing = get_columns(db, table)
            missing = [column for column in columns if column not in existing]
            if missing:
                warnings.append(f"{table} missing columns: {', '.join(missing)}")

        for problem in db.query(Problem).order_by(Problem.id.asc()).all():
            tests_dir = problem_dir(problem.id) / "tests"
            if not tests_dir.exists():
                warnings.append(f"problem {problem.id}: tests directory missing")
                continue
            inputs = sorted(tests_dir.glob("*.in"))
            outputs = sorted(tests_dir.glob("*.out"))
            if not inputs:
                warnings.append(f"problem {problem.id}: no input testcases")
            if len(inputs) != len(outputs):
                warnings.append(f"problem {problem.id}: input/output file count mismatch")
            for input_path in inputs:
                if not input_path.with_suffix(".out").exists():
                    warnings.append(f"problem {problem.id}: missing output for {input_path.name}")

        broken_contest_links = db.query(ContestProblem).outerjoin(Problem, ContestProblem.problem_id == Problem.id).filter(Problem.id == None).count()  # noqa: E711
        if broken_contest_links:
            warnings.append(f"broken contest-problem links: {broken_contest_links}")
        broken_submission_links = db.query(Submission).outerjoin(Problem, Submission.problem_id == Problem.id).filter(Problem.id == None).count()  # noqa: E711
        if broken_submission_links:
            warnings.append(f"broken submission-problem links: {broken_submission_links}")
        group_contests_without_contest = db.query(GroupContest).filter(GroupContest.contest_id == None).count()  # noqa: E711
        if group_contests_without_contest:
            warnings.append(f"group contests without contest row: {group_contests_without_contest}")

        if warnings:
            print("WARNINGS")
            for warning in warnings:
                print(f"- {warning}")
            return 1
        print("OK: basic regression checks passed")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
