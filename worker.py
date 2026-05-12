from __future__ import annotations

import os
import time
import traceback
from datetime import datetime
from app.database import SessionLocal
from app.models import Submission, Problem, JudgeJob, JudgeLog
from app.judge import judge_code, normalize_language, SUPPORTED_LANGUAGES

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


def add_log(db, submission_id: int | None, event: str, message: str) -> None:
    try:
        db.add(JudgeLog(submission_id=submission_id, worker_name=WORKER_NAME, event=event, message=message[:4000]))
        db.commit()
    except Exception:
        db.rollback()


def validate_problem_testcases(problem_id: int) -> tuple[bool, str]:
    tests_dir = os.path.join("problems", str(problem_id), "tests")
    if not os.path.isdir(tests_dir):
        return False, "테스트 케이스 폴더를 찾을 수 없습니다."
    input_files = sorted([name for name in os.listdir(tests_dir) if name.endswith(".in")])
    if not input_files:
        return False, "테스트 케이스 입력 파일이 없습니다."
    missing = []
    for name in input_files:
        out_name = name[:-3] + ".out"
        if not os.path.exists(os.path.join(tests_dir, out_name)):
            missing.append(out_name)
    if missing:
        return False, "출력 파일 누락: " + ", ".join(missing[:10])
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
        .all()
    )
    for submission in orphan:
        db.add(JudgeJob(submission_id=submission.id, job_type="judge", status="QUEUED"))
    if orphan:
        db.commit()

    try:
        job_row = (
            db.query(JudgeJob.id)
            .filter(JudgeJob.status == "QUEUED")
            .order_by(JudgeJob.priority.desc(), JudgeJob.id.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
    except Exception:
        db.rollback()
        job_row = (
            db.query(JudgeJob.id)
            .filter(JudgeJob.status == "QUEUED")
            .order_by(JudgeJob.priority.desc(), JudgeJob.id.asc())
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
    job.started_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    return job


def _finish_job(job: JudgeJob, status: str, error_message: str = "") -> None:
    job.status = status
    job.finished_at = datetime.utcnow()
    job.error_message = (error_message or "")[:4000]


def judge_one(db) -> bool:
    job = _claim_next_job(db)
    if job is None:
        return False

    submission = db.query(Submission).filter(Submission.id == job.submission_id).first()
    if submission is None:
        _finish_job(job, "FAILED", "submission not found")
        db.commit()
        return True
    if job.status == "CANCELED":
        return True

    submission.judge_status = "JUDGING"
    submission.result = "JUDGING"
    submission.detail = f"{WORKER_NAME}에서 채점 중입니다."
    db.commit()
    db.refresh(submission)

    try:
        problem = submission.problem or db.query(Problem).filter(Problem.id == submission.problem_id).first()
        if problem is None:
            submission.result = "SE"
            submission.detail = "문제를 찾을 수 없습니다."
            submission.runtime_ms = 0
            submission.memory_kb = 0
            submission.judge_status = "FAILED"
        elif not problem.is_judge_ready:
            submission.result = "SE"
            submission.detail = "채점 준비 중인 문제입니다."
            submission.runtime_ms = 0
            submission.memory_kb = 0
            submission.judge_status = "FAILED"
        elif normalize_language(submission.language) not in _allowed_languages(getattr(problem, "allowed_languages", "python")):
            submission.result = "SE"
            submission.detail = "이 문제에서 허용되지 않은 언어입니다."
            submission.runtime_ms = 0
            submission.memory_kb = 0
            submission.judge_status = "FAILED"
        else:
            ok_tests, test_message = validate_problem_testcases(problem.id)
            if not ok_tests:
                add_log(db, submission.id, "testcase_missing", test_message)
                submission.result = "SE"
                submission.detail = test_message
                submission.runtime_ms = 0
                submission.memory_kb = 0
                submission.judge_status = "FAILED"
                _finish_job(job, "FAILED", test_message)
                db.commit()
                add_log(db, submission.id, "finish", f"{submission.result} / {submission.judge_status} / testcase validation failed")
                return True
            add_log(db, submission.id, "start", f"job={job.id}, type={job.job_type}, problem={problem.id}, language={submission.language}, {test_message}")
            result, detail, runtime_ms, memory_kb = judge_code(
                problem.id,
                submission.code,
                normalize_language(submission.language),
                problem.time_limit,
                problem.memory_limit,
            )
            submission.result = result
            submission.detail = detail
            submission.runtime_ms = runtime_ms
            submission.memory_kb = memory_kb
            submission.judge_status = "DONE" if result != "SE" else "FAILED"
        _finish_job(job, "DONE" if submission.judge_status == "DONE" else "FAILED", submission.detail or "")
    except Exception as exc:
        err_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        add_log(db, submission.id, "exception", err_text)
        submission.result = "SE"
        submission.detail = "채점 워커 오류가 발생했습니다.\n" + "".join(traceback.format_exception_only(type(exc), exc)).strip()
        submission.runtime_ms = 0
        submission.memory_kb = 0
        submission.judge_status = "FAILED"
        _finish_job(job, "FAILED", err_text)

    db.commit()
    add_log(db, submission.id, "finish", f"job={job.id} / {submission.result} / {submission.judge_status} / {submission.runtime_ms}ms / {submission.memory_kb}KB")
    print(f"[{WORKER_NAME}] job #{job.id}, submission #{submission.id}: {submission.result} ({submission.judge_status})", flush=True)
    return True

def main() -> None:
    print(f"[{WORKER_NAME}] judge worker started", flush=True)
    last_heartbeat = 0.0
    while True:
        db = SessionLocal()
        try:
            now_ts = time.time()
            if now_ts - last_heartbeat >= HEARTBEAT_INTERVAL:
                add_log(db, None, "heartbeat", "worker alive")
                last_heartbeat = now_ts
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
