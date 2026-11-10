from __future__ import annotations

import os
import time
import traceback
import threading
from datetime import datetime, timezone
from app.database import SessionLocal
from app.models import Submission, Problem, JudgeJob, JudgeLog
from app.judge import judge_code, normalize_language, SUPPORTED_LANGUAGES, effective_time_limit_for_language

POLL_INTERVAL = float(os.getenv("OJ_WORKER_POLL_INTERVAL", "1"))
HEARTBEAT_INTERVAL = float(os.getenv("OJ_WORKER_HEARTBEAT_INTERVAL", "30"))


def _allowed_languages(raw: str | None) -> set[str]:
    values = set()
    for token in (raw or "python").replace(",", " ").split():
        lang = normalize_language(token)
        if lang in SUPPORTED_LANGUAGES:
            values.add(lang)
    return values or {"python"}

WORKER_NAME = os.getenv("OJ_WORKER_NAME", "worker")
WORKER_JOB_TYPES = {token.strip() for token in os.getenv("OJ_WORKER_JOB_TYPES", "judge,rejudge").split(",") if token.strip()}
if not WORKER_JOB_TYPES:
    WORKER_JOB_TYPES = {"judge", "rejudge"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def add_log(db, submission_id: int | None, event: str, message: str) -> None:
    try:
        db.add(JudgeLog(submission_id=submission_id, worker_name=WORKER_NAME, event=event, message=message[:4000], created_at=utc_now()))
        db.commit()
    except Exception:
        db.rollback()


def validate_problem_testcases(problem_id: int) -> tuple[bool, str]:
    tests_dir = os.path.join("problems", str(problem_id), "tests")
    if not os.path.isdir(tests_dir):
        return False, "테스트 케이스 폴더를 찾을 수 없습니다."
    input_files = sorted([name for name in os.listdir(tests_dir) if name.endswith(".in")])
    output_files = sorted([name for name in os.listdir(tests_dir) if name.endswith(".out")])
    if not input_files:
        return False, "테스트 케이스 입력 파일이 없습니다."
    missing = []
    for name in input_files:
        out_name = name[:-3] + ".out"
        if not os.path.exists(os.path.join(tests_dir, out_name)):
            missing.append(out_name)
    if missing:
        return False, "출력 파일 누락: " + ", ".join(missing[:10])
    input_stems = {name[:-3] for name in input_files}
    orphan_outputs = [name for name in output_files if name[:-4] not in input_stems]
    if orphan_outputs:
        return False, "입력 파일 없이 출력 파일만 존재: " + ", ".join(orphan_outputs[:10])
    return True, f"테스트 케이스 {len(input_files)}개 확인"



def _claim_next_job(db) -> JudgeJob | None:
    """DB 기반 judge_jobs 큐에서 다음 작업 하나를 안전하게 가져온다."""
    # 이전 버전에서 만들어진 PENDING 제출에 job이 없으면 자동 보정한다.
    orphan = (
        db.query(Submission)
        .filter(Submission.judge_status == "PENDING")
        .filter(~Submission.id.in_(db.query(JudgeJob.submission_id).filter(JudgeJob.status.in_(["QUEUED", "RUNNING"]))))
        .order_by(Submission.id.asc())
        .limit(20)
        .with_for_update(skip_locked=True)
        .all()
    )
    for submission in orphan:
        if db.query(JudgeJob.id).filter(JudgeJob.submission_id == submission.id,
                                       JudgeJob.status.in_(["QUEUED", "RUNNING"])).first():
            continue
        problem = submission.problem or db.query(Problem).filter(Problem.id == submission.problem_id).first()
        priority = int(getattr(problem, "judge_priority", 0) or 0)
        db.add(JudgeJob(submission_id=submission.id, job_type="judge", status="QUEUED", priority=priority, created_at=utc_now()))
    if orphan:
        db.commit()

    job_row = (
        db.query(JudgeJob.id)
        .filter(JudgeJob.status == "QUEUED")
        .filter(JudgeJob.job_type.in_(WORKER_JOB_TYPES))
        .order_by(JudgeJob.priority.desc(), JudgeJob.id.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if job_row is None:
        return None
    job = db.query(JudgeJob).filter(JudgeJob.id == job_row[0]).first()
    if job is None or job.status != "QUEUED":
        return None
    job.status = "RUNNING"
    job.worker_name = WORKER_NAME
    job.attempts = (job.attempts or 0) + 1
    job.started_at = utc_now()
    db.commit()
    db.refresh(job)
    return job


def _finish_job(job: JudgeJob, status: str, error_message: str = "") -> None:
    job.status = status
    job.finished_at = utc_now()
    job.error_message = (error_message or "")[:4000]


def _judge_submission(db, submission):
    problem = submission.problem
    if problem is None:
        return "SE", "문제를 찾을 수 없습니다.", 0, 0
    if not problem.is_judge_ready:
        return "SE", "채점 준비 중인 문제입니다.", 0, 0
    language = normalize_language(submission.language)
    if language not in _allowed_languages(problem.allowed_languages):
        return "SE", "이 문제에서 허용되지 않은 언어입니다.", 0, 0
    ok_tests, message = validate_problem_testcases(problem.id)
    if not ok_tests:
        return "SE", message, 0, 0
    effective_time_limit = effective_time_limit_for_language(
        problem.time_limit, language, problem.python_time_limit, problem.c_time_limit,
        problem.cpp_time_limit, problem.java_time_limit,
    )
    # Snapshot inputs before logging expires ORM state; do not dirty the live rows.
    problem_id, code, memory_limit = problem.id, submission.code, problem.memory_limit
    add_log(db, submission.id, "start", f"problem={problem_id}, language={language}, {message}")
    return judge_code(problem_id, code, language, effective_time_limit, memory_limit)


def judge_one(db) -> bool:
    job = _claim_next_job(db)
    if job is None:
        return False
    job_id, submission_id = job.id, job.submission_id
    submission = db.query(Submission).filter(Submission.id == submission_id).with_for_update().populate_existing().first()
    db.refresh(job, with_for_update=True)
    if job.status != "RUNNING":
        db.rollback()
        return True
    if submission is None:
        _finish_job(job, "FAILED", "submission not found")
        db.commit()
        return True
    submission.judge_status = "JUDGING"
    submission.result = "JUDGING"
    submission.detail = f"{WORKER_NAME}에서 채점 중입니다."
    db.commit()
    try:
        outcome = _judge_submission(db, submission)
    except Exception as exc:
        db.rollback()
        error = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        add_log(db, submission_id, "exception", error)
        outcome = ("SE", "채점 워커 오류가 발생했습니다.", 0, 0)

    # Serialize with cancellation/rejudge, then re-read job ownership. An obsolete
    # attempt must never overwrite a newer WAITING/JUDGING/DONE submission.
    db.rollback()
    submission = db.query(Submission).filter(Submission.id == submission_id).with_for_update().populate_existing().first()
    job = db.query(JudgeJob).filter(JudgeJob.id == job_id).with_for_update().populate_existing().first()
    newer = db.query(JudgeJob.id).filter(JudgeJob.submission_id == submission_id, JudgeJob.id > job_id).first()
    if submission is None or job is None or job.status != "RUNNING" or newer is not None:
        db.rollback()
        add_log(db, submission_id if submission else None, "discard", f"obsolete job={job_id}")
        return True
    result, detail, runtime_ms, memory_kb = outcome
    submission.result, submission.detail = result, detail
    submission.runtime_ms, submission.memory_kb = runtime_ms, memory_kb
    submission.judge_status = "FAILED" if result == "SE" else "DONE"
    _finish_job(job, submission.judge_status, detail if result == "SE" else "")
    db.commit()
    add_log(db, submission_id, "finish", f"job={job_id} / {result} / {runtime_ms}ms / {memory_kb}KB")
    return True


def _heartbeat_loop(stop):
    while not stop.is_set():
        db = SessionLocal()
        try:
            add_log(db, None, "heartbeat", f"worker alive / job_types={','.join(sorted(WORKER_JOB_TYPES))}")
        finally:
            db.close()
        stop.wait(HEARTBEAT_INTERVAL)


def main() -> None:
    print(f"[{WORKER_NAME}] judge worker started (job_types={','.join(sorted(WORKER_JOB_TYPES))})", flush=True)
    stop = threading.Event()
    threading.Thread(target=_heartbeat_loop, args=(stop,), daemon=True).start()
    while True:
        db = SessionLocal()
        try:
            worked = judge_one(db)
        except Exception:
            db.rollback()
            traceback.print_exc()
            worked = False
        finally:
            db.close()

        if not worked:
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

