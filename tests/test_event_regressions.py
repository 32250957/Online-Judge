import io
import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from starlette.requests import Request

from app.database import SessionLocal
from app.models import User, Problem, Submission, Contest, ContestProblem, Group, GroupMember, GroupContest, JudgeJob
from app.security import hash_password


def make_user(db, name="participant", **kwargs):
    user = User(username=name, password_hash=hash_password("test-password"), **kwargs)
    db.add(user)
    db.commit()
    return user


def make_problem(db, **kwargs):
    problem = Problem(title="sum", description="test", input_description="input", output_description="output", **kwargs)
    db.add(problem)
    db.commit()
    return problem


def make_contest(db, **kwargs):
    values = dict(title="event", description="", start_time=datetime(2026, 11, 11, 9), end_time=datetime(2026, 11, 11, 12))
    values.update(kwargs)
    contest = Contest(**values)
    db.add(contest)
    db.commit()
    return contest


def add_submission(db, user, problem, contest=None, **kwargs):
    values = dict(user_id=user.id, problem_id=problem.id, contest_id=contest.id if contest else None,
                  language="python", code="print(1)", result="AC", judge_status="DONE", created_at=datetime(2026, 11, 11, 0, 30))
    values.update(kwargs)
    submission = Submission(**values)
    db.add(submission)
    db.commit()
    return submission


def request_for(user, method="GET", path="/"):
    return Request({"type": "http", "method": method, "path": path, "headers": [], "query_string": b"",
                    "session": {"user_id": user.id} if user else {}})


def test_elapsed_and_ranking_exclude_ce_se(db, app_main):
    user, problem, contest = make_user(db), make_problem(db), make_contest(db)
    db.add(ContestProblem(contest_id=contest.id, problem_id=problem.id, label="A"))
    db.commit()
    for result in ["CE", "SE", "WA"]:
        add_submission(db, user, problem, contest, result=result, created_at=datetime(2026, 11, 11, 0, 10))
    add_submission(db, user, problem, contest)
    row = app_main.build_contest_rankings(db, contest)[0]
    assert row["penalty"] == 50  # 30 elapsed minutes + one WA.
    assert row["wrong_count"] == 1


def test_freeze_uses_utc_cutoff(db, app_main):
    user, problem, contest = make_user(db), make_problem(db), make_contest(db)
    db.add(ContestProblem(contest_id=contest.id, problem_id=problem.id, label="A"))
    db.commit()
    add_submission(db, user, problem, contest, created_at=datetime(2026, 11, 11, 2, 40))
    assert app_main.build_contest_rankings(db, contest, datetime(2026, 11, 11, 11, 30)) == []


@pytest.mark.parametrize("restriction", ["private", "hidden", "freeze", "foreign_group", "private_problem"])
def test_result_visibility_all_paths(db, app_main, monkeypatch, restriction):
    owner, outsider = make_user(db, "owner"), make_user(db, "outsider")
    problem = make_problem(db, is_public=restriction != "private_problem")
    contest = None if restriction == "private_problem" else make_contest(db)
    monkeypatch.setattr(app_main, "now", lambda: datetime(2026, 11, 11, 11, 45))
    if contest:
        contest.is_public = restriction != "private"
        contest.hide_ranking = restriction == "hidden"
        contest.scoreboard_freeze_enabled = restriction == "freeze"
        contest.scoreboard_freeze_minutes = 30
        if restriction == "foreign_group":
            group = Group(name="private-class", owner_id=owner.id)
            db.add(group)
            db.flush()
            db.add(GroupContest(group_id=group.id, contest_id=contest.id, title="event"))
        db.commit()
    submission = add_submission(db, owner, problem, contest, created_at=datetime(2026, 11, 11, 2, 40))
    assert app_main.visible_submissions_query(db, outsider).count() == 0
    assert app_main.visible_submissions_query(db, owner).count() == 1
    assert app_main.home_recent_submission_logs(db, outsider) == []
    request = request_for(outsider)
    for endpoint in [app_main.api_submission_status, app_main.api_submission_events, app_main.submission_detail]:
        with pytest.raises(HTTPException) as error:
            endpoint(submission.id, request, db)
        assert error.value.status_code == 404
    assert app_main.api_submission_status_bulk(str(submission.id), request, db) == {"submissions": []}


def test_group_manager_cannot_attach_site_contest(db, app_main):
    manager = make_user(db)
    group = Group(name="class", owner_id=manager.id)
    db.add(group)
    db.commit()
    contest = make_contest(db)
    with pytest.raises(HTTPException) as error:
        app_main.create_group_contest_shell(
            group.id, request_for(manager, "POST"), title="hijack", description="", start_time="", end_time="",
            problem_order="", contest_id=str(contest.id), is_exam_mode=None, hide_ranking=None,
            is_public="on", score_enabled=None, scoreboard_freeze_enabled=None, scoreboard_freeze_minutes=0,
            result_display_mode="full", db=db,
        )
    assert error.value.status_code == 403
    assert db.query(GroupContest).count() == 0


def test_cancel_during_judge_discards_old_result(db, app_main, monkeypatch):
    import worker
    user, problem = make_user(db), make_problem(db)
    submission = add_submission(db, user, problem, result="WAITING", judge_status="PENDING")
    job = app_main.enqueue_submission(submission, db=db)
    db.commit()
    job_id, submission_id = job.id, submission.id
    def cancel(*args):
        with SessionLocal() as other:
            assert app_main.cancel_single_judge_job(other, job_id)
        return "AC", "old result", 1, 1
    monkeypatch.setattr(worker, "_judge_submission", cancel)
    assert worker.judge_one(db)
    db.expire_all()
    assert db.get(JudgeJob, job_id).status == "CANCELED"
    assert db.get(Submission, submission_id).result == "SE"


def test_rejudge_during_judge_keeps_new_job(db, app_main, monkeypatch):
    import worker
    user, problem = make_user(db), make_problem(db)
    submission = add_submission(db, user, problem, result="WAITING", judge_status="PENDING")
    job = app_main.enqueue_submission(submission, db=db)
    db.commit()
    old_id, sid = job.id, submission.id
    def requeue(*args):
        with SessionLocal() as other:
            app_main.enqueue_submission(other.get(Submission, sid), db=other, job_type="rejudge")
            other.commit()
        return "AC", "obsolete", 1, 1
    monkeypatch.setattr(worker, "_judge_submission", requeue)
    worker.judge_one(db)
    db.expire_all()
    assert db.get(Submission, sid).result == "WAITING"
    assert db.get(JudgeJob, old_id).status == "CANCELED"
    assert db.query(JudgeJob).filter(JudgeJob.status == "QUEUED").count() == 1


def backup_bytes(main, db):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"type": "online_judge_full_backup"}))
        for model in main.BACKUP_MODEL_LIST:
            archive.writestr(f"db/{model.__tablename__}.json", json.dumps(main.serialize_model_rows(db, model)))
        archive.writestr("problems/1/tests/1.in", "restored")
        archive.writestr("uploads/notice.txt", "restored")
    return buffer.getvalue()


def test_restore_preserves_mount_roots_and_sequence(db, app_main):
    if db.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL restore is exercised by the CI service")
    user = make_user(db)
    problem = make_problem(db)
    add_submission(db, user, problem)
    # Include a group-created problem to exercise real FK insertion order.
    group = Group(name="class", owner_id=user.id)
    db.add(group)
    db.commit()
    problem.origin_group_id = group.id
    db.commit()
    data = backup_bytes(app_main, db)
    for name in ["problems", "uploads"]:
        Path(name).mkdir()
        Path(name, "old.txt").write_text("old")
    roots = [Path(name).stat().st_ino for name in ["problems", "uploads"]]
    app_main.restore_backup_zip(db, data)
    assert roots == [Path(name).stat().st_ino for name in ["problems", "uploads"]]
    assert Path("problems/1/tests/1.in").read_text() == "restored"
    new_user = make_user(db, "after-restore")
    assert new_user.id > user.id
    new_submission = add_submission(db, new_user, db.get(Problem, problem.id))
    assert new_submission.id > 1


def test_invalid_backup_leaves_files_and_db(db, app_main):
    user = make_user(db)
    Path("problems").mkdir()
    Path("problems/keep.txt").write_text("keep")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", '{"type":"online_judge_full_backup"}')
    with pytest.raises(KeyError):
        app_main.restore_backup_zip(db, buffer.getvalue())
    assert db.query(User).count() == 1
    assert Path("problems/keep.txt").read_text() == "keep"


def test_initial_password_redirect_is_enforced(db, app_main):
    import asyncio
    user = make_user(db, must_change_password=True)
    called = []
    async def next_handler(request):
        called.append(True)
    response = asyncio.run(app_main.school_exam_lock_middleware(request_for(user, path="/problems"), next_handler))
    assert response.status_code == 303
    assert response.headers["location"] == "/change-password"
    assert not called


def test_password_change_remains_accessible_during_exam(db, app_main, monkeypatch):
    import asyncio
    from starlette.responses import HTMLResponse
    user = make_user(db, must_change_password=True)
    monkeypatch.setattr(app_main, "get_active_school_exam_lock", lambda *args: {"contest": SimpleNamespace(id=1)})
    async def next_handler(request):
        return HTMLResponse("change password")
    response = asyncio.run(app_main.school_exam_lock_middleware(request_for(user, path="/change-password"), next_handler))
    assert response.status_code == 200


def test_public_and_group_member_can_view_results(db, app_main, monkeypatch):
    owner, member = make_user(db, "owner"), make_user(db, "member")
    problem, contest = make_problem(db), make_contest(db)
    monkeypatch.setattr(app_main, "now", lambda: datetime(2026, 11, 11, 10))
    submission = add_submission(db, owner, problem, contest)
    assert app_main.visible_submissions_query(db, None).count() == 1
    group = Group(name="class", owner_id=owner.id)
    db.add(group)
    db.flush()
    db.add_all([GroupContest(group_id=group.id, contest_id=contest.id, title="event"),
                GroupMember(group_id=group.id, user_id=member.id)])
    db.commit()
    assert app_main.visible_submissions_query(db, None).count() == 0
    assert app_main.api_submission_status(submission.id, request_for(member), db)["result"] == "AC"


def test_pending_submission_limit(db, app_main):
    user, problem = make_user(db), make_problem(db)
    for _ in range(app_main.MAX_PENDING_SUBMISSIONS):
        add_submission(db, user, problem, result="WAITING", judge_status="PENDING")
    with pytest.raises(HTTPException) as error:
        app_main.submit(request_for(user, "POST"), problem.id, "python", "print(1)", "private", None, None, db)
    assert error.value.status_code == 429


def test_restore_file_failure_rolls_back_db_and_files(db, app_main, monkeypatch):
    if db.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL restore is exercised by the CI service")
    make_user(db, "backup-user")
    data = backup_bytes(app_main, db)
    live = make_user(db, "live-user")
    for name in ["problems", "uploads"]:
        Path(name).mkdir()
        Path(name, "keep.txt").write_text("keep")
    original = app_main.replace_directory_contents
    calls = []
    def fail_once(source, target):
        calls.append(target)
        original(source, target)
        if len(calls) == 1:
            raise OSError("simulated disk failure")
    monkeypatch.setattr(app_main, "replace_directory_contents", fail_once)
    with pytest.raises(OSError):
        app_main.restore_backup_zip(db, data)
    assert db.query(User).filter(User.username == "live-user").count() == 1
    assert Path("problems/keep.txt").read_text() == "keep"
    assert Path("uploads/keep.txt").read_text() == "keep"


def test_http_auth_csrf_and_password_gate(db, app_main):
    import re
    from fastapi.testclient import TestClient
    make_user(db, "freshuser", must_change_password=True)
    with TestClient(app_main.app) as client:
        page = client.get("/login")
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        assert client.post("/login", data={"username": "freshuser", "password": "test-password"}).status_code == 403
        logged = client.post("/login", data={"username": "freshuser", "password": "test-password", "csrf_token": token}, follow_redirects=False)
        assert logged.status_code == 303
        assert logged.headers["location"] == "/change-password"
        blocked = client.get("/problems", follow_redirects=False)
        assert blocked.headers["location"] == "/change-password"
        changed = client.post("/change-password", data={"new_password": "new-test-password", "new_password_confirm": "new-test-password", "csrf_token": token}, follow_redirects=False)
        assert changed.status_code == 303
        assert client.get("/problems").status_code == 200
