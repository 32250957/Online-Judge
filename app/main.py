from pathlib import Path
import json
import re
import csv
import io
import shutil
import subprocess
import zipfile
import uuid
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
from markupsafe import Markup, escape

from fastapi import FastAPI, Depends, Request, Form, HTTPException, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, or_, text, case
from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal, engine
from app.schema import ensure_postgresql_schema
from app.models import User, Problem, ProblemExample, ProblemNote, ProblemHint, Submission, Contest, ContestProblem, ContestQuestion, Group, GroupMember, Message, GroupJoinRequest, GroupProblemSet, GroupContest, GroupPractice, GroupProblemSetProblem, GroupPracticeProblem, JudgeJob, JudgeLog, AuditLog, ContestEditorial, BoardPost, BoardComment
from app.security import hash_password, verify_password
from app.judge import judge_python, judge_code, normalize_language, language_label, SUPPORTED_LANGUAGES
from app.services.domain import (
    create_contest_with_problems,
    create_group_contest as service_create_group_contest,
    create_group_practice_with_problems,
    delete_group_tree,
    ensure_group_owner_membership,
    link_existing_contest_to_group,
    parse_problem_id_list,
)
from fastapi.templating import Jinja2Templates
from fastapi.exception_handlers import http_exception_handler

SUBMIT_BAN_SECONDS_DEFAULT = 10


ensure_postgresql_schema(engine)


app = FastAPI(title="Online Judge Contest MVP")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
Path("uploads/editorials").mkdir(parents=True, exist_ok=True)
Path("uploads/school_group_requests").mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
templates = Jinja2Templates(directory="app/templates")


def rich_text(value):
    """Safely render long text with preserved line breaks and MathJax support.

    HTML is escaped, so users can write text and LaTeX formulas without enabling
    arbitrary HTML/script injection.
    """
    escaped = escape(value or "")
    rendered = str(escaped).replace("\n", "<br>\n")
    return Markup(f'<div class="rich-text math-content">{rendered}</div>')


templates.env.filters["rich_text"] = rich_text
templates.env.globals["language_label"] = language_label

RESULT_LABELS = {
    "AC": "맞았습니다!!",
    "RE": "런타임 에러",
    "CE": "컴파일 에러",
    "TLE": "시간 초과",
    "MLE": "메모리 초과",
    "WA": "틀렸습니다",
    "WAITING": "채점 대기",
    "JUDGING": "채점 중",
    "SE": "시스템 에러",
}

# v37 performance helpers
RANKING_CACHE_TTL_SECONDS = 5
RANKING_CACHE: dict[int, dict] = {}
SLOW_REQUEST_THRESHOLD_MS = 800
SLOW_REQUESTS: list[dict] = []


def result_label(result: str | None) -> str:
    if not result:
        return "채점 대기"
    return RESULT_LABELS.get(result, result)

templates.env.globals["result_label"] = result_label


@app.middleware("http")
async def collect_slow_requests(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    response.headers["X-Response-Time-ms"] = str(elapsed_ms)
    if elapsed_ms >= SLOW_REQUEST_THRESHOLD_MS:
        SLOW_REQUESTS.append({
            "path": request.url.path,
            "method": request.method,
            "elapsed_ms": elapsed_ms,
            "created_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        del SLOW_REQUESTS[:-50]
    return response

BOARD_TYPES = {
    "all": "전체",
    "notice": "공지",
    "free": "자유",
    "promo": "홍보",
    "question": "질문",
    "request": "요청",
}
GROUP_BOARD_TYPES = {
    "notice": "공지",
    "free": "자유",
    "promo": "홍보",
    "question": "질문",
    "request": "요청",
    "general": "일반",
}
GROUP_BOARD_TABS = {
    "all": "전체",
    "notice": "공지",
    "free": "자유",
    "promo": "홍보",
    "question": "질문",
}
GROUP_BOARD_WRITE_TYPES = {
    "notice": "공지",
    "free": "자유",
    "promo": "홍보",
    "question": "질문",
}
templates.env.globals["BOARD_TYPES"] = BOARD_TYPES
templates.env.globals["GROUP_BOARD_TYPES"] = GROUP_BOARD_TYPES
templates.env.globals["GROUP_BOARD_TABS"] = GROUP_BOARD_TABS
templates.env.globals["GROUP_BOARD_WRITE_TYPES"] = GROUP_BOARD_WRITE_TYPES


APP_TIMEZONE = ZoneInfo("Asia/Seoul")


def now():
    """앱 전체에서 사용할 현재 시각.

    DB에는 timezone 없는 datetime-local 값과 맞추기 위해 Asia/Seoul 기준 naive datetime으로 저장/비교한다.
    Docker 컨테이너가 UTC로 떠도 대회/연습 시간이 브라우저의 한국 시각과 어긋나지 않게 한다.
    """
    return datetime.now(APP_TIMEZONE).replace(tzinfo=None)


def parse_datetime_local(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M")


def format_datetime_local(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M")


def to_app_epoch_ms(value: datetime) -> int:
    """Convert an app-local naive datetime to JavaScript epoch milliseconds.

    The app stores contest/practice times as timezone-naive Asia/Seoul local
    datetimes.  Contest pages need epoch milliseconds for browser-side timers,
    so attach the app timezone before calling timestamp().
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=APP_TIMEZONE)
    else:
        value = value.astimezone(APP_TIMEZONE)
    return int(value.timestamp() * 1000)


def default_event_times() -> tuple[datetime, datetime]:
    """폼을 여는 시점 기준 기본 시작/종료 시각을 만든다.

    시작: 현재 시스템 시각 + 5분
    종료: 기본 시작 시각 + 2시간
    초/마이크로초는 datetime-local 입력에 맞춰 버린다.
    """
    start = (now() + timedelta(minutes=5)).replace(second=0, microsecond=0)
    end = start + timedelta(hours=2)
    return start, end


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def cleanup_expired_submit_ban(user: Optional[User], db: Session) -> Optional[User]:
    if user and user.submit_banned_until and user.submit_banned_until <= now():
        user.submit_banned_until = None
        user.ban_reason = None
        db.commit()
        db.refresh(user)
    return user


def get_current_user(request: Request, db: Session):
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = db.query(User).filter(User.id == user_id).first()
    return cleanup_expired_submit_ban(user, db)


def require_login(request: Request, db: Session):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Login required")
    return user


def require_admin(request: Request, db: Session):
    user = require_login(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required")
    return user


@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    user = None
    try:
        db = SessionLocal()
        user = get_current_user(request, db)
    except Exception:
        pass
    finally:
        try:
            db.close()
        except Exception:
            pass
    if exc.status_code == 404:
        return templates.TemplateResponse("404.html", {"request": request, "user": user}, status_code=404)
    return await http_exception_handler(request, exc)



def problem_dir(problem_id: int) -> Path:
    return Path("problems") / str(problem_id)


def read_problem_tests(problem_id: int) -> tuple[str, str]:
    tests_dir = problem_dir(problem_id) / "tests"
    inputs, outputs = [], []
    for input_path in sorted(tests_dir.glob("*.in"), key=lambda p: int(p.stem) if p.stem.isdigit() else p.stem):
        output_path = input_path.with_suffix(".out")
        inputs.append(input_path.read_text(encoding="utf-8").rstrip("\n"))
        outputs.append(output_path.read_text(encoding="utf-8").rstrip("\n") if output_path.exists() else "")
    return "\n---\n".join(inputs), "\n---\n".join(outputs)


def read_problem_testcases(problem_id: int) -> list[dict]:
    """Return test cases as editable rows from problems/<id>/tests/*.in/out."""
    tests_dir = problem_dir(problem_id) / "tests"
    cases = []
    for input_path in sorted(tests_dir.glob("*.in"), key=lambda p: int(p.stem) if p.stem.isdigit() else p.stem):
        output_path = input_path.with_suffix(".out")
        cases.append({
            "index": len(cases) + 1,
            "stem": input_path.stem,
            "input": input_path.read_text(encoding="utf-8").rstrip("\n"),
            "output": output_path.read_text(encoding="utf-8").rstrip("\n") if output_path.exists() else "",
        })
    return cases


def write_problem_testcases(problem_id: int, cases: list[tuple[str, str]]) -> None:
    tests_dir = problem_dir(problem_id) / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    if not cases:
        raise ValueError("테스트 케이스는 1개 이상 필요합니다.")
    for old_file in tests_dir.glob("*"):
        old_file.unlink()
    for index, (input_data, output_data) in enumerate(cases, start=1):
        (tests_dir / f"{index}.in").write_text((input_data or "") + "\n", encoding="utf-8")
        (tests_dir / f"{index}.out").write_text((output_data or "") + "\n", encoding="utf-8")


def append_problem_testcase(problem_id: int, input_data: str, output_data: str) -> None:
    cases = [(case["input"], case["output"]) for case in read_problem_testcases(problem_id)]
    cases.append((input_data, output_data))
    write_problem_testcases(problem_id, cases)


def update_problem_testcase(problem_id: int, case_index: int, input_data: str, output_data: str) -> None:
    cases = [(case["input"], case["output"]) for case in read_problem_testcases(problem_id)]
    if case_index < 1 or case_index > len(cases):
        raise ValueError("존재하지 않는 테스트케이스입니다.")
    cases[case_index - 1] = (input_data, output_data)
    write_problem_testcases(problem_id, cases)


def delete_problem_testcase(problem_id: int, case_index: int) -> None:
    cases = [(case["input"], case["output"]) for case in read_problem_testcases(problem_id)]
    if case_index < 1 or case_index > len(cases):
        raise ValueError("존재하지 않는 테스트케이스입니다.")
    del cases[case_index - 1]
    write_problem_testcases(problem_id, cases)


def delete_submission_tree(db: Session, submission: Submission) -> None:
    db.query(JudgeJob).filter(JudgeJob.submission_id == submission.id).delete(synchronize_session=False)
    db.query(JudgeLog).filter(JudgeLog.submission_id == submission.id).delete(synchronize_session=False)
    db.delete(submission)


def delete_contest_tree(db: Session, contest: Contest) -> None:
    submission_ids = [row[0] for row in db.query(Submission.id).filter(Submission.contest_id == contest.id).all()]
    if submission_ids:
        db.query(JudgeJob).filter(JudgeJob.submission_id.in_(submission_ids)).delete(synchronize_session=False)
        db.query(JudgeLog).filter(JudgeLog.submission_id.in_(submission_ids)).delete(synchronize_session=False)
        db.query(Submission).filter(Submission.id.in_(submission_ids)).delete(synchronize_session=False)
    db.query(ContestQuestion).filter(ContestQuestion.contest_id == contest.id).delete(synchronize_session=False)
    db.query(ContestEditorial).filter(ContestEditorial.contest_id == contest.id).delete(synchronize_session=False)
    db.query(ContestProblem).filter(ContestProblem.contest_id == contest.id).delete(synchronize_session=False)
    db.query(GroupContest).filter(GroupContest.contest_id == contest.id).delete(synchronize_session=False)
    db.delete(contest)


def delete_problem_tree(db: Session, problem: Problem) -> None:
    submission_ids = [row[0] for row in db.query(Submission.id).filter(Submission.problem_id == problem.id).all()]
    if submission_ids:
        db.query(JudgeJob).filter(JudgeJob.submission_id.in_(submission_ids)).delete(synchronize_session=False)
        db.query(JudgeLog).filter(JudgeLog.submission_id.in_(submission_ids)).delete(synchronize_session=False)
        db.query(Submission).filter(Submission.id.in_(submission_ids)).delete(synchronize_session=False)
    db.query(ContestEditorial).filter(ContestEditorial.problem_id == problem.id).delete(synchronize_session=False)
    db.query(ContestQuestion).filter(ContestQuestion.problem_id == problem.id).update({ContestQuestion.problem_id: None}, synchronize_session=False)
    db.query(ContestProblem).filter(ContestProblem.problem_id == problem.id).delete(synchronize_session=False)
    db.query(GroupProblemSetProblem).filter(GroupProblemSetProblem.problem_id == problem.id).delete(synchronize_session=False)
    db.query(GroupPracticeProblem).filter(GroupPracticeProblem.problem_id == problem.id).delete(synchronize_session=False)
    db.query(ProblemExample).filter(ProblemExample.problem_id == problem.id).delete(synchronize_session=False)
    pdir = problem_dir(problem.id)
    if pdir.exists():
        shutil.rmtree(pdir, ignore_errors=True)
    db.delete(problem)



def split_blocks(text: str) -> list[str]:
    """--- 줄을 기준으로 입력 블록을 나눈다.
    Windows CRLF, 앞뒤 공백이 있는 구분선도 처리한다.
    예: "1 2\r\n---\r\n10 20" -> ["1 2", "10 20"]
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"(?m)^\s*---\s*$", normalized)
    return [block.strip("\n") for block in blocks if block.strip("\n") != ""]


def read_problem_examples(problem: Problem) -> tuple[str, str]:
    if not problem or not getattr(problem, "examples", None):
        return "", ""
    return "\n---\n".join(example.input_text for example in problem.examples), "\n---\n".join(example.output_text for example in problem.examples)


def save_problem_examples(db: Session, problem: Problem, sample_inputs: str, sample_outputs: str) -> None:
    for example in list(problem.examples):
        db.delete(example)
    input_blocks = split_blocks(sample_inputs)
    output_blocks = split_blocks(sample_outputs)
    if len(input_blocks) != len(output_blocks):
        raise ValueError("예제 입력 수와 예제 출력 수가 다릅니다.")
    for index, (input_text, output_text) in enumerate(zip(input_blocks, output_blocks)):
        db.add(ProblemExample(problem_id=problem.id, input_text=input_text, output_text=output_text, order_index=index))


def read_problem_notes(problem: Problem) -> str:
    if not problem or not getattr(problem, "notes", None):
        return ""
    return "\n---\n".join(note.content for note in problem.notes)


def read_problem_hints(problem: Problem) -> str:
    if not problem or not getattr(problem, "hints", None):
        return ""
    return "\n---\n".join(hint.content for hint in problem.hints)


def save_problem_notes_and_hints(db: Session, problem: Problem, notes_text: str, hints_text: str) -> None:
    for note in list(problem.notes):
        db.delete(note)
    for hint in list(problem.hints):
        db.delete(hint)

    for index, content in enumerate(split_blocks(notes_text)):
        if content.strip():
            db.add(ProblemNote(problem_id=problem.id, content=content.strip(), order_index=index))
    for index, content in enumerate(split_blocks(hints_text)):
        if content.strip():
            db.add(ProblemHint(problem_id=problem.id, content=content.strip(), order_index=index))




def audit_log(db: Session, request: Request | None, actor: Optional[User], action: str, target_type: str = "", target_id: int | None = None, summary: str = "") -> AuditLog:
    ip_address = ""
    if request is not None and request.client is not None:
        ip_address = request.client.host or ""
    row = AuditLog(
        actor_id=actor.id if actor else None,
        actor_username=actor.username if actor else "",
        action=action,
        target_type=target_type,
        target_id=target_id,
        summary=summary[:4000],
        ip_address=ip_address,
    )
    db.add(row)
    return row

def create_message(db: Session, user_id: int, title: str, content: str = "", message_type: str = "notice", related_group_id: int | None = None, related_submission_id: int | None = None, action_status: str = "none") -> Message:
    message = Message(user_id=user_id, title=title, content=content, message_type=message_type, related_group_id=related_group_id, related_submission_id=related_submission_id, action_status=action_status)
    db.add(message)
    return message


def create_messages_for_users(db: Session, users: list[User], title: str, content: str = "", message_type: str = "notice", related_group_id: int | None = None, related_submission_id: int | None = None) -> int:
    """Create the same message for a de-duplicated user list."""
    seen: set[int] = set()
    count = 0
    for target in users:
        if target is None or target.id in seen:
            continue
        seen.add(target.id)
        create_message(db, target.id, title, content, message_type, related_group_id=related_group_id, related_submission_id=related_submission_id)
        count += 1
    return count


def notify_admins(db: Session, title: str, content: str = "", message_type: str = "admin_notice", related_group_id: int | None = None, related_submission_id: int | None = None) -> int:
    admins = db.query(User).filter(User.is_admin == True).order_by(User.id.asc()).all()  # noqa: E712
    return create_messages_for_users(db, admins, title, content, message_type, related_group_id=related_group_id, related_submission_id=related_submission_id)


def notify_group_members(db: Session, group: Group, title: str, content: str = "", message_type: str = "group_notice") -> int:
    members = db.query(User).join(GroupMember, GroupMember.user_id == User.id).filter(GroupMember.group_id == group.id).order_by(User.id.asc()).all()
    if group.owner and group.owner not in members:
        members.append(group.owner)
    return create_messages_for_users(db, members, title, content, message_type, related_group_id=group.id)


def contest_participant_users(db: Session, contest_id: int) -> list[User]:
    rows = db.query(User).join(Submission, Submission.user_id == User.id).filter(Submission.contest_id == contest_id).order_by(User.id.asc()).distinct().all()
    return rows

def save_problem_files(problem_id, title, description, input_description, output_description, time_limit, memory_limit, test_inputs, test_outputs, allowed_languages: str = "python,c,cpp,java"):
    p_dir = problem_dir(problem_id)
    tests_dir = p_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "id": problem_id,
        "title": title,
        "description": description,
        "input_description": input_description,
        "output_description": output_description,
        "time_limit": time_limit,
        "memory_limit": memory_limit,
        "allowed_languages": allowed_languages,
    }
    (p_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    input_blocks = split_blocks(test_inputs)
    output_blocks = split_blocks(test_outputs)
    if len(input_blocks) != len(output_blocks):
        raise ValueError("입력 테스트 케이스 수와 출력 테스트 케이스 수가 다릅니다.")
    if not input_blocks:
        raise ValueError("테스트 케이스는 1개 이상 필요합니다.")

    for old_file in tests_dir.glob("*"):
        old_file.unlink()
    for index, (input_data, output_data) in enumerate(zip(input_blocks, output_blocks), start=1):
        (tests_dir / f"{index}.in").write_text(input_data + "\n", encoding="utf-8")
        (tests_dir / f"{index}.out").write_text(output_data + "\n", encoding="utf-8")


def contest_status(contest: Contest) -> str:
    current = now()
    if contest.is_ended or current > contest.end_time:
        return "종료"
    if current < contest.start_time:
        return "시작 전"
    return "진행 중"


def can_submit_in_contest(contest: Contest):
    current = now()
    return (not contest.is_ended) and contest.start_time <= current <= contest.end_time




def contest_has_started(contest: Contest) -> bool:
    return now() >= contest.start_time


def can_view_contest_problem_list(user: Optional[User], contest: Contest) -> bool:
    # 관리자는 출제/검수 목적상 시작 전에도 볼 수 있고, 일반 사용자는 시작 전에는 목록 자체를 볼 수 없다.
    return bool(user and user.is_admin) or contest_has_started(contest)


def require_exam_problem_configuration_editable(user: Optional[User], contest: Contest, db: Optional[Session] = None) -> None:
    if not (getattr(contest, "is_exam_mode", False) and contest_has_started(contest)):
        return
    if user and user.is_admin:
        return
    if db is not None and can_manage_contest(user, contest, db):
        return
    raise HTTPException(status_code=403, detail="시험/평가 모드가 시작된 뒤에는 OJ 관리자 또는 해당 그룹 관리자만 문제 추가/삭제/순서 변경을 할 수 있습니다.")


def ensure_exam_mode_allowed_for_group(group: Optional[Group], enabled: bool) -> None:
    if enabled and not (group and group.is_school_group and group.school_group_request_status == "approved"):
        raise HTTPException(status_code=403, detail="시험/평가 모드는 학교 분반 그룹 승인 완료 후 사용할 수 있습니다.")


def ensure_site_contest_exam_mode_disabled(enabled: bool) -> None:
    if enabled:
        raise HTTPException(status_code=403, detail="시험/평가 모드는 학교 분반 그룹 대회에서만 사용할 수 있습니다.")


def get_contest_link_for_submission(db: Session, submission: Submission) -> Optional[ContestProblem]:
    if submission.contest_id is None:
        return None
    return db.query(ContestProblem).filter(
        ContestProblem.contest_id == submission.contest_id,
        ContestProblem.problem_id == submission.problem_id,
    ).first()

def index_to_label(index: int) -> str:
    label = ""
    n = index
    while True:
        label = chr(ord("A") + (n % 26)) + label
        n = n // 26 - 1
        if n < 0:
            return label


def relabel_contest_problems(db: Session, contest_id: int) -> None:
    links = db.query(ContestProblem).filter(ContestProblem.contest_id == contest_id).order_by(ContestProblem.order_index.asc(), ContestProblem.problem_id.asc()).all()
    group_contest = db.query(GroupContest).filter(GroupContest.contest_id == contest_id).first()
    for index, link in enumerate(links):
        link.order_index = index
        link.label = index_to_label(index)
        if group_contest is not None and link.problem is not None and getattr(link.problem, "origin_type", "regular") == "group_contest":
            link.problem.display_code = build_group_contest_problem_code(group_contest.group_id, contest_id, link.label)


def contest_is_closed(contest: Contest) -> bool:
    return contest.is_ended or now() > contest.end_time


def require_contest_editable(contest: Contest) -> None:
    if contest_is_closed(contest):
        raise HTTPException(status_code=403, detail="종료된 대회에서는 질문 등록 또는 문제 추가/순서 변경을 할 수 없습니다.")


def add_problem_to_contest(db: Session, contest: Contest, problem: Problem) -> None:
    exists = db.query(ContestProblem).filter(
        ContestProblem.contest_id == contest.id,
        ContestProblem.problem_id == problem.id,
    ).first()
    if exists:
        return
    order_index = db.query(ContestProblem).filter(ContestProblem.contest_id == contest.id).count()
    db.add(ContestProblem(contest_id=contest.id, problem_id=problem.id, label=index_to_label(order_index), order_index=order_index))
    db.flush()
    relabel_contest_problems(db, contest.id)


def resolve_contest_problem(db: Session, contest_id: int, label_or_id: str) -> Optional[ContestProblem]:
    link = db.query(ContestProblem).filter(
        ContestProblem.contest_id == contest_id,
        ContestProblem.label == label_or_id.upper(),
    ).first()
    if link:
        return link
    if label_or_id.isdigit():
        return db.query(ContestProblem).filter(
            ContestProblem.contest_id == contest_id,
            ContestProblem.problem_id == int(label_or_id),
        ).first()
    return None


def get_attempt_count_until_ac(db: Session, user_id: int, problem_id: int, contest_id: int | None):
    query = db.query(Submission).filter(Submission.user_id == user_id, Submission.problem_id == problem_id)
    query = query.filter(Submission.contest_id.is_(None) if contest_id is None else Submission.contest_id == contest_id)
    count = 0
    for submission in query.order_by(Submission.id.asc()).all():
        count += 1
        if submission.result == "AC":
            return count
    return count


def _contest_ranking_signature(db: Session, contest_id: int) -> tuple[int, int, int]:
    count_value, max_id_value, done_count_value = db.query(
        func.count(Submission.id),
        func.coalesce(func.max(Submission.id), 0),
        func.sum(case((Submission.judge_status.in_(["DONE", "FAILED"]), 1), else_=0)),
    ).filter(Submission.contest_id == contest_id).one()
    return int(count_value or 0), int(max_id_value or 0), int(done_count_value or 0)


def _hydrate_cached_rankings(db: Session, cached_rows: list[dict]) -> list[dict]:
    user_ids = [row.get("user_id") for row in cached_rows if row.get("user_id") is not None]
    users = {user.id: user for user in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    hydrated = []
    for row in cached_rows:
        copied = dict(row)
        copied["user"] = users.get(row.get("user_id"))
        copied["solved_problems"] = set(row.get("solved_problem_ids", []))
        copied["best_ac"] = row.get("best_ac", {})
        hydrated.append(copied)
    return hydrated


def build_contest_rankings(db: Session, contest: Contest) -> list[dict]:
    contest_problem_ids = {link.problem_id for link in contest.problem_links if not link.exclude_from_ranking}
    if not contest_problem_ids:
        return []

    signature = _contest_ranking_signature(db, contest.id)
    cached = RANKING_CACHE.get(contest.id)
    current_ts = time.time()
    if cached and cached.get("signature") == signature and current_ts - cached.get("created_ts", 0) <= RANKING_CACHE_TTL_SECONDS:
        return _hydrate_cached_rankings(db, cached.get("rows", []))

    submissions = (
        db.query(Submission)
        .options(joinedload(Submission.user))
        .filter(Submission.contest_id == contest.id)
        .order_by(Submission.id.asc())
        .all()
    )
    by_user: dict[int, dict] = {}
    for submission in submissions:
        if submission.user_id is None or submission.problem_id not in contest_problem_ids:
            continue
        row = by_user.setdefault(submission.user_id, {
            "user": submission.user,
            "user_id": submission.user_id,
            "solved_problems": set(),
            "wrong_count": 0,
            "runtime_ms": 0,
            "memory_kb": 0,
            "best_ac": {},
        })
        if submission.result == "AC":
            current_best = row["best_ac"].get(submission.problem_id)
            candidate = (submission.runtime_ms or 0, submission.memory_kb or 0, submission.id)
            if current_best is None or candidate < current_best:
                row["best_ac"][submission.problem_id] = candidate
                row["solved_problems"].add(submission.problem_id)
        else:
            row["wrong_count"] += 1

    rankings = []
    for row in by_user.values():
        best_values = list(row["best_ac"].values())
        row["solved_count"] = len(row["solved_problems"])
        row["runtime_ms"] = sum(item[0] for item in best_values)
        row["memory_kb"] = sum(item[1] for item in best_values)
        rankings.append(row)

    rankings.sort(key=lambda item: (-item["solved_count"], item["wrong_count"], item["runtime_ms"], item["memory_kb"], item["user"].username if item["user"] else ""))
    for rank, row in enumerate(rankings, start=1):
        row["rank"] = rank

    cache_rows = []
    for row in rankings:
        cache_rows.append({
            "rank": row["rank"],
            "user_id": row["user_id"],
            "solved_problem_ids": sorted(row["solved_problems"]),
            "wrong_count": row["wrong_count"],
            "runtime_ms": row["runtime_ms"],
            "memory_kb": row["memory_kb"],
            "solved_count": row["solved_count"],
            "best_ac": {str(k): list(v) for k, v in row["best_ac"].items()},
        })
    RANKING_CACHE[contest.id] = {"signature": signature, "created_ts": current_ts, "rows": cache_rows}
    return rankings


def can_view_submission_code(viewer: Optional[User], submission: Submission) -> bool:
    if viewer is None:
        return False
    if viewer.is_admin:
        return True
    if submission.user_id == viewer.id:
        return True
    # 대회 제출은 항상 본인/관리자만 열람 가능
    if submission.contest_id is not None:
        return False
    # 문제 관리에서 강제 비공개가 켜져 있으면 유저 희망 공개와 무관하게 차단
    if submission.problem and submission.problem.force_private_submission:
        return False
    if submission.visibility == "public":
        return True
    if submission.visibility == "accepted_only" and submission.result == "AC":
        return True
    return False


def visibility_label(value: str) -> str:
    return {
        "private": "비공개",
        "public": "공개",
        "accepted_only": "맞았을 때만 공개",
    }.get(value, "비공개")


def contest_ranking_visible_to(user: Optional[User], contest: Contest) -> bool:
    if user and user.is_admin:
        return True
    if getattr(contest, "is_exam_mode", False) or getattr(contest, "hide_ranking", False):
        return False
    return True


def can_view_submission_detail_message(viewer: Optional[User], submission: Submission) -> bool:
    # 상세 채점 메시지는 운영 모드와 무관하게 관리자에게만 노출한다.
    # 일반 사용자는 실행시간/메모리/최종 결과만 확인할 수 있다.
    return bool(viewer and viewer.is_admin)


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "unknown").strip("_") or "unknown"


def extension_for_language(language: str) -> str:
    return {
        "python": "py",
        "c": "c",
        "cpp": "cpp",
        "java": "java",
    }.get(normalize_language(language), "txt")


def build_contest_score_rows(db: Session, contest: Contest) -> list[list]:
    if not getattr(contest, "score_enabled", False):
        return [["message"], ["이 대회는 배점 기능을 사용하지 않습니다."]]
    links = [link for link in contest.problem_links if not link.exclude_from_ranking]
    links.sort(key=lambda link: (link.order_index, link.problem_id))
    header = ["rank", "username", "full_name", "student_id", "total_score"] + [f"{link.label}({link.score})" for link in links]

    submissions = (
        db.query(Submission)
        .options(joinedload(Submission.user))
        .filter(Submission.contest_id == contest.id, Submission.user_id.isnot(None))
        .all()
    )
    users = {}
    accepted_pairs = set()
    for submission in submissions:
        if submission.user:
            users[submission.user_id] = submission.user
        if submission.result == "AC":
            accepted_pairs.add((submission.user_id, submission.problem_id))

    rows_data = []
    for user_id, submitter in users.items():
        row_scores = []
        total = 0
        for link in links:
            score = int(link.score or 0) if (user_id, link.problem_id) in accepted_pairs else 0
            row_scores.append(score)
            total += score
        rows_data.append({"username": submitter.username, "full_name": getattr(submitter, "full_name", ""), "student_id": getattr(submitter, "student_id", ""), "total": total, "scores": row_scores})
    rows_data.sort(key=lambda item: (-item["total"], item["username"]))
    rows = [header]
    for rank, item in enumerate(rows_data, start=1):
        rows.append([rank, item["username"], item["full_name"], item["student_id"], item["total"]] + item["scores"])
    return rows


def build_final_code_zip(contest: Contest, db: Session) -> io.BytesIO:
    links = sorted(contest.problem_links, key=lambda link: (link.order_index, link.problem_id))
    link_by_problem_id = {link.problem_id: link for link in links}
    latest: dict[tuple[int, int], Submission] = {}
    submissions = db.query(Submission).filter(Submission.contest_id == contest.id).order_by(Submission.id.asc()).all()
    for submission in submissions:
        if submission.user_id is None or submission.problem_id not in link_by_problem_id:
            continue
        latest[(submission.user_id, submission.problem_id)] = submission
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if not latest:
            zf.writestr("README.txt", "제출 코드가 없습니다.\n")
        for (user_id, problem_id), submission in sorted(latest.items(), key=lambda item: (item[1].user.username if item[1].user else "", link_by_problem_id[item[0][1]].order_index)):
            username = safe_filename(submission.user.username if submission.user else f"user_{user_id}")
            link = link_by_problem_id[problem_id]
            ext = extension_for_language(submission.language)
            filename = f"{username}/{link.label}_{problem_id}_submission_{submission.id}_{submission.result}.{ext}"
            zf.writestr(filename, submission.code or "")
    buffer.seek(0)
    return buffer


def zip_response(filename: str, buffer: io.BytesIO) -> StreamingResponse:
    return StreamingResponse(buffer, media_type="application/zip", headers={"Content-Disposition": f"attachment; filename={filename}"})



def safe_judge_python(problem: Problem, code: str) -> tuple[str, str, int, int]:
    """채점 함수 예외가 서버 500으로 번지지 않도록 막는 안전 래퍼."""
    try:
        return judge_python(problem.id, code, problem.time_limit, problem.memory_limit)
    except FileNotFoundError as exc:
        # Docker가 설치되지 않았거나 실행 경로에 없을 때
        return "SE", f"채점 환경 오류입니다. Docker 실행 여부를 확인하세요.\n{exc}", 0, 0
    except Exception as exc:
        return "SE", f"채점 중 서버 오류가 발생했습니다.\n{type(exc).__name__}: {exc}", 0, 0


def validate_submission_payload(language: str, code: str) -> None:
    language = normalize_language(language)
    if language not in SUPPORTED_LANGUAGES:
        raise HTTPException(status_code=400, detail="지원하지 않는 언어입니다.")
    if len(code.encode("utf-8")) > 65536:
        raise HTTPException(status_code=400, detail="제출 코드가 너무 큽니다. 현재 제한은 64KB입니다.")



def parse_allowed_languages(raw: str | None) -> list[str]:
    values: list[str] = []
    for token in re.split(r"[\s,]+", raw or ""):
        lang = normalize_language(token)
        if lang in SUPPORTED_LANGUAGES and lang not in values:
            values.append(lang)
    return values or ["python"]


def language_options_for_problem(problem: Problem) -> list[dict]:
    allowed = set(parse_allowed_languages(getattr(problem, "allowed_languages", "python")))
    return [{"value": key, "label": language_label(key)} for key in SUPPORTED_LANGUAGES if key in allowed]


def allowed_language_labels(problem: Problem) -> str:
    return ", ".join(language_label(lang) for lang in parse_allowed_languages(getattr(problem, "allowed_languages", "python")))


templates.env.globals["allowed_language_labels"] = allowed_language_labels

def language_allowed_for_problem(problem: Problem, language: str) -> bool:
    return normalize_language(language) in set(parse_allowed_languages(getattr(problem, "allowed_languages", "python")))

def enqueue_submission(submission: Submission, reason: str = "채점 대기 중입니다.", db: Session | None = None, job_type: str = "judge") -> JudgeJob | None:
    submission.result = "WAITING"
    submission.judge_status = "PENDING"
    submission.detail = reason
    submission.runtime_ms = 0
    submission.memory_kb = 0
    if db is None:
        return None
    # 기존에 남아 있던 미완료 job은 취소하고 새 job 하나만 큐에 넣는다.
    db.query(JudgeJob).filter(
        JudgeJob.submission_id == submission.id,
        JudgeJob.status.in_(["QUEUED", "RUNNING"]),
    ).update({
        JudgeJob.status: "CANCELED",
        JudgeJob.finished_at: datetime.utcnow(),
        JudgeJob.error_message: "새 채점 요청으로 취소됨",
    }, synchronize_session=False)
    job = JudgeJob(submission_id=submission.id, job_type=job_type, status="QUEUED", priority=0)
    db.add(job)
    return job


def rejudge_submission(submission: Submission, db: Session | None = None) -> None:
    # Docker worker가 비동기 judge_jobs 큐에서 다시 채점하도록 대기열에 넣는다.
    enqueue_submission(submission, "재채점 대기 중입니다.", db=db, job_type="rejudge")


def render_contest_404(request: Request, user: Optional[User], contest: Optional[Contest] = None):
    return templates.TemplateResponse("404.html", {
        "request": request,
        "user": user,
        "return_url": f"/contests/{contest.id}#problems" if contest else "/",
        "return_label": "해당 대회 문제 목록으로 돌아가기" if contest else "문제 목록으로 돌아가기",
    }, status_code=404)


def render_contest_form(request, user, db, start_time=None, end_time=None, error=None):
    default_start_dt, default_end_dt = default_event_times()
    return templates.TemplateResponse("contest_form.html", {
        "request": request,
        "user": user,
        "problems": db.query(Problem).order_by(Problem.is_contest_only.asc(), Problem.id.asc()).all(),
        "default_start": start_time or format_datetime_local(default_start_dt),
        "default_end": end_time or format_datetime_local(default_end_dt),
        "error": error,
    })


def get_group_membership(db: Session, user: Optional[User], group: Optional[Group]) -> Optional[GroupMember]:
    if user is None or group is None:
        return None
    return db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == user.id).first()


def is_group_member(user: Optional[User], group: Group) -> bool:
    if user is None:
        return False
    if user.is_admin or group.owner_id == user.id:
        return True
    return any(member.user_id == user.id for member in group.members)


def is_group_manager(user: Optional[User], group: Group, db: Optional[Session] = None) -> bool:
    if user is None:
        return False
    if user.is_admin or group.owner_id == user.id:
        return True
    membership = None
    if db is not None:
        membership = get_group_membership(db, user, group)
    else:
        membership = next((member for member in group.members if member.user_id == user.id), None)
    return bool(membership and membership.role in {"owner", "admin"})


def is_group_owner_or_site_admin(user: Optional[User], group: Group) -> bool:
    return bool(user and (user.is_admin or group.owner_id == user.id))


def require_group_owner_or_site_admin(user: User, group: Group) -> None:
    if not is_group_owner_or_site_admin(user, group):
        raise HTTPException(status_code=403, detail="그룹 소유자 또는 OJ 관리자만 사용할 수 있습니다.")


def require_group_owner_or_admin(user: User, group: Group) -> None:
    # 기존 라우트 호환용 이름. 실제 의미는 그룹 관리자 이상이다.
    if not is_group_manager(user, group):
        raise HTTPException(status_code=403, detail="그룹 관리자 이상만 사용할 수 있습니다.")


def can_view_group(user: Optional[User], group: Group) -> bool:
    return group.is_public or is_group_member(user, group)


def user_can_manage_group(user: Optional[User], group: Group) -> bool:
    return is_group_manager(user, group)


def can_create_group_contest_problem(user: Optional[User], group: Optional[Group], db: Optional[Session] = None) -> bool:
    if user is None or group is None:
        return False
    if user.is_admin:
        return True
    return bool(group.is_school_group and is_group_manager(user, group, db))


def build_group_contest_problem_code(group_id: int, contest_id: int, label: str) -> str:
    return f"G{group_id}-C{contest_id}-{label}"


def can_edit_problem(user: Optional[User], problem: Optional[Problem], db: Session) -> bool:
    if user is None or problem is None:
        return False
    if user.is_admin:
        return True
    if problem.origin_type != "group_contest" or problem.origin_group_id is None:
        return False
    group = db.query(Group).filter(Group.id == problem.origin_group_id).first()
    # 그룹 대회에서 생성된 문제는 생성 권한 단계에서 이미 학교 분반 그룹으로 제한한다.
    # 이후 수정은 해당 출처 그룹의 관리자/소유자에게 허용해야 한다.
    return bool(group and is_group_manager(user, group, db))


def can_manage_problem_public_settings(user: Optional[User], problem: Optional[Problem]) -> bool:
    return bool(user and user.is_admin)

def permission_role_label(user: Optional[User], group: Optional[Group] = None) -> str:
    if user is None:
        return "비로그인"
    if user.is_admin:
        return "OJ 관리자"
    if group is not None and is_group_manager(user, group):
        return "학교 분반 그룹 관리자" if group.is_school_group else "일반 그룹 관리자"
    return "일반 사용자"


def can_manage_contest(user: Optional[User], contest: Optional[Contest], db: Session) -> bool:
    if user is None or contest is None:
        return False
    if user.is_admin:
        return True
    group_contest = db.query(GroupContest).filter(GroupContest.contest_id == contest.id).first()
    if group_contest is None:
        return False
    group = db.query(Group).filter(Group.id == group_contest.group_id).first()
    return bool(group and is_group_manager(user, group, db))


def can_view_submission(user: Optional[User], submission: Optional[Submission]) -> bool:
    if submission is None:
        return False
    if user and user.is_admin:
        return True
    return bool(user and submission.user_id == user.id)


def collect_permission_summary(db: Session) -> list[dict]:
    return [
        {"role": "OJ 관리자", "group_manage": "전체 가능", "contest_manage": "전체 가능", "problem_edit": "전체 가능", "submission_view": "전체 가능", "publish": "가능"},
        {"role": "학교 분반 그룹 관리자", "group_manage": "소속 그룹 가능", "contest_manage": "소속 그룹 대회 가능", "problem_edit": "그룹 대회 생성 문제 가능", "submission_view": "소속 대회 중심", "publish": "검토 요청 가능"},
        {"role": "일반 그룹 관리자", "group_manage": "소속 그룹 가능", "contest_manage": "소속 그룹 대회 가능", "problem_edit": "기존 공개 문제 직접 수정 불가", "submission_view": "소속 대회 중심", "publish": "불가"},
        {"role": "일반 사용자", "group_manage": "불가", "contest_manage": "불가", "problem_edit": "불가", "submission_view": "본인 제출 중심", "publish": "불가"},
    ]


def next_site_contest_display_number(db: Session) -> int:
    group_contest_ids = db.query(GroupContest.contest_id).filter(GroupContest.contest_id.isnot(None))
    value = db.query(func.max(Contest.display_number)).filter(~Contest.id.in_(group_contest_ids)).scalar()
    if not value:
        value = db.query(func.count(Contest.id)).filter(~Contest.id.in_(group_contest_ids)).scalar()
    return int(value or 0) + 1


def next_group_contest_display_number(db: Session, group_id: int) -> int:
    value = db.query(func.max(GroupContest.display_number)).filter(GroupContest.group_id == group_id).scalar()
    if not value:
        value = db.query(func.count(GroupContest.id)).filter(GroupContest.group_id == group_id).scalar()
    return int(value or 0) + 1


def next_board_post_display_number(db: Session, *, board_scope: str, group_id: int | None = None) -> int:
    query = db.query(func.max(BoardPost.display_number)).filter(BoardPost.board_scope == board_scope)
    if board_scope == "group":
        query = query.filter(BoardPost.group_id == group_id)
    value = query.scalar()
    if not value:
        count_query = db.query(func.count(BoardPost.id)).filter(BoardPost.board_scope == board_scope)
        if board_scope == "group":
            count_query = count_query.filter(BoardPost.group_id == group_id)
        value = count_query.scalar()
    return int(value or 0) + 1


def safe_upload_filename(filename: str) -> str:
    raw = Path(filename or "attachment").name
    cleaned = re.sub(r"[^0-9A-Za-z가-힣._ -]", "_", raw).strip(" .")
    return cleaned or "attachment"


def active_group_contest_ids(db: Session) -> set[int]:
    current = now()
    rows = (
        db.query(GroupContest.group_id)
        .join(Contest, GroupContest.contest_id == Contest.id)
        .filter(GroupContest.group_id.isnot(None), Contest.start_time <= current, Contest.end_time >= current, Contest.is_ended == False)  # noqa: E712
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def require_group_contest_problem_create_allowed(user: User, contest: Contest, db: Session) -> Optional[GroupContest]:
    group_contest = db.query(GroupContest).filter(GroupContest.contest_id == contest.id).first()
    if group_contest is None:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="OJ 관리자만 일반 대회용 새 문제를 등록할 수 있습니다.")
        return None
    group = group_contest.group
    if not can_create_group_contest_problem(user, group, db):
        raise HTTPException(status_code=403, detail="학교 분반 그룹의 관리자만 그룹 대회용 새 문제를 등록할 수 있습니다.")
    return group_contest


def require_group_contest_access(user: Optional[User], contest: Contest, db: Session) -> None:
    group_contest = db.query(GroupContest).filter(GroupContest.contest_id == contest.id).first()
    if group_contest is None:
        return
    if user is None:
        raise HTTPException(status_code=403, detail="그룹 회원만 접근할 수 있는 대회입니다.")
    if user.is_admin:
        return
    group = group_contest.group
    if group is None:
        raise HTTPException(status_code=404, detail="삭제된 그룹의 대회는 OJ 관리자만 확인할 수 있습니다.")
    if not is_group_member(user, group):
        raise HTTPException(status_code=403, detail="해당 그룹 회원만 접근할 수 있는 대회입니다.")
    if not contest.is_public and not is_group_manager(user, group, db):
        raise HTTPException(status_code=404, detail="비공개 그룹 대회입니다.")


def require_contest_manager(user: User, contest: Contest, db: Session) -> None:
    if user.is_admin:
        return
    group_contest = db.query(GroupContest).filter(GroupContest.contest_id == contest.id).first()
    if group_contest is not None and group_contest.group is not None and is_group_manager(user, group_contest.group, db):
        return
    raise HTTPException(status_code=403, detail="대회 관리자 권한이 필요합니다.")


def group_practice_is_closed(practice: GroupPractice) -> bool:
    return bool(practice.end_time is not None and now() >= practice.end_time)


def group_practice_is_open(practice: GroupPractice) -> bool:
    current = now()
    if practice.start_time and current < practice.start_time:
        return False
    if practice.end_time and current > practice.end_time:
        return False
    return True


def times_overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and end_a > start_b


def get_active_school_exam_lock(db: Session, user: Optional[User]) -> Optional[dict]:
    if user is None or user.is_admin:
        return None
    current = now()
    rows = (
        db.query(GroupContest)
        .join(Group, GroupContest.group_id == Group.id)
        .join(Contest, GroupContest.contest_id == Contest.id)
        .filter(
            Group.is_school_group == True,  # noqa: E712
            Contest.is_exam_mode == True,  # noqa: E712
            Contest.is_ended == False,  # noqa: E712
            Contest.start_time <= current,
            Contest.end_time > current,
        )
        .order_by(Contest.end_time.asc(), GroupContest.id.asc())
        .all()
    )
    for group_contest in rows:
        group = group_contest.group
        contest = group_contest.contest
        if group is None or contest is None:
            continue
        membership = get_group_membership(db, user, group)
        if membership is None and group.owner_id != user.id:
            continue
        if is_group_manager(user, group, db):
            continue
        return {"group": group, "group_contest": group_contest, "contest": contest}
    return None


def validate_school_exam_contest_overlap(db: Session, group: Group, start_time: datetime, end_time: datetime, *, exclude_contest_id: int | None = None) -> None:
    if not group.is_school_group:
        return
    member_ids = {member.user_id for member in group.members}
    member_ids.add(group.owner_id)
    if not member_ids:
        return
    existing = (
        db.query(GroupContest)
        .join(Group, GroupContest.group_id == Group.id)
        .join(Contest, GroupContest.contest_id == Contest.id)
        .filter(
            Group.is_school_group == True,  # noqa: E712
            Contest.is_exam_mode == True,  # noqa: E712
            Contest.start_time < end_time,
            Contest.end_time > start_time,
        )
        .all()
    )
    for other in existing:
        if other.contest_id is None or other.group is None or other.contest is None:
            continue
        if exclude_contest_id is not None and other.contest_id == exclude_contest_id:
            continue
        other_member_ids = {member.user_id for member in other.group.members}
        other_member_ids.add(other.group.owner_id)
        overlap_ids = sorted(member_ids & other_member_ids)
        if overlap_ids:
            users = db.query(User).filter(User.id.in_(overlap_ids)).order_by(User.username.asc()).limit(10).all()
            names = ", ".join(user.username for user in users)
            more = " 등" if len(overlap_ids) > 10 else ""
            raise ValueError(
                f"학교 분반 그룹 시험 시간이 겹칩니다. 겹치는 그룹: {other.group.name}, 겹치는 사용자: {names}{more}"
            )




def contest_editorial_visible_to(user: Optional[User], contest: Contest, editorial_count: int) -> bool:
    if user and user.is_admin:
        return True
    return contest_is_closed(contest) and editorial_count > 0


def save_editorial_image(file: UploadFile | None, contest_id: int, problem_id: int) -> str:
    if file is None or not file.filename:
        return ""
    content_type = (file.content_type or "").lower()
    allowed_content_types = {"image/png", "image/jpeg", "image/gif", "image/webp"}
    suffix = Path(file.filename).suffix.lower()
    allowed_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    if content_type not in allowed_content_types and suffix not in allowed_suffixes:
        raise HTTPException(status_code=400, detail="이미지 파일만 업로드할 수 있습니다. PNG, JPG, GIF, WEBP를 사용하세요.")
    if suffix not in allowed_suffixes:
        suffix = ".png"
    target_dir = Path("uploads/editorials") / str(contest_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"problem_{problem_id}_{uuid.uuid4().hex}{suffix}"
    target = target_dir / filename
    with target.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    return "/" + str(target).replace("\\", "/")


def is_allowed_during_school_exam(lock: dict, path: str, method: str) -> bool:
    group_id = lock["group"].id
    contest_id = lock["contest"].id
    allowed_prefixes = (
        "/static/",
        f"/contests/{contest_id}/problems/",
        "/submissions/",
        "/api/submissions/",
    )
    if path.startswith(allowed_prefixes):
        return True
    allowed_exact = {
        "/logout",
        f"/groups/{group_id}",
        f"/contests/{contest_id}",
    }
    if path in allowed_exact:
        return True
    if path == "/submit" and method.upper() == "POST":
        return True
    return False


@app.middleware("http")
async def school_exam_lock_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static/"):
        return await call_next(request)
    db = SessionLocal()
    try:
        user = get_current_user(request, db)
        lock = get_active_school_exam_lock(db, user)
        if lock is not None and not is_allowed_during_school_exam(lock, path, request.method):
            target = f"/contests/{lock['contest'].id}"
            if request.method.upper() == "GET":
                return RedirectResponse(url=target, status_code=303)
            return HTMLResponse("학교 분반 그룹 시험 모드 진행 중에는 해당 시험 대회 외부로 이동할 수 없습니다.", status_code=403)
    finally:
        db.close()
    return await call_next(request)


# SessionMiddleware must be registered after the custom HTTP middleware above so
# request.session is available inside school_exam_lock_middleware.  In FastAPI/
# Starlette, middleware added later wraps middleware added earlier.
app.add_middleware(SessionMiddleware, secret_key="change-this-secret-key-before-deploy")


def parse_id_list(raw: str) -> list[int]:
    values: list[int] = []
    for token in re.split(r"[\s,]+", raw or ""):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise ValueError("문제 번호는 숫자만 입력할 수 있습니다.")
        pid = int(token)
        if pid not in values:
            values.append(pid)
    return values


def relabel_group_problemset_items(db: Session, problem_set_id: int) -> None:
    items = db.query(GroupProblemSetProblem).filter(GroupProblemSetProblem.problem_set_id == problem_set_id).order_by(GroupProblemSetProblem.order_index.asc(), GroupProblemSetProblem.id.asc()).all()
    for index, item in enumerate(items):
        item.order_index = index


def relabel_group_practice_items(db: Session, practice_id: int) -> None:
    items = db.query(GroupPracticeProblem).filter(GroupPracticeProblem.practice_id == practice_id).order_by(GroupPracticeProblem.order_index.asc(), GroupPracticeProblem.id.asc()).all()
    for index, item in enumerate(items):
        item.order_index = index


def build_practice_board(db: Session, practice: GroupPractice, members: list[GroupMember], items: list[GroupPracticeProblem]) -> list[dict]:
    """그룹 연습 보드 계산.

    핵심 기준은 submissions.practice_id 이다.
    이전 버전에서 practice_id가 기록되지 않은 제출이 있을 수 있으므로,
    연습 시간이 설정된 경우에만 같은 기간의 일반 제출을 보조로 포함한다.
    단, practice_id가 다른 연습으로 찍힌 제출은 섞지 않는다.
    """
    board = []
    for member in members:
        user = member.user
        row = {"user": user, "cells": [], "solved_count": 0}
        for item in items:
            exact_query = db.query(Submission).filter(
                Submission.user_id == member.user_id,
                Submission.problem_id == item.problem_id,
                Submission.practice_id == practice.id,
            )
            submissions = exact_query.order_by(Submission.id.asc()).all()

            # 구버전 호환: practice_id 컬럼이 추가되기 전 제출은 기간 기준으로만 보조 집계
            if not submissions and (practice.start_time or practice.end_time):
                fallback_query = db.query(Submission).filter(
                    Submission.user_id == member.user_id,
                    Submission.problem_id == item.problem_id,
                    Submission.practice_id.is_(None),
                    Submission.contest_id.is_(None),
                )
                if practice.start_time:
                    fallback_query = fallback_query.filter(Submission.created_at >= practice.start_time)
                if practice.end_time:
                    fallback_query = fallback_query.filter(Submission.created_at <= practice.end_time)
                submissions = fallback_query.order_by(Submission.id.asc()).all()

            attempts = 0
            solved = False
            for submission in submissions:
                attempts += 1
                if submission.result == "AC":
                    solved = True
                    break
            if solved:
                row["solved_count"] += 1
            row["cells"].append({"problem": item.problem, "attempts": attempts, "solved": solved})
        board.append(row)
    board.sort(key=lambda r: (-r["solved_count"], r["user"].username if r["user"] else ""))
    return board


@app.get("/", response_class=HTMLResponse)
def site_home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    public_problem_query = db.query(Problem).filter(Problem.is_contest_only == False)
    if not (user and user.is_admin):
        public_problem_query = public_problem_query.filter(Problem.is_public == True)
    recent_problems = public_problem_query.order_by(Problem.id.desc()).limit(8).all()
    group_contest_ids = db.query(GroupContest.contest_id).filter(GroupContest.contest_id != None)
    recent_contests = (
        db.query(Contest)
        .filter(Contest.is_public == True, ~Contest.id.in_(group_contest_ids))
        .order_by(Contest.id.desc())
        .limit(5)
        .all()
    )
    notices = db.query(BoardPost).filter(BoardPost.board_scope == "site", BoardPost.board_type == "notice").order_by(BoardPost.is_pinned.desc(), BoardPost.id.desc()).limit(5).all()
    board_posts = db.query(BoardPost).filter(BoardPost.board_scope == "site").order_by(BoardPost.id.desc()).limit(8).all()
    return templates.TemplateResponse("home.html", {"request": request, "user": user, "recent_problems": recent_problems, "recent_contests": recent_contests, "notices": notices, "board_posts": board_posts, "now": now()})


@app.get("/problems", response_class=HTMLResponse)
def index(request: Request, q: str = "", difficulty: str = "", tag: str = "", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    query = db.query(Problem).filter(Problem.is_contest_only == False)
    if not (user and user.is_admin):
        query = query.filter(Problem.is_public == True)
    if q.strip():
        raw_q = q.strip()
        keyword = f"%{raw_q}%"
        conditions = [Problem.title.ilike(keyword), Problem.tags.ilike(keyword), Problem.source.ilike(keyword), Problem.problem_author.ilike(keyword), Problem.error_finder.ilike(keyword), Problem.typo_finder.ilike(keyword)]
        if raw_q.isdigit():
            conditions.append(Problem.id == int(raw_q))
        query = query.filter(or_(*conditions))
    if difficulty.strip():
        query = query.filter(Problem.difficulty == difficulty.strip())
    if tag.strip():
        query = query.filter(Problem.tags.ilike(f"%{tag.strip()}%"))
    problems = query.order_by(Problem.id.asc()).all()
    difficulties = [row[0] for row in db.query(Problem.difficulty).filter(Problem.is_contest_only == False).distinct().order_by(Problem.difficulty.asc()).all() if row[0]]
    return templates.TemplateResponse("index.html", {"request": request, "user": user, "problems": problems, "q": q, "difficulty": difficulty, "tag": tag, "difficulties": difficulties})


def normalize_board_type(board_type: str, *, group: bool = False, allow_all: bool = False) -> str:
    allowed = GROUP_BOARD_WRITE_TYPES if group else BOARD_TYPES
    if allow_all and board_type == "all":
        return board_type
    if board_type not in allowed or board_type == "all":
        raise HTTPException(status_code=404, detail="Board not found")
    return board_type


def can_manage_board_post(user: Optional[User], post: BoardPost) -> bool:
    if user is None:
        return False
    if user.is_admin or post.author_id == user.id:
        return True
    return False


def can_manage_group_board_post(user: Optional[User], group: Group, post: BoardPost) -> bool:
    if user is None:
        return False
    return user.is_admin or post.author_id == user.id or user_can_manage_group(user, group)


@app.get("/boards", response_class=HTMLResponse)
def boards_redirect():
    return RedirectResponse(url="/boards/all", status_code=303)


@app.get("/boards/{board_type}", response_class=HTMLResponse)
def board_list(board_type: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    board_type = normalize_board_type(board_type, allow_all=True)
    query = db.query(BoardPost).filter(BoardPost.board_scope == "site")
    if board_type != "all":
        query = query.filter(BoardPost.board_type == board_type)
    posts = query.order_by(BoardPost.is_pinned.desc(), BoardPost.id.desc()).all()
    can_write = user is not None and board_type != "all" and (board_type != "notice" or user.is_admin)
    return templates.TemplateResponse("board_list.html", {"request": request, "user": user, "board_type": board_type, "board_name": BOARD_TYPES[board_type], "posts": posts, "can_write": can_write, "board_types": BOARD_TYPES})


@app.post("/boards/{board_type}/new")
def create_board_post(board_type: str, request: Request, title: str = Form(...), content: str = Form(""), is_pinned: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    board_type = normalize_board_type(board_type)
    if board_type == "all":
        raise HTTPException(status_code=400, detail="전체 탭에는 글을 직접 작성할 수 없습니다.")
    if board_type == "notice" and not user.is_admin:
        raise HTTPException(status_code=403, detail="공지사항은 관리자만 작성할 수 있습니다.")
    post = BoardPost(board_scope="site", board_type=board_type, display_number=next_board_post_display_number(db, board_scope="site"), author_id=user.id, title=title.strip()[:200], content=content, is_pinned=(is_pinned == "on" and user.is_admin))
    db.add(post)
    db.commit()
    return RedirectResponse(url=f"/boards/posts/{post.id}", status_code=303)


@app.get("/boards/posts/{post_id}", response_class=HTMLResponse)
def board_post_detail(post_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    post = db.query(BoardPost).filter(BoardPost.id == post_id, BoardPost.board_scope == "site").first()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    comments = db.query(BoardComment).filter(BoardComment.post_id == post.id).order_by(BoardComment.id.asc()).all()
    return templates.TemplateResponse("board_post.html", {"request": request, "user": user, "post": post, "board_name": BOARD_TYPES.get(post.board_type, post.board_type), "can_manage_post": can_manage_board_post(user, post), "comments": comments})


@app.post("/boards/posts/{post_id}/edit")
def edit_board_post(post_id: int, request: Request, title: str = Form(...), content: str = Form(""), is_pinned: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    post = db.query(BoardPost).filter(BoardPost.id == post_id, BoardPost.board_scope == "site").first()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if not can_manage_board_post(user, post):
        raise HTTPException(status_code=403, detail="Permission denied")
    if post.board_type == "notice" and not user.is_admin:
        raise HTTPException(status_code=403, detail="공지사항은 관리자만 수정할 수 있습니다.")
    post.title = title.strip()[:200]
    post.content = content
    if user.is_admin:
        post.is_pinned = is_pinned == "on"
    db.commit()
    return RedirectResponse(url=f"/boards/posts/{post.id}", status_code=303)


@app.post("/boards/posts/{post_id}/delete")
def delete_board_post(post_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    post = db.query(BoardPost).filter(BoardPost.id == post_id, BoardPost.board_scope == "site").first()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if not can_manage_board_post(user, post):
        raise HTTPException(status_code=403, detail="Permission denied")
    board_type = post.board_type
    db.query(BoardComment).filter(BoardComment.post_id == post.id).delete()
    db.delete(post)
    db.commit()
    return RedirectResponse(url=f"/boards/{board_type}", status_code=303)


@app.post("/boards/posts/{post_id}/comments")
def create_board_comment(post_id: int, request: Request, content: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    post = db.query(BoardPost).filter(BoardPost.id == post_id, BoardPost.board_scope == "site").first()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if not content.strip():
        return RedirectResponse(url=f"/boards/posts/{post.id}", status_code=303)
    db.add(BoardComment(post_id=post.id, author_id=user.id, content=content.strip()))
    db.commit()
    return RedirectResponse(url=f"/boards/posts/{post.id}", status_code=303)


@app.post("/boards/posts/{post_id}/comments/{comment_id}/delete")
def delete_board_comment(post_id: int, comment_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    comment = db.query(BoardComment).filter(BoardComment.id == comment_id, BoardComment.post_id == post_id).first()
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    post = db.query(BoardPost).filter(BoardPost.id == post_id, BoardPost.board_scope == "site").first()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if not (user.is_admin or comment.author_id == user.id):
        raise HTTPException(status_code=403, detail="Permission denied")
    db.delete(comment)
    db.commit()
    return RedirectResponse(url=f"/boards/posts/{post_id}", status_code=303)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("register.html", {"request": request, "user": get_current_user(request, db)})


@app.post("/register")
def register(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == username).first():
        return templates.TemplateResponse("register.html", {"request": request, "user": None, "error": "이미 존재하는 아이디입니다."})
    db.add(User(username=username, password_hash=hash_password(password), is_admin=False))
    db.commit()
    # 회원가입 직후 자동 로그인 상태가 남지 않도록 세션을 비우고 로그인 화면으로 보낸다.
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("login.html", {"request": request, "user": get_current_user(request, db)})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "user": None, "error": "아이디 또는 비밀번호가 올바르지 않습니다."})
    request.session["user_id"] = user.id
    request.session["username"] = user.username
    request.session["is_admin"] = user.is_admin
    audit_log(db, request, user, "login", "user", user.id, "로그인")
    db.commit()
    if getattr(user, "must_change_password", False):
        return RedirectResponse(url="/change-password", status_code=303)
    return RedirectResponse(url="/", status_code=303)


@app.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    return templates.TemplateResponse("change_password.html", {"request": request, "user": user})


@app.post("/change-password")
def change_password(request: Request, current_password: str = Form(""), new_password: str = Form(...), new_password_confirm: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    if len(new_password) < 4:
        return templates.TemplateResponse("change_password.html", {"request": request, "user": user, "error": "새 비밀번호는 4자 이상이어야 합니다."})
    if new_password != new_password_confirm:
        return templates.TemplateResponse("change_password.html", {"request": request, "user": user, "error": "새 비밀번호 확인이 일치하지 않습니다."})
    if not getattr(user, "must_change_password", False) and not verify_password(current_password, user.password_hash):
        return templates.TemplateResponse("change_password.html", {"request": request, "user": user, "error": "현재 비밀번호가 올바르지 않습니다."})
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


@app.get("/problems/{problem_id}", response_class=HTMLResponse)
def problem_detail(problem_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")
    if problem.is_contest_only:
        raise HTTPException(status_code=404, detail="Problem not found")
    if not problem.is_public and not (user and user.is_admin):
        raise HTTPException(status_code=404, detail="Problem not found")
    return templates.TemplateResponse("problem.html", {"request": request, "user": user, "problem": problem, "contest": None, "link": None, "now": now(), "language_options": language_options_for_problem(problem)})


@app.post("/submit")
def submit(request: Request, problem_id: int = Form(...), language: str = Form(...), code: str = Form(...), visibility: str = Form("private"), contest_id: int | None = Form(None), practice_id: int | None = Form(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    language = normalize_language(language)
    validate_submission_payload(language, code)
    current = now()
    if user.submit_banned_until is not None and user.submit_banned_until > current:
        raise HTTPException(status_code=403, detail=f"제출 제한 중입니다. 제한 종료: {user.submit_banned_until}")

    exam_lock = get_active_school_exam_lock(db, user)
    if exam_lock is not None and contest_id != exam_lock["contest"].id:
        raise HTTPException(status_code=403, detail="학교 분반 그룹 시험 모드 진행 중에는 해당 시험 대회에만 제출할 수 있습니다.")

    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")

    if not problem.is_judge_ready and not (user and user.is_admin):
        raise HTTPException(status_code=403, detail="채점 준비 중인 문제입니다.")
    if not language_allowed_for_problem(problem, language):
        raise HTTPException(status_code=400, detail="이 문제에서 허용되지 않은 언어입니다.")

    practice = None
    if practice_id is not None:
        practice = db.query(GroupPractice).filter(GroupPractice.id == practice_id).first()
        if practice is None:
            raise HTTPException(status_code=404, detail="Practice not found")
        group = practice.group
        if group is None or not is_group_member(user, group):
            raise HTTPException(status_code=403, detail="그룹 회원만 연습 문제를 제출할 수 있습니다.")
        if not group_practice_is_open(practice):
            raise HTTPException(status_code=403, detail="연습 시간이 아니거나 연습이 종료되어 제출할 수 없습니다.")
        if db.query(GroupPracticeProblem).filter(GroupPracticeProblem.practice_id == practice.id, GroupPracticeProblem.problem_id == problem.id).first() is None:
            raise HTTPException(status_code=403, detail="연습에 포함되지 않은 문제입니다.")

    contest = None
    if contest_id is not None:
        contest = db.query(Contest).filter(Contest.id == contest_id).first()
        if contest is None:
            raise HTTPException(status_code=404, detail="Contest not found")
        require_group_contest_access(user, contest, db)
        if not can_submit_in_contest(contest):
            raise HTTPException(status_code=403, detail="대회 시간이 아니거나 대회가 종료되어 제출할 수 없습니다.")
        if not db.query(ContestProblem).filter(ContestProblem.contest_id == contest.id, ContestProblem.problem_id == problem.id).first():
            raise HTTPException(status_code=403, detail="대회에 포함되지 않은 문제입니다.")
    elif practice_id is None and (problem.is_contest_only or not problem.is_public):
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="제출할 수 없는 문제입니다.")

    if visibility not in {"private", "public", "accepted_only"}:
        visibility = "private"
    # 대회 제출은 타인이 볼 수 없도록 항상 비공개 처리
    if contest_id is not None:
        visibility = "private"

    submission = Submission(
        user_id=user.id,
        problem_id=problem_id,
        contest_id=contest_id,
        practice_id=practice_id,
        language=language,
        code=code,
        result="WAITING",
        judge_status="PENDING",
        detail="채점 대기 중입니다.",
        visibility=visibility,
    )
    db.add(submission)
    db.flush()
    enqueue_submission(submission, "채점 대기 중입니다.", db=db, job_type="judge")
    db.commit()
    db.refresh(submission)
    if practice is not None:
        return RedirectResponse(url=f"/groups/{practice.group_id}#practice", status_code=303)
    return RedirectResponse(url=f"/submissions/{submission.id}", status_code=303)


def render_submissions_page(request: Request, db: Session, only_mine: bool):
    user = require_login(request, db) if only_mine else get_current_user(request, db)
    query = db.query(Submission)
    if only_mine:
        query = query.filter(Submission.user_id == user.id)

    problem_id = request.query_params.get("problem_id", "").strip()
    username = request.query_params.get("username", "").strip()
    language = request.query_params.get("language", "").strip()
    result = request.query_params.get("result", "").strip()
    contest_scope = request.query_params.get("contest_scope", "").strip()
    page = max(int(request.query_params.get("page", "1") or 1), 1)
    per_page = int(request.query_params.get("per_page", "50") or 50)
    if per_page not in {25, 50, 100, 200}:
        per_page = 50

    if problem_id:
        if problem_id.isdigit():
            query = query.filter(Submission.problem_id == int(problem_id))
        else:
            query = query.filter(False)
    if username:
        query = query.join(User, Submission.user_id == User.id).filter(User.username.ilike(f"%{username}%"))
    if language:
        query = query.filter(Submission.language == normalize_language(language))
    if result:
        query = query.filter(Submission.result == result)
    if contest_scope == "contest":
        query = query.filter(Submission.contest_id.isnot(None))
    elif contest_scope == "practice":
        query = query.filter(Submission.practice_id.isnot(None))
    elif contest_scope == "normal":
        query = query.filter(Submission.contest_id.is_(None), Submission.practice_id.is_(None))

    total_count = query.count()
    total_pages = max((total_count + per_page - 1) // per_page, 1)
    if page > total_pages:
        page = total_pages
    items = (
        query.options(joinedload(Submission.user), joinedload(Submission.problem))
        .order_by(Submission.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    filters = {"problem_id": problem_id, "username": username, "language": language, "result": result, "contest_scope": contest_scope, "per_page": per_page}
    result_options = [row[0] for row in db.query(Submission.result).distinct().order_by(Submission.result.asc()).all() if row[0]]
    return templates.TemplateResponse("submissions.html", {
        "request": request,
        "user": user,
        "submissions": items,
        "only_mine": only_mine,
        "filters": filters,
        "result_options": result_options,
        "language_options": SUPPORTED_LANGUAGES,
        "visibility_label": visibility_label,
        "get_contest_link": lambda s: get_contest_link_for_submission(db, s),
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_count": total_count,
    })


@app.get("/submissions", response_class=HTMLResponse)
def submissions(request: Request, db: Session = Depends(get_db)):
    return render_submissions_page(request, db, only_mine=False)


@app.get("/my-submissions", response_class=HTMLResponse)
def my_submissions(request: Request, db: Session = Depends(get_db)):
    return render_submissions_page(request, db, only_mine=True)


@app.get("/submissions/{submission_id}", response_class=HTMLResponse)
def submission_detail(submission_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    exam_lock = get_active_school_exam_lock(db, user)
    if exam_lock is not None and submission.contest_id != exam_lock["contest"].id:
        raise HTTPException(status_code=403, detail="학교 분반 그룹 시험 모드 진행 중에는 해당 시험 제출만 확인할 수 있습니다.")
    code_visible = can_view_submission_code(user, submission)
    contest_link = get_contest_link_for_submission(db, submission)
    if code_visible and user and not user.is_admin and submission.user_id and submission.user_id != user.id:
        create_message(db, submission.user_id, "제출 코드 열람 알림", f"{user.username}님이 제출 #{submission.id} 코드를 확인했습니다.", "code_view", related_submission_id=submission.id)
        db.commit()
    detail_message_visible = can_view_submission_detail_message(user, submission)
    return templates.TemplateResponse("submission.html", {"request": request, "user": user, "submission": submission, "contest_link": contest_link, "code_visible": code_visible, "detail_message_visible": detail_message_visible, "visibility_label": visibility_label})


def requeue_stuck_judging_submissions(db: Session, minutes: int = 10, actor: str = "system") -> int:
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    running_jobs = (
        db.query(JudgeJob)
        .filter(JudgeJob.status == "RUNNING")
        .filter(JudgeJob.started_at.isnot(None))
        .filter(JudgeJob.started_at < cutoff)
        .all()
    )
    count = 0
    seen_submission_ids: set[int] = set()
    for job in running_jobs:
        if job.submission_id in seen_submission_ids:
            continue
        seen_submission_ids.add(job.submission_id)
        submission = db.query(Submission).filter(Submission.id == job.submission_id).first()
        job.status = "FAILED"
        job.finished_at = datetime.utcnow()
        job.error_message = "stuck job auto requeued"
        if submission is None:
            continue
        if submission.judge_status != "JUDGING":
            continue
        submission.judge_status = "PENDING"
        submission.result = "WAITING"
        submission.detail = "멈춘 채점으로 감지되어 자동으로 다시 대기열에 등록되었습니다."
        db.add(JudgeJob(submission_id=submission.id, job_type="rejudge", status="QUEUED", priority=0))
        db.add(JudgeLog(submission_id=submission.id, worker_name=actor, event="auto_requeue", message="stuck RUNNING judge job automatically requeued"))
        count += 1
    if count:
        db.commit()
    return count




def requeue_failed_judge_jobs(db: Session, actor: str = "admin") -> int:
    failed_jobs = db.query(JudgeJob).filter(JudgeJob.status == "FAILED").all()
    count = 0
    for job in failed_jobs:
        submission = db.query(Submission).filter(Submission.id == job.submission_id).first()
        if submission is None:
            continue
        enqueue_submission(submission, "실패한 채점 작업이 다시 대기열에 등록되었습니다.", db=db, job_type="rejudge")
        db.add(JudgeLog(submission_id=submission.id, worker_name=actor, event="manual_requeue_failed", message=f"failed judge job #{job.id} requeued"))
        count += 1
    if count:
        db.commit()
    return count


def requeue_single_judge_job(db: Session, job_id: int, actor: str = "admin") -> bool:
    job = db.query(JudgeJob).filter(JudgeJob.id == job_id).first()
    if job is None:
        return False
    submission = db.query(Submission).filter(Submission.id == job.submission_id).first()
    if submission is None:
        job.status = "FAILED"
        job.finished_at = datetime.utcnow()
        job.error_message = "연결된 제출을 찾을 수 없습니다."
        db.commit()
        return False
    enqueue_submission(submission, f"채점 작업 #{job.id}이 다시 대기열에 등록되었습니다.", db=db, job_type="rejudge")
    db.add(JudgeLog(submission_id=submission.id, worker_name=actor, event="manual_requeue_job", message=f"judge job #{job.id} requeued"))
    db.commit()
    return True


def cancel_single_judge_job(db: Session, job_id: int, actor: str = "admin") -> bool:
    job = db.query(JudgeJob).filter(JudgeJob.id == job_id).first()
    if job is None or job.status not in {"QUEUED", "RUNNING"}:
        return False
    job.status = "CANCELED"
    job.finished_at = datetime.utcnow()
    job.error_message = "관리자에 의해 취소됨"
    submission = db.query(Submission).filter(Submission.id == job.submission_id).first()
    if submission is not None and submission.judge_status in {"PENDING", "JUDGING"}:
        submission.judge_status = "FAILED"
        submission.result = "SE"
        submission.detail = "채점 작업이 관리자에 의해 취소되었습니다."
    db.add(JudgeLog(submission_id=job.submission_id, worker_name=actor, event="manual_cancel_job", message=f"judge job #{job.id} canceled"))
    db.commit()
    return True

def count_stuck_running_judge_jobs(db: Session, minutes: int = 10) -> int:
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    return (
        db.query(JudgeJob)
        .filter(JudgeJob.status == "RUNNING")
        .filter(JudgeJob.started_at.isnot(None))
        .filter(JudgeJob.started_at < cutoff)
        .count()
    )


def submission_status_payload(submission: Submission, detail_visible: bool = False) -> dict:
    return {
        "id": submission.id,
        "judge_status": submission.judge_status,
        "result": submission.result,
        "runtime_ms": submission.runtime_ms,
        "memory_kb": submission.memory_kb,
        "detail": submission.detail if detail_visible else "",
        "done": submission.judge_status in {"DONE", "FAILED"},
    }


@app.get("/api/submissions/status-bulk")
def api_submission_status_bulk(ids: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    requeue_stuck_judging_submissions(db, actor="api")
    wanted = []
    for token in ids.replace(" ", ",").split(","):
        if token.strip().isdigit():
            wanted.append(int(token.strip()))
    wanted = wanted[:100]
    if not wanted:
        return {"submissions": []}
    submissions = db.query(Submission).filter(Submission.id.in_(wanted)).all()
    exam_lock = get_active_school_exam_lock(db, user)
    rows = []
    for submission in submissions:
        if exam_lock is not None and submission.contest_id != exam_lock["contest"].id:
            continue
        detail_visible = can_view_submission_detail_message(user, submission)
        rows.append(submission_status_payload(submission, detail_visible))
    return {"submissions": rows}


@app.get("/api/submissions/{submission_id}/events")
def api_submission_events(submission_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    exam_lock = get_active_school_exam_lock(db, user)
    if exam_lock is not None and submission.contest_id != exam_lock["contest"].id:
        raise HTTPException(status_code=403, detail="학교 분반 그룹 시험 모드 진행 중에는 해당 시험 제출만 확인할 수 있습니다.")
    detail_visible = can_view_submission_detail_message(user, submission)

    def event_stream():
        last_payload = None
        for _ in range(180):
            local_db = SessionLocal()
            try:
                current = local_db.query(Submission).filter(Submission.id == submission_id).first()
                if current is None:
                    yield "event: error\ndata: {\"error\": \"not_found\"}\n\n"
                    break
                payload = submission_status_payload(current, detail_visible)
                encoded = json.dumps(payload, ensure_ascii=False)
                if encoded != last_payload:
                    yield f"data: {encoded}\n\n"
                    last_payload = encoded
                if payload["done"]:
                    break
            finally:
                local_db.close()
            time.sleep(1)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/submissions/{submission_id}/status")
def api_submission_status(submission_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    exam_lock = get_active_school_exam_lock(db, user)
    if exam_lock is not None and submission.contest_id != exam_lock["contest"].id:
        raise HTTPException(status_code=403, detail="학교 분반 그룹 시험 모드 진행 중에는 해당 시험 제출만 확인할 수 있습니다.")
    detail_visible = can_view_submission_detail_message(user, submission)
    return submission_status_payload(submission, detail_visible)


@app.get("/api/worker/status")
def api_worker_status(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    auto_requeued = requeue_stuck_judging_submissions(db, actor="worker-status")
    stuck_cutoff = datetime.utcnow() - timedelta(minutes=10)
    heartbeat_cutoff = datetime.utcnow() - timedelta(minutes=2)
    recent_heartbeats = db.query(JudgeLog).filter(JudgeLog.event == "heartbeat").order_by(JudgeLog.id.desc()).limit(20).all()
    active_workers = len({log.worker_name for log in recent_heartbeats if log.created_at and log.created_at >= heartbeat_cutoff})
    return {
        "pending": db.query(Submission).filter(Submission.judge_status == "PENDING").count(),
        "judging": db.query(Submission).filter(Submission.judge_status == "JUDGING").count(),
        "queued_jobs": db.query(JudgeJob).filter(JudgeJob.status == "QUEUED").count(),
        "running_jobs": db.query(JudgeJob).filter(JudgeJob.status == "RUNNING").count(),
        "failed_jobs": db.query(JudgeJob).filter(JudgeJob.status == "FAILED").count(),
        "stuck": count_stuck_running_judge_jobs(db),
        "failed": db.query(Submission).filter(Submission.judge_status == "FAILED").count(),
        "auto_requeued": auto_requeued,
        "active_workers": active_workers,
        "recent_heartbeats": [
            {"time": str(log.created_at), "worker": log.worker_name, "message": log.message}
            for log in recent_heartbeats[:5]
        ],
        "recent_logs": [
            {"time": str(log.created_at), "worker": log.worker_name, "event": log.event, "submission_id": log.submission_id, "message": log.message}
            for log in db.query(JudgeLog).order_by(JudgeLog.id.desc()).limit(20).all()
        ],
    }


@app.get("/admin/worker", response_class=HTMLResponse)
def admin_worker_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    requeue_stuck_judging_submissions(db, actor="worker-page")
    stuck_cutoff = datetime.utcnow() - timedelta(minutes=10)
    heartbeat_cutoff = datetime.utcnow() - timedelta(minutes=2)
    recent_heartbeats = db.query(JudgeLog).filter(JudgeLog.event == "heartbeat").order_by(JudgeLog.id.desc()).limit(20).all()
    active_workers = len({log.worker_name for log in recent_heartbeats if log.created_at and log.created_at >= heartbeat_cutoff})
    pending = db.query(Submission).filter(Submission.judge_status == "PENDING").count()
    judging = db.query(Submission).filter(Submission.judge_status == "JUDGING").count()
    queued_jobs = db.query(JudgeJob).filter(JudgeJob.status == "QUEUED").count()
    running_jobs = db.query(JudgeJob).filter(JudgeJob.status == "RUNNING").count()
    failed_jobs = db.query(JudgeJob).filter(JudgeJob.status == "FAILED").count()
    stuck = count_stuck_running_judge_jobs(db)
    failed = db.query(Submission).filter(Submission.judge_status == "FAILED").count()
    logs = db.query(JudgeLog).order_by(JudgeLog.id.desc()).limit(100).all()
    recent_jobs = db.query(JudgeJob).order_by(JudgeJob.id.desc()).limit(100).all()
    return templates.TemplateResponse("worker_status.html", {"request": request, "user": user, "pending": pending, "judging": judging, "stuck": stuck, "failed": failed, "queued_jobs": queued_jobs, "running_jobs": running_jobs, "failed_jobs": failed_jobs, "recent_jobs": recent_jobs, "logs": logs, "active_workers": active_workers, "recent_heartbeats": recent_heartbeats[:5]})


@app.post("/admin/worker/requeue-stuck")
def admin_requeue_stuck_submissions(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    requeue_stuck_judging_submissions(db, actor="admin")
    return RedirectResponse(url="/admin/worker", status_code=303)


@app.get("/admin/judge-queue", response_class=HTMLResponse)
def admin_judge_queue_page(request: Request, status: str = Query(""), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    statuses = ["QUEUED", "RUNNING", "DONE", "FAILED", "CANCELED"]
    query = db.query(JudgeJob).order_by(JudgeJob.id.desc())
    if status in statuses:
        query = query.filter(JudgeJob.status == status)
    else:
        status = ""
    jobs = query.limit(300).all()
    counts = {key: db.query(JudgeJob).filter(JudgeJob.status == key).count() for key in statuses}
    stuck_cutoff = datetime.utcnow() - timedelta(minutes=10)
    stuck_count = count_stuck_running_judge_jobs(db)
    heartbeat_cutoff = datetime.utcnow() - timedelta(minutes=2)
    recent_heartbeats = db.query(JudgeLog).filter(JudgeLog.event == "heartbeat").order_by(JudgeLog.id.desc()).limit(20).all()
    active_workers = len({log.worker_name for log in recent_heartbeats if log.created_at and log.created_at >= heartbeat_cutoff})
    return templates.TemplateResponse("judge_queue.html", {"request": request, "user": user, "jobs": jobs, "counts": counts, "status": status, "statuses": statuses, "stuck_count": stuck_count, "active_workers": active_workers})


@app.post("/admin/judge-queue/requeue-stuck")
def admin_judge_queue_requeue_stuck(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    requeue_stuck_judging_submissions(db, actor="admin-queue")
    return RedirectResponse(url="/admin/judge-queue", status_code=303)


@app.post("/admin/judge-queue/requeue-failed")
def admin_judge_queue_requeue_failed(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    requeue_failed_judge_jobs(db, actor="admin-queue")
    return RedirectResponse(url="/admin/judge-queue?status=QUEUED", status_code=303)


@app.post("/admin/judge-queue/jobs/{job_id}/requeue")
def admin_judge_queue_requeue_job(job_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    requeue_single_judge_job(db, job_id, actor="admin-queue")
    return RedirectResponse(url="/admin/judge-queue", status_code=303)


@app.post("/admin/judge-queue/jobs/{job_id}/cancel")
def admin_judge_queue_cancel_job(job_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    cancel_single_judge_job(db, job_id, actor="admin-queue")
    return RedirectResponse(url="/admin/judge-queue", status_code=303)


@app.get("/admin/permissions", response_class=HTMLResponse)
def admin_permissions(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    rows = collect_permission_summary(db)
    return templates.TemplateResponse("admin_permissions.html", {"request": request, "user": user, "rows": rows})


@app.get("/admin/judge-logs/{log_id}", response_class=HTMLResponse)
def judge_log_detail(log_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    log = db.query(JudgeLog).filter(JudgeLog.id == log_id).first()
    if log is None:
        raise HTTPException(status_code=404, detail="Judge log not found")
    related_logs = []
    if log.submission_id:
        related_logs = db.query(JudgeLog).filter(JudgeLog.submission_id == log.submission_id).order_by(JudgeLog.id.asc()).all()
    return templates.TemplateResponse("judge_log_detail.html", {"request": request, "user": user, "log": log, "related_logs": related_logs})


@app.get("/admin/system", response_class=HTMLResponse)
def admin_system_status(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    db_ok = True
    db_error = ""
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        db_error = f"{exc.__class__.__name__}: {exc}"

    docker_ok = False
    docker_message = "docker 명령을 찾을 수 없습니다."
    if shutil.which("docker"):
        try:
            result = subprocess.run(["docker", "--version"], capture_output=True, text=True, timeout=3)
            docker_ok = result.returncode == 0
            docker_message = (result.stdout or result.stderr).strip()
        except Exception as exc:
            docker_message = f"{exc.__class__.__name__}: {exc}"

    images = []
    if docker_ok:
        for image in sorted({cfg["image"] for cfg in SUPPORTED_LANGUAGES.values()}):
            found = False
            try:
                result = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True, timeout=5)
                found = result.returncode == 0
            except Exception:
                found = False
            images.append({"name": image, "found": found})

    pending = db.query(Submission).filter(Submission.judge_status == "PENDING").count()
    judging = db.query(Submission).filter(Submission.judge_status == "JUDGING").count()
    queued_jobs = db.query(JudgeJob).filter(JudgeJob.status == "QUEUED").count()
    running_jobs = db.query(JudgeJob).filter(JudgeJob.status == "RUNNING").count()
    failed_jobs = db.query(JudgeJob).filter(JudgeJob.status == "FAILED").count()
    recent_se = db.query(Submission).filter(Submission.result == "SE").order_by(Submission.id.desc()).limit(10).all()
    recent_logs = db.query(JudgeLog).order_by(JudgeLog.id.desc()).limit(20).all()
    return templates.TemplateResponse("system_status.html", {
        "request": request, "user": user, "db_ok": db_ok, "db_error": db_error,
        "docker_ok": docker_ok, "docker_message": docker_message, "images": images,
        "pending": pending, "judging": judging, "recent_se": recent_se, "recent_logs": recent_logs,
    })


@app.get("/contests", response_class=HTMLResponse)
def contests(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    group_contest_ids = {row[0] for row in db.query(GroupContest.contest_id).filter(GroupContest.contest_id.isnot(None)).all()}
    query = db.query(Contest)
    if group_contest_ids:
        query = query.filter(~Contest.id.in_(group_contest_ids))
    contests = query.order_by(Contest.display_number.desc(), Contest.id.desc()).all()
    return templates.TemplateResponse("contests.html", {"request": request, "user": user, "contests": contests, "now": now(), "contest_status": contest_status})


@app.get("/contests/{contest_id}", response_class=HTMLResponse)
def contest_detail(contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_group_contest_access(user, contest, db)

    problem_list_visible = can_view_contest_problem_list(user, contest)
    stats = []
    if problem_list_visible:
        for link in contest.problem_links:
            problem = link.problem
            total = 0
            if user:
                total = db.query(Submission).filter(Submission.user_id == user.id, Submission.contest_id == contest.id, Submission.problem_id == problem.id).count()
            stats.append({"link": link, "problem": problem, "total": total})

    questions_query = db.query(ContestQuestion).filter(ContestQuestion.contest_id == contest.id)
    if not (user and user.is_admin):
        if user:
            questions_query = questions_query.filter((ContestQuestion.is_public == True) | (ContestQuestion.user_id == user.id))
        else:
            questions_query = questions_query.filter(ContestQuestion.is_public == True)
    questions = questions_query.order_by(ContestQuestion.id.desc()).all()
    rankings = build_contest_rankings(db, contest)
    group_contest = db.query(GroupContest).filter(GroupContest.contest_id == contest.id).first()
    contest_manage_allowed = bool(user and user.is_admin)
    if group_contest and group_contest.group is not None and is_group_manager(user, group_contest.group, db):
        contest_manage_allowed = True
    editorial_rows = db.query(ContestEditorial).filter(ContestEditorial.contest_id == contest.id).all()
    editorial_map = {row.problem_id: row for row in editorial_rows}
    editorial_count = sum(1 for row in editorial_rows if (row.content or row.image_path))
    editorial_visible = contest_editorial_visible_to(user, contest, editorial_count)

    return templates.TemplateResponse("contest_detail.html", {
        "request": request,
        "user": user,
        "contest": contest,
        "group_contest": group_contest,
        "now": now(),
        "server_now_ms": to_app_epoch_ms(now()),
        "contest_start_ms": to_app_epoch_ms(contest.start_time),
        "contest_end_ms": to_app_epoch_ms(contest.end_time),
        "stats": stats,
        "problem_list_visible": problem_list_visible,
        "questions": questions,
        "rankings": rankings,
        "ranking_visible": contest_ranking_visible_to(user, contest),
        "contest_closed": contest_is_closed(contest),
        "contest_started": contest_has_started(contest),
        "contest_status": contest_status,
        "editorial_map": editorial_map,
        "editorial_visible": editorial_visible,
        "editorial_count": editorial_count,
        "can_manage_contest": contest_manage_allowed,
        "can_create_new_contest_problem": can_create_group_contest_problem(user, group_contest.group, db) if group_contest else bool(user and user.is_admin),
        "public_problems": (
            db.query(Problem).filter(Problem.is_public == True, Problem.is_contest_only == False).order_by(Problem.id.asc()).all()
            if contest_manage_allowed and group_contest is not None and not (user and user.is_admin)
            else (db.query(Problem).order_by(Problem.is_contest_only.asc(), Problem.id.asc()).all() if contest_manage_allowed else [])
        ),
    })


@app.get("/contests/{contest_id}/problems/{label_or_id}", response_class=HTMLResponse)
def contest_problem(contest_id: int, label_or_id: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    link = resolve_contest_problem(db, contest_id, label_or_id)
    if contest is None or link is None:
        raise HTTPException(status_code=404, detail="Problem not in contest")
    require_group_contest_access(user, contest, db)
    # 시작 전에는 일반 사용자에게 대회 문제 목록/지문을 숨긴다.
    if not contest_has_started(contest) and not (user and user.is_admin):
        return render_contest_404(request, user, contest)
    # 종료 후 대회 전용 문제가 아직 일반 문제로 전환되지 않았다면 숨긴다.
    # 전환된 문제이거나 기존 일반 문제라면 일반 문제 페이지로 보낸다.
    if contest_is_closed(contest):
        if link.problem.is_contest_only:
            return render_contest_404(request, user, contest)
        return RedirectResponse(url=f"/problems/{link.problem.id}", status_code=303)
    return templates.TemplateResponse("problem.html", {"request": request, "user": user, "problem": link.problem, "contest": contest, "link": link, "now": now(), "language_options": language_options_for_problem(link.problem)})


@app.get("/groups/{group_id}/practices/{practice_id}/problems/{problem_id}", response_class=HTMLResponse)
def group_practice_problem(group_id: int, practice_id: int, problem_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    practice = db.query(GroupPractice).filter(GroupPractice.id == practice_id, GroupPractice.group_id == group_id).first()
    item = db.query(GroupPracticeProblem).filter(GroupPracticeProblem.practice_id == practice_id, GroupPracticeProblem.problem_id == problem_id).first()
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if group is None or practice is None or item is None or problem is None:
        raise HTTPException(status_code=404, detail="Practice problem not found")
    if not is_group_member(user, group):
        raise HTTPException(status_code=403, detail="그룹 회원만 연습 문제를 볼 수 있습니다.")
    if problem.is_contest_only and not (user and user.is_admin):
        raise HTTPException(status_code=404, detail="Problem not found")
    return templates.TemplateResponse("problem.html", {"request": request, "user": user, "problem": problem, "contest": None, "link": None, "practice": practice, "group": group, "now": now(), "language_options": language_options_for_problem(problem)})




@app.post("/admin/contests/{contest_id}/editorials/{problem_id}")
def update_contest_editorial(contest_id: int, problem_id: int, request: Request, content: str = Form(""), image: UploadFile = File(None), clear_image: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    link = db.query(ContestProblem).filter(ContestProblem.contest_id == contest_id, ContestProblem.problem_id == problem_id).first()
    if contest is None or link is None:
        raise HTTPException(status_code=404, detail="대회 문제를 찾을 수 없습니다.")
    require_contest_manager(user, contest, db)
    editorial = db.query(ContestEditorial).filter(ContestEditorial.contest_id == contest_id, ContestEditorial.problem_id == problem_id).first()
    if editorial is None:
        editorial = ContestEditorial(contest_id=contest_id, problem_id=problem_id)
        db.add(editorial)
    editorial.content = content.strip()
    if clear_image == "on":
        editorial.image_path = ""
    new_image_path = save_editorial_image(image, contest_id, problem_id)
    if new_image_path:
        editorial.image_path = new_image_path
    editorial.updated_at = now()
    db.commit()
    return RedirectResponse(url=f"/contests/{contest_id}#editorials", status_code=303)


@app.post("/contests/{contest_id}/questions/new")
def create_question(contest_id: int, request: Request, problem_id: str = Form(""), content: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_group_contest_access(user, contest, db)
    if contest.is_exam_mode and not user.is_admin:
        raise HTTPException(status_code=403, detail="시험 모드에서는 질문 탭을 사용할 수 없습니다.")
    require_contest_editable(contest)
    pid = int(problem_id) if problem_id.strip().isdigit() else None
    db.add(ContestQuestion(contest_id=contest.id, problem_id=pid, user_id=user.id, content=content, is_public=False))
    db.commit()
    return RedirectResponse(url=f"/contests/{contest.id}", status_code=303)


@app.post("/admin/questions/{question_id}/answer")
def answer_question(question_id: int, request: Request, answer: str = Form(...), is_public: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    question = db.query(ContestQuestion).filter(ContestQuestion.id == question_id).first()
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    contest = db.query(Contest).filter(Contest.id == question.contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_manager(user, contest, db)
    question.answer = answer
    question.is_public = is_public == "on"
    question.answered_at = now()
    db.commit()
    return RedirectResponse(url=f"/contests/{question.contest_id}", status_code=303)



def get_table_columns_for_check(db: Session, table_name: str) -> list[str]:
    try:
        rows = db.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = :table_name
            ORDER BY ordinal_position
        """), {"table_name": table_name}).fetchall()
        if rows:
            return [row[0] for row in rows]
    except Exception:
        pass
    try:
        rows = db.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        return [row[1] for row in rows]
    except Exception:
        return []


def problem_file_check_rows(db: Session) -> list[dict]:
    rows = []
    for problem in db.query(Problem).order_by(Problem.id.asc()).all():
        p_dir = problem_dir(problem.id)
        tests_dir = p_dir / "tests"
        input_files = sorted(tests_dir.glob("*.in")) if tests_dir.exists() else []
        output_files = sorted(tests_dir.glob("*.out")) if tests_dir.exists() else []
        missing_outputs = [path.name for path in input_files if not path.with_suffix(".out").exists()]
        missing_inputs = [path.name for path in output_files if not path.with_suffix(".in").exists()]
        issues = []
        if not p_dir.exists():
            issues.append("문제 폴더 없음")
        if not tests_dir.exists():
            issues.append("tests 폴더 없음")
        if tests_dir.exists() and not input_files:
            issues.append("입력 테스트케이스 없음")
        if len(input_files) != len(output_files):
            issues.append("입출력 파일 수 불일치")
        if missing_outputs:
            issues.append("출력 파일 누락: " + ", ".join(missing_outputs[:5]))
        if missing_inputs:
            issues.append("입력 파일 누락: " + ", ".join(missing_inputs[:5]))
        if p_dir.exists() and not (p_dir / "meta.json").exists():
            issues.append("meta.json 없음")
        rows.append({
            "problem": problem,
            "input_count": len(input_files),
            "output_count": len(output_files),
            "status": "OK" if not issues else "WARN",
            "issues": issues,
        })
    return rows


def collect_system_checks(db: Session) -> dict:
    expected_columns = {
        "problems": ["origin_type", "origin_group_id", "origin_contest_id", "review_status", "display_code"],
        "groups": ["is_school_group", "school_group_request_status", "school_group_request_file_path", "school_group_request_file_name"],
        "contests": ["display_number", "is_ended"],
        "group_contests": ["display_number", "group_id", "contest_id"],
        "board_posts": ["display_number", "board_scope", "group_id"],
        "judge_jobs": ["submission_id", "job_type", "status", "attempts", "worker_name", "created_at", "started_at", "finished_at"],
    }
    schema_checks = []
    for table_name, columns in expected_columns.items():
        existing = set(get_table_columns_for_check(db, table_name))
        missing = [column for column in columns if column not in existing]
        schema_checks.append({"table": table_name, "status": "OK" if not missing else "WARN", "missing": missing})

    file_rows = problem_file_check_rows(db)
    known_problem_ids = {str(row[0]) for row in db.query(Problem.id).all()}
    problems_root = Path("problems")
    orphan_dirs = []
    if problems_root.exists():
        for child in sorted(problems_root.iterdir(), key=lambda p: p.name):
            if child.is_dir() and child.name.isdigit() and child.name not in known_problem_ids:
                orphan_dirs.append(child.name)

    broken_contest_links = db.query(ContestProblem).outerjoin(Problem, ContestProblem.problem_id == Problem.id).filter(Problem.id == None).count()  # noqa: E711
    broken_submission_problem_links = db.query(Submission).outerjoin(Problem, Submission.problem_id == Problem.id).filter(Problem.id == None).count()  # noqa: E711
    group_contests_without_group = db.query(GroupContest).filter(GroupContest.group_id == None).count()  # noqa: E711
    group_contests_without_contest = db.query(GroupContest).filter(GroupContest.contest_id == None).count()  # noqa: E711
    queued_jobs = db.query(JudgeJob).filter(JudgeJob.status == "QUEUED").count()
    running_jobs = db.query(JudgeJob).filter(JudgeJob.status == "RUNNING").count()
    failed_jobs = db.query(JudgeJob).filter(JudgeJob.status == "FAILED").count()

    warnings = []
    warnings.extend(f"{row['problem'].id}번 문제: {', '.join(row['issues'])}" for row in file_rows if row["issues"])
    if orphan_dirs:
        warnings.append("DB에 없는 문제 폴더: " + ", ".join(orphan_dirs[:20]))
    if broken_contest_links:
        warnings.append(f"존재하지 않는 문제를 가리키는 대회-문제 연결 {broken_contest_links}개")
    if broken_submission_problem_links:
        warnings.append(f"존재하지 않는 문제를 가리키는 제출 {broken_submission_problem_links}개")
    if group_contests_without_contest:
        warnings.append(f"대회 정보가 없는 그룹 대회 연결 {group_contests_without_contest}개")

    return {
        "schema_checks": schema_checks,
        "problem_file_rows": file_rows,
        "orphan_problem_dirs": orphan_dirs,
        "link_checks": {
            "broken_contest_links": broken_contest_links,
            "broken_submission_problem_links": broken_submission_problem_links,
            "group_contests_without_group": group_contests_without_group,
            "group_contests_without_contest": group_contests_without_contest,
            "queued_jobs": queued_jobs,
            "running_jobs": running_jobs,
            "failed_jobs": failed_jobs,
        },
        "warning_count": len(warnings),
        "warnings": warnings,
        "checked_at": now(),
    }


def serialize_model_rows(db: Session, model):
    rows = []
    for obj in db.query(model).all():
        row = {}
        for column in obj.__table__.columns:
            value = getattr(obj, column.name)
            if isinstance(value, datetime):
                value = value.isoformat(sep=" ")
            row[column.name] = value
        rows.append(row)
    return rows


def add_directory_to_zip(zf: zipfile.ZipFile, directory: Path, prefix: str) -> None:
    if not directory.exists():
        return
    for path in directory.rglob("*"):
        if path.is_file():
            zf.write(path, arcname=str(Path(prefix) / path.relative_to(directory)))


@app.get("/admin/diagnostics", response_class=HTMLResponse)
def admin_diagnostics(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    checks = collect_system_checks(db)
    return templates.TemplateResponse("admin_diagnostics.html", {"request": request, "user": user, "checks": checks})


@app.get("/admin/diagnostics.json")
def admin_diagnostics_json(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    checks = collect_system_checks(db)
    payload = {
        "checked_at": checks["checked_at"].isoformat(sep=" "),
        "warning_count": checks["warning_count"],
        "warnings": checks["warnings"],
        "schema_checks": checks["schema_checks"],
        "orphan_problem_dirs": checks["orphan_problem_dirs"],
        "link_checks": checks["link_checks"],
        "problem_file_rows": [
            {
                "problem_id": row["problem"].id,
                "title": row["problem"].title,
                "input_count": row["input_count"],
                "output_count": row["output_count"],
                "status": row["status"],
                "issues": row["issues"],
            }
            for row in checks["problem_file_rows"]
        ],
    }
    return payload


@app.get("/admin/backups", response_class=HTMLResponse)
def admin_backups(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    checks = collect_system_checks(db)
    counts = {
        "users": db.query(User).count(),
        "problems": db.query(Problem).count(),
        "submissions": db.query(Submission).count(),
        "contests": db.query(Contest).count(),
        "groups": db.query(Group).count(),
        "board_posts": db.query(BoardPost).count(),
        "warnings": checks["warning_count"],
    }
    return templates.TemplateResponse("admin_backups.html", {"request": request, "user": user, "counts": counts, "now": now()})


@app.get("/admin/backups/download")
def download_full_backup(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    model_list = [
        User, Problem, ProblemExample, ProblemNote, ProblemHint, Contest, ContestProblem,
        ContestEditorial, Submission, ContestQuestion, Group, GroupMember, GroupJoinRequest,
        GroupProblemSet, GroupProblemSetProblem, GroupPractice, GroupPracticeProblem,
        GroupContest, JudgeJob, JudgeLog, BoardPost, BoardComment, Message,
    ]
    buffer = io.BytesIO()
    created_at = now().strftime("%Y%m%d_%H%M%S")
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "created_at": now().isoformat(sep=" "),
            "type": "online_judge_basic_backup",
            "note": "JSON data plus problems/uploads files. This archive is for preservation and manual recovery, not automatic restore.",
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for model in model_list:
            zf.writestr(f"db/{model.__tablename__}.json", json.dumps(serialize_model_rows(db, model), ensure_ascii=False, indent=2))
        checks = collect_system_checks(db)
        diagnostic_payload = {
            "checked_at": checks["checked_at"].isoformat(sep=" "),
            "warning_count": checks["warning_count"],
            "warnings": checks["warnings"],
            "schema_checks": checks["schema_checks"],
            "orphan_problem_dirs": checks["orphan_problem_dirs"],
            "link_checks": checks["link_checks"],
        }
        zf.writestr("diagnostics.json", json.dumps(diagnostic_payload, ensure_ascii=False, indent=2))
        add_directory_to_zip(zf, Path("problems"), "problems")
        add_directory_to_zip(zf, Path("uploads"), "uploads")
    buffer.seek(0)
    filename = f"online_judge_backup_{created_at}.zip"
    return StreamingResponse(buffer, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    today_start = datetime(now().year, now().month, now().day)
    recent_submissions = db.query(Submission).order_by(Submission.id.desc()).limit(10).all()
    result_rows = db.query(Submission.result, func.count(Submission.id)).group_by(Submission.result).all()
    public_review_problems = db.query(Problem).filter(Problem.origin_type == "group_contest", Problem.review_status == "review_pending").order_by(Problem.id.asc()).all()
    active_ids = active_group_contest_ids(db)
    active_groups = db.query(Group).filter(Group.id.in_(active_ids)).order_by(Group.id.desc()).all() if active_ids else []
    admin_group_contests = db.query(GroupContest).outerjoin(Contest, GroupContest.contest_id == Contest.id).order_by(GroupContest.id.desc()).all()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "user": user,
        "problem_count": db.query(Problem).count(),
        "user_count": db.query(User).count(),
        "submission_count": db.query(Submission).count(),
        "today_submission_count": db.query(Submission).filter(Submission.created_at >= today_start).count(),
        "contest_count": db.query(Contest).count(),
        "group_count": db.query(Group).count(),
        "school_group_pending": db.query(Group).filter(Group.school_group_request_status == "pending").order_by(Group.id.asc()).all(),
        "public_review_problems": public_review_problems,
        "active_groups": active_groups,
        "admin_group_contests": admin_group_contests,
        "contest_status": contest_status,
        "recent_submissions": recent_submissions,
        "result_rows": result_rows,
        "judge_queue_counts": {key: db.query(JudgeJob).filter(JudgeJob.status == key).count() for key in ["QUEUED", "RUNNING", "FAILED"]},
        "active_worker_count": len({log.worker_name for log in db.query(JudgeLog).filter(JudgeLog.event == "heartbeat", JudgeLog.created_at >= datetime.utcnow() - timedelta(minutes=2)).order_by(JudgeLog.id.desc()).limit(20).all()}),
        "ranking_cache_size": len(RANKING_CACHE),
        "slow_request_count": len(SLOW_REQUESTS),
        "recent_audit_logs": db.query(AuditLog).order_by(AuditLog.id.desc()).limit(8).all(),
    })



@app.get("/admin/security", response_class=HTMLResponse)
def admin_security_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    admins = db.query(User).filter(User.is_admin == True).order_by(User.id.asc()).all()  # noqa: E712
    all_users = db.query(User).order_by(User.id.asc()).all()
    return templates.TemplateResponse("admin_security.html", {
        "request": request,
        "user": user,
        "admins": admins,
        "users": all_users,
        "admin_count": len(admins),
    })


@app.post("/admin/security/create-admin")
def admin_security_create_admin(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    student_id: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = require_admin(request, db)
    username = username.strip()
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="아이디는 3자 이상이어야 합니다.")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다.")
    if db.query(User).filter(User.username == username).first() is not None:
        raise HTTPException(status_code=400, detail="이미 사용 중인 아이디입니다.")
    target = User(
        username=username,
        password_hash=hash_password(password),
        is_admin=True,
        full_name=full_name.strip()[:100],
        student_id=student_id.strip()[:50],
        must_change_password=True,
    )
    db.add(target)
    db.flush()
    audit_log(db, request, admin, "admin_create", "user", target.id, f"관리자 계정 생성: {target.username}")
    db.commit()
    return RedirectResponse(url="/admin/security", status_code=303)


@app.post("/admin/security/users/{user_id}/username")
def admin_security_update_username(user_id: int, request: Request, username: str = Form(...), db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    new_username = username.strip()
    if len(new_username) < 3:
        raise HTTPException(status_code=400, detail="아이디는 3자 이상이어야 합니다.")
    exists = db.query(User).filter(User.username == new_username, User.id != target.id).first()
    if exists is not None:
        raise HTTPException(status_code=400, detail="이미 사용 중인 아이디입니다.")
    old_username = target.username
    target.username = new_username
    audit_log(db, request, admin, "admin_username_change", "user", target.id, f"아이디 변경: {old_username} -> {new_username}")
    db.commit()
    if target.id == admin.id:
        request.session["username"] = target.username
    return RedirectResponse(url="/admin/security", status_code=303)


@app.post("/admin/security/users/{user_id}/password")
def admin_security_update_password(
    user_id: int,
    request: Request,
    new_password: str = Form(...),
    force_change: str | None = Form(None),
    db: Session = Depends(get_db),
):
    admin = require_admin(request, db)
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다.")
    target.password_hash = hash_password(new_password)
    target.must_change_password = bool(force_change)
    if target.id != admin.id:
        create_message(db, target.id, "비밀번호 변경 안내", "OJ 관리자가 비밀번호를 변경했습니다.", "notice")
    audit_log(db, request, admin, "admin_password_change", "user", target.id, f"비밀번호 변경: {target.username}")
    db.commit()
    return RedirectResponse(url="/admin/security", status_code=303)


@app.post("/admin/security/users/{user_id}/admin-role")
def admin_security_update_admin_role(user_id: int, request: Request, is_admin: str | None = Form(None), db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    make_admin = bool(is_admin)
    if target.is_admin and not make_admin:
        admin_count = db.query(User).filter(User.is_admin == True).count()  # noqa: E712
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="마지막 관리자 권한은 회수할 수 없습니다.")
        if target.id == admin.id:
            raise HTTPException(status_code=400, detail="현재 로그인한 본인의 관리자 권한은 여기서 회수할 수 없습니다.")
    old = target.is_admin
    target.is_admin = make_admin
    if old != target.is_admin:
        audit_log(db, request, admin, "admin_role_change", "user", target.id, f"관리자 권한 변경: {target.username} -> {'관리자' if target.is_admin else '일반'}")
    db.commit()
    return RedirectResponse(url="/admin/security", status_code=303)



@app.get("/admin/audit-logs", response_class=HTMLResponse)
def admin_audit_logs(
    request: Request,
    action: str = Query(""),
    actor: str = Query(""),
    target_type: str = Query(""),
    page: int = Query(1),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    page = max(page, 1)
    per_page = 50
    query = db.query(AuditLog)
    if action.strip():
        query = query.filter(AuditLog.action.ilike(f"%{action.strip()}%"))
    if actor.strip():
        query = query.filter(AuditLog.actor_username.ilike(f"%{actor.strip()}%"))
    if target_type.strip():
        query = query.filter(AuditLog.target_type == target_type.strip())
    total = query.count()
    logs = query.order_by(AuditLog.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
    actions = [row[0] for row in db.query(AuditLog.action).group_by(AuditLog.action).order_by(AuditLog.action.asc()).all()]
    target_types = [row[0] for row in db.query(AuditLog.target_type).filter(AuditLog.target_type != "").group_by(AuditLog.target_type).order_by(AuditLog.target_type.asc()).all()]
    return templates.TemplateResponse("admin_audit_logs.html", {
        "request": request,
        "user": user,
        "logs": logs,
        "actions": actions,
        "target_types": target_types,
        "filters": {"action": action, "actor": actor, "target_type": target_type},
        "page": page,
        "per_page": per_page,
        "total": total,
    })

@app.get("/admin/performance", response_class=HTMLResponse)
def admin_performance_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    table_counts = [
        {"name": "users", "label": "회원", "count": db.query(User).count()},
        {"name": "problems", "label": "문제", "count": db.query(Problem).count()},
        {"name": "submissions", "label": "제출", "count": db.query(Submission).count()},
        {"name": "contests", "label": "대회", "count": db.query(Contest).count()},
        {"name": "judge_jobs", "label": "채점 작업", "count": db.query(JudgeJob).count()},
    ]
    heavy_contests = (
        db.query(Contest.id, Contest.title, func.count(Submission.id).label("submission_count"))
        .outerjoin(Submission, Submission.contest_id == Contest.id)
        .group_by(Contest.id, Contest.title)
        .order_by(text("submission_count DESC"))
        .limit(10)
        .all()
    )
    queued_jobs = db.query(JudgeJob).filter(JudgeJob.status == "QUEUED").count()
    running_jobs = db.query(JudgeJob).filter(JudgeJob.status == "RUNNING").count()
    failed_jobs = db.query(JudgeJob).filter(JudgeJob.status == "FAILED").count()
    return templates.TemplateResponse("admin_performance.html", {
        "request": request,
        "user": user,
        "table_counts": table_counts,
        "heavy_contests": heavy_contests,
        "slow_requests": list(reversed(SLOW_REQUESTS)),
        "ranking_cache_size": len(RANKING_CACHE),
        "ranking_cache_ttl": RANKING_CACHE_TTL_SECONDS,
        "queue_counts": {"queued": queued_jobs, "running": running_jobs, "failed": failed_jobs},
    })

@app.get("/admin/notifications", response_class=HTMLResponse)
def admin_notifications_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    recent_messages = db.query(Message).order_by(Message.id.desc()).limit(30).all()
    recent_notices = db.query(BoardPost).filter(BoardPost.board_scope == "site", BoardPost.board_type == "notice").order_by(BoardPost.id.desc()).limit(10).all()
    groups = db.query(Group).order_by(Group.id.desc()).limit(100).all()
    contests = db.query(Contest).order_by(Contest.id.desc()).limit(100).all()
    return templates.TemplateResponse("admin_notifications.html", {
        "request": request,
        "user": user,
        "recent_messages": recent_messages,
        "recent_notices": recent_notices,
        "groups": groups,
        "contests": contests,
    })


@app.post("/admin/notifications/site")
def admin_send_site_notification(
    request: Request,
    title: str = Form(...),
    content: str = Form(""),
    create_board_notice: str | None = Form(None),
    pin_notice: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    title = title.strip()[:200]
    if not title:
        raise HTTPException(status_code=400, detail="제목을 입력해야 합니다.")
    targets = db.query(User).order_by(User.id.asc()).all()
    count = create_messages_for_users(db, targets, title, content, "site_notice")
    if create_board_notice == "on":
        post = BoardPost(
            board_scope="site",
            board_type="notice",
            display_number=next_board_post_display_number(db, board_scope="site"),
            author_id=user.id,
            title=title,
            content=content,
            is_pinned=pin_notice == "on",
        )
        db.add(post)
    db.commit()
    return RedirectResponse(url=f"/admin/notifications?sent={count}", status_code=303)


@app.post("/admin/notifications/group")
def admin_send_group_notification(
    request: Request,
    group_id: int = Form(...),
    title: str = Form(...),
    content: str = Form(""),
    create_board_notice: str | None = Form(None),
    pin_notice: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    title = title.strip()[:200]
    if not title:
        raise HTTPException(status_code=400, detail="제목을 입력해야 합니다.")
    count = notify_group_members(db, group, title, content, "group_notice")
    if create_board_notice == "on":
        post = BoardPost(
            board_scope="group",
            board_type="notice",
            group_id=group.id,
            display_number=next_board_post_display_number(db, board_scope="group", group_id=group.id),
            author_id=user.id,
            title=title,
            content=content,
            is_pinned=pin_notice == "on",
        )
        db.add(post)
    db.commit()
    return RedirectResponse(url=f"/admin/notifications?sent={count}", status_code=303)


@app.post("/admin/notifications/contest")
def admin_send_contest_notification(
    request: Request,
    contest_id: int = Form(...),
    title: str = Form(...),
    content: str = Form(""),
    db: Session = Depends(get_db),
):
    require_admin(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    title = title.strip()[:200]
    if not title:
        raise HTTPException(status_code=400, detail="제목을 입력해야 합니다.")
    targets = contest_participant_users(db, contest.id)
    count = create_messages_for_users(db, targets, title, content, "contest_notice")
    db.commit()
    return RedirectResponse(url=f"/admin/notifications?sent={count}", status_code=303)


def build_admin_problem_query(
    db: Session,
    problem_id: str = "",
    registration_type: str = "all",
    keyword: str = "",
    difficulty: str = "",
    tag: str = "",
    source: str = "",
):
    query = db.query(Problem)

    problem_id = (problem_id or "").strip()
    if problem_id:
        if problem_id.isdigit():
            query = query.filter(Problem.id == int(problem_id))
        else:
            query = query.filter(Problem.display_code.ilike(f"%{problem_id}%"))

    allowed_types = {"all", "regular", "contest_only", "group_contest", "review_pending", "public", "private", "judge_ready", "judge_not_ready"}
    if registration_type not in allowed_types:
        registration_type = "all"

    if registration_type == "regular":
        query = query.filter(Problem.is_contest_only == False, Problem.origin_type != "group_contest")  # noqa: E712
    elif registration_type == "contest_only":
        query = query.filter(Problem.is_contest_only == True)  # noqa: E712
    elif registration_type == "group_contest":
        query = query.filter(Problem.origin_type == "group_contest")
    elif registration_type == "review_pending":
        query = query.filter(Problem.review_status == "review_pending")
    elif registration_type == "public":
        query = query.filter(Problem.is_public == True)  # noqa: E712
    elif registration_type == "private":
        query = query.filter(Problem.is_public == False)  # noqa: E712
    elif registration_type == "judge_ready":
        query = query.filter(Problem.is_judge_ready == True)  # noqa: E712
    elif registration_type == "judge_not_ready":
        query = query.filter(Problem.is_judge_ready == False)  # noqa: E712

    keyword = (keyword or "").strip()
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(or_(
            Problem.title.ilike(like),
            Problem.description.ilike(like),
            Problem.tags.ilike(like),
            Problem.source.ilike(like),
            Problem.problem_author.ilike(like),
        ))

    difficulty = (difficulty or "").strip()
    if difficulty:
        query = query.filter(Problem.difficulty.ilike(f"%{difficulty}%"))

    tag = (tag or "").strip()
    if tag:
        query = query.filter(Problem.tags.ilike(f"%{tag}%"))

    source = (source or "").strip()
    if source:
        query = query.filter(Problem.source.ilike(f"%{source}%"))

    return query


@app.get("/admin/problems", response_class=HTMLResponse)
def admin_problem_list(
    request: Request,
    problem_id: str = Query(""),
    registration_type: str = Query("all"),
    keyword: str = Query(""),
    difficulty: str = Query(""),
    tag: str = Query(""),
    source: str = Query(""),
    page: int = Query(1),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    page = max(page, 1)
    per_page = 50
    query = build_admin_problem_query(db, problem_id, registration_type, keyword, difficulty, tag, source)
    total_count = query.count()
    problems = query.order_by(Problem.id.asc()).offset((page - 1) * per_page).limit(per_page).all()
    total_pages = max((total_count + per_page - 1) // per_page, 1)

    return templates.TemplateResponse("admin_problems.html", {
        "request": request,
        "user": user,
        "problems": problems,
        "total_count": total_count,
        "problem_id": (problem_id or "").strip(),
        "registration_type": registration_type if registration_type else "all",
        "keyword": (keyword or "").strip(),
        "difficulty": (difficulty or "").strip(),
        "tag": (tag or "").strip(),
        "source": (source or "").strip(),
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    })


@app.post("/admin/problems/bulk-rejudge")
def admin_bulk_rejudge_problems(
    request: Request,
    problem_id: str = Form(""),
    registration_type: str = Form("all"),
    keyword: str = Form(""),
    difficulty: str = Form(""),
    tag: str = Form(""),
    source: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    query = build_admin_problem_query(db, problem_id, registration_type, keyword, difficulty, tag, source)
    problem_ids = [row[0] for row in query.with_entities(Problem.id).all()]
    if not problem_ids:
        return templates.TemplateResponse("rejudge_result.html", {"request": request, "user": user, "problem": None, "count": 0, "message": "조건에 맞는 문제가 없습니다."})

    submissions = db.query(Submission).filter(Submission.problem_id.in_(problem_ids)).order_by(Submission.id.asc()).all()
    count = 0
    for submission in submissions:
        rejudge_submission(submission, db=db)
        count += 1
        if count % 100 == 0:
            db.commit()
    create_message(db, user.id, "대량 재채점 등록 완료", f"검색 조건에 해당하는 {len(problem_ids)}개 문제의 제출 {count}건을 재채점 큐에 등록했습니다.", "rejudge_notice")
    audit_log(db, request, user, "bulk_rejudge", "problem", None, f"{len(problem_ids)}개 문제, {count}건 재채점 등록")
    db.commit()
    return templates.TemplateResponse("rejudge_result.html", {
        "request": request,
        "user": user,
        "problem": None,
        "count": count,
        "message": f"검색 조건에 해당하는 {len(problem_ids)}개 문제의 제출을 재채점 큐에 등록했습니다.",
    })


@app.post("/admin/problems/{problem_id}/copy")
def copy_problem(
    problem_id: int,
    request: Request,
    new_problem_id: int = Form(...),
    title: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    source_problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if source_problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")
    if db.query(Problem).filter(Problem.id == new_problem_id).first():
        raise HTTPException(status_code=400, detail="이미 존재하는 문제 번호입니다.")

    copied = Problem(
        id=new_problem_id,
        title=(title.strip() or f"{source_problem.title} 복사본"),
        description=source_problem.description,
        input_description=source_problem.input_description,
        output_description=source_problem.output_description,
        time_limit=source_problem.time_limit,
        memory_limit=source_problem.memory_limit,
        is_contest_only=False,
        is_public=False,
        force_private_submission=source_problem.force_private_submission,
        is_judge_ready=source_problem.is_judge_ready,
        difficulty=source_problem.difficulty,
        tags=source_problem.tags,
        source=source_problem.source,
        problem_author=source_problem.problem_author,
        error_finder=source_problem.error_finder,
        typo_finder=source_problem.typo_finder,
        allowed_languages=source_problem.allowed_languages,
        origin_type="regular",
        review_status="none",
        display_code=None,
    )
    db.add(copied)
    db.flush()

    for example in source_problem.examples:
        db.add(ProblemExample(problem_id=copied.id, input_text=example.input_text, output_text=example.output_text, order_index=example.order_index))
    for note in source_problem.notes:
        db.add(ProblemNote(problem_id=copied.id, content=note.content, order_index=note.order_index))
    for hint in source_problem.hints:
        db.add(ProblemHint(problem_id=copied.id, content=hint.content, order_index=hint.order_index))

    src_dir = problem_dir(source_problem.id)
    dst_dir = problem_dir(copied.id)
    if src_dir.exists():
        if dst_dir.exists():
            shutil.rmtree(dst_dir, ignore_errors=True)
        shutil.copytree(src_dir, dst_dir)
        meta_path = dst_dir / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["id"] = copied.id
                meta["title"] = copied.title
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

    audit_log(db, request, user, "problem_copy", "problem", copied.id, f"{source_problem.id}번 문제를 {copied.id}번으로 복제")
    db.commit()
    return RedirectResponse(url=f"/admin/problems/{copied.id}/edit", status_code=303)


@app.get("/admin/problems/new", response_class=HTMLResponse)
def new_problem_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    return templates.TemplateResponse("problem_form.html", {"request": request, "user": user, "problem": None, "test_inputs": "1 2\n---\n10 20", "test_outputs": "3\n---\n30", "testcases": [], "sample_inputs": "1 2", "sample_outputs": "3", "action": "/admin/problems/new", "notes_text": "", "hints_text": ""})


@app.post("/admin/problems/new")
def create_problem(request: Request, problem_id: int = Form(...), title: str = Form(...), description: str = Form(...), input_description: str = Form(...), output_description: str = Form(...), time_limit: int = Form(...), memory_limit: int = Form(...), difficulty: str = Form("미지정"), tags: str = Form(""), source: str = Form(""), problem_author: str = Form(""), error_finder: str = Form(""), typo_finder: str = Form(""), test_inputs: str = Form(...), test_outputs: str = Form(...), sample_inputs: str = Form(""), sample_outputs: str = Form(""), notes_text: str = Form(""), hints_text: str = Form(""), is_public: str | None = Form(None), is_judge_ready: str | None = Form(None), force_private_submission: str | None = Form(None), allowed_languages: str = Form("python,c,cpp,java"), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if db.query(Problem).filter(Problem.id == problem_id).first():
        return templates.TemplateResponse("problem_form.html", {"request": request, "user": user, "error": "이미 존재하는 문제 번호입니다.", "problem": None, "test_inputs": test_inputs, "test_outputs": test_outputs, "testcases": [], "sample_inputs": sample_inputs, "sample_outputs": sample_outputs, "action": "/admin/problems/new", "notes_text": notes_text, "hints_text": hints_text})
    try:
        if time_limit < 1 or time_limit > 10 or memory_limit < 16 or memory_limit > 1024:
            raise ValueError("시간 제한은 1~10초, 메모리 제한은 16~1024MB 범위여야 합니다.")
        save_problem_files(problem_id, title, description, input_description, output_description, time_limit, memory_limit, test_inputs, test_outputs, ",".join(parse_allowed_languages(allowed_languages)))
    except ValueError as e:
        return templates.TemplateResponse("problem_form.html", {"request": request, "user": user, "error": str(e), "problem": None, "test_inputs": test_inputs, "test_outputs": test_outputs, "testcases": [], "sample_inputs": sample_inputs, "sample_outputs": sample_outputs, "action": "/admin/problems/new", "notes_text": notes_text, "hints_text": hints_text})
    problem = Problem(id=problem_id, title=title, description=description, input_description=input_description, output_description=output_description, time_limit=time_limit, memory_limit=memory_limit, difficulty=difficulty.strip() or "미지정", tags=tags.strip(), source=source.strip(), problem_author=problem_author.strip(), error_finder=error_finder.strip(), typo_finder=typo_finder.strip(), allowed_languages=",".join(parse_allowed_languages(allowed_languages)), is_contest_only=False, is_public=is_public == "on", is_judge_ready=is_judge_ready == "on", force_private_submission=force_private_submission == "on")
    db.add(problem)
    db.flush()
    try:
        save_problem_examples(db, problem, sample_inputs, sample_outputs)
        save_problem_notes_and_hints(db, problem, notes_text, hints_text)
    except ValueError as e:
        db.rollback()
        return templates.TemplateResponse("problem_form.html", {"request": request, "user": user, "error": str(e), "problem": None, "test_inputs": test_inputs, "test_outputs": test_outputs, "testcases": [], "sample_inputs": sample_inputs, "sample_outputs": sample_outputs, "action": "/admin/problems/new", "notes_text": notes_text, "hints_text": hints_text})
    audit_log(db, request, user, "problem_create", "problem", problem.id, f"문제 생성: {problem.title}")
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/admin/problems/{problem_id}/edit", response_class=HTMLResponse)
def edit_problem_page(problem_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")
    if not can_edit_problem(user, problem, db):
        raise HTTPException(status_code=403, detail="문제 수정 권한이 없습니다.")
    test_inputs, test_outputs = read_problem_tests(problem.id)
    sample_inputs, sample_outputs = read_problem_examples(problem)
    return templates.TemplateResponse("problem_form.html", {"request": request, "user": user, "problem": problem, "test_inputs": test_inputs, "test_outputs": test_outputs, "testcases": read_problem_testcases(problem.id), "sample_inputs": sample_inputs, "sample_outputs": sample_outputs, "action": f"/admin/problems/{problem.id}/edit", "notes_text": read_problem_notes(problem), "hints_text": read_problem_hints(problem), "can_manage_public_settings": can_manage_problem_public_settings(user, problem)})


@app.post("/admin/problems/{problem_id}/edit")
def edit_problem(problem_id: int, request: Request, title: str = Form(...), description: str = Form(...), input_description: str = Form(...), output_description: str = Form(...), time_limit: int = Form(...), memory_limit: int = Form(...), difficulty: str = Form("미지정"), tags: str = Form(""), source: str = Form(""), problem_author: str = Form(""), error_finder: str = Form(""), typo_finder: str = Form(""), test_inputs: str = Form(...), test_outputs: str = Form(...), sample_inputs: str = Form(""), sample_outputs: str = Form(""), notes_text: str = Form(""), hints_text: str = Form(""), is_public: str | None = Form(None), is_judge_ready: str | None = Form(None), force_private_submission: str | None = Form(None), allowed_languages: str = Form("python,c,cpp,java"), promote: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")
    if not can_edit_problem(user, problem, db):
        raise HTTPException(status_code=403, detail="문제 수정 권한이 없습니다.")
    try:
        if time_limit < 1 or time_limit > 10 or memory_limit < 16 or memory_limit > 1024:
            raise ValueError("시간 제한은 1~10초, 메모리 제한은 16~1024MB 범위여야 합니다.")
        save_problem_files(problem_id, title, description, input_description, output_description, time_limit, memory_limit, test_inputs, test_outputs, ",".join(parse_allowed_languages(allowed_languages)))
    except ValueError as e:
        return templates.TemplateResponse("problem_form.html", {"request": request, "user": user, "error": str(e), "problem": problem, "test_inputs": test_inputs, "test_outputs": test_outputs, "testcases": read_problem_testcases(problem.id), "sample_inputs": sample_inputs, "sample_outputs": sample_outputs, "action": f"/admin/problems/{problem.id}/edit", "notes_text": read_problem_notes(problem), "hints_text": read_problem_hints(problem)})
    problem.title = title
    problem.description = description
    problem.input_description = input_description
    problem.output_description = output_description
    problem.time_limit = time_limit
    problem.memory_limit = memory_limit
    problem.difficulty = difficulty.strip() or "미지정"
    problem.tags = tags.strip()
    problem.source = source.strip()
    problem.problem_author = problem_author.strip()
    problem.error_finder = error_finder.strip()
    problem.typo_finder = typo_finder.strip()
    problem.allowed_languages = ",".join(parse_allowed_languages(allowed_languages))
    if can_manage_problem_public_settings(user, problem):
        problem.is_public = is_public == "on"
        problem.force_private_submission = force_private_submission == "on"
    problem.is_judge_ready = is_judge_ready == "on"
    try:
        save_problem_examples(db, problem, sample_inputs, sample_outputs)
        save_problem_notes_and_hints(db, problem, notes_text, hints_text)
    except ValueError as e:
        db.rollback()
        return templates.TemplateResponse("problem_form.html", {"request": request, "user": user, "error": str(e), "problem": problem, "test_inputs": test_inputs, "test_outputs": test_outputs, "testcases": read_problem_testcases(problem.id), "sample_inputs": sample_inputs, "sample_outputs": sample_outputs, "action": f"/admin/problems/{problem.id}/edit", "notes_text": read_problem_notes(problem), "hints_text": read_problem_hints(problem)})
    promoted = False
    if promote == "on" and can_manage_problem_public_settings(user, problem):
        promoted = True
        problem.is_contest_only = False
        problem.is_public = True
        problem.force_private_submission = False
        problem.review_status = "approved"
        problem.display_code = None
    audit_log(db, request, user, "problem_promote" if promoted else "problem_edit", "problem", problem.id, f"문제 수정: {problem.title}")
    db.commit()
    return RedirectResponse(url=f"/admin/problems/{problem.id}/edit", status_code=303)


@app.post("/admin/problems/{problem_id}/rejudge")
def rejudge_problem(problem_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")

    submissions = db.query(Submission).filter(Submission.problem_id == problem.id).order_by(Submission.id.asc()).all()
    count = 0
    for submission in submissions:
        rejudge_submission(submission, db=db)
        count += 1
        # 너무 오래 트랜잭션을 잡지 않도록 제출 하나마다 반영한다.
        db.commit()
    create_message(db, user.id, "문제 재채점 등록 완료", f"{problem.id}번 문제의 제출 {count}건을 재채점 큐에 등록했습니다.", "rejudge_notice")
    audit_log(db, request, user, "problem_rejudge", "problem", problem.id, f"{count}건 재채점 등록")
    db.commit()
    return templates.TemplateResponse("rejudge_result.html", {
        "request": request,
        "user": user,
        "problem": problem,
        "count": count,
    })


@app.post("/admin/submissions/{submission_id}/rejudge")
def rejudge_single_submission(submission_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    rejudge_submission(submission, db=db)
    audit_log(db, request, user, "submission_rejudge", "submission", submission.id, f"제출 #{submission.id} 재채점 등록")
    db.commit()
    return RedirectResponse(url=f"/submissions/{submission.id}", status_code=303)


@app.post("/admin/submissions/{submission_id}/delete")
def delete_submission_admin(submission_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    audit_log(db, request, user, "submission_delete", "submission", submission.id, f"제출 삭제: #{submission.id}")
    delete_submission_tree(db, submission)
    db.commit()
    return RedirectResponse(url="/submissions", status_code=303)


@app.post("/admin/problems/{problem_id}/delete")
def delete_problem_admin(problem_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")
    audit_log(db, request, user, "problem_delete", "problem", problem.id, f"문제 삭제: {problem.title}")
    delete_problem_tree(db, problem)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/admin/problems/{problem_id}/testcases/add")
def add_testcase_route(problem_id: int, request: Request, input_text: str = Form(""), output_text: str = Form(""), db: Session = Depends(get_db)):
    require_admin(request, db)
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")
    append_problem_testcase(problem.id, input_text, output_text)
    return RedirectResponse(url=f"/admin/problems/{problem.id}/edit#testcases", status_code=303)


@app.post("/admin/problems/{problem_id}/testcases/{case_index}/edit")
def edit_testcase_route(problem_id: int, case_index: int, request: Request, input_text: str = Form(""), output_text: str = Form(""), db: Session = Depends(get_db)):
    require_admin(request, db)
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")
    try:
        update_problem_testcase(problem.id, case_index, input_text, output_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url=f"/admin/problems/{problem.id}/edit#testcases", status_code=303)


@app.post("/admin/problems/{problem_id}/testcases/{case_index}/delete")
def delete_testcase_route(problem_id: int, case_index: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")
    try:
        delete_problem_testcase(problem.id, case_index)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url=f"/admin/problems/{problem.id}/edit#testcases", status_code=303)


@app.post("/admin/contests/{contest_id}/rejudge")
def rejudge_contest(contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    submissions = db.query(Submission).filter(Submission.contest_id == contest.id).order_by(Submission.id.asc()).all()
    count = 0
    for submission in submissions:
        rejudge_submission(submission, db=db)
        count += 1
        db.commit()
    return templates.TemplateResponse("rejudge_result.html", {"request": request, "user": user, "problem": None, "contest": contest, "count": count})


@app.post("/admin/group-practices/{practice_id}/rejudge")
def rejudge_group_practice(practice_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    practice = db.query(GroupPractice).filter(GroupPractice.id == practice_id).first()
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice not found")
    group = db.query(Group).filter(Group.id == practice.group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    submissions = db.query(Submission).filter(Submission.practice_id == practice.id).order_by(Submission.id.asc()).all()
    for submission in submissions:
        rejudge_submission(submission)
        db.commit()
    return RedirectResponse(url=f"/groups/{practice.group_id}#practice", status_code=303)


@app.get("/groups", response_class=HTMLResponse)
def groups_page(request: Request, q: str = "", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    query = db.query(Group)
    if q.strip():
        query = query.filter(Group.name.ilike(f"%{q.strip()}%"))
    active_ids = active_group_contest_ids(db) if user and user.is_admin else set()
    all_groups = query.order_by(Group.id.desc()).all()
    groups = [group for group in all_groups if can_view_group(user, group)]
    if user and user.is_admin:
        groups.sort(key=lambda group: (0 if group.id in active_ids else 1, -group.id))
    return templates.TemplateResponse("groups.html", {"request": request, "user": user, "groups": groups, "q": q, "active_group_ids": active_ids})


@app.get("/groups/new", response_class=HTMLResponse)
def new_group_page(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    return templates.TemplateResponse("group_form.html", {"request": request, "user": user, "error": None})


@app.post("/groups/new")
def create_group(request: Request, name: str = Form(...), description: str = Form(""), is_public: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    if db.query(Group).filter(Group.name == name).first():
        return templates.TemplateResponse("group_form.html", {"request": request, "user": user, "error": "이미 존재하는 그룹 이름입니다."})
    group = Group(name=name, description=description, owner_id=user.id, is_public=is_public == "on")
    db.add(group)
    db.flush()
    ensure_group_owner_membership(db, group, user)
    db.commit()
    db.refresh(group)
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.get("/groups/{group_id}", response_class=HTMLResponse)
def group_detail(group_id: int, request: Request, board_type: str = Query("all"), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    if not can_view_group(user, group):
        raise HTTPException(status_code=404, detail="Group not found")
    membership = None
    if user:
        membership = db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == user.id).first()
    pending_request = None
    if user:
        pending_request = db.query(GroupJoinRequest).filter(GroupJoinRequest.group_id == group.id, GroupJoinRequest.user_id == user.id, GroupJoinRequest.status == "pending").first()
    can_manage = user_can_manage_group(user, group)
    join_requests = db.query(GroupJoinRequest).filter(GroupJoinRequest.group_id == group.id, GroupJoinRequest.status == "pending").order_by(GroupJoinRequest.id.asc()).all() if can_manage else []
    problem_sets = db.query(GroupProblemSet).filter(GroupProblemSet.group_id == group.id).order_by(GroupProblemSet.id.desc()).all()
    group_contests = db.query(GroupContest).filter(GroupContest.group_id == group.id).order_by(GroupContest.id.desc()).all()
    if not can_manage:
        group_contests = [gc for gc in group_contests if gc.contest and gc.contest.is_public]
    group_contest_counts = {}
    for group_contest in group_contests:
        if group_contest.contest_id:
            group_contest_counts[group_contest.id] = db.query(ContestProblem).filter(ContestProblem.contest_id == group_contest.contest_id).count()
        else:
            group_contest_counts[group_contest.id] = 0
    practices = db.query(GroupPractice).filter(GroupPractice.group_id == group.id).order_by(GroupPractice.id.desc()).all()
    active_group_board_type = board_type if board_type in GROUP_BOARD_TABS else "all"
    group_posts_query = db.query(BoardPost).filter(BoardPost.board_scope == "group", BoardPost.group_id == group.id)
    if active_group_board_type != "all":
        group_posts_query = group_posts_query.filter(BoardPost.board_type == active_group_board_type)
    group_posts = group_posts_query.order_by(BoardPost.is_pinned.desc(), BoardPost.id.desc()).limit(30).all()
    problem_set_items = {ps.id: db.query(GroupProblemSetProblem).filter(GroupProblemSetProblem.problem_set_id == ps.id).order_by(GroupProblemSetProblem.order_index.asc(), GroupProblemSetProblem.id.asc()).all() for ps in problem_sets}
    practice_items = {practice.id: db.query(GroupPracticeProblem).filter(GroupPracticeProblem.practice_id == practice.id).order_by(GroupPracticeProblem.order_index.asc(), GroupPracticeProblem.id.asc()).all() for practice in practices}
    practice_boards = {practice.id: build_practice_board(db, practice, list(group.members), practice_items.get(practice.id, [])) for practice in practices}
    selectable_contests = db.query(Contest).order_by(Contest.id.desc()).all()
    can_manage_members = is_group_owner_or_site_admin(user, group)
    default_start_dt, default_end_dt = default_event_times()
    return templates.TemplateResponse("group_detail.html", {
        "request": request,
        "user": user,
        "group": group,
        "membership": membership,
        "can_manage": can_manage,
        "can_manage_members": can_manage_members,
        "practice_is_closed": group_practice_is_closed,
        "practice_is_open": group_practice_is_open,
        "pending_request": pending_request,
        "join_requests": join_requests,
        "problem_sets": problem_sets,
        "group_contests": group_contests,
        "group_contest_counts": group_contest_counts,
        "practices": practices,
        "group_posts": group_posts,
        "active_group_board_type": active_group_board_type,
        "problem_set_items": problem_set_items,
        "practice_items": practice_items,
        "practice_boards": practice_boards,
        "selectable_contests": selectable_contests,
        "default_start": format_datetime_local(default_start_dt),
        "default_end": format_datetime_local(default_end_dt),
    })


@app.get("/groups/{group_id}/practices/{practice_id}", response_class=HTMLResponse)
def group_practice_detail(group_id: int, practice_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    practice = db.query(GroupPractice).filter(GroupPractice.id == practice_id, GroupPractice.group_id == group_id).first()
    if group is None or practice is None or not can_view_group(user, group):
        raise HTTPException(status_code=404, detail="Practice not found")
    if not (user and (is_group_member(user, group) or user_can_manage_group(user, group))):
        raise HTTPException(status_code=403, detail="그룹 회원만 연습을 볼 수 있습니다.")
    can_manage = user_can_manage_group(user, group)
    items = db.query(GroupPracticeProblem).filter(GroupPracticeProblem.practice_id == practice.id).order_by(GroupPracticeProblem.order_index.asc(), GroupPracticeProblem.id.asc()).all()
    members = list(group.members)
    board = build_practice_board(db, practice, members, items)
    progress_rows = build_group_problem_progress(db, members, [item.problem_id for item in items], practice_id=practice.id)
    return templates.TemplateResponse("group_practice_detail.html", {
        "request": request,
        "user": user,
        "group": group,
        "practice": practice,
        "items": items,
        "board": board,
        "progress_rows": progress_rows,
        "can_manage": can_manage,
        "practice_is_closed": group_practice_is_closed,
        "practice_is_open": group_practice_is_open,
    })


@app.post("/groups/{group_id}/board/new")
def create_group_board_post(group_id: int, request: Request, board_type: str = Form("general"), title: str = Form(...), content: str = Form(""), is_pinned: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    if not (is_group_member(user, group) or user_can_manage_group(user, group)):
        raise HTTPException(status_code=403, detail="Permission denied")
    board_type = normalize_board_type(board_type, group=True)
    if board_type == "notice" and not user_can_manage_group(user, group):
        raise HTTPException(status_code=403, detail="그룹 공지는 그룹 관리자 이상만 작성할 수 있습니다.")
    post = BoardPost(board_scope="group", board_type=board_type, display_number=next_board_post_display_number(db, board_scope="group", group_id=group.id), group_id=group.id, author_id=user.id, title=title.strip()[:200], content=content, is_pinned=(is_pinned == "on" and user_can_manage_group(user, group)))
    db.add(post)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}/board/{post.id}", status_code=303)


@app.post("/groups/{group_id}/notifications/send")
def send_group_notification_from_group(
    group_id: int,
    request: Request,
    title: str = Form(...),
    content: str = Form(""),
    create_board_notice: str | None = Form(None),
    pin_notice: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    if not user_can_manage_group(user, group):
        raise HTTPException(status_code=403, detail="그룹 알림은 그룹 관리자 이상만 보낼 수 있습니다.")
    title = title.strip()[:200]
    if not title:
        raise HTTPException(status_code=400, detail="제목을 입력해야 합니다.")
    notify_group_members(db, group, title, content, "group_notice")
    if create_board_notice == "on":
        db.add(BoardPost(
            board_scope="group",
            board_type="notice",
            group_id=group.id,
            display_number=next_board_post_display_number(db, board_scope="group", group_id=group.id),
            author_id=user.id,
            title=title,
            content=content,
            is_pinned=pin_notice == "on",
        ))
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#board", status_code=303)


@app.get("/groups/{group_id}/board/{post_id}", response_class=HTMLResponse)
def group_board_post_detail(group_id: int, post_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None or not can_view_group(user, group):
        raise HTTPException(status_code=404, detail="Group not found")
    if not (user and (is_group_member(user, group) or user_can_manage_group(user, group))):
        raise HTTPException(status_code=403, detail="그룹 게시판은 그룹 회원만 볼 수 있습니다.")
    post = db.query(BoardPost).filter(BoardPost.id == post_id, BoardPost.board_scope == "group", BoardPost.group_id == group.id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    comments = db.query(BoardComment).filter(BoardComment.post_id == post.id).order_by(BoardComment.id.asc()).all()
    return templates.TemplateResponse("group_board_post.html", {"request": request, "user": user, "group": group, "post": post, "board_name": GROUP_BOARD_TYPES.get(post.board_type, post.board_type), "can_manage_post": can_manage_group_board_post(user, group, post), "can_pin_post": user_can_manage_group(user, group), "comments": comments})


@app.post("/groups/{group_id}/board/{post_id}/edit")
def edit_group_board_post(group_id: int, post_id: int, request: Request, board_type: str = Form("general"), title: str = Form(...), content: str = Form(""), is_pinned: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    post = db.query(BoardPost).filter(BoardPost.id == post_id, BoardPost.board_scope == "group", BoardPost.group_id == group.id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if not can_manage_group_board_post(user, group, post):
        raise HTTPException(status_code=403, detail="Permission denied")
    board_type = normalize_board_type(board_type, group=True)
    if board_type == "notice" and not user_can_manage_group(user, group):
        raise HTTPException(status_code=403, detail="그룹 공지는 그룹 관리자 이상만 사용할 수 있습니다.")
    post.board_type = board_type
    post.title = title.strip()[:200]
    post.content = content
    if user_can_manage_group(user, group):
        post.is_pinned = is_pinned == "on"
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}/board/{post.id}", status_code=303)


@app.post("/groups/{group_id}/board/{post_id}/delete")
def delete_group_board_post(group_id: int, post_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    post = db.query(BoardPost).filter(BoardPost.id == post_id, BoardPost.board_scope == "group", BoardPost.group_id == group.id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if not can_manage_group_board_post(user, group, post):
        raise HTTPException(status_code=403, detail="Permission denied")
    group_id_value = group.id
    db.query(BoardComment).filter(BoardComment.post_id == post.id).delete()
    db.delete(post)
    db.commit()
    return RedirectResponse(url=f"/groups/{group_id_value}#board", status_code=303)


@app.post("/groups/{group_id}/board/{post_id}/comments")
def create_group_board_comment(group_id: int, post_id: int, request: Request, content: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None or not can_view_group(user, group):
        raise HTTPException(status_code=404, detail="Group not found")
    if not (is_group_member(user, group) or user_can_manage_group(user, group)):
        raise HTTPException(status_code=403, detail="Permission denied")
    post = db.query(BoardPost).filter(BoardPost.id == post_id, BoardPost.board_scope == "group", BoardPost.group_id == group.id).first()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    if content.strip():
        db.add(BoardComment(post_id=post.id, author_id=user.id, content=content.strip()))
        db.commit()
    return RedirectResponse(url=f"/groups/{group.id}/board/{post.id}", status_code=303)


@app.post("/groups/{group_id}/board/{post_id}/comments/{comment_id}/delete")
def delete_group_board_comment(group_id: int, post_id: int, comment_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    comment = db.query(BoardComment).filter(BoardComment.id == comment_id, BoardComment.post_id == post_id).first()
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if not (user.is_admin or user_can_manage_group(user, group) or comment.author_id == user.id):
        raise HTTPException(status_code=403, detail="Permission denied")
    db.delete(comment)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}/board/{post_id}", status_code=303)


@app.post("/groups/{group_id}/join")
def join_group(group_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    if not group.is_public:
        raise HTTPException(status_code=404, detail="Group not found")
    if is_group_member(user, group):
        return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)

    pending = db.query(GroupJoinRequest).filter(
        GroupJoinRequest.group_id == group.id,
        GroupJoinRequest.user_id == user.id,
        GroupJoinRequest.status == "pending",
    ).first()
    if pending is None:
        db.add(GroupJoinRequest(group_id=group.id, user_id=user.id, status="pending"))
        create_message(
            db,
            group.owner_id,
            "그룹 가입 신청",
            f"{user.username}님이 {group.name} 그룹 가입을 신청했습니다. 그룹 상세 화면의 가입 신청 관리에서 승인/거절할 수 있습니다.",
            "notice",
            related_group_id=group.id,
        )
        db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.post("/groups/{group_id}/leave")
def leave_group(group_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    membership = db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == user.id).first()
    if membership and membership.role != "owner":
        db.delete(membership)
        db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)



@app.post("/groups/{group_id}/delete")
def delete_group(group_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_site_admin(user, group)

    group_name = group.name
    audit_log(db, request, user, "group_delete", "group", group.id, f"그룹 삭제: {group_name}")
    # 서비스 계층에서 하위 항목/연습 제출 참조까지 안전하게 정리한다.
    delete_group_tree(db, group)
    db.commit()
    return RedirectResponse(url="/groups", status_code=303)


@app.post("/groups/{group_id}/transfer-owner")
def transfer_group_owner(group_id: int, request: Request, username: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_site_admin(user, group)

    target = db.query(User).filter(User.username == username).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    target_membership = db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == target.id).first()
    if target_membership is None:
        raise HTTPException(status_code=400, detail="그룹 회원에게만 소유권을 양도할 수 있습니다.")

    old_owner_membership = db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == group.owner_id).first()
    if old_owner_membership:
        old_owner_membership.role = "member"
    target_membership.role = "owner"
    group.owner_id = target.id
    create_message(db, target.id, "그룹 소유권 양도", f"{group.name} 그룹의 소유자가 되었습니다.", "notice", related_group_id=group.id)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.get("/messages", response_class=HTMLResponse)
def messages_page(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    messages = db.query(Message).filter(Message.user_id == user.id).order_by(Message.id.desc()).all()
    for message in messages:
        message.is_read = True
    db.commit()
    return templates.TemplateResponse("messages.html", {"request": request, "user": user, "messages": messages})


@app.post("/messages/{message_id}/group-invite/{action}")
def respond_group_invite(message_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    message = db.query(Message).filter(Message.id == message_id, Message.user_id == user.id).first()
    if message is None or message.message_type != "group_invite" or message.action_status != "pending" or message.related_group_id is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if action not in {"accept", "reject"}:
        raise HTTPException(status_code=400, detail="Invalid action")
    if action == "accept":
        exists = db.query(GroupMember).filter(GroupMember.group_id == message.related_group_id, GroupMember.user_id == user.id).first()
        if exists is None:
            db.add(GroupMember(group_id=message.related_group_id, user_id=user.id, role="member"))
        message.action_status = "accepted"
    else:
        message.action_status = "rejected"
    db.commit()
    return RedirectResponse(url="/messages", status_code=303)


@app.post("/groups/{group_id}/invite")
def invite_group_member(group_id: int, request: Request, username: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    target = db.query(User).filter(User.username == username).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    exists = db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == target.id).first()
    if exists is None:
        create_message(db, target.id, "그룹 초대", f"{group.name} 그룹에 초대되었습니다. 수락하면 그룹 회원이 됩니다.", "group_invite", related_group_id=group.id, action_status="pending")
        db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.post("/groups/{group_id}/members/bulk-add")
def bulk_add_existing_group_members(group_id: int, request: Request, csv_text: str = Form(""), csv_file: UploadFile | None = File(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    added_members = 0
    skipped = 0
    for row in parse_user_bulk_csv(read_csv_text(csv_text, csv_file)):
        target = db.query(User).filter(User.username == row["username"]).first()
        if target is None:
            skipped += 1
            continue
        exists = db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == target.id).first()
        if exists:
            skipped += 1
            continue
        db.add(GroupMember(group_id=group.id, user_id=target.id, role="member"))
        added_members += 1
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.post("/groups/{group_id}/members/bulk-create")
def bulk_create_group_members(group_id: int, request: Request, csv_text: str = Form(""), csv_file: UploadFile | None = File(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    if not group.is_school_group:
        raise HTTPException(status_code=403, detail="그룹 내 계정 일괄 생성은 학교 분반 그룹에서만 사용할 수 있습니다.")
    created_users = 0
    added_members = 0
    skipped = 0
    for row in parse_user_bulk_csv(read_csv_text(csv_text, csv_file)):
        target = db.query(User).filter(User.username == row["username"]).first()
        if target is None:
            target = User(username=row["username"], password_hash=hash_password(row["password"]), full_name=row["full_name"], student_id=row["student_id"], must_change_password=True)
            db.add(target)
            db.flush()
            created_users += 1
        exists = db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == target.id).first()
        if exists:
            skipped += 1
            continue
        db.add(GroupMember(group_id=group.id, user_id=target.id, role="member"))
        added_members += 1
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.post("/groups/{group_id}/join-requests/{request_id}/{action}")
def handle_group_join_request(group_id: int, request_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    join_request = db.query(GroupJoinRequest).filter(GroupJoinRequest.id == request_id, GroupJoinRequest.group_id == group.id, GroupJoinRequest.status == "pending").first()
    if join_request is None:
        raise HTTPException(status_code=404, detail="Join request not found")
    if action == "approve":
        join_request.status = "approved"
        exists = db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == join_request.user_id).first()
        if exists is None:
            db.add(GroupMember(group_id=group.id, user_id=join_request.user_id, role="member"))
        create_message(db, join_request.user_id, "그룹 가입 승인", f"{group.name} 그룹 가입 신청이 승인되었습니다.", "notice", related_group_id=group.id)
    elif action == "reject":
        join_request.status = "rejected"
        create_message(db, join_request.user_id, "그룹 가입 거절", f"{group.name} 그룹 가입 신청이 거절되었습니다.", "notice", related_group_id=group.id)
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.post("/groups/{group_id}/school-group/apply")
def apply_school_group(group_id: int, request: Request, reason: str = Form(""), attachment: UploadFile | None = File(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_site_admin(user, group)
    if group.is_school_group:
        return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)
    group.school_group_request_status = "pending"
    group.school_group_request_reason = reason.strip()
    if attachment is not None and attachment.filename:
        safe_name = safe_upload_filename(attachment.filename)
        stored_name = f"group_{group.id}_{uuid.uuid4().hex}_{safe_name}"
        stored_path = Path("uploads/school_group_requests") / stored_name
        with stored_path.open("wb") as out:
            shutil.copyfileobj(attachment.file, out)
        group.school_group_request_file_path = f"/uploads/school_group_requests/{stored_name}"
        group.school_group_request_file_name = safe_name
    admins = db.query(User).filter(User.is_admin == True).all()  # noqa: E712
    for admin in admins:
        create_message(db, admin.id, "학교 분반 그룹 신청", f"{group.name} 그룹에서 학교 분반 그룹 활성화를 신청했습니다.", "notice", related_group_id=group.id)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.post("/admin/groups/{group_id}/school-group/{action}")
def review_school_group(group_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    if action == "approve":
        group.is_school_group = True
        group.school_group_request_status = "approved"
        create_message(db, group.owner_id, "학교 분반 그룹 승인", f"{group.name} 그룹이 학교 분반 그룹으로 승인되었습니다.", "notice", related_group_id=group.id)
    elif action == "reject":
        group.is_school_group = False
        group.school_group_request_status = "rejected"
        create_message(db, group.owner_id, "학교 분반 그룹 거절", f"{group.name} 그룹의 학교 분반 그룹 신청이 거절되었습니다.", "notice", related_group_id=group.id)
    elif action == "disable":
        group.is_school_group = False
        group.school_group_request_status = "none"
        group.school_group_request_reason = ""
        group.school_group_request_file_path = ""
        group.school_group_request_file_name = ""
        create_message(db, group.owner_id, "학교 분반 그룹 해제", f"{group.name} 그룹의 학교 분반 그룹 설정이 해제되었습니다.", "notice", related_group_id=group.id)
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/groups/{group_id}/members/{member_user_id}/role")
def update_group_member_role(group_id: int, member_user_id: int, request: Request, role: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_site_admin(user, group)
    membership = db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == member_user_id).first()
    if membership is None:
        raise HTTPException(status_code=404, detail="Member not found")
    if membership.user_id == group.owner_id:
        raise HTTPException(status_code=400, detail="소유자 역할은 이 화면에서 변경할 수 없습니다.")
    if role not in {"member", "admin"}:
        raise HTTPException(status_code=400, detail="Invalid role")
    membership.role = role
    if role == "admin":
        create_message(db, member_user_id, "그룹 관리자 지정", f"{group.name} 그룹의 관리자로 지정되었습니다.", "notice", related_group_id=group.id)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.post("/groups/{group_id}/edit-description")
def edit_group_description(group_id: int, request: Request, description: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    group.description = description
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.post("/groups/{group_id}/visibility")
def update_group_visibility(group_id: int, request: Request, is_public: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    group.is_public = is_public == "on"
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.post("/groups/{group_id}/problemsets/{problem_set_id}/edit-description")
def edit_group_problemset_description(group_id: int, problem_set_id: int, request: Request, description: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    problem_set = db.query(GroupProblemSet).filter(GroupProblemSet.id == problem_set_id, GroupProblemSet.group_id == group_id).first()
    if group is None or problem_set is None:
        raise HTTPException(status_code=404, detail="Not found")
    require_group_owner_or_admin(user, group)
    problem_set.description = description
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#problemsets", status_code=303)




@app.post("/admin/group-contests/{group_contest_id}/rejudge")
def rejudge_group_contest(group_contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group_contest = db.query(GroupContest).filter(GroupContest.id == group_contest_id).first()
    if group_contest is None or group_contest.contest_id is None:
        raise HTTPException(status_code=404, detail="Group contest not found")
    group = db.query(Group).filter(Group.id == group_contest.group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    submissions = db.query(Submission).filter(Submission.contest_id == group_contest.contest_id).order_by(Submission.id.asc()).all()
    for submission in submissions:
        rejudge_submission(submission)
        db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#contests", status_code=303)


@app.post("/groups/{group_id}/contests/{group_contest_id}/edit-description")
def edit_group_contest_description(group_id: int, group_contest_id: int, request: Request, description: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    group_contest = db.query(GroupContest).filter(GroupContest.id == group_contest_id, GroupContest.group_id == group_id).first()
    if group is None or group_contest is None:
        raise HTTPException(status_code=404, detail="Not found")
    require_group_owner_or_admin(user, group)
    group_contest.description = description
    if group_contest.contest:
        group_contest.contest.description = description
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#contests", status_code=303)


@app.post("/groups/{group_id}/practices/{practice_id}/edit-description")
def edit_group_practice_description(group_id: int, practice_id: int, request: Request, description: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    practice = db.query(GroupPractice).filter(GroupPractice.id == practice_id, GroupPractice.group_id == group_id).first()
    if group is None or practice is None:
        raise HTTPException(status_code=404, detail="Not found")
    require_group_owner_or_admin(user, group)
    practice.description = description
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}/practices/{practice.id}", status_code=303)


@app.post("/groups/{group_id}/problemsets/new")
def create_group_problemset(group_id: int, request: Request, title: str = Form(...), description: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    db.add(GroupProblemSet(group_id=group.id, title=title, description=description))
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#problemsets", status_code=303)


@app.post("/groups/{group_id}/contests/new")
def create_group_contest_shell(group_id: int, request: Request, title: str = Form(...), description: str = Form(""), start_time: str = Form(""), end_time: str = Form(""), problem_order: str = Form(""), contest_id: str = Form(""), is_exam_mode: str | None = Form(None), hide_ranking: str | None = Form(None), is_public: str | None = Form(None), score_enabled: str | None = Form(None), result_display_mode: str = Form("full"), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)

    try:
        linked_contest_id = int(contest_id) if contest_id.strip().isdigit() else None
        exam_enabled = is_exam_mode == "on"
        ensure_exam_mode_allowed_for_group(group, exam_enabled)
        if linked_contest_id is not None:
            group_contest = link_existing_contest_to_group(db, group=group, contest_id=linked_contest_id, title=title, description=description)
            if not getattr(group_contest, "display_number", 0):
                group_contest.display_number = next_group_contest_display_number(db, group.id)
            linked_contest = db.query(Contest).filter(Contest.id == linked_contest_id).first()
            if linked_contest is None:
                raise LookupError("Contest not found")
            if exam_enabled:
                validate_school_exam_contest_overlap(db, group, linked_contest.start_time, linked_contest.end_time, exclude_contest_id=linked_contest.id)
            linked_contest.is_exam_mode = exam_enabled
            linked_contest.hide_ranking = hide_ranking == "on" or linked_contest.is_exam_mode
            linked_contest.is_public = is_public == "on"
            linked_contest.score_enabled = score_enabled == "on"
            linked_contest.result_display_mode = "full"
            db.commit()
            return RedirectResponse(url=f"/groups/{group.id}#contests", status_code=303)

        if not start_time.strip() or not end_time.strip():
            raise ValueError("새 그룹 대회를 만들려면 시작/종료 시각이 필요합니다.")
        start_dt = parse_datetime_local(start_time)
        end_dt = parse_datetime_local(end_time)
        if exam_enabled:
            validate_school_exam_contest_overlap(db, group, start_dt, end_dt)
        problem_ids = parse_problem_id_list(problem_order)
        group_contest = service_create_group_contest(
            db,
            group=group,
            title=title,
            description=description,
            start_time=start_dt,
            end_time=end_dt,
            problem_ids=problem_ids,
            now=now(),
            is_exam_mode=exam_enabled,
            hide_ranking=(hide_ranking == "on"),
            is_public=(is_public == "on"),
            score_enabled=(score_enabled == "on"),
        )
        if not getattr(group_contest, "display_number", 0):
            group_contest.display_number = next_group_contest_display_number(db, group.id)
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"그룹 대회 생성 중 DB 오류가 발생했습니다: {exc.__class__.__name__}") from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"그룹 대회 생성 중 오류가 발생했습니다: {exc.__class__.__name__}: {exc}") from exc
    return RedirectResponse(url=f"/groups/{group.id}#contests", status_code=303)


@app.post("/groups/{group_id}/contests/{group_contest_id}/visibility")
def update_group_contest_visibility(group_id: int, group_contest_id: int, request: Request, is_public: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    group_contest = db.query(GroupContest).filter(GroupContest.id == group_contest_id, GroupContest.group_id == group_id).first()
    if group is None or group_contest is None or group_contest.contest_id is None:
        raise HTTPException(status_code=404, detail="Group contest not found")
    require_group_owner_or_admin(user, group)
    contest = db.query(Contest).filter(Contest.id == group_contest.contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    contest.is_public = is_public == "on"
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#contests", status_code=303)


@app.post("/groups/{group_id}/contests/{group_contest_id}/delete")
def delete_group_contest_route(group_id: int, group_contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    group_contest = db.query(GroupContest).filter(GroupContest.id == group_contest_id, GroupContest.group_id == group_id).first()
    if group is None or group_contest is None:
        raise HTTPException(status_code=404, detail="Group contest not found")
    require_group_owner_or_admin(user, group)
    contest = db.query(Contest).filter(Contest.id == group_contest.contest_id).first() if group_contest.contest_id else None
    if contest is not None:
        delete_contest_tree(db, contest)
    else:
        db.delete(group_contest)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#contests", status_code=303)


@app.post("/groups/{group_id}/contests/{group_contest_id}/add-problem")
def add_problem_to_group_contest(group_id: int, group_contest_id: int, request: Request, problem_ids: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    group_contest = db.query(GroupContest).filter(GroupContest.id == group_contest_id, GroupContest.group_id == group_id).first()
    if group is None or group_contest is None or group_contest.contest_id is None:
        raise HTTPException(status_code=404, detail="Group contest not found")
    require_group_owner_or_admin(user, group)
    contest = db.query(Contest).filter(Contest.id == group_contest.contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_editable(contest)
    try:
        ids = parse_id_list(problem_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    for pid in ids:
        query = db.query(Problem).filter(Problem.id == pid)
        if not user.is_admin:
            query = query.filter(Problem.is_contest_only == False, Problem.is_public == True)  # noqa: E712
        problem = query.first()
        if problem is None:
            continue
        add_problem_to_contest(db, contest, problem)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#contests", status_code=303)


@app.post("/groups/{group_id}/practices/new")
def create_group_practice(group_id: int, request: Request, title: str = Form(...), description: str = Form(""), start_time: str = Form(""), end_time: str = Form(""), problem_order: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    try:
        start_dt = parse_datetime_local(start_time) if start_time.strip() else None
        end_dt = parse_datetime_local(end_time) if end_time.strip() else None
        problem_ids = parse_problem_id_list(problem_order)
        create_group_practice_with_problems(
            db,
            group=group,
            title=title,
            description=description,
            start_time=start_dt,
            end_time=end_dt,
            problem_ids=problem_ids,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url=f"/groups/{group.id}#practice", status_code=303)




@app.post("/groups/{group_id}/problemsets/{problem_set_id}/add-problem")
def add_problem_to_group_problemset(group_id: int, problem_set_id: int, request: Request, problem_ids: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    problem_set = db.query(GroupProblemSet).filter(GroupProblemSet.id == problem_set_id, GroupProblemSet.group_id == group_id).first()
    if group is None or problem_set is None:
        raise HTTPException(status_code=404, detail="Not found")
    require_group_owner_or_admin(user, group)
    try:
        ids = parse_id_list(problem_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    for pid in ids:
        problem = db.query(Problem).filter(Problem.id == pid, Problem.is_contest_only == False).first()
        if problem is None:
            continue
        exists = db.query(GroupProblemSetProblem).filter(GroupProblemSetProblem.problem_set_id == problem_set.id, GroupProblemSetProblem.problem_id == problem.id).first()
        if exists is None:
            order_index = db.query(GroupProblemSetProblem).filter(GroupProblemSetProblem.problem_set_id == problem_set.id).count()
            db.add(GroupProblemSetProblem(problem_set_id=problem_set.id, problem_id=problem.id, order_index=order_index))
            db.flush()
    relabel_group_problemset_items(db, problem_set.id)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#problemsets", status_code=303)


@app.post("/groups/{group_id}/problemsets/{problem_set_id}/items/{item_id}/delete")
def remove_problem_from_group_problemset(group_id: int, problem_set_id: int, item_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    item = db.query(GroupProblemSetProblem).filter(GroupProblemSetProblem.id == item_id, GroupProblemSetProblem.problem_set_id == problem_set_id).first()
    if group is None or item is None:
        raise HTTPException(status_code=404, detail="Not found")
    require_group_owner_or_admin(user, group)
    db.delete(item)
    db.flush()
    relabel_group_problemset_items(db, problem_set_id)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#problemsets", status_code=303)


@app.post("/groups/{group_id}/practices/{practice_id}/add-problem")
def add_problem_to_group_practice(group_id: int, practice_id: int, request: Request, problem_ids: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    practice = db.query(GroupPractice).filter(GroupPractice.id == practice_id, GroupPractice.group_id == group_id).first()
    if group is None or practice is None:
        raise HTTPException(status_code=404, detail="Not found")
    require_group_owner_or_admin(user, group)
    if group_practice_is_closed(practice):
        raise HTTPException(status_code=403, detail="종료된 연습은 문제 추가 또는 순서 변경을 할 수 없습니다.")
    try:
        ids = parse_id_list(problem_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    for pid in ids:
        problem = db.query(Problem).filter(Problem.id == pid, Problem.is_contest_only == False).first()
        if problem is None:
            continue
        exists = db.query(GroupPracticeProblem).filter(GroupPracticeProblem.practice_id == practice.id, GroupPracticeProblem.problem_id == problem.id).first()
        if exists is None:
            order_index = db.query(GroupPracticeProblem).filter(GroupPracticeProblem.practice_id == practice.id).count()
            db.add(GroupPracticeProblem(practice_id=practice.id, problem_id=problem.id, order_index=order_index))
            db.flush()
    relabel_group_practice_items(db, practice.id)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}/practices/{practice.id}", status_code=303)


@app.post("/groups/{group_id}/practices/{practice_id}/items/{item_id}/delete")
def remove_problem_from_group_practice(group_id: int, practice_id: int, item_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    item = db.query(GroupPracticeProblem).filter(GroupPracticeProblem.id == item_id, GroupPracticeProblem.practice_id == practice_id).first()
    if group is None or item is None:
        raise HTTPException(status_code=404, detail="Not found")
    require_group_owner_or_admin(user, group)
    practice = db.query(GroupPractice).filter(GroupPractice.id == practice_id, GroupPractice.group_id == group_id).first()
    if practice and group_practice_is_closed(practice):
        raise HTTPException(status_code=403, detail="종료된 연습은 문제를 제거할 수 없습니다.")
    db.delete(item)
    db.flush()
    relabel_group_practice_items(db, practice_id)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}/practices/{practice.id}", status_code=303)



@app.post("/groups/{group_id}/problemsets/{problem_set_id}/reorder")
def reorder_group_problemset(group_id: int, problem_set_id: int, request: Request, ordered_problem_ids: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    problem_set = db.query(GroupProblemSet).filter(GroupProblemSet.id == problem_set_id, GroupProblemSet.group_id == group_id).first()
    if group is None or problem_set is None:
        raise HTTPException(status_code=404, detail="Not found")
    require_group_owner_or_admin(user, group)
    try:
        ids = parse_id_list(ordered_problem_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    items = db.query(GroupProblemSetProblem).filter(GroupProblemSetProblem.problem_set_id == problem_set.id).all()
    by_pid = {item.problem_id: item for item in items}
    if set(ids) != set(by_pid) or len(ids) != len(by_pid):
        raise HTTPException(status_code=400, detail="현재 문제집에 포함된 문제 번호를 빠짐없이 한 번씩 입력해야 합니다.")
    for index, pid in enumerate(ids):
        by_pid[pid].order_index = index
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#problemsets", status_code=303)


@app.post("/groups/{group_id}/practices/{practice_id}/reorder")
def reorder_group_practice(group_id: int, practice_id: int, request: Request, ordered_problem_ids: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    practice = db.query(GroupPractice).filter(GroupPractice.id == practice_id, GroupPractice.group_id == group_id).first()
    if group is None or practice is None:
        raise HTTPException(status_code=404, detail="Not found")
    require_group_owner_or_admin(user, group)
    if group_practice_is_closed(practice):
        raise HTTPException(status_code=403, detail="종료된 연습은 문제 추가 또는 순서 변경을 할 수 없습니다.")
    try:
        ids = parse_id_list(ordered_problem_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    items = db.query(GroupPracticeProblem).filter(GroupPracticeProblem.practice_id == practice.id).all()
    by_pid = {item.problem_id: item for item in items}
    if set(ids) != set(by_pid) or len(ids) != len(by_pid):
        raise HTTPException(status_code=400, detail="현재 연습에 포함된 문제 번호를 빠짐없이 한 번씩 입력해야 합니다.")
    for index, pid in enumerate(ids):
        by_pid[pid].order_index = index
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}/practices/{practice.id}", status_code=303)


@app.post("/groups/{group_id}/practices/{practice_id}/end")
def end_group_practice(group_id: int, practice_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    practice = db.query(GroupPractice).filter(GroupPractice.id == practice_id, GroupPractice.group_id == group_id).first()
    if group is None or practice is None:
        raise HTTPException(status_code=404, detail="Not found")
    require_group_owner_or_admin(user, group)
    practice.end_time = now()
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}/practices/{practice.id}", status_code=303)


@app.get("/admin/contests/new", response_class=HTMLResponse)
def new_contest_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    return render_contest_form(request, user, db)


@app.post("/admin/contests/new")
def create_contest(request: Request, title: str = Form(...), description: str = Form(...), start_time: str = Form(...), end_time: str = Form(...), problem_ids: list[int] = Form(default=[]), problem_order: str = Form(""), is_exam_mode: str | None = Form(None), hide_ranking: str | None = Form(None), score_enabled: str | None = Form(None), result_display_mode: str = Form("full"), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if is_exam_mode == "on":
        return render_contest_form(request, user, db, start_time, end_time, "시험/평가 모드는 학교 분반 승인 그룹의 그룹 대회에서만 사용할 수 있습니다.")
    try:
        final_problem_ids = parse_problem_id_list(problem_order) if problem_order.strip() else parse_problem_id_list(problem_ids)
        contest = create_contest_with_problems(
            db,
            title=title,
            description=description,
            start_time=parse_datetime_local(start_time),
            end_time=parse_datetime_local(end_time),
            problem_ids=final_problem_ids,
            now=now(),
            score_enabled=(score_enabled == "on"),
        )
        contest.display_number = next_site_contest_display_number(db)
        contest.is_exam_mode = is_exam_mode == "on"
        contest.hide_ranking = hide_ranking == "on" or contest.is_exam_mode
        contest.result_display_mode = "full"
        db.commit()
        db.refresh(contest)
    except ValueError as exc:
        db.rollback()
        return render_contest_form(request, user, db, start_time, end_time, str(exc))
    except SQLAlchemyError as exc:
        db.rollback()
        return render_contest_form(request, user, db, start_time, end_time, f"대회 생성 중 DB 오류가 발생했습니다: {exc.__class__.__name__}")
    except Exception as exc:
        db.rollback()
        return render_contest_form(request, user, db, start_time, end_time, f"대회 생성 중 오류가 발생했습니다: {exc.__class__.__name__}: {exc}")
    return RedirectResponse(url=f"/contests/{contest.id}", status_code=303)


@app.post("/admin/contests/{contest_id}/settings")
def update_contest_settings(contest_id: int, request: Request, is_exam_mode: str | None = Form(None), hide_ranking: str | None = Form(None), is_public: str | None = Form(None), score_enabled: str | None = Form(None), start_time: str = Form(""), end_time: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_manager(user, contest, db)
    group_contest = db.query(GroupContest).filter(GroupContest.contest_id == contest.id).first()

    # 시작 전 대회는 시간을 수정할 수 있다. 진행 중/종료 대회는 기록 보존을 위해 시간 변경을 막는다.
    if start_time.strip() or end_time.strip():
        if contest_has_started(contest) or contest_is_closed(contest):
            raise HTTPException(status_code=400, detail="시작 전 대회만 시간을 수정할 수 있습니다.")
        new_start = parse_datetime_local(start_time) if start_time.strip() else contest.start_time
        new_end = parse_datetime_local(end_time) if end_time.strip() else contest.end_time
        if new_end <= new_start:
            raise HTTPException(status_code=400, detail="종료 시간은 시작 시간보다 늦어야 합니다.")
        if group_contest is not None and contest.is_exam_mode:
            group = db.query(Group).filter(Group.id == group_contest.group_id).first()
            if group and group.is_school_group:
                validate_school_exam_contest_overlap(db, group, new_start, new_end, exclude_contest_id=contest.id)
        contest.start_time = new_start
        contest.end_time = new_end

    if group_contest is None:
        exam_enabled = is_exam_mode == "on"
        ensure_site_contest_exam_mode_disabled(exam_enabled)
        contest.is_exam_mode = exam_enabled
        contest.hide_ranking = hide_ranking == "on" or contest.is_exam_mode
    else:
        # 그룹 대회의 시험 모드는 생성 시에만 결정한다. 이후에는 공개 여부/순위표 숨김만 조정한다.
        contest.hide_ranking = hide_ranking == "on" or contest.is_exam_mode
        contest.is_public = is_public == "on"
    contest.score_enabled = score_enabled == "on"
    contest.result_display_mode = "full"
    audit_log(db, request, user, "contest_settings", "contest", contest.id, f"대회 설정 변경: {contest.title}")
    db.commit()
    return RedirectResponse(url=f"/contests/{contest.id}", status_code=303)


@app.post("/admin/contests/{contest_id}/end")
def end_contest(contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_manager(user, contest, db)
    contest.is_ended = True
    contest.end_time = now()
    audit_log(db, request, user, "contest_end", "contest", contest.id, f"대회 종료: {contest.title}")
    db.commit()
    return RedirectResponse(url=f"/contests/{contest.id}", status_code=303)


@app.post("/admin/contests/{contest_id}/add-existing-problem")
def add_existing_problem(contest_id: int, request: Request, problem_id: int = Form(...), score: int = Form(100), db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is not None:
        require_contest_manager(user, contest, db)
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if contest is None or problem is None:
        raise HTTPException(status_code=404, detail="Not found")
    require_contest_editable(contest)
    group_contest = db.query(GroupContest).filter(GroupContest.contest_id == contest.id).first()
    if group_contest is not None and not user.is_admin and (problem.is_contest_only or not problem.is_public):
        raise HTTPException(status_code=403, detail="일반 그룹 대회에는 기존 공개 문제만 추가할 수 있습니다.")
    add_problem_to_contest(db, contest, problem)
    link = db.query(ContestProblem).filter(ContestProblem.contest_id == contest.id, ContestProblem.problem_id == problem.id).first()
    if link:
        link.score = max(0, min(int(score), 100000))
    db.commit()
    return RedirectResponse(url=f"/contests/{contest.id}", status_code=303)


@app.post("/admin/contests/{contest_id}/add-new-problem")
def add_new_contest_problem(contest_id: int, request: Request, problem_id: str = Form(""), title: str = Form(...), description: str = Form(...), input_description: str = Form(...), output_description: str = Form(...), time_limit: int = Form(2), memory_limit: int = Form(256), score: int = Form(100), sample_inputs: str = Form(""), sample_outputs: str = Form(""), test_inputs: str = Form(...), test_outputs: str = Form(...), is_judge_ready: str | None = Form("on"), publish_notice_agree: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_manager(user, contest, db)
    require_contest_editable(contest)
    require_exam_problem_configuration_editable(user, contest, db)
    group_contest = require_group_contest_problem_create_allowed(user, contest, db)

    if group_contest is not None and not user.is_admin and publish_notice_agree != "on":
        raise HTTPException(status_code=400, detail="문제 공개 가능성 및 저작권 고지에 동의해야 등록할 수 있습니다.")

    if problem_id.strip():
        if group_contest is not None and not user.is_admin:
            raise HTTPException(status_code=403, detail="그룹 대회용 문제는 내부 문제 ID를 직접 지정할 수 없습니다.")
        new_problem_id = int(problem_id)
        if db.query(Problem).filter(Problem.id == new_problem_id).first():
            raise HTTPException(status_code=400, detail="이미 존재하는 내부 문제 ID입니다. 비워두면 자동으로 생성됩니다.")
    else:
        max_id = db.query(Problem.id).order_by(Problem.id.desc()).first()
        new_problem_id = (max_id[0] if max_id else 0) + 1

    save_problem_files(new_problem_id, title, description, input_description, output_description, time_limit, memory_limit, test_inputs, test_outputs)
    problem = Problem(
        id=new_problem_id,
        title=title,
        description=description,
        input_description=input_description,
        output_description=output_description,
        time_limit=time_limit,
        memory_limit=memory_limit,
        is_contest_only=True,
        is_public=False,
        is_judge_ready=is_judge_ready == "on",
        force_private_submission=True,
        origin_type="group_contest" if group_contest is not None else "regular",
        origin_group_id=group_contest.group_id if group_contest is not None else None,
        origin_contest_id=contest.id if group_contest is not None else None,
        review_status="group_only" if group_contest is not None else "none",
    )
    db.add(problem)
    db.flush()
    save_problem_examples(db, problem, sample_inputs, sample_outputs)
    add_problem_to_contest(db, contest, problem)
    link = db.query(ContestProblem).filter(ContestProblem.contest_id == contest.id, ContestProblem.problem_id == problem.id).first()
    if link:
        link.score = max(0, min(int(score), 100000))
        if group_contest is not None:
            problem.display_code = build_group_contest_problem_code(group_contest.group_id, contest.id, link.label)
    db.commit()
    return RedirectResponse(url=f"/contests/{contest.id}#problems", status_code=303)


@app.post("/groups/{group_id}/problems/{problem_id}/request-public-review")
def request_group_problem_public_review(group_id: int, problem_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if group is None or problem is None:
        raise HTTPException(status_code=404, detail="Not found")
    require_group_owner_or_admin(user, group)
    if problem.origin_type != "group_contest" or problem.origin_group_id != group.id:
        raise HTTPException(status_code=403, detail="이 그룹에서 만든 그룹 대회 문제가 아닙니다.")
    if problem.review_status == "approved":
        raise HTTPException(status_code=400, detail="이미 일반 문제로 공개된 문제입니다.")
    problem.review_status = "review_pending"
    admins = db.query(User).filter(User.is_admin == True).all()  # noqa: E712
    for admin in admins:
        create_message(db, admin.id, "그룹 대회 문제 공개 검토 요청", f"{group.name} 그룹에서 만든 '{problem.title}' 문제의 일반 공개 검토를 요청했습니다.", "notice", related_group_id=group.id)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#contests", status_code=303)


@app.post("/admin/problems/{problem_id}/approve-public")
def approve_group_problem_as_public(problem_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")
    if problem.origin_type != "group_contest":
        raise HTTPException(status_code=400, detail="그룹 대회에서 만든 문제가 아닙니다.")
    problem.is_contest_only = False
    problem.is_public = True
    problem.force_private_submission = False
    problem.review_status = "approved"
    problem.display_code = None
    if problem.origin_group_id:
        group = db.query(Group).filter(Group.id == problem.origin_group_id).first()
        if group:
            create_message(db, group.owner_id, "그룹 대회 문제 공개 승인", f"'{problem.title}' 문제가 OJ 일반 문제로 공개 승인되었습니다.", "notice", related_group_id=group.id)
    db.commit()
    return RedirectResponse(url=f"/problems/{problem.id}", status_code=303)


@app.post("/admin/problems/{problem_id}/reject-public")
def reject_group_problem_public_review(problem_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")
    if problem.origin_type != "group_contest":
        raise HTTPException(status_code=400, detail="그룹 대회에서 만든 문제가 아닙니다.")
    problem.review_status = "rejected"
    if problem.origin_group_id:
        group = db.query(Group).filter(Group.id == problem.origin_group_id).first()
        if group:
            create_message(db, group.owner_id, "그룹 대회 문제 공개 반려", f"'{problem.title}' 문제의 OJ 일반 공개 요청이 반려되었습니다.", "notice", related_group_id=group.id)
    db.commit()
    return RedirectResponse(url=f"/admin/problems/{problem.id}/edit", status_code=303)


@app.post("/admin/contests/{contest_id}/problems/{problem_id}/score")
def update_contest_problem_score(contest_id: int, problem_id: int, request: Request, score: int = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_manager(user, contest, db)
    link = db.query(ContestProblem).filter(ContestProblem.contest_id == contest_id, ContestProblem.problem_id == problem_id).first()
    if link is None:
        raise HTTPException(status_code=404, detail="Contest problem not found")
    link.score = max(0, min(int(score), 100000))
    db.commit()
    return RedirectResponse(url=f"/contests/{contest_id}#problems", status_code=303)


@app.post("/admin/contests/{contest_id}/problems/{problem_id}/toggle-ranking")
def toggle_contest_problem_ranking(contest_id: int, problem_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_manager(user, contest, db)
    link = db.query(ContestProblem).filter(ContestProblem.contest_id == contest_id, ContestProblem.problem_id == problem_id).first()
    if link is None:
        raise HTTPException(status_code=404, detail="Contest problem not found")
    link.exclude_from_ranking = not link.exclude_from_ranking
    db.commit()
    return RedirectResponse(url=f"/contests/{contest_id}#problems", status_code=303)


@app.post("/admin/contests/{contest_id}/reorder-problems")
def reorder_contest_problems(contest_id: int, request: Request, ordered_problem_ids: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_manager(user, contest, db)
    require_contest_editable(contest)
    require_exam_problem_configuration_editable(user, contest, db)

    try:
        wanted_ids = [int(value.strip()) for value in ordered_problem_ids.replace("\n", ",").split(",") if value.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="문제 ID는 쉼표 또는 줄바꿈으로 구분된 숫자여야 합니다.")

    links = db.query(ContestProblem).filter(ContestProblem.contest_id == contest.id).all()
    link_by_problem_id = {link.problem_id: link for link in links}
    if len(wanted_ids) != len(set(wanted_ids)) or set(wanted_ids) != set(link_by_problem_id):
        raise HTTPException(status_code=400, detail="현재 대회에 포함된 문제 ID를 빠짐없이 한 번씩만 입력해야 합니다.")

    for index, problem_id in enumerate(wanted_ids):
        link_by_problem_id[problem_id].order_index = index
        link_by_problem_id[problem_id].label = index_to_label(index)
    db.commit()
    return RedirectResponse(url=f"/contests/{contest.id}#problems", status_code=303)





def member_display_name(user: User | None) -> str:
    if user is None:
        return ""
    if user.full_name:
        return f"{user.full_name}({user.username})"
    return user.username


def build_group_problem_progress(db: Session, members: list[GroupMember], problem_ids: list[int], contest_id: int | None = None, practice_id: int | None = None) -> list[dict]:
    rows = []
    for problem_id in problem_ids:
        problem = db.query(Problem).filter(Problem.id == problem_id).first()
        solved = []
        unsolved = []
        not_submitted = []
        for member in members:
            user = member.user
            if user is None:
                continue
            query = db.query(Submission).filter(Submission.user_id == user.id, Submission.problem_id == problem_id)
            if contest_id is not None:
                query = query.filter(Submission.contest_id == contest_id)
            if practice_id is not None:
                query = query.filter(Submission.practice_id == practice_id)
            submissions = query.order_by(Submission.id.asc()).all()
            if not submissions:
                not_submitted.append(user)
            elif any(s.result == "AC" for s in submissions):
                solved.append(user)
            else:
                unsolved.append(user)
        rows.append({
            "problem_id": problem_id,
            "problem": problem,
            "solved": solved,
            "unsolved": unsolved,
            "not_submitted": not_submitted,
        })
    return rows


def progress_csv_rows(progress_rows: list[dict]) -> list[list]:
    rows = [["problem_id", "title", "ac_count", "unsolved_count", "not_submitted_count", "ac_users", "unsolved_users", "not_submitted_users"]]
    for row in progress_rows:
        rows.append([
            row["problem_id"],
            row["problem"].title if row.get("problem") else "",
            len(row["solved"]),
            len(row["unsolved"]),
            len(row["not_submitted"]),
            ", ".join(member_display_name(user) for user in row["solved"]),
            ", ".join(member_display_name(user) for user in row["unsolved"]),
            ", ".join(member_display_name(user) for user in row["not_submitted"]),
        ])
    return rows


@app.get("/groups/{group_id}/members.csv")
def export_group_members_csv(group_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    members = db.query(GroupMember).filter(GroupMember.group_id == group.id).join(User, User.id == GroupMember.user_id).order_by(User.student_id.asc(), User.username.asc()).all()
    rows = [["username", "full_name", "student_id", "role", "joined_at"]]
    for member in members:
        rows.append([member.user.username if member.user else member.user_id, member.user.full_name if member.user else "", member.user.student_id if member.user else "", member.role, member.joined_at])
    return csv_response(f"group_{group.id}_members.csv", rows)


@app.post("/groups/{group_id}/members/bulk-delete")
def bulk_delete_group_members(group_id: int, request: Request, member_ids: list[int] = Form([]), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    for member_user_id in member_ids:
        if member_user_id == group.owner_id:
            continue
        db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == member_user_id).delete(synchronize_session=False)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.post("/groups/{group_id}/members/bulk-role")
def bulk_update_group_member_roles(group_id: int, request: Request, member_ids: list[int] = Form([]), role: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    if role not in {"member", "admin"}:
        raise HTTPException(status_code=400, detail="Invalid role")
    for member_user_id in member_ids:
        if member_user_id == group.owner_id:
            continue
        membership = db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == member_user_id).first()
        if membership:
            membership.role = role
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.post("/groups/{group_id}/members/bulk-reset-password")
def bulk_reset_group_member_passwords(group_id: int, request: Request, member_ids: list[int] = Form([]), new_password: str = Form("changeme1234"), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다.")
    member_user_ids = {member.user_id for member in db.query(GroupMember).filter(GroupMember.group_id == group.id).all()}
    for member_user_id in member_ids:
        if member_user_id not in member_user_ids:
            continue
        target = db.query(User).filter(User.id == member_user_id).first()
        if target:
            target.password_hash = hash_password(new_password)
            target.must_change_password = True
            create_message(db, target.id, "비밀번호 초기화", f"{group.name} 그룹 관리자가 비밀번호를 초기화했습니다. 로그인 후 새 비밀번호로 변경해 주세요.", "notice", related_group_id=group.id)
    db.commit()
    return RedirectResponse(url=f"/groups/{group.id}#overview", status_code=303)


@app.get("/admin/group-practices/{practice_id}/progress.csv")
def export_group_practice_progress_csv(practice_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    practice = db.query(GroupPractice).filter(GroupPractice.id == practice_id).first()
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice not found")
    group = db.query(Group).filter(Group.id == practice.group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    members = db.query(GroupMember).filter(GroupMember.group_id == group.id).all()
    problem_ids = [item.problem_id for item in db.query(GroupPracticeProblem).filter(GroupPracticeProblem.practice_id == practice.id).order_by(GroupPracticeProblem.order_index.asc(), GroupPracticeProblem.id.asc()).all()]
    return csv_response(f"group_practice_{practice.id}_progress.csv", progress_csv_rows(build_group_problem_progress(db, members, problem_ids, practice_id=practice.id)))


@app.get("/admin/group-contests/{group_contest_id}/progress", response_class=HTMLResponse)
def group_contest_progress_page(group_contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group_contest = db.query(GroupContest).filter(GroupContest.id == group_contest_id).first()
    if group_contest is None or group_contest.contest is None:
        raise HTTPException(status_code=404, detail="Group contest not found")
    group = db.query(Group).filter(Group.id == group_contest.group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    members = db.query(GroupMember).filter(GroupMember.group_id == group.id).all()
    links = db.query(ContestProblem).filter(ContestProblem.contest_id == group_contest.contest_id).order_by(ContestProblem.order_index.asc(), ContestProblem.problem_id.asc()).all()
    progress_rows = build_group_problem_progress(db, members, [link.problem_id for link in links], contest_id=group_contest.contest_id)
    return templates.TemplateResponse("group_contest_progress.html", {"request": request, "user": user, "group": group, "group_contest": group_contest, "contest": group_contest.contest, "progress_rows": progress_rows})


@app.get("/admin/group-contests/{group_contest_id}/progress.csv")
def export_group_contest_progress_csv(group_contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group_contest = db.query(GroupContest).filter(GroupContest.id == group_contest_id).first()
    if group_contest is None or group_contest.contest is None:
        raise HTTPException(status_code=404, detail="Group contest not found")
    group = db.query(Group).filter(Group.id == group_contest.group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    members = db.query(GroupMember).filter(GroupMember.group_id == group.id).all()
    problem_ids = [link.problem_id for link in db.query(ContestProblem).filter(ContestProblem.contest_id == group_contest.contest_id).order_by(ContestProblem.order_index.asc(), ContestProblem.problem_id.asc()).all()]
    return csv_response(f"group_contest_{group_contest.id}_progress.csv", progress_csv_rows(build_group_problem_progress(db, members, problem_ids, contest_id=group_contest.contest_id)))

def csv_response(filename: str, rows: list[list]) -> StreamingResponse:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(rows)
    data = buffer.getvalue().encode("utf-8-sig")
    return StreamingResponse(io.BytesIO(data), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/admin/export/submissions.csv")
def export_submissions_csv(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    rows = [["id", "username", "problem_id", "contest_id", "language", "result", "runtime_ms", "memory_kb", "created_at"]]
    for s in db.query(Submission).order_by(Submission.id.asc()).all():
        rows.append([s.id, s.user.username if s.user else "", s.problem_id, s.contest_id or "", s.language, s.result, s.runtime_ms, s.memory_kb, s.created_at])
    return csv_response("submissions.csv", rows)


@app.get("/admin/contests/{contest_id}/ranking.csv")
def export_contest_ranking_csv(contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_manager(user, contest, db)
    rows = [["rank", "username", "solved_count", "wrong_count", "runtime_ms", "memory_kb"]]
    for row in build_contest_rankings(db, contest):
        rows.append([row["rank"], row["user"].username if row["user"] else "", row["solved_count"], row["wrong_count"], row["runtime_ms"], row["memory_kb"]])
    return csv_response(f"contest_{contest.id}_ranking.csv", rows)


@app.get("/admin/contests/{contest_id}/submissions.csv")
def export_contest_submissions_csv(contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_manager(user, contest, db)
    rows = [["id", "username", "problem_id", "label", "language", "result", "runtime_ms", "memory_kb", "created_at"]]
    for s in db.query(Submission).filter(Submission.contest_id == contest.id).order_by(Submission.id.asc()).all():
        link = get_contest_link_for_submission(db, s)
        rows.append([s.id, s.user.username if s.user else "", s.problem_id, link.label if link else "", s.language, s.result, s.runtime_ms, s.memory_kb, s.created_at])
    return csv_response(f"contest_{contest.id}_submissions.csv", rows)


@app.get("/admin/contests/{contest_id}/scores", response_class=HTMLResponse)
def contest_score_page(contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_manager(user, contest, db)
    if not contest.score_enabled:
        raise HTTPException(status_code=403, detail="이 대회는 배점 기능을 사용하지 않습니다.")
    return templates.TemplateResponse("score_table.html", {"request": request, "user": user, "contest": contest, "rows": build_contest_score_rows(db, contest)})


@app.get("/admin/contests/{contest_id}/scores.csv")
def export_contest_scores_csv(contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_manager(user, contest, db)
    if not contest.score_enabled:
        raise HTTPException(status_code=403, detail="이 대회는 배점 기능을 사용하지 않습니다.")
    return csv_response(f"contest_{contest.id}_scores.csv", build_contest_score_rows(db, contest))


@app.get("/admin/contests/{contest_id}/final-codes.zip")
def export_contest_final_codes_zip(contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_manager(user, contest, db)
    return zip_response(f"contest_{contest.id}_final_codes.zip", build_final_code_zip(contest, db))


@app.get("/admin/group-contests/{group_contest_id}/ranking.csv")
def export_group_contest_ranking_csv(group_contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group_contest = db.query(GroupContest).filter(GroupContest.id == group_contest_id).first()
    if group_contest is None or group_contest.contest is None:
        raise HTTPException(status_code=404, detail="Group contest not found")
    group = db.query(Group).filter(Group.id == group_contest.group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    rows = [["rank", "username", "solved_count", "wrong_count", "runtime_ms", "memory_kb"]]
    for row in build_contest_rankings(db, group_contest.contest):
        rows.append([row["rank"], row["user"].username if row["user"] else "", row["solved_count"], row["wrong_count"], row["runtime_ms"], row["memory_kb"]])
    return csv_response(f"group_contest_{group_contest.id}_ranking.csv", rows)


@app.get("/admin/group-contests/{group_contest_id}/scores", response_class=HTMLResponse)
def group_contest_score_page(group_contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group_contest = db.query(GroupContest).filter(GroupContest.id == group_contest_id).first()
    if group_contest is None or group_contest.contest is None:
        raise HTTPException(status_code=404, detail="Group contest not found")
    group = db.query(Group).filter(Group.id == group_contest.group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    if not group_contest.contest.score_enabled:
        raise HTTPException(status_code=403, detail="이 대회는 배점 기능을 사용하지 않습니다.")
    return templates.TemplateResponse("score_table.html", {"request": request, "user": user, "contest": group_contest.contest, "group_contest": group_contest, "group": group, "rows": build_contest_score_rows(db, group_contest.contest)})


@app.get("/admin/group-contests/{group_contest_id}/scores.csv")
def export_group_contest_scores_csv(group_contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group_contest = db.query(GroupContest).filter(GroupContest.id == group_contest_id).first()
    if group_contest is None or group_contest.contest is None:
        raise HTTPException(status_code=404, detail="Group contest not found")
    group = db.query(Group).filter(Group.id == group_contest.group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    if not group_contest.contest.score_enabled:
        raise HTTPException(status_code=403, detail="이 대회는 배점 기능을 사용하지 않습니다.")
    return csv_response(f"group_contest_{group_contest.id}_scores.csv", build_contest_score_rows(db, group_contest.contest))


@app.get("/admin/group-contests/{group_contest_id}/final-codes.zip")
def export_group_contest_final_codes_zip(group_contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group_contest = db.query(GroupContest).filter(GroupContest.id == group_contest_id).first()
    if group_contest is None or group_contest.contest is None:
        raise HTTPException(status_code=404, detail="Group contest not found")
    group = db.query(Group).filter(Group.id == group_contest.group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    return zip_response(f"group_contest_{group_contest.id}_final_codes.zip", build_final_code_zip(group_contest.contest, db))


@app.get("/admin/group-practices/{practice_id}/board.csv")
def export_group_practice_board_csv(practice_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    practice = db.query(GroupPractice).filter(GroupPractice.id == practice_id).first()
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice not found")
    group = db.query(Group).filter(Group.id == practice.group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    members = db.query(GroupMember).filter(GroupMember.group_id == practice.group_id).order_by(GroupMember.user_id.asc()).all()
    items = db.query(GroupPracticeProblem).filter(GroupPracticeProblem.practice_id == practice.id).order_by(GroupPracticeProblem.order_index.asc(), GroupPracticeProblem.id.asc()).all()
    board = build_practice_board(db, practice, members, items)
    header = ["username", "solved_count"] + [str(item.problem_id) for item in items]
    rows = [header]
    for row in board:
        rows.append([row["user"].username if row["user"] else "", row["solved_count"]] + [f"AC/{cell['attempts']}" if cell["solved"] else (f"TRY/{cell['attempts']}" if cell["attempts"] else "") for cell in row["cells"]])
    return csv_response(f"group_practice_{practice.id}_board.csv", rows)


@app.get("/users/{username}", response_class=HTMLResponse)
def user_profile(username: str, request: Request, db: Session = Depends(get_db)):
    viewer = get_current_user(request, db)
    target = db.query(User).filter(User.username == username).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    submissions = db.query(Submission).filter(Submission.user_id == target.id, Submission.contest_id.is_(None)).order_by(Submission.id.desc()).all()
    solved_ids = sorted({s.problem_id for s in submissions if s.result == "AC"})
    tried_ids = sorted({s.problem_id for s in submissions})
    wrong_count = sum(1 for s in submissions if s.result not in {"AC", "JUDGING", "WAITING"})
    ac_count = sum(1 for s in submissions if s.result == "AC")
    tried_only_ids = sorted(set(tried_ids) - set(solved_ids))
    result_rows = db.query(Submission.result, func.count(Submission.id)).filter(Submission.user_id == target.id).group_by(Submission.result).all()
    recent = submissions[:10]
    return templates.TemplateResponse("user_profile.html", {
        "request": request,
        "user": viewer,
        "target": target,
        "solved_ids": solved_ids,
        "tried_ids": tried_ids,
        "tried_only_ids": tried_only_ids,
        "wrong_count": wrong_count,
        "ac_count": ac_count,
        "result_rows": result_rows,
        "recent": recent,
    })


@app.post("/users/{username}/profile-background")
def update_profile_background(username: str, request: Request, profile_background_url: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    target = db.query(User).filter(User.username == username).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not (user.is_admin or user.id == target.id):
        raise HTTPException(status_code=403, detail="본인 또는 관리자만 수정할 수 있습니다.")
    target.profile_background_url = profile_background_url.strip()[:500]
    db.commit()
    return RedirectResponse(url=f"/users/{target.username}", status_code=303)

@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    users = db.query(User).order_by(User.id.asc()).all()
    for item in users:
        cleanup_expired_submit_ban(item, db)
    return templates.TemplateResponse("admin_users.html", {"request": request, "user": user, "users": users, "now": now()})


@app.post("/admin/users/{user_id}/profile")
def admin_update_user_profile(user_id: int, request: Request, full_name: str = Form(""), student_id: str = Form(""), db: Session = Depends(get_db)):
    require_admin(request, db)
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    target.full_name = full_name.strip()[:100]
    target.student_id = student_id.strip()[:50]
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/reset-password")
def admin_reset_user_password(user_id: int, request: Request, new_password: str = Form(...), db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="본인 비밀번호는 이 화면에서 초기화하지 않습니다.")
    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다.")
    target.password_hash = hash_password(new_password)
    target.must_change_password = True
    create_message(db, target.id, "비밀번호 초기화", "관리자가 비밀번호를 초기화했습니다. 로그인 후 비밀번호를 변경해 주세요.", "notice")
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


def read_csv_text(csv_text: str = "", csv_file: UploadFile | None = None) -> str:
    if csv_file is not None and csv_file.filename:
        raw = csv_file.file.read()
        for encoding in ("utf-8-sig", "utf-8", "cp949"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise HTTPException(status_code=400, detail="CSV 파일 인코딩을 읽을 수 없습니다. UTF-8 또는 CP949로 저장해 주세요.")
    return csv_text or ""


def parse_user_bulk_csv(csv_text: str) -> list[dict]:
    rows = []
    if not csv_text.strip():
        return rows
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    for row in reader:
        username = (row.get("username") or row.get("아이디") or "").strip()
        password = (row.get("password") or row.get("비밀번호") or "changeme1234").strip()
        full_name = (row.get("full_name") or row.get("name") or row.get("이름") or "").strip()
        student_id = (row.get("student_id") or row.get("학번") or "").strip()
        if username:
            rows.append({"username": username, "password": password or "changeme1234", "full_name": full_name, "student_id": student_id})
    return rows


@app.post("/admin/users/bulk-create")
def admin_bulk_create_users(request: Request, csv_text: str = Form(""), csv_file: UploadFile | None = File(None), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    created = 0
    skipped = 0
    for row in parse_user_bulk_csv(read_csv_text(csv_text, csv_file)):
        if db.query(User).filter(User.username == row["username"]).first():
            skipped += 1
            continue
        db.add(User(username=row["username"], password_hash=hash_password(row["password"]), full_name=row["full_name"], student_id=row["student_id"], must_change_password=True))
        created += 1
    db.commit()
    users = db.query(User).order_by(User.id.asc()).all()
    return templates.TemplateResponse("admin_users.html", {"request": request, "user": user, "users": users, "now": now(), "message": f"일괄 생성 완료: 생성 {created}명, 건너뜀 {skipped}명"})


@app.post("/admin/users/bulk-update-profile")
def admin_bulk_update_user_profiles(request: Request, csv_text: str = Form(""), csv_file: UploadFile | None = File(None), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    updated = 0
    skipped = 0
    for row in parse_user_bulk_csv(read_csv_text(csv_text, csv_file)):
        target = db.query(User).filter(User.username == row["username"]).first()
        if target is None:
            skipped += 1
            continue
        target.full_name = row["full_name"]
        target.student_id = row["student_id"]
        updated += 1
    db.commit()
    users = db.query(User).order_by(User.id.asc()).all()
    return templates.TemplateResponse("admin_users.html", {"request": request, "user": user, "users": users, "now": now(), "message": f"이름/학번 일괄 등록 완료: 수정 {updated}명, 건너뜀 {skipped}명"})


@app.post("/admin/users/bulk-reset-password")
def admin_bulk_reset_user_passwords(request: Request, user_ids: list[int] = Form([]), new_password: str = Form("changeme1234"), db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다.")
    for user_id in user_ids:
        if user_id == admin.id:
            continue
        target = db.query(User).filter(User.id == user_id).first()
        if target:
            target.password_hash = hash_password(new_password)
            target.must_change_password = True
            create_message(db, target.id, "비밀번호 초기화", "관리자가 비밀번호를 초기화했습니다. 로그인 후 비밀번호를 변경해 주세요.", "notice")
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/toggle-admin")
def toggle_admin(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id != admin.id:
        target.is_admin = not target.is_admin
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/ban-submit")
def ban_submit(user_id: int, request: Request, seconds: int = Form(...), reason: str = Form(""), db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id != admin.id:
        target.submit_banned_until = now() + timedelta(seconds=seconds)
        target.ban_reason = reason or f"{seconds}초 제출 제한"
        create_message(db, target.id, "제출 제한 안내", f"{seconds}초 동안 제출이 제한되었습니다. 사유: {target.ban_reason}", "submit_ban")
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/unban-submit")
def unban_submit(user_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    target.submit_banned_until = None
    target.ban_reason = None
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)
