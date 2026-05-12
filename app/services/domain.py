from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from app.models import (
    Contest,
    ContestProblem,
    Group,
    GroupContest,
    GroupJoinRequest,
    GroupMember,
    GroupPractice,
    GroupPracticeProblem,
    GroupProblemSet,
    GroupProblemSetProblem,
    Message,
    BoardPost,
    BoardComment,
    Problem,
    Submission,
    User,
)


def index_to_label(index: int) -> str:
    if index < 0:
        raise ValueError("index must be non-negative")
    label = ""
    n = index
    while True:
        label = chr(ord("A") + (n % 26)) + label
        n = n // 26 - 1
        if n < 0:
            return label


def parse_problem_id_list(raw: str | Iterable[int] | None) -> list[int]:
    values: list[int] = []
    if raw is None:
        return values
    if not isinstance(raw, str):
        tokens = [str(value) for value in raw]
    else:
        tokens = re.split(r"[\s,]+", raw or "")
    for token in tokens:
        token = str(token).strip()
        if not token:
            continue
        if not token.isdigit():
            raise ValueError("문제 번호는 숫자만 입력할 수 있습니다.")
        value = int(token)
        if value not in values:
            values.append(value)
    return values



def resolve_existing_problem_ids(db: Session, problem_ids: Iterable[int], *, allow_contest_only: bool = True) -> list[int]:
    ordered: list[int] = []
    missing: list[int] = []
    for problem_id in problem_ids:
        if problem_id in ordered:
            continue
        query = db.query(Problem).filter(Problem.id == problem_id)
        if not allow_contest_only:
            query = query.filter(Problem.is_contest_only == False)  # noqa: E712
        problem = query.first()
        if problem is None:
            missing.append(problem_id)
            continue
        ordered.append(problem.id)
    if missing:
        raise ValueError("존재하지 않거나 사용할 수 없는 문제 번호입니다: " + ", ".join(map(str, missing)))
    return ordered

def relabel_contest_problem_links(db: Session, contest_id: int) -> None:
    links = (
        db.query(ContestProblem)
        .filter(ContestProblem.contest_id == contest_id)
        .order_by(ContestProblem.order_index.asc(), ContestProblem.problem_id.asc())
        .all()
    )
    for index, link in enumerate(links):
        link.order_index = index
        link.label = index_to_label(index)


def add_contest_problem_links(db: Session, contest: Contest, problem_ids: Iterable[int]) -> None:
    existing_ids = {
        row[0]
        for row in db.query(ContestProblem.problem_id)
        .filter(ContestProblem.contest_id == contest.id)
        .all()
    }
    next_order = len(existing_ids)
    for problem_id in problem_ids:
        if problem_id in existing_ids:
            continue
        problem = db.query(Problem).filter(Problem.id == problem_id).first()
        if problem is None:
            continue
        db.add(
            ContestProblem(
                contest_id=contest.id,
                problem_id=problem.id,
                label=index_to_label(next_order),
                order_index=next_order,
            )
        )
        existing_ids.add(problem.id)
        next_order += 1
    db.flush()
    relabel_contest_problem_links(db, contest.id)


def create_contest_with_problems(
    db: Session,
    *,
    title: str,
    description: str,
    start_time: datetime,
    end_time: datetime,
    problem_ids: Iterable[int],
    now: datetime,
    title_prefix: str = "",
    score_enabled: bool = False,
) -> Contest:
    if start_time < now:
        raise ValueError("시작 시각은 현재보다 과거일 수 없습니다.")
    if end_time <= start_time:
        raise ValueError("종료 시각은 시작 시각보다 뒤여야 합니다.")
    resolved_problem_ids = resolve_existing_problem_ids(db, problem_ids, allow_contest_only=True)
    contest = Contest(
        title=f"{title_prefix}{title}",
        description=description,
        start_time=start_time,
        end_time=end_time,
        score_enabled=bool(score_enabled),
    )
    db.add(contest)
    db.flush()
    add_contest_problem_links(db, contest, resolved_problem_ids)
    return contest


def link_existing_contest_to_group(db: Session, *, group: Group, contest_id: int, title: str, description: str) -> GroupContest:
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise LookupError("Contest not found")
    exists = db.query(GroupContest).filter(GroupContest.group_id == group.id, GroupContest.contest_id == contest.id).first()
    if exists:
        exists.title = title or exists.title
        exists.description = description
        return exists
    link = GroupContest(group_id=group.id, contest_id=contest.id, title=title, description=description)
    db.add(link)
    return link


def create_group_contest(
    db: Session,
    *,
    group: Group,
    title: str,
    description: str,
    start_time: datetime,
    end_time: datetime,
    problem_ids: Iterable[int],
    now: datetime,
    is_exam_mode: bool = False,
    hide_ranking: bool = False,
    is_public: bool = True,
    score_enabled: bool = False,
) -> GroupContest:
    contest = create_contest_with_problems(
        db,
        title=title,
        description=description,
        start_time=start_time,
        end_time=end_time,
        problem_ids=problem_ids,
        now=now,
        title_prefix=f"[{group.name}] ",
        score_enabled=score_enabled,
    )
    # 관계를 직접 연결해 두어 라우트에서 group_contest.contest가 None으로 보이는 문제를 방지한다.
    contest.is_exam_mode = bool(is_exam_mode)
    contest.hide_ranking = bool(hide_ranking or is_exam_mode)
    contest.is_public = bool(is_public)
    contest.result_display_mode = "full"
    link = GroupContest(group_id=group.id, contest_id=contest.id, title=title, description=description, contest=contest)
    db.add(link)
    return link


def relabel_group_practice_items(db: Session, practice_id: int) -> None:
    items = (
        db.query(GroupPracticeProblem)
        .filter(GroupPracticeProblem.practice_id == practice_id)
        .order_by(GroupPracticeProblem.order_index.asc(), GroupPracticeProblem.id.asc())
        .all()
    )
    for index, item in enumerate(items):
        item.order_index = index


def create_group_practice_with_problems(
    db: Session,
    *,
    group: Group,
    title: str,
    description: str,
    start_time: datetime | None,
    end_time: datetime | None,
    problem_ids: Iterable[int],
) -> GroupPractice:
    if start_time and end_time and end_time <= start_time:
        raise ValueError("종료 시각은 시작 시각보다 뒤여야 합니다.")
    resolved_problem_ids = resolve_existing_problem_ids(db, problem_ids, allow_contest_only=False)
    practice = GroupPractice(group_id=group.id, title=title, description=description, start_time=start_time, end_time=end_time)
    db.add(practice)
    db.flush()
    for order_index, problem_id in enumerate(resolved_problem_ids):
        db.add(GroupPracticeProblem(practice_id=practice.id, problem_id=problem_id, order_index=order_index))
    db.flush()
    relabel_group_practice_items(db, practice.id)
    return practice


def delete_group_tree(db: Session, group: Group) -> None:
    problem_set_ids = [row[0] for row in db.query(GroupProblemSet.id).filter(GroupProblemSet.group_id == group.id).all()]
    practice_ids = [row[0] for row in db.query(GroupPractice.id).filter(GroupPractice.group_id == group.id).all()]
    if problem_set_ids:
        db.query(GroupProblemSetProblem).filter(GroupProblemSetProblem.problem_set_id.in_(problem_set_ids)).delete(synchronize_session=False)
    if practice_ids:
        db.query(GroupPracticeProblem).filter(GroupPracticeProblem.practice_id.in_(practice_ids)).delete(synchronize_session=False)
        db.query(Submission).filter(Submission.practice_id.in_(practice_ids)).update({Submission.practice_id: None}, synchronize_session=False)
    # 그룹 게시판 글/댓글을 먼저 삭제하지 않으면 groups.id 참조 때문에 그룹 삭제가 500으로 실패할 수 있다.
    group_post_ids = [row[0] for row in db.query(BoardPost.id).filter(BoardPost.board_scope == "group", BoardPost.group_id == group.id).all()]
    if group_post_ids:
        db.query(BoardComment).filter(BoardComment.post_id.in_(group_post_ids)).delete(synchronize_session=False)
        db.query(BoardPost).filter(BoardPost.id.in_(group_post_ids)).delete(synchronize_session=False)

    # 그룹 삭제 시에도 대회 정보/문제/제출기록은 보존한다.
    # 대신 그룹과의 소속만 끊고, 진행 중인 그룹 대회는 즉시 종료하여 일반 대회 탭에 노출되지 않게 한다.
    group_contests = db.query(GroupContest).filter(GroupContest.group_id == group.id).all()
    for group_contest in group_contests:
        if group_contest.contest is not None:
            group_contest.contest.is_ended = True
            if group_contest.contest.end_time > datetime.now():
                group_contest.contest.end_time = datetime.now()
            group_contest.contest.is_public = False
        group_contest.group_id = None
        if not group_contest.title and group_contest.contest is not None:
            group_contest.title = group_contest.contest.title

    # 그룹에서 만든 대회 전용 문제는 제출/대회 이력 보존을 위해 문제 자체를 삭제하지 않고 출처 참조만 끊는다.
    db.query(Problem).filter(Problem.origin_group_id == group.id).update({Problem.origin_group_id: None}, synchronize_session=False)

    db.query(GroupProblemSet).filter(GroupProblemSet.group_id == group.id).delete(synchronize_session=False)
    db.query(GroupPractice).filter(GroupPractice.group_id == group.id).delete(synchronize_session=False)
    db.query(GroupJoinRequest).filter(GroupJoinRequest.group_id == group.id).delete(synchronize_session=False)
    db.query(Message).filter(Message.related_group_id == group.id).update({Message.related_group_id: None}, synchronize_session=False)
    db.query(GroupMember).filter(GroupMember.group_id == group.id).delete(synchronize_session=False)
    db.delete(group)


def ensure_group_owner_membership(db: Session, group: Group, owner: User) -> None:
    membership = db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == owner.id).first()
    if membership is None:
        db.add(GroupMember(group_id=group.id, user_id=owner.id, role="owner"))
    else:
        membership.role = "owner"
