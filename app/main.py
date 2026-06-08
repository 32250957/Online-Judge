from pathlib import Path
import os
import secrets
import json
import re
import html
import csv
import io
import shutil
import subprocess
import zipfile
import uuid
import time
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional
from markupsafe import Markup, escape

from fastapi import FastAPI, Depends, Request, Form, HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, or_, and_, text, case, DateTime
from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal, engine, Base
from app.schema import ensure_postgresql_schema
from app.models import User, AlgorithmTag, Problem, ProblemExample, ProblemNote, ProblemHint, Submission, Contest, ContestProblem, ContestQuestion, Group, GroupMember, Message, GroupJoinRequest, GroupProblemSet, GroupContest, GroupPractice, GroupProblemSetProblem, GroupPracticeProblem, JudgeJob, JudgeLog, AuditLog, ContestEditorial, BoardPost, BoardComment, ProfileAsset
from app.security import hash_password, password_hash_needs_upgrade, verify_password
from app.judge import judge_python, judge_code, normalize_language, language_label, SUPPORTED_LANGUAGES, effective_time_limit_for_language
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
SESSION_MAX_AGE_SECONDS = int(os.getenv("SESSION_MAX_AGE_SECONDS", str(60 * 60 * 24 * 30)))
SESSION_NORMAL_SECONDS = int(os.getenv("SESSION_NORMAL_SECONDS", str(60 * 60 * 12)))
SESSION_REMEMBER_SECONDS = int(os.getenv("SESSION_REMEMBER_SECONDS", str(60 * 60 * 24 * 30)))
CSRF_SESSION_KEY = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"
UNSAFE_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ACCOUNT_ALLOWED_PATTERN = re.compile(r"^[A-Za-z0-9`~!@#$%^&*|\\\'\";:₩\\?]+$")
ACCOUNT_ALLOWED_HINT = "영어 대소문자, 숫자, 특수문자 ` ~ ! @ # $ % ^ & * | ' \" ; : ₩ \\ ? 만 사용할 수 있습니다."
DELETED_USERNAME_PREFIX = "Deleted_User_"
BACKUP_DIR = Path(os.getenv("OJ_BACKUP_DIR", "backups"))
BACKUP_MODEL_LIST = [
    User, AlgorithmTag, Problem, ProblemExample, ProblemNote, ProblemHint, Contest, ContestProblem,
    ContestEditorial, Submission, ContestQuestion, Group, GroupMember, GroupJoinRequest,
    GroupProblemSet, GroupProblemSetProblem, GroupPractice, GroupPracticeProblem,
    GroupContest, JudgeJob, JudgeLog, AuditLog, BoardPost, BoardComment, ProfileAsset, Message,
]
BACKUP_RESTORE_CONFIRM_TEXT = "복구합니다"
ACTIVE_VISITORS: dict[str, float] = {}
ACTIVE_VISITOR_TTL_SECONDS = int(os.getenv("ACTIVE_VISITOR_TTL_SECONDS", "60"))
ACTIVE_VISITOR_SESSION_KEY = "active_visitor_id"
MAX_IMAGE_UPLOAD_BYTES = int(os.getenv("MAX_IMAGE_UPLOAD_BYTES", str(5 * 1024 * 1024)))
MAX_PRIVATE_ATTACHMENT_BYTES = int(os.getenv("MAX_PRIVATE_ATTACHMENT_BYTES", str(10 * 1024 * 1024)))
MAX_REQUEST_BODY_BYTES = int(os.getenv("MAX_REQUEST_BODY_BYTES", str(20 * 1024 * 1024)))


def ensure_active_visitor_id(request: Request) -> str:
    """동일 브라우저/세션의 비로그인 방문자를 하나로 묶기 위한 키를 보장한다."""
    visitor_id = request.session.get(ACTIVE_VISITOR_SESSION_KEY)
    if not visitor_id:
        visitor_id = secrets.token_urlsafe(16)
        request.session[ACTIVE_VISITOR_SESSION_KEY] = visitor_id
    return str(visitor_id)


def active_guest_key(visitor_id: str) -> str:
    return f"guest:{visitor_id}"


def active_user_key(user_id: int | str) -> str:
    return f"user:{user_id}"


def active_visitor_key(request: Request) -> str:
    session_user_id = request.session.get("user_id")
    visitor_id = ensure_active_visitor_id(request)
    if session_user_id is not None:
        # 로그인으로 전환된 같은 세션의 비로그인 기록은 즉시 제거한다.
        ACTIVE_VISITORS.pop(active_guest_key(visitor_id), None)
        return active_user_key(session_user_id)
    return active_guest_key(visitor_id)


def register_active_visitor(request: Request) -> int:
    ACTIVE_VISITORS[active_visitor_key(request)] = time.time()
    return count_active_visitors()


def unregister_active_visitor(request: Request) -> None:
    visitor_id = request.session.get(ACTIVE_VISITOR_SESSION_KEY)
    if visitor_id:
        ACTIVE_VISITORS.pop(active_guest_key(str(visitor_id)), None)
    user_id = request.session.get("user_id")
    if user_id is not None:
        ACTIVE_VISITORS.pop(active_user_key(user_id), None)


def count_active_visitors() -> int:
    cutoff = time.time() - ACTIVE_VISITOR_TTL_SECONDS
    stale_keys = [key for key, seen_at in ACTIVE_VISITORS.items() if seen_at < cutoff]
    for key in stale_keys:
        ACTIVE_VISITORS.pop(key, None)
    return len(ACTIVE_VISITORS)


def validate_username_value(username: str) -> str | None:
    if not (4 <= len(username) <= 20):
        return "아이디는 4자 이상 20자 이하로 입력해야 합니다."
    if ACCOUNT_ALLOWED_PATTERN.fullmatch(username) is None:
        return "아이디에는 " + ACCOUNT_ALLOWED_HINT
    if username.startswith(DELETED_USERNAME_PREFIX):
        return f"{DELETED_USERNAME_PREFIX}로 시작하는 아이디는 사용할 수 없습니다."
    return None


def validate_password_value(password: str) -> str | None:
    if not (8 <= len(password) <= 128):
        return "비밀번호는 8자 이상 128자 이하로 입력해야 합니다."
    if "\x00" in password:
        return "비밀번호에는 널 문자를 사용할 수 없습니다."
    return None


def deleted_username_for(user_id: int) -> str:
    return f"{DELETED_USERNAME_PREFIX}{user_id}"


def display_username(user: Optional[User]) -> str:
    if user is None:
        return "-"
    if getattr(user, "is_deleted", False):
        return "탈퇴한 사용자"
    return user.username


def csv_display_username(user: Optional[User], admin_detail: bool = True) -> str:
    if user is None:
        return ""
    if getattr(user, "is_deleted", False):
        return f"탈퇴한 사용자 ({user.username})" if admin_detail else "탈퇴한 사용자"
    return user.username


def active_user_query(db: Session):
    return db.query(User).filter(User.is_deleted == False)  # noqa: E712


def count_active_group_members(group: Group) -> int:
    return sum(1 for member in (group.members or []) if member.user and not member.user.is_deleted and not member.user.is_admin)


SOLVED_AC_TIER_NAMES = {
    0: "Unrated",
    1: "Bronze V", 2: "Bronze IV", 3: "Bronze III", 4: "Bronze II", 5: "Bronze I",
    6: "Silver V", 7: "Silver IV", 8: "Silver III", 9: "Silver II", 10: "Silver I",
    11: "Gold V", 12: "Gold IV", 13: "Gold III", 14: "Gold II", 15: "Gold I",
    16: "Platinum V", 17: "Platinum IV", 18: "Platinum III", 19: "Platinum II", 20: "Platinum I",
    21: "Diamond V", 22: "Diamond IV", 23: "Diamond III", 24: "Diamond II", 25: "Diamond I",
    26: "Ruby V", 27: "Ruby IV", 28: "Ruby III", 29: "Ruby II", 30: "Ruby I",
    31: "Master",
}

SOLVED_AC_TIER_THRESHOLDS = [
    (3000, 31),
    (2950, 30), (2900, 29), (2850, 28), (2800, 27), (2700, 26),
    (2600, 25), (2500, 24), (2400, 23), (2300, 22), (2200, 21),
    (2100, 20), (2000, 19), (1900, 18), (1750, 17), (1600, 16),
    (1400, 15), (1250, 14), (1100, 13), (950, 12), (800, 11),
    (650, 10), (500, 9), (400, 8), (300, 7), (200, 6),
    (150, 5), (120, 4), (90, 3), (60, 2), (30, 1),
]


def tier_name(tier: int | None) -> str:
    try:
        value = int(tier or 0)
    except (TypeError, ValueError):
        value = 0
    return SOLVED_AC_TIER_NAMES.get(value, "Unrated")


def tier_group(tier: int | None) -> str:
    try:
        value = int(tier or 0)
    except (TypeError, ValueError):
        value = 0
    if value >= 31:
        return "master"
    if value >= 26:
        return "ruby"
    if value >= 21:
        return "diamond"
    if value >= 16:
        return "platinum"
    if value >= 11:
        return "gold"
    if value >= 6:
        return "silver"
    if value >= 1:
        return "bronze"
    return "unrated"




def tier_short(tier: int | None) -> str:
    name = tier_name(tier)
    if name == "Unrated":
        return "UR"
    roman_to_arabic = {"V": "5", "IV": "4", "III": "3", "II": "2", "I": "1"}
    parts = name.split()
    if len(parts) == 2:
        return f"{parts[0][0]}{roman_to_arabic.get(parts[1], parts[1])}"
    return name

def tier_badge_html(tier: int | None, *, small: bool = False) -> Markup:
    name = tier_name(tier)
    cls = f"tier-text tier-{tier_group(tier)}"
    if small:
        cls += " tier-small"
    return Markup(f'<span class="{cls}">{escape(name)}</span>')


def rating_solved_count_bonus(solved_count: int) -> int:
    if solved_count <= 0:
        return 0
    return round(200 * (1 - (0.997 ** solved_count)))


def tier_from_rating(rating: int) -> int:
    for threshold, tier in SOLVED_AC_TIER_THRESHOLDS:
        if rating >= threshold:
            return tier
    return 0


def user_rating_summary(db: Session, user_or_id) -> dict:
    user_id = user_or_id.id if hasattr(user_or_id, "id") else int(user_or_id)
    ac_rows = (
        db.query(Problem.id, Problem.tier)
        .join(Submission, Submission.problem_id == Problem.id)
        .filter(Submission.user_id == user_id, Submission.result == "AC", Problem.is_contest_only == False)  # noqa: E712
        .distinct()
        .all()
    )
    tier_values = sorted([max(0, min(30, int(row[1] or 0))) for row in ac_rows], reverse=True)
    top100 = tier_values[:100]
    problem_rating = sum(top100)
    solved_count = len(tier_values)
    solved_bonus = rating_solved_count_bonus(solved_count)
    rating = problem_rating + solved_bonus
    tier = tier_from_rating(rating)
    distribution = {key: 0 for key in ["Bronze", "Silver", "Gold", "Platinum", "Diamond", "Ruby", "Unrated"]}
    for value in tier_values:
        if value >= 26:
            distribution["Ruby"] += 1
        elif value >= 21:
            distribution["Diamond"] += 1
        elif value >= 16:
            distribution["Platinum"] += 1
        elif value >= 11:
            distribution["Gold"] += 1
        elif value >= 6:
            distribution["Silver"] += 1
        elif value >= 1:
            distribution["Bronze"] += 1
        else:
            distribution["Unrated"] += 1
    return {
        "rating": rating,
        "tier": tier,
        "tier_name": tier_name(tier),
        "problem_rating": problem_rating,
        "solved_bonus": solved_bonus,
        "solved_count": solved_count,
        "top100_count": len(top100),
        "distribution": distribution,
    }


def rating_progress_summary(rating: int) -> dict:
    rating = max(0, int(rating or 0))
    current_threshold = 0
    next_threshold = None
    next_tier = None
    # thresholds are sorted high -> low, so reverse to walk upward.
    for threshold, tier in sorted(SOLVED_AC_TIER_THRESHOLDS, key=lambda item: item[0]):
        if rating >= threshold:
            current_threshold = threshold
        elif next_threshold is None:
            next_threshold = threshold
            next_tier = tier
            break
    if next_threshold is None:
        return {
            "percent": 100,
            "remaining": 0,
            "next_tier_name": "Master",
            "current_threshold": current_threshold,
            "next_threshold": current_threshold,
        }
    span = max(1, next_threshold - current_threshold)
    percent = round(((rating - current_threshold) / span) * 100)
    percent = max(0, min(100, percent))
    return {
        "percent": percent,
        "remaining": max(0, next_threshold - rating),
        "next_tier_name": tier_name(next_tier),
        "current_threshold": current_threshold,
        "next_threshold": next_threshold,
    }


def recalculate_user_rating(db: Session, target: User) -> dict:
    summary = user_rating_summary(db, target)
    target.ac_rating = summary["rating"]
    target.ac_tier = summary["tier"]
    target.ac_rating_problem_sum = summary["problem_rating"]
    target.ac_rating_solved_bonus = summary["solved_bonus"]
    target.solved_count = summary["solved_count"]
    return summary


def recalculate_all_user_ratings(db: Session) -> int:
    users = db.query(User).filter(User.is_deleted == False).order_by(User.id.asc()).all()  # noqa: E712
    for target in users:
        recalculate_user_rating(db, target)
    return len(users)


def active_algorithm_tags(db: Session):
    return db.query(AlgorithmTag).filter(AlgorithmTag.is_active == True).order_by(AlgorithmTag.order_index.asc(), AlgorithmTag.name.asc(), AlgorithmTag.id.asc()).all()  # noqa: E712


def normalize_algorithm_tag_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower())
    key = re.sub(r"_+", "_", key).strip("_")
    return key[:80]


def selected_algorithm_tags(db: Session, keys: list[str] | None) -> str:
    if not keys:
        return ""
    requested = []
    seen = set()
    for key in keys:
        normalized = normalize_algorithm_tag_key(str(key))
        if normalized and normalized not in seen:
            requested.append(normalized)
            seen.add(normalized)
    if not requested:
        return ""
    valid = {tag.key for tag in active_algorithm_tags(db)}
    return ",".join([key for key in requested if key in valid])


def problem_tag_keys(problem: Problem | None) -> set[str]:
    if problem is None or not getattr(problem, "tags", ""):
        return set()
    return {normalize_algorithm_tag_key(item) for item in problem.tags.split(",") if normalize_algorithm_tag_key(item)}


def problem_tag_names(db: Session, problem: Problem | None) -> list[str]:
    keys = problem_tag_keys(problem)
    if not keys:
        return []
    rows = db.query(AlgorithmTag).filter(AlgorithmTag.key.in_(keys)).order_by(AlgorithmTag.order_index.asc(), AlgorithmTag.name.asc()).all()
    name_by_key = {tag.key: tag.name for tag in rows}
    return [name_by_key.get(key, key) for key in (problem.tags or "").split(",") if normalize_algorithm_tag_key(key) in keys]


def problem_tag_display_map(db: Session, problems: list[Problem]) -> dict[int, str]:
    all_keys: set[str] = set()
    for problem in problems:
        all_keys.update(problem_tag_keys(problem))
    if not all_keys:
        return {problem.id: "-" for problem in problems}
    rows = db.query(AlgorithmTag).filter(AlgorithmTag.key.in_(all_keys)).order_by(AlgorithmTag.order_index.asc(), AlgorithmTag.name.asc()).all()
    name_by_key = {tag.key: tag.name for tag in rows}
    result: dict[int, str] = {}
    for problem in problems:
        names = []
        for raw_key in (problem.tags or "").split(","):
            key = normalize_algorithm_tag_key(raw_key)
            if key:
                names.append(name_by_key.get(key, raw_key.strip()))
        result[problem.id] = ", ".join(names) if names else "-"
    return result


def problem_form_context(db: Session, **kwargs) -> dict:
    ctx = dict(kwargs)
    ctx.setdefault("algorithm_tags", active_algorithm_tags(db))
    ctx.setdefault("selected_tag_keys", problem_tag_keys(ctx.get("problem")))
    return ctx



def tier_color_hex(group: str) -> str:
    return {
        "ruby": "#d91e63",
        "diamond": "#4f6fff",
        "platinum": "#2bb8a5",
        "gold": "#d18d16",
        "silver": "#7c8799",
        "bronze": "#8c4d16",
        "unrated": "#5f6980",
    }.get(group, "#5f6980")


def streak_day_key(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    kst = value.astimezone(APP_TIMEZONE) - timedelta(hours=6)
    return kst.date().isoformat()


def profile_asset_problem_ids(asset: ProfileAsset) -> set[int]:
    result = set()
    for raw in (asset.condition_problem_ids or "").replace("\n", ",").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            result.add(int(raw))
        except ValueError:
            continue
    return result


def profile_asset_earned(asset: ProfileAsset, solved_ids: set[int], *, solved_dates: dict[str, int] | None = None, longest_streak: int = 0, tag_ratings: dict[str, int] | None = None) -> bool:
    condition_type = asset.condition_type or "single"
    required = profile_asset_problem_ids(asset)
    if getattr(asset, "is_default", False) or condition_type == "default":
        return True
    if condition_type == "all":
        return bool(required) and required.issubset(solved_ids)
    if condition_type in {"single", "any"}:
        return bool(required & solved_ids)
    if condition_type == "period_solve":
        # condition_value: days,count  예) 7,5 => 최근 7일 동안 5문제 이상 해결
        try:
            days_raw, count_raw = (asset.condition_value or "0,0").split(",", 1)
            days = max(1, int(days_raw.strip()))
            need_count = max(1, int(count_raw.strip()))
        except Exception:
            return False
        solved_dates = solved_dates or {}
        today = (datetime.now(APP_TIMEZONE) - timedelta(hours=6)).date()
        total = 0
        for i in range(days):
            total += solved_dates.get((today - timedelta(days=i)).isoformat(), 0)
        return total >= need_count
    if condition_type == "streak":
        try:
            need_days = max(1, int(asset.condition_value or "0"))
        except Exception:
            return False
        return longest_streak >= need_days
    if condition_type == "tag_rating":
        # condition_value: count,rating  예) 3,800 => 태그 레이팅 800 이상이 3개 이상
        try:
            count_raw, rating_raw = (asset.condition_value or "0,0").split(",", 1)
            need_count = max(1, int(count_raw.strip()))
            need_rating = max(1, int(rating_raw.strip()))
        except Exception:
            return False
        tag_ratings = tag_ratings or {}
        return sum(1 for rating in tag_ratings.values() if rating >= need_rating) >= need_count
    return False


def profile_asset_condition_text(asset: ProfileAsset) -> str:
    condition_type = asset.condition_type or "single"
    ids = sorted(profile_asset_problem_ids(asset))
    joined = ", ".join(map(str, ids))
    if getattr(asset, "is_default", False) or condition_type == "default":
        return "기본 지급"
    if condition_type == "all":
        return f"문제 {joined} 모두 해결" if ids else "조건 미설정"
    if condition_type == "any":
        return f"문제 {joined} 중 하나 이상 해결" if ids else "조건 미설정"
    if condition_type == "period_solve":
        return f"최근 {asset.condition_value.replace(',', '일 동안 ')}문제 이상 해결" if asset.condition_value else "기간 해결 조건 미설정"
    if condition_type == "streak":
        return f"스트릭 {asset.condition_value}일 이상" if asset.condition_value else "스트릭 조건 미설정"
    if condition_type == "tag_rating":
        return f"태그 레이팅 조건 {asset.condition_value}" if asset.condition_value else "태그 레이팅 조건 미설정"
    return f"문제 {joined} 해결" if ids else "조건 미설정"


def read_limited_upload(file: UploadFile, max_bytes: int, label: str) -> bytes:
    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"{label} 파일은 {max_bytes // (1024 * 1024)}MB 이하만 업로드할 수 있습니다.")
    if not data:
        raise HTTPException(status_code=400, detail=f"비어 있는 {label} 파일은 업로드할 수 없습니다.")
    return data


def detect_image_suffix(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def save_validated_image_upload(file: UploadFile | None, target_dir: Path, filename_prefix: str = "") -> str:
    if not file or not file.filename:
        return ""
    data = read_limited_upload(file, MAX_IMAGE_UPLOAD_BYTES, "이미지")
    suffix = detect_image_suffix(data)
    if suffix is None:
        raise HTTPException(status_code=400, detail="실제 PNG, JPG, GIF, WEBP 이미지 파일만 업로드할 수 있습니다.")
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{filename_prefix}{uuid.uuid4().hex}{suffix}"
    (target_dir / filename).write_bytes(data)
    return filename


def save_profile_asset_upload(file: UploadFile | None) -> str:
    filename = save_validated_image_upload(file, Path("uploads/profile_assets"))
    return f"/uploads/profile_assets/{filename}" if filename else ""

def save_user_profile_upload(file: UploadFile | None, kind: str) -> str:
    safe_kind = "background" if kind == "background" else "avatar"
    filename = save_validated_image_upload(file, Path("uploads/profiles"), f"{safe_kind}_")
    return f"/uploads/profiles/{filename}" if filename else ""

def build_user_profile_context(db: Session, request: Request, viewer: User | None, target: User, **extra) -> dict:
    submissions = db.query(Submission).filter(Submission.user_id == target.id, Submission.contest_id.is_(None)).order_by(Submission.id.desc()).all()
    solved_ids = sorted({s.problem_id for s in submissions if s.result == "AC"})
    solved_set = set(solved_ids)
    tried_ids = sorted({s.problem_id for s in submissions})
    tried_only_ids = sorted(set(tried_ids) - solved_set)
    wrong_count = sum(1 for s in submissions if s.result not in {"AC", "JUDGING", "WAITING"})
    ac_count = sum(1 for s in submissions if s.result == "AC")
    result_rows = db.query(Submission.result, func.count(Submission.id)).filter(Submission.user_id == target.id).group_by(Submission.result).all()
    solved_problems = db.query(Problem).filter(Problem.id.in_(solved_ids)).order_by(Problem.tier.desc(), Problem.id.asc()).all() if solved_ids else []
    tried_only_problems = db.query(Problem).filter(Problem.id.in_(tried_only_ids)).order_by(Problem.id.asc()).all() if tried_only_ids else []
    rating_summary = user_rating_summary(db, target)

    tier_order = ["Ruby", "Diamond", "Platinum", "Gold", "Silver", "Bronze", "Unrated"]
    difficulty_rows = []
    total_solved = max(1, len(solved_ids))
    donut_parts = []
    start_deg = 0.0
    for name in tier_order:
        count = rating_summary["distribution"].get(name, 0)
        percent = round(count * 100 / total_solved, 1) if solved_ids else 0
        group = name.lower()
        color = tier_color_hex(group)
        difficulty_rows.append({"name": name, "count": count, "percent": percent, "group": group, "color": color})
        if count > 0 and solved_ids:
            end_deg = start_deg + (count / total_solved) * 360
            donut_parts.append(f"{color} {start_deg:.2f}deg {end_deg:.2f}deg")
            start_deg = end_deg
    difficulty_donut_style = "conic-gradient(" + ", ".join(donut_parts) + ")" if donut_parts else "conic-gradient(rgba(255,255,255,.08) 0 360deg)"

    solved_problem_by_id = {p.id: p for p in solved_problems}
    tag_counts = []
    for tag in active_algorithm_tags(db):
        count = 0
        tier_values = []
        for problem in solved_problem_by_id.values():
            if tag.key in problem_tag_keys(problem):
                count += 1
                # 태그 레이팅은 해결한 문제의 티어 값을 그대로 누적한다.
                tier_values.append(max(0, min(30, int(problem.tier or 0))))
        if count:
            tag_rating = sum(sorted(tier_values, reverse=True)[:100]) + rating_solved_count_bonus(count)
            tag_counts.append({"tag": tag, "count": count, "percent": round(count * 100 / total_solved, 1), "rating": tag_rating, "tier": tier_from_rating(tag_rating)})
    tag_counts.sort(key=lambda row: (-row["rating"], -row["count"], row["tag"].name))

    # Fixed radar axes: 12 o'clock math, then clockwise.
    radar_tag_keys = ["math", "implementation", "greedy", "string", "data_structures", "graphs", "dp", "geometry"]
    tag_rating_by_key = {row["tag"].key: row["rating"] for row in tag_counts}
    max_tag_rating = max([tag_rating_by_key.get(key, 0) for key in radar_tag_keys] + [1])
    radar_points = []
    radar_labels = []
    import math
    for index, key in enumerate(radar_tag_keys):
        angle = -math.pi / 2 + (2 * math.pi * index / len(radar_tag_keys))
        rating = tag_rating_by_key.get(key, 0)
        ratio = 0 if rating <= 0 else max(0.12, rating / max_tag_rating)
        radius = 118 * ratio
        x = 150 + math.cos(angle) * radius
        y = 150 + math.sin(angle) * radius
        lx = 150 + math.cos(angle) * 140
        ly = 150 + math.sin(angle) * 140
        radar_points.append(f"{x:.1f},{y:.1f}")
        radar_labels.append({"label": key, "x": round(lx, 1), "y": round(ly, 1)})
    tag_radar_points = " ".join(radar_points) if radar_points else "150,150"

    difficulty_detail_rows = []
    tier_counts = {int(problem.tier or 0): 0 for problem in solved_problems}
    for problem in solved_problems:
        try:
            key = int(problem.tier or 0)
        except (TypeError, ValueError):
            key = 0
        tier_counts[key] = tier_counts.get(key, 0) + 1
    for value in range(0, 32):
        count = tier_counts.get(value, 0)
        difficulty_detail_rows.append({
            "name": tier_name(value),
            "group": tier_group(value),
            "count": count,
            "percent": "-" if not solved_ids else f"{round(count * 100 / total_solved, 1)}%",
        })

    solved_problem_tag_map = problem_tag_display_map(db, solved_problems) if solved_problems else {}

    rating_tiles = []
    for problem in sorted(solved_problems, key=lambda item: int(item.tier or 0), reverse=True)[:100]:
        rating_tiles.append({
            "short": tier_short(problem.tier),
            "group": tier_group(problem.tier),
        })

    # Streak: day changes at 06:00 KST. The grid shows the latest 53 weeks.
    ac_dates = {}
    for submission in submissions:
        if submission.result == "AC" and submission.created_at:
            key = streak_day_key(submission.created_at)
            ac_dates[key] = ac_dates.get(key, 0) + 1
    today_key_dt = (datetime.now(APP_TIMEZONE) - timedelta(hours=6)).date()
    start_date = today_key_dt - timedelta(days=370)
    streak_days = []
    for offset in range(371):
        day = start_date + timedelta(days=offset)
        count = ac_dates.get(day.isoformat(), 0)
        if count <= 0:
            level = 0
        elif count <= 2:
            level = 1
        elif count <= 4:
            level = 2
        elif count <= 8:
            level = 3
        else:
            level = 4
        streak_days.append({"date": day.isoformat(), "count": count, "level": level})
    current_streak = 0
    check_day = today_key_dt
    while ac_dates.get(check_day.isoformat(), 0) > 0:
        current_streak += 1
        check_day -= timedelta(days=1)
    longest_streak = 0
    current_run = 0
    for day in sorted(ac_dates):
        # Easier: scan full grid range and count consecutive active days.
        pass
    longest_streak = 0
    current_run = 0
    for item in streak_days:
        if item["count"] > 0:
            current_run += 1
            longest_streak = max(longest_streak, current_run)
        else:
            current_run = 0

    language_order = ["python", "cpp", "java", "c"]
    language_counts = db.query(Submission.language, Submission.result, func.count(Submission.id)).filter(Submission.user_id == target.id).group_by(Submission.language, Submission.result).all()
    raw_language_rows = {}
    for language, result, count in language_counts:
        raw_language_rows.setdefault(language or "unknown", []).append((result, count))
    language_result_rows = {}
    for language in language_order:
        rows = raw_language_rows.pop(language, [])
        if rows:
            language_result_rows[language_label(language)] = rows
    for language, rows in raw_language_rows.items():
        if rows:
            language_result_rows[language_label(language)] = rows

    assets = db.query(ProfileAsset).filter(ProfileAsset.is_active == True).order_by(ProfileAsset.id.desc()).all()  # noqa: E712
    badge_preview_items = []
    background_preview_items = []
    earned_badges = []
    earned_backgrounds = []
    selected_badge = None
    selected_background = None
    for asset in assets:
        earned_bool = profile_asset_earned(asset, solved_set, solved_dates=ac_dates, longest_streak=longest_streak, tag_ratings=tag_rating_by_key)
        item = {
            "id": asset.id,
            "icon": asset.icon_text or "★",
            "image": asset.image_url,
            "title": asset.title,
            "description": profile_asset_condition_text(asset),
            "earned": "획득" if earned_bool else "미획득",
            "earned_bool": earned_bool,
        }
        if asset.asset_type == "background":
            background_preview_items.append(item)
            if earned_bool:
                earned_backgrounds.append(item)
            if asset.id == getattr(target, "selected_profile_background_id", 0) and earned_bool:
                selected_background = item
        else:
            badge_preview_items.append(item)
            if earned_bool:
                earned_badges.append(item)
            if asset.id == getattr(target, "selected_profile_badge_id", 0) and earned_bool:
                selected_badge = item
    if not badge_preview_items:
        badge_preview_items = [
            {"icon": "★", "title": "첫걸음", "description": "첫 문제를 해결했습니다", "earned": "-", "earned_bool": False},
            {"icon": "AC", "title": "정답의 시작", "description": "맞았습니다!!를 달성했습니다", "earned": "-", "earned_bool": False},
        ]
    if not background_preview_items:
        background_preview_items = []

    user_rank = "-"
    user_percentile = "-"

    context = {
        "request": request,
        "user": viewer,
        "target": target,
        "solved_ids": solved_ids,
        "tried_ids": tried_ids,
        "tried_only_ids": tried_only_ids,
        "wrong_count": wrong_count,
        "ac_count": ac_count,
        "result_rows": result_rows,
        "rating_summary": rating_summary,
        "rating_progress": rating_progress_summary(rating_summary["rating"]),
        "accuracy_percent": round((ac_count * 100 / max(1, len(submissions))), 1) if submissions else 0,
        "solved_problems": solved_problems,
        "tried_only_problems": tried_only_problems,
        "difficulty_rows": difficulty_rows,
        "difficulty_donut_style": difficulty_donut_style,
        "tag_rows": tag_counts,
        "tag_radar_points": tag_radar_points,
        "radar_labels": radar_labels,
        "difficulty_detail_rows": difficulty_detail_rows,
        "solved_problem_tag_map": solved_problem_tag_map,
        "rating_problem_tiles": rating_tiles,
        "language_result_rows": language_result_rows,
        "badge_preview_items": badge_preview_items,
        "background_preview_items": background_preview_items,
        "earned_badges": earned_badges,
        "earned_backgrounds": earned_backgrounds,
        "selected_badge": selected_badge,
        "selected_background": selected_background,
        "streak_days": streak_days,
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "user_rank": user_rank,
        "user_percentile": user_percentile,
    }
    context.update(extra)
    return context


DANGEROUS_CONFIRM_TEXTS = {
    "delete_user": "탈퇴 처리하겠습니다",
    "reset_password": "초기화하겠습니다",
    "bulk_create": "일괄 생성하겠습니다",
    "bulk_reset": "일괄 초기화하겠습니다",
    "delete_problem": "문제를 삭제하겠습니다",
    "delete_submission": "제출을 삭제하겠습니다",
    "delete_testcase": "테스트케이스를 삭제하겠습니다",
    "rejudge": "재채점하겠습니다",
    "end_contest": "대회를 종료하겠습니다",
    "delete_contest": "대회를 삭제하겠습니다",
}


def require_confirm_text(confirm_text: str, action_key: str) -> None:
    expected = DANGEROUS_CONFIRM_TEXTS[action_key]
    if (confirm_text or "").strip() != expected:
        raise HTTPException(status_code=400, detail=f"확인 문구를 정확히 입력해야 합니다: {expected}")


templates_pending_globals = []

def delete_user_account(db: Session, target: User) -> None:
    # 소프트 탈퇴 처리: 제출/게시글/댓글은 유지하고, 아이디만 회수 가능하게 변경한다.
    # 화면 표시 이름은 템플릿 헬퍼에서 "탈퇴한 사용자"로 숨긴다.
    target.username = deleted_username_for(target.id)
    target.password_hash = hash_password(uuid.uuid4().hex + uuid.uuid4().hex)
    target.is_admin = False
    target.is_deleted = True
    target.deleted_at = now()
    target.submit_banned_until = None
    target.ban_reason = "탈퇴한 계정"
    target.must_change_password = False
    target.profile_background_url = ""
    target.profile_image_url = ""
    target.selected_profile_badge_id = 0
    target.selected_profile_background_id = 0
    target.full_name = ""
    target.student_id = ""
    db.query(GroupJoinRequest).filter(GroupJoinRequest.user_id == target.id).delete(synchronize_session=False)
    db.query(Message).filter(Message.user_id == target.id).delete(synchronize_session=False)


DEFAULT_ALGORITHM_TAGS = [
    ("implementation", "구현"), ("math", "수학"), ("data_structures", "자료 구조"),
    ("string", "문자열"), ("sorting", "정렬"), ("greedy", "그리디 알고리즘"),
    ("bruteforcing", "브루트포스 알고리즘"), ("dp", "다이나믹 프로그래밍"),
    ("graphs", "그래프 이론"), ("graph_traversal", "그래프 탐색"),
    ("dfs", "깊이 우선 탐색"), ("bfs", "너비 우선 탐색"),
    ("binary_search", "이분 탐색"), ("prefix_sum", "누적 합"),
    ("backtracking", "백트래킹"), ("number_theory", "정수론"),
    ("geometry", "기하학"), ("simulation", "시뮬레이션"),
    ("shortest_path", "최단 경로"), ("dijkstra", "데이크스트라"),
    ("segtree", "세그먼트 트리"), ("priority_queue", "우선순위 큐"),
    ("trees", "트리"), ("combinatorics", "조합론"),
]


def seed_default_algorithm_tags() -> None:
    db = SessionLocal()
    try:
        if db.query(AlgorithmTag).count() == 0:
            for index, (key, name) in enumerate(DEFAULT_ALGORITHM_TAGS, start=1):
                db.add(AlgorithmTag(key=key, name=name, order_index=index, is_active=True))
            db.commit()
    finally:
        db.close()


ensure_postgresql_schema(engine)
seed_default_algorithm_tags()


app = FastAPI(title="Online Judge Contest MVP")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
Path("uploads/editorials").mkdir(parents=True, exist_ok=True)
Path("uploads/school_group_requests").mkdir(parents=True, exist_ok=True)
Path("uploads/profile_assets").mkdir(parents=True, exist_ok=True)
Path("uploads/profiles").mkdir(parents=True, exist_ok=True)
app.mount("/uploads/editorials", StaticFiles(directory="uploads/editorials"), name="editorial_uploads")
app.mount("/uploads/profile_assets", StaticFiles(directory="uploads/profile_assets"), name="profile_asset_uploads")
app.mount("/uploads/profiles", StaticFiles(directory="uploads/profiles"), name="profile_uploads")
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
templates.env.globals["display_username"] = display_username
templates.env.globals["count_active_group_members"] = count_active_group_members
templates.env.globals["tier_name"] = tier_name
templates.env.globals["tier_group"] = tier_group
templates.env.globals["tier_short"] = tier_short
templates.env.globals["tier_badge"] = tier_badge_html
templates.env.globals["user_rating_summary"] = user_rating_summary


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def csrf_input(request: Request) -> Markup:
    token = ensure_csrf_token(request)
    return Markup(f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{escape(token)}">')


def csrf_token(request: Request) -> str:
    return ensure_csrf_token(request)


def session_expiry_at(seconds: int) -> str:
    # now()는 앱 화면/세션 기준 KST naive datetime을 반환한다.
    # 세션 만료 비교도 같은 기준으로 맞춰 timezone aware/naive 비교 오류를 막는다.
    return (now() + timedelta(seconds=seconds)).isoformat()


def parse_session_expiry(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(APP_TIMEZONE).replace(tzinfo=None)
    return parsed


templates.env.globals["csrf_input"] = csrf_input
templates.env.globals["csrf_token"] = csrf_token

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
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


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


def utc_now() -> datetime:
    """DB에 저장하고 내부 비교에 사용할 UTC 기준 naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_to_kst(value: datetime | None) -> datetime | None:
    """UTC naive/aware datetime을 화면 표시용 KST datetime으로 변환한다."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.astimezone(APP_TIMEZONE)


def format_kst(value: datetime | None, default: str = "-") -> str:
    converted = utc_to_kst(value)
    if converted is None:
        return default
    return converted.strftime("%Y-%m-%d %H:%M:%S KST")


def format_plain_time(value: datetime | None, default: str = "-") -> str:
    """앱 로컬 시간으로 저장된 값을 초 단위까지만 표시한다.

    대회/연습 시작·종료 시각처럼 사용자가 KST 기준으로 입력해 저장한
    timezone-naive datetime은 UTC 변환 없이 그대로 표시해야 한다.
    """
    if value is None:
        return default
    return value.strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["kst_time"] = format_kst
templates.env.filters["plain_time"] = format_plain_time


def now():
    """앱 화면/입력용 현재 시각.

    대회/연습 생성 폼처럼 사용자가 한국 시간으로 입력하는 영역은 KST naive 값을 사용한다.
    채점 큐, worker heartbeat, stuck job 판정 같은 내부 런타임 비교는 utc_now()를 사용한다.
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
    expires_at = parse_session_expiry(request.session.get("login_expires_at"))
    if expires_at is not None and expires_at <= now():
        request.session.clear()
        return None
    user = db.query(User).filter(User.id == user_id).first()
    if user and getattr(user, "is_deleted", False):
        request.session.clear()
        return None
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
    admins = db.query(User).filter(User.is_admin == True, User.is_deleted == False).order_by(User.id.asc()).all()  # noqa: E712
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


def contest_phase_info(contest: Contest) -> dict:
    current = now()
    status = contest_status(contest)
    if status == "시작 전":
        remaining = max(int((contest.start_time - current).total_seconds()), 0)
        return {
            "status": status,
            "class": "warn",
            "time_label": "시작까지",
            "remaining_seconds": remaining,
            "message": "아직 대회가 시작되지 않았습니다. 일반 참가자는 문제 목록과 제출을 사용할 수 없습니다.",
        }
    if status == "진행 중":
        remaining = max(int((contest.end_time - current).total_seconds()), 0)
        return {
            "status": status,
            "class": "ok",
            "time_label": "종료까지",
            "remaining_seconds": remaining,
            "message": "대회가 진행 중입니다. 종료 시각 이후에는 제출이 차단됩니다.",
        }
    return {
        "status": status,
        "class": "danger",
        "time_label": "종료됨",
        "remaining_seconds": 0,
        "message": "대회가 종료되었습니다. 대회 전용 문제는 일반 문제로 전환되기 전까지 대회 밖에서 접근할 수 없습니다.",
    }


def format_seconds_korean(seconds: int) -> str:
    seconds = max(int(seconds or 0), 0)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}시간 {m}분 {s}초"


templates.env.globals["contest_phase_info"] = contest_phase_info
templates.env.globals["format_seconds_korean"] = format_seconds_korean


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


def _contest_ranking_signature(db: Session, contest_id: int) -> tuple:
    count_value, max_id_value, done_count_value = db.query(
        func.count(Submission.id),
        func.coalesce(func.max(Submission.id), 0),
        func.sum(case((Submission.judge_status.in_(["DONE", "FAILED"]), 1), else_=0)),
    ).filter(Submission.contest_id == contest_id).one()
    problem_signature = tuple(
        (link.problem_id, link.order_index, link.label, bool(link.exclude_from_ranking), int(link.score or 0))
        for link in db.query(ContestProblem)
        .filter(ContestProblem.contest_id == contest_id)
        .order_by(ContestProblem.order_index.asc(), ContestProblem.problem_id.asc())
        .all()
    )
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    return (
        int(count_value or 0),
        int(max_id_value or 0),
        int(done_count_value or 0),
        bool(getattr(contest, "score_enabled", False)),
        bool(getattr(contest, "scoreboard_freeze_enabled", False)),
        int(getattr(contest, "scoreboard_freeze_minutes", 0) or 0),
        problem_signature,
    )


def _hydrate_cached_rankings(db: Session, cached_rows: list[dict]) -> list[dict]:
    user_ids = [row.get("user_id") for row in cached_rows if row.get("user_id") is not None]
    users = {user.id: user for user in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    hydrated = []
    for row in cached_rows:
        copied = dict(row)
        copied["user"] = users.get(row.get("user_id"))
        copied["solved_problems"] = set(row.get("solved_problem_ids", []))
        copied["best_ac"] = row.get("best_ac", {})
        copied["cells"] = row.get("cells", {})
        hydrated.append(copied)
    return hydrated


def contest_elapsed_minutes(contest: Contest, submitted_at: datetime | None) -> int:
    if submitted_at is None:
        return 0
    seconds = (submitted_at - contest.start_time).total_seconds()
    return max(0, int(seconds // 60))


def contest_freeze_start_time(contest: Contest) -> datetime | None:
    if not getattr(contest, "scoreboard_freeze_enabled", False):
        return None
    minutes = int(getattr(contest, "scoreboard_freeze_minutes", 0) or 0)
    if minutes <= 0:
        return None
    return contest.end_time - timedelta(minutes=minutes)


def contest_scoreboard_frozen_now(contest: Contest) -> bool:
    freeze_start = contest_freeze_start_time(contest)
    current = now()
    return bool(freeze_start is not None and contest.start_time <= current < contest.end_time and current >= freeze_start)


def scoreboard_freeze_cutoff_for_user(user: Optional[User], contest: Contest) -> datetime | None:
    if user and user.is_admin:
        return None
    if not contest_scoreboard_frozen_now(contest):
        return None
    return contest_freeze_start_time(contest)


def build_contest_rankings(db: Session, contest: Contest, freeze_cutoff: datetime | None = None) -> list[dict]:
    links = [link for link in contest.problem_links if not link.exclude_from_ranking]
    links.sort(key=lambda link: (link.order_index, link.problem_id))
    if not links:
        return []

    signature = _contest_ranking_signature(db, contest.id)
    cache_key = (contest.id, freeze_cutoff.isoformat() if freeze_cutoff else "live")
    cached = RANKING_CACHE.get(cache_key)
    current_ts = time.time()
    if cached and cached.get("signature") == signature and current_ts - cached.get("created_ts", 0) <= RANKING_CACHE_TTL_SECONDS:
        return _hydrate_cached_rankings(db, cached.get("rows", []))

    link_by_problem_id = {link.problem_id: link for link in links}
    score_mode = bool(getattr(contest, "score_enabled", False))
    final_pending_results = {"WAITING", "PENDING", "JUDGING"}
    submissions_query = (
        db.query(Submission)
        .options(joinedload(Submission.user))
        .filter(Submission.contest_id == contest.id)
    )
    if freeze_cutoff is not None:
        submissions_query = submissions_query.filter(Submission.created_at < freeze_cutoff)
    submissions = submissions_query.order_by(Submission.created_at.asc(), Submission.id.asc()).all()
    by_user: dict[int, dict] = {}
    for submission in submissions:
        if submission.user_id is None or submission.problem_id not in link_by_problem_id:
            continue
        row = by_user.setdefault(submission.user_id, {
            "user": submission.user,
            "user_id": submission.user_id,
            "solved_problems": set(),
            "wrong_before_ac": {link.problem_id: 0 for link in links},
            "cells": {link.problem_id: "" for link in links},
            "penalty": 0,
            "last_ac_time": 0,
            "total_score": 0,
            "best_ac": {},
        })
        if submission.problem_id in row["solved_problems"]:
            continue
        if submission.result == "AC":
            ac_time = contest_elapsed_minutes(contest, submission.created_at)
            wrong = row["wrong_before_ac"].get(submission.problem_id, 0)
            link = link_by_problem_id[submission.problem_id]
            row["solved_problems"].add(submission.problem_id)
            row["penalty"] += ac_time + wrong * 20
            row["last_ac_time"] = max(row["last_ac_time"], ac_time)
            row["total_score"] += int(link.score or 0)
            row["cells"][submission.problem_id] = "+" if wrong == 0 else f"+{wrong}"
            row["best_ac"][submission.problem_id] = (ac_time, wrong, submission.id)
        elif submission.result not in final_pending_results:
            row["wrong_before_ac"][submission.problem_id] = row["wrong_before_ac"].get(submission.problem_id, 0) + 1
            row["cells"][submission.problem_id] = f"-{row['wrong_before_ac'][submission.problem_id]}"

    rankings = []
    for row in by_user.values():
        row["solved_count"] = len(row["solved_problems"])
        row["wrong_count"] = sum(row["wrong_before_ac"].values())
        # 기존 템플릿/CSV 호환을 위해 필드는 남기되, ICPC식 순위에는 사용하지 않는다.
        row["runtime_ms"] = 0
        row["memory_kb"] = 0
        rankings.append(row)

    if score_mode:
        rankings.sort(key=lambda item: (
            -item["total_score"],
            item["penalty"],
            -item["solved_count"],
            item["last_ac_time"],
            item["user"].username if item["user"] else "",
        ))
    else:
        rankings.sort(key=lambda item: (
            -item["solved_count"],
            item["penalty"],
            item["last_ac_time"],
            item["user"].username if item["user"] else "",
        ))
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
            "total_score": row["total_score"],
            "penalty": row["penalty"],
            "last_ac_time": row["last_ac_time"],
            "cells": {str(k): v for k, v in row["cells"].items()},
            "best_ac": {str(k): list(v) for k, v in row["best_ac"].items()},
        })
    RANKING_CACHE[cache_key] = {"signature": signature, "created_ts": current_ts, "rows": cache_rows}
    return rankings


def build_contest_score_rows(db: Session, contest: Contest) -> list[list]:
    links = [link for link in contest.problem_links if not link.exclude_from_ranking]
    links.sort(key=lambda link: (link.order_index, link.problem_id))
    if not links:
        return [["message"], ["순위에 반영되는 문제가 없습니다."]]

    header = ["rank", "username", "full_name", "student_id"]
    if getattr(contest, "score_enabled", False):
        header.append("total_score")
    header += ["solved_count", "penalty"] + [f"{link.label}({int(link.score or 0)}점)" for link in links]

    rows = [header]
    for row in build_contest_rankings(db, contest):
        submitter = row.get("user")
        body = [
            row["rank"],
            csv_display_username(submitter),
            "" if getattr(submitter, "is_deleted", False) else getattr(submitter, "full_name", ""),
            "" if getattr(submitter, "is_deleted", False) else getattr(submitter, "student_id", ""),
        ]
        if getattr(contest, "score_enabled", False):
            body.append(row.get("total_score", 0))
        body += [row.get("solved_count", 0), row.get("penalty", 0)]
        body += [row.get("cells", {}).get(link.problem_id, row.get("cells", {}).get(str(link.problem_id), "")) for link in links]
        rows.append(body)
    return rows


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
    if getattr(contest, "hide_ranking", False):
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
        for (user_id, problem_id), submission in sorted(latest.items(), key=lambda item: (csv_display_username(item[1].user) if item[1].user else "", link_by_problem_id[item[0][1]].order_index)):
            username = safe_filename(csv_display_username(submission.user) if submission.user else f"user_{user_id}")
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
        return judge_code(problem.id, code, "python", problem_effective_time_limit(problem, "python"), problem.memory_limit)
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


def problem_effective_time_limit(problem: Problem, language: str) -> int:
    return effective_time_limit_for_language(
        getattr(problem, "time_limit", 2),
        language,
        getattr(problem, "python_time_limit", None),
        getattr(problem, "c_time_limit", None),
        getattr(problem, "cpp_time_limit", None),
        getattr(problem, "java_time_limit", None),
    )

def language_time_limit_summary(problem: Problem) -> str:
    parts = []
    for lang in ["python", "c", "cpp", "java"]:
        if lang in set(parse_allowed_languages(getattr(problem, "allowed_languages", "python"))):
            parts.append(f"{language_label(lang)} {problem_effective_time_limit(problem, lang)}초")
    return ", ".join(parts) if parts else "-"

def optional_time_limit(value) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        v = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(60, v))


def allowed_language_labels(problem: Problem) -> str:
    return ", ".join(language_label(lang) for lang in parse_allowed_languages(getattr(problem, "allowed_languages", "python")))


def testcase_status_for_problem(problem_id: int) -> dict:
    tests_dir = problem_dir(problem_id) / "tests"
    input_files = sorted(tests_dir.glob("*.in")) if tests_dir.exists() else []
    output_files = sorted(tests_dir.glob("*.out")) if tests_dir.exists() else []
    missing_outputs = [path.name for path in input_files if not path.with_suffix(".out").exists()]
    missing_inputs = [path.name for path in output_files if not path.with_suffix(".in").exists()]
    empty_outputs = [path.name for path in output_files if path.exists() and path.stat().st_size == 0]
    issues = []
    if not tests_dir.exists():
        issues.append("tests 폴더 없음")
    if tests_dir.exists() and not input_files:
        issues.append("입력 테스트케이스 없음")
    if missing_outputs:
        issues.append("출력 파일 누락: " + ", ".join(missing_outputs[:5]))
    if missing_inputs:
        issues.append("입력 파일 누락: " + ", ".join(missing_inputs[:5]))
    if empty_outputs:
        issues.append("빈 출력 파일: " + ", ".join(empty_outputs[:5]))
    return {
        "input_count": len(input_files),
        "output_count": len(output_files),
        "pair_count": len([path for path in input_files if path.with_suffix(".out").exists()]),
        "missing_inputs": missing_inputs,
        "missing_outputs": missing_outputs,
        "empty_outputs": empty_outputs,
        "issues": issues,
        "ok": not issues,
    }


def problem_status_badges(problem: Problem) -> list[dict]:
    badges = []
    badges.append({"label": "공개" if problem.is_public else "비공개", "kind": "ok" if problem.is_public else "danger"})
    badges.append({"label": "제출 가능" if problem.is_judge_ready else "채점 준비 중", "kind": "ok" if problem.is_judge_ready else "danger"})
    if problem.is_contest_only:
        badges.append({"label": "대회 전용", "kind": "danger"})
    if getattr(problem, "origin_type", "regular") == "group_contest":
        badges.append({"label": "그룹 대회 문제", "kind": "info"})
    if getattr(problem, "force_private_submission", False):
        badges.append({"label": "코드 비공개", "kind": "info"})
    if getattr(problem, "review_status", "none") == "review_pending":
        badges.append({"label": "공개 검토 요청", "kind": "danger"})
    tc = testcase_status_for_problem(problem.id)
    badges.append({"label": f"테케 {tc['pair_count']}개" if tc["ok"] else "테케 점검 필요", "kind": "ok" if tc["ok"] else "danger"})
    return badges


templates.env.globals["allowed_language_labels"] = allowed_language_labels
templates.env.globals["language_time_limit_summary"] = language_time_limit_summary
templates.env.globals["problem_effective_time_limit"] = problem_effective_time_limit
templates.env.globals["language_options_for_problem"] = language_options_for_problem
templates.env.globals["testcase_status_for_problem"] = testcase_status_for_problem
templates.env.globals["problem_status_badges"] = problem_status_badges

def language_allowed_for_problem(problem: Problem, language: str) -> bool:
    return normalize_language(language) in set(parse_allowed_languages(getattr(problem, "allowed_languages", "python")))

def judge_priority_for_submission(db: Session | None, submission: Submission) -> int:
    if db is None:
        problem = getattr(submission, "problem", None)
    else:
        problem = getattr(submission, "problem", None) or db.query(Problem).filter(Problem.id == submission.problem_id).first()
    return max(-100, min(100, int(getattr(problem, "judge_priority", 0) or 0)))


def enqueue_submission(submission: Submission, reason: str = "채점 대기 중입니다.", db: Session | None = None, job_type: str = "judge") -> JudgeJob | None:
    submission.result = "WAITING"
    submission.judge_status = "PENDING"
    submission.detail = reason
    submission.runtime_ms = 0
    submission.memory_kb = 0
    if db is None:
        return None
    # 같은 제출에 대해 남아 있는 진행/실패 job은 새 채점 요청으로 정리한다.
    # FAILED job도 함께 정리해야 재채점 성공 후 과거 실패가 큐 실패 목록에 계속 남지 않는다.
    db.query(JudgeJob).filter(
        JudgeJob.submission_id == submission.id,
        JudgeJob.status.in_(["QUEUED", "RUNNING", "FAILED"]),
    ).update({
        JudgeJob.status: "CANCELED",
        JudgeJob.finished_at: utc_now(),
        JudgeJob.error_message: "새 채점 요청으로 취소됨",
    }, synchronize_session=False)
    job = JudgeJob(submission_id=submission.id, job_type=job_type, status="QUEUED", priority=judge_priority_for_submission(db, submission), created_at=utc_now())
    db.add(job)
    return job


def rejudge_submission(submission: Submission, db: Session | None = None) -> None:
    # Docker worker가 비동기 judge_jobs 큐에서 다시 채점하도록 대기열에 넣는다.
    enqueue_submission(submission, "재채점 대기 중입니다.", db=db, job_type="rejudge")


def normalize_rejudge_language_filters(raw_languages: list[str] | None) -> list[str]:
    """관리자 재채점 필터에서 넘어온 언어 값을 내부 표준 언어 코드로 정리한다."""
    languages: list[str] = []
    for raw in raw_languages or []:
        lang = normalize_language(raw)
        if lang in SUPPORTED_LANGUAGES and lang not in languages:
            languages.append(lang)
    return languages


def submission_matches_languages(submission: Submission, languages: list[str]) -> bool:
    if not languages:
        return True
    return normalize_language(submission.language) in set(languages)


def rejudge_submissions_in_batches(db: Session, submissions: list[Submission], languages: list[str] | None = None, commit_every: int = 100) -> int:
    selected_languages = languages or []
    count = 0
    for submission in submissions:
        if not submission_matches_languages(submission, selected_languages):
            continue
        rejudge_submission(submission, db=db)
        count += 1
        if count % commit_every == 0:
            db.commit()
    return count


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


def is_approved_school_group(group: Optional[Group]) -> bool:
    return bool(group and group.is_school_group and group.school_group_request_status == "approved")


def can_manage_school_group_members(user: Optional[User], group: Optional[Group], db: Optional[Session] = None) -> bool:
    if user is None or group is None:
        return False
    return bool(is_approved_school_group(group) and is_group_manager(user, group, db))


def require_school_group_member_admin(user: User, group: Group, db: Optional[Session] = None) -> None:
    if not can_manage_school_group_members(user, group, db):
        raise HTTPException(status_code=403, detail="회원 비밀번호 초기화와 CSV 일괄 추가는 승인된 학교 분반 그룹에서만 사용할 수 있습니다.")


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
    target_dir = Path("uploads/editorials") / str(contest_id)
    filename = save_validated_image_upload(file, target_dir, f"problem_{problem_id}_")
    return f"/uploads/editorials/{contest_id}/{filename}" if filename else ""


def is_allowed_during_school_exam(lock: dict, path: str, method: str) -> bool:
    group_id = lock["group"].id
    contest_id = lock["contest"].id
    method = method.upper()

    allowed_prefixes = (
        "/static/",
        f"/contests/{contest_id}/problems/",
        "/submissions/",
        "/api/submissions/",
    )
    if path.startswith(allowed_prefixes):
        return True

    allowed_exact = {
        f"/groups/{group_id}",
        f"/contests/{contest_id}",
    }
    if path in allowed_exact:
        return True

    # 시험/평가 모드 중에도 현재 시험 대회 내부에서 이루어지는 제출과 질문은 허용한다.
    # 질문 등록은 POST 경로가 /contests/{id}/questions/new 형태라서,
    # 기존 허용 목록에 없으면 시험 외부 이동으로 오인되어 차단된다.
    if method == "POST":
        if path == "/submit":
            return True
        if path == f"/contests/{contest_id}/questions/new":
            return True
        if path.startswith("/admin/questions/") and path.endswith("/answer"):
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


@app.middleware("http")
async def active_visitor_tracking_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/static/") and not path.startswith("/uploads/") and not path.startswith("/api/home-status"):
        register_active_visitor(request)
    return await call_next(request)


@app.middleware("http")
async def no_store_private_pages_middleware(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    content_type = response.headers.get("content-type", "")
    if not path.startswith("/static/") and ("text/html" in content_type or path in {"/logout"}):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.middleware("http")
async def csrf_protection_middleware(request: Request, call_next):
    if request.method.upper() in UNSAFE_HTTP_METHODS:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BODY_BYTES:
                    return HTMLResponse("요청 본문이 너무 큽니다.", status_code=413)
            except ValueError:
                return HTMLResponse("잘못된 Content-Length 헤더입니다.", status_code=400)
        expected = request.session.get(CSRF_SESSION_KEY)
        supplied = request.headers.get("X-CSRF-Token")

        # request.form() consumes the ASGI body stream.  We read the body once,
        # temporarily replay it for token parsing, and replay it again so the
        # actual route can still receive Form/File parameters normally.
        body = await request.body()

        async def replay_body():
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = replay_body
        if supplied is None:
            try:
                form = await request.form()
                supplied = form.get(CSRF_FORM_FIELD)
            except Exception:
                supplied = None
        request._receive = replay_body

        if not expected or not supplied or not secrets.compare_digest(str(expected), str(supplied)):
            return HTMLResponse("잘못된 요청입니다. 페이지를 새로고침한 뒤 다시 시도해 주세요.", status_code=403)
    return await call_next(request)


# SessionMiddleware must be registered after the custom HTTP middleware above so
# request.session is available inside school_exam_lock_middleware and
# csrf_protection_middleware. In FastAPI/Starlette, middleware added later wraps
# middleware added earlier.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY") or secrets.token_urlsafe(64),
    max_age=SESSION_MAX_AGE_SECONDS,
    same_site="lax",
    https_only=os.getenv("SESSION_COOKIE_SECURE", "0").lower() in {"1", "true", "yes", "on"},
)


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



def today_start() -> datetime:
    """KST 기준 오늘 00:00을 DB 저장 기준인 UTC naive datetime으로 변환한다."""
    today_kst = datetime.now(APP_TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    return today_kst.astimezone(timezone.utc).replace(tzinfo=None)


def judgeable_problem_query(db: Session, user: Optional[User]):
    query = db.query(Problem).filter(Problem.is_contest_only == False, Problem.is_judge_ready == True)  # noqa: E712
    if not (user and user.is_admin):
        query = query.filter(Problem.is_public == True)
    return query


def home_today_submission_count(db: Session) -> int:
    return db.query(Submission).filter(Submission.created_at >= today_start()).count()


def home_recent_submission_logs(db: Session, limit: int = 4) -> list[dict]:
    submissions = db.query(Submission).options(joinedload(Submission.user), joinedload(Submission.problem)).order_by(Submission.id.desc()).limit(limit).all()
    logs = []
    for submission in submissions:
        username = display_username(submission.user) if submission.user else "guest"
        problem_title = submission.problem.title if submission.problem else f"문제 {submission.problem_id}"
        logs.append({
            "id": submission.id,
            "username": username,
            "user_profile_url": f"/users/{submission.user.username}" if submission.user and not getattr(submission.user, "is_deleted", False) else "",
            "problem_id": submission.problem_id,
            "problem_title": problem_title,
            "result": result_label(submission.result),
            "result_code": submission.result,
        })
    return logs


def problem_statement_excerpt(problem: Problem, max_length: int = 180) -> str:
    text = problem.description or ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "문제 지문이 아직 작성되지 않았습니다."
    if len(text) > max_length:
        return text[:max_length].rstrip() + "..."
    return text


def home_problem_tag_names(db: Session, problem: Problem) -> str:
    mapping = {tag.key: tag.name for tag in db.query(AlgorithmTag).filter(AlgorithmTag.is_active == True).all()}  # noqa: E712
    names = []
    for key in [token.strip() for token in (problem.tags or "").split(",") if token.strip()]:
        names.append(mapping.get(key, key))
    return ", ".join(names) if names else "태그 없음"


def recommended_home_problems(db: Session, user: Optional[User], limit: int = 4) -> list[dict]:
    base_query = judgeable_problem_query(db, user)
    solved_ids: set[int] = set()
    tag_score: dict[str, int] = {}
    max_allowed_tier = 2
    highest_solved_tier = 0

    if user:
        solved_rows = (
            db.query(Problem.id, Problem.tags, Problem.tier)
            .join(Submission, Submission.problem_id == Problem.id)
            .filter(Submission.user_id == user.id, Submission.result == "AC", Problem.is_contest_only == False)  # noqa: E712
            .distinct()
            .all()
        )
        solved_ids = {row.id for row in solved_rows}
        highest_solved_tier = max([int(row.tier or 0) for row in solved_rows] or [0])
        # 추천 난이도 상한: AC한 문제 중 최고 티어보다 1단계 높은 문제까지
        # AC 기록이 없으면 Bronze IV 수준까지 노출한다.
        max_allowed_tier = min(30, highest_solved_tier + 1) if highest_solved_tier > 0 else 2
        for row in solved_rows:
            for key in [token.strip() for token in (row.tags or "").split(",") if token.strip()]:
                tag_score[key] = tag_score.get(key, 0) + 1
        if solved_ids:
            base_query = base_query.filter(~Problem.id.in_(solved_ids))
    else:
        max_allowed_tier = 2

    base_query = base_query.filter(Problem.tier <= max_allowed_tier)
    candidates = base_query.order_by(Problem.id.desc()).limit(120).all()
    if not candidates:
        return []

    tag_name_map = {tag.key: tag.name for tag in db.query(AlgorithmTag).filter(AlgorithmTag.is_active == True).all()}  # noqa: E712

    def candidate_score(problem: Problem) -> tuple[int, int, int]:
        keys = [token.strip() for token in (problem.tags or "").split(",") if token.strip()]
        tag_points = max([tag_score.get(key, 0) for key in keys] or [0])
        if highest_solved_tier > 0 and problem.tier:
            tier_points = max(0, 10 - abs(int(problem.tier or 0) - min(max_allowed_tier, highest_solved_tier)))
        else:
            tier_points = max(0, int(problem.tier or 0))
        return (tag_points, tier_points, problem.id)

    selected = sorted(candidates, key=candidate_score, reverse=True)[:limit]
    result = []
    for problem in selected:
        keys = [token.strip() for token in (problem.tags or "").split(",") if token.strip()]
        first_tag = tag_name_map.get(keys[0], keys[0]) if keys else "태그 없음"
        if user and candidate_score(problem)[0] > 0:
            match = "자주 푼 태그"
        elif user and highest_solved_tier > 0 and int(problem.tier or 0) == max_allowed_tier:
            match = "최고 티어 +1 도전"
        elif user and highest_solved_tier > 0:
            match = "해결 티어 범위"
        else:
            match = "입문 추천"
        solved_count = (
            db.query(Submission.user_id)
            .filter(Submission.problem_id == problem.id, Submission.result == "AC")
            .distinct()
            .count()
        )
        result.append({"problem": problem, "tag": first_tag, "match": match, "solved_count": solved_count})
    return result


def active_home_contests(db: Session, user: Optional[User], limit: int = 2) -> list[Contest]:
    current = now()
    query = db.query(Contest).filter(Contest.end_time >= current, Contest.is_ended == False)  # noqa: E712
    if not (user and user.is_admin):
        query = query.filter(Contest.is_public == True)
    return query.order_by(Contest.start_time.asc(), Contest.end_time.asc()).limit(limit).all()


@app.get("/", response_class=HTMLResponse)
def site_home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    recent_problems = judgeable_problem_query(db, user).order_by(Problem.id.desc()).limit(6).all()
    recommended_problems = recommended_home_problems(db, user, limit=4)
    recent_contests = active_home_contests(db, user, limit=2)
    notices = db.query(BoardPost).filter(BoardPost.board_scope == "site", BoardPost.board_type == "notice").order_by(BoardPost.is_pinned.desc(), BoardPost.id.desc()).limit(5).all()
    board_posts = db.query(BoardPost).filter(BoardPost.board_scope == "site").order_by(BoardPost.id.desc()).limit(5).all()
    queue_status = front_queue_status(db)
    profile_summary = user_rating_summary(db, user) if user else None
    profile_tier_class = tier_group(profile_summary["tier"]) if profile_summary else "unrated"
    profile_progress = rating_progress_summary(profile_summary["rating"]) if profile_summary else None
    context = {
        "request": request,
        "user": user,
        "recent_problems": recent_problems,
        "recommended_problems": recommended_problems,
        "recent_contests": recent_contests,
        "notices": notices,
        "board_posts": board_posts,
        "recent_submission_logs": home_recent_submission_logs(db),
        "judgeable_problem_count": judgeable_problem_query(db, user).count(),
        "today_submission_count": home_today_submission_count(db),
        "online_user_count": count_active_visitors(),
        "queue_status": queue_status,
        "profile_summary": profile_summary,
        "profile_tier_class": profile_tier_class,
        "profile_progress": profile_progress,
        "now": now(),
        "problem_statement_excerpt": problem_statement_excerpt,
    }
    return templates.TemplateResponse("home.html", context)


def front_queue_status(db: Session) -> dict:
    queued_judge = db.query(JudgeJob).filter(JudgeJob.job_type == "judge", JudgeJob.status == "QUEUED").count()
    running_judge = db.query(JudgeJob).filter(JudgeJob.job_type == "judge", JudgeJob.status == "RUNNING").count()
    queued_rejudge = db.query(JudgeJob).filter(JudgeJob.job_type == "rejudge", JudgeJob.status == "QUEUED").count()
    running_rejudge = db.query(JudgeJob).filter(JudgeJob.job_type == "rejudge", JudgeJob.status == "RUNNING").count()
    waiting_submissions = db.query(Submission).filter(Submission.result == "WAITING").count()
    total = max(queued_judge + running_judge + queued_rejudge + running_rejudge + waiting_submissions, 1)
    return {
        "normal": {"queued": queued_judge, "running": running_judge, "width": min(100, round(((queued_judge + running_judge) / total) * 100))},
        "rejudge": {"queued": queued_rejudge, "running": running_rejudge, "width": min(100, round(((queued_rejudge + running_rejudge) / total) * 100))},
        "waiting": {"count": waiting_submissions, "width": min(100, round((waiting_submissions / total) * 100))},
        "healthy": True,
    }


def active_front_contest(db: Session, user: Optional[User]) -> Optional[Contest]:
    current = now()
    query = db.query(Contest).filter(Contest.start_time <= current, Contest.end_time >= current, Contest.is_ended == False)  # noqa: E712
    if not (user and user.is_admin):
        query = query.filter(Contest.is_public == True)
    return query.order_by(Contest.end_time.asc(), Contest.id.desc()).first()


@app.get("/api/front-status")
def api_front_status(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    contest = active_front_contest(db, user)
    queue = front_queue_status(db)
    return {
        "queue": queue,
        "contest": {
            "exists": contest is not None,
            "title": contest.title if contest else "진행 중인 대회가 없습니다",
        },
    }



@app.get("/api/active-heartbeat")
def api_active_heartbeat(request: Request):
    return {"online_user_count": register_active_visitor(request)}

@app.get("/api/home-status")
def api_home_status(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return {
        "judgeable_problem_count": judgeable_problem_query(db, user).count(),
        "today_submission_count": home_today_submission_count(db),
        "online_user_count": count_active_visitors(),
        "queue": front_queue_status(db),
        "recent_submissions": home_recent_submission_logs(db),
    }


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
    if tag.strip():
        tag_key = normalize_algorithm_tag_key(tag.strip())
        query = query.filter(Problem.tags.ilike(f"%{tag_key}%"))
    problems = query.order_by(Problem.id.asc()).all()
    problem_tags_display = problem_tag_display_map(db, problems)
    algorithm_tags = db.query(AlgorithmTag).filter(AlgorithmTag.is_active == True).order_by(AlgorithmTag.order_index.asc(), AlgorithmTag.name.asc()).all()  # noqa: E712
    queue_status = front_queue_status(db)
    active_contest = active_front_contest(db, user)
    profile_summary = user_rating_summary(db, user) if user else None
    profile_tier_class = tier_group(profile_summary["tier"]) if profile_summary else "unrated"
    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": user,
        "problems": problems,
        "q": q,
        "tag": tag,
        "algorithm_tags": algorithm_tags,
        "problem_tags_display": problem_tags_display,
        "queue_status": queue_status,
        "active_contest": active_contest,
        "profile_summary": profile_summary,
        "profile_tier_class": profile_tier_class,
    })


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
    username = username.strip()
    username_error = validate_username_value(username)
    if username_error:
        return templates.TemplateResponse("register.html", {"request": request, "user": None, "error": username_error})
    password_error = validate_password_value(password)
    if password_error:
        return templates.TemplateResponse("register.html", {"request": request, "user": None, "error": password_error})
    if db.query(User).filter(User.username == username).first():
        return templates.TemplateResponse("register.html", {"request": request, "user": None, "error": "이미 존재하는 아이디입니다."})
    db.add(User(username=username, password_hash=hash_password(password), is_admin=False))
    db.commit()
    # 회원가입 직후 자동 로그인 상태가 남지 않도록 세션을 비우고 로그인 화면으로 보낸다.
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("login.html", {"request": request, "user": get_current_user(request, db), "error": request.query_params.get("error", "")})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), remember_me: Optional[str] = Form(None), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if user is None or getattr(user, "is_deleted", False) or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "user": None, "error": "아이디 또는 비밀번호가 올바르지 않습니다."})
    if password_hash_needs_upgrade(user.password_hash):
        user.password_hash = hash_password(password)
    old_csrf_token = request.session.get(CSRF_SESSION_KEY)
    old_active_visitor_id = request.session.get(ACTIVE_VISITOR_SESSION_KEY)
    if old_active_visitor_id:
        ACTIVE_VISITORS.pop(active_guest_key(str(old_active_visitor_id)), None)
    request.session.clear()
    request.session[CSRF_SESSION_KEY] = old_csrf_token or secrets.token_urlsafe(32)
    request.session[ACTIVE_VISITOR_SESSION_KEY] = old_active_visitor_id or secrets.token_urlsafe(16)
    request.session["user_id"] = user.id
    request.session["username"] = user.username
    request.session["is_admin"] = user.is_admin
    request.session["remember_me"] = bool(remember_me)
    request.session["login_expires_at"] = session_expiry_at(SESSION_REMEMBER_SECONDS if remember_me else SESSION_NORMAL_SECONDS)
    ACTIVE_VISITORS[active_user_key(user.id)] = time.time()
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
    password_error = validate_password_value(new_password)
    if password_error:
        return templates.TemplateResponse("change_password.html", {"request": request, "user": user, "error": password_error})
    if new_password != new_password_confirm:
        return templates.TemplateResponse("change_password.html", {"request": request, "user": user, "error": "새 비밀번호 확인이 일치하지 않습니다."})
    if not getattr(user, "must_change_password", False) and not verify_password(current_password, user.password_hash):
        return templates.TemplateResponse("change_password.html", {"request": request, "user": user, "error": "현재 비밀번호가 올바르지 않습니다."})
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    lock = get_active_school_exam_lock(db, user)
    if lock is not None:
        return RedirectResponse(url=f"/contests/{lock['contest'].id}", status_code=303)
    unregister_active_visitor(request)
    request.session.clear()
    response = RedirectResponse(url="/", status_code=303)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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
    return templates.TemplateResponse("problem.html", {"request": request, "user": user, "problem": problem, "contest": None, "link": None, "now": now(), "language_options": language_options_for_problem(problem), "problem_tag_names": problem_tag_names(db, problem)})


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




def _active_worker_count(db: Session) -> int:
    heartbeat_cutoff = utc_now() - timedelta(minutes=2)
    recent_heartbeats = db.query(JudgeLog).filter(JudgeLog.event == "heartbeat").order_by(JudgeLog.id.desc()).limit(50).all()
    return len({log.worker_name for log in recent_heartbeats if log.created_at and log.created_at >= heartbeat_cutoff})


def _job_queue_position(db: Session, job: JudgeJob) -> int | None:
    if job.status != "QUEUED":
        return None
    ahead = (
        db.query(JudgeJob)
        .filter(JudgeJob.status == "QUEUED")
        .filter(or_(JudgeJob.priority > job.priority, and_(JudgeJob.priority == job.priority, JudgeJob.id < job.id)))
        .count()
    )
    return ahead + 1


def _latest_job_id_subquery(db: Session):
    return (
        db.query(
            JudgeJob.submission_id.label("submission_id"),
            func.max(JudgeJob.id).label("latest_id"),
        )
        .group_by(JudgeJob.submission_id)
        .subquery()
    )


def current_failed_jobs_query(db: Session):
    """제출별 최신 job이 FAILED인 경우만 현재 실패로 본다."""
    latest = _latest_job_id_subquery(db)
    return (
        db.query(JudgeJob)
        .join(latest, JudgeJob.id == latest.c.latest_id)
        .filter(JudgeJob.status == "FAILED")
    )


def current_failed_job_count(db: Session) -> int:
    return current_failed_jobs_query(db).count()


def active_queue_jobs_query(db: Session):
    """사용자/관리자 큐 화면에서 현재 의미 있는 작업만 조회한다."""
    latest = _latest_job_id_subquery(db)
    return (
        db.query(JudgeJob)
        .outerjoin(latest, JudgeJob.id == latest.c.latest_id)
        .filter(or_(
            JudgeJob.status.in_(["QUEUED", "RUNNING"]),
            and_(JudgeJob.status == "FAILED", latest.c.latest_id.isnot(None)),
        ))
    )


@app.get("/queue", response_class=HTMLResponse)
def user_judge_queue_page(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    statuses = ["QUEUED", "RUNNING", "DONE", "FAILED", "CANCELED"]
    counts = {key: (current_failed_job_count(db) if key == "FAILED" else db.query(JudgeJob).filter(JudgeJob.status == key).count()) for key in statuses}
    active_workers = _active_worker_count(db)
    user_jobs = (
        active_queue_jobs_query(db)
        .join(Submission, JudgeJob.submission_id == Submission.id)
        .options(joinedload(JudgeJob.submission).joinedload(Submission.problem))
        .filter(Submission.user_id == user.id)
        .order_by(JudgeJob.id.desc())
        .limit(50)
        .all()
    )
    recent_user_jobs = (
        db.query(JudgeJob)
        .join(Submission, JudgeJob.submission_id == Submission.id)
        .options(joinedload(JudgeJob.submission).joinedload(Submission.problem))
        .filter(Submission.user_id == user.id)
        .filter(JudgeJob.status.in_(["DONE", "CANCELED"]))
        .order_by(JudgeJob.id.desc())
        .limit(20)
        .all()
    )
    positions = {job.id: _job_queue_position(db, job) for job in user_jobs}
    return templates.TemplateResponse("queue_status.html", {
        "request": request,
        "user": user,
        "counts": counts,
        "active_workers": active_workers,
        "queued_count": counts.get("QUEUED", 0),
        "running_count": counts.get("RUNNING", 0),
        "user_jobs": user_jobs,
        "recent_user_jobs": recent_user_jobs,
        "positions": positions,
    })


@app.get("/api/queue/status")
def api_user_judge_queue_status(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    counts = {key: (current_failed_job_count(db) if key == "FAILED" else db.query(JudgeJob).filter(JudgeJob.status == key).count()) for key in ["QUEUED", "RUNNING", "FAILED"]}
    jobs = (
        active_queue_jobs_query(db)
        .join(Submission, JudgeJob.submission_id == Submission.id)
        .filter(Submission.user_id == user.id)
        .order_by(JudgeJob.id.desc())
        .limit(20)
        .all()
    )
    return {
        "active_workers": _active_worker_count(db),
        "queued_count": counts.get("QUEUED", 0),
        "running_count": counts.get("RUNNING", 0),
        "failed_count": counts.get("FAILED", 0),
        "jobs": [
            {
                "id": job.id,
                "submission_id": job.submission_id,
                "status": job.status,
                "priority": job.priority,
                "position": _job_queue_position(db, job),
                "worker_name": job.worker_name if job.status == "RUNNING" else "",
                "created_at": format_kst(job.created_at),
                "started_at": format_kst(job.started_at, "") if job.started_at else "",
            }
            for job in jobs
        ],
    }

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
    cutoff = utc_now() - timedelta(minutes=minutes)
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
        job.finished_at = utc_now()
        job.error_message = "stuck job auto requeued"
        if submission is None:
            continue
        if submission.judge_status != "JUDGING":
            continue
        submission.judge_status = "PENDING"
        submission.result = "WAITING"
        submission.detail = "멈춘 채점으로 감지되어 자동으로 다시 대기열에 등록되었습니다."
        db.add(JudgeJob(submission_id=submission.id, job_type="rejudge", status="QUEUED", priority=judge_priority_for_submission(db, submission), created_at=utc_now()))
        db.add(JudgeLog(submission_id=submission.id, worker_name=actor, event="auto_requeue", message="stuck RUNNING judge job automatically requeued", created_at=utc_now()))
        count += 1
    if count:
        db.commit()
    return count




def requeue_failed_judge_jobs(db: Session, actor: str = "admin") -> int:
    failed_jobs = current_failed_jobs_query(db).all()
    count = 0
    for job in failed_jobs:
        submission = db.query(Submission).filter(Submission.id == job.submission_id).first()
        if submission is None:
            continue
        enqueue_submission(submission, "실패한 채점 작업이 다시 대기열에 등록되었습니다.", db=db, job_type="rejudge")
        db.add(JudgeLog(submission_id=submission.id, worker_name=actor, event="manual_requeue_failed", message=f"failed judge job #{job.id} requeued", created_at=utc_now()))
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
        job.finished_at = utc_now()
        job.error_message = "연결된 제출을 찾을 수 없습니다."
        db.commit()
        return False
    enqueue_submission(submission, f"채점 작업 #{job.id}이 다시 대기열에 등록되었습니다.", db=db, job_type="rejudge")
    db.add(JudgeLog(submission_id=submission.id, worker_name=actor, event="manual_requeue_job", message=f"judge job #{job.id} requeued", created_at=utc_now()))
    db.commit()
    return True


def cancel_single_judge_job(db: Session, job_id: int, actor: str = "admin") -> bool:
    job = db.query(JudgeJob).filter(JudgeJob.id == job_id).first()
    if job is None or job.status not in {"QUEUED", "RUNNING"}:
        return False
    job.status = "CANCELED"
    job.finished_at = utc_now()
    job.error_message = "관리자에 의해 취소됨"
    submission = db.query(Submission).filter(Submission.id == job.submission_id).first()
    if submission is not None and submission.judge_status in {"PENDING", "JUDGING"}:
        submission.judge_status = "FAILED"
        submission.result = "SE"
        submission.detail = "채점 작업이 관리자에 의해 취소되었습니다."
    db.add(JudgeLog(submission_id=job.submission_id, worker_name=actor, event="manual_cancel_job", message=f"judge job #{job.id} canceled", created_at=utc_now()))
    db.commit()
    return True

def count_stuck_running_judge_jobs(db: Session, minutes: int = 10) -> int:
    cutoff = utc_now() - timedelta(minutes=minutes)
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


def build_worker_operation_rows(db: Session) -> list[dict]:
    heartbeat_cutoff = utc_now() - timedelta(minutes=2)
    heartbeat_rows = db.query(JudgeLog).filter(JudgeLog.event == "heartbeat").order_by(JudgeLog.id.desc()).limit(200).all()
    workers = {}
    for log in heartbeat_rows:
        row = workers.setdefault(log.worker_name, {
            "worker_name": log.worker_name,
            "last_heartbeat": log.created_at,
            "active": bool(log.created_at and log.created_at >= heartbeat_cutoff),
            "current_jobs": [],
            "recent_done": 0,
            "recent_failed": 0,
            "avg_runtime_ms": 0,
        })
        if row["last_heartbeat"] is None or (log.created_at and log.created_at > row["last_heartbeat"]):
            row["last_heartbeat"] = log.created_at
            row["active"] = bool(log.created_at and log.created_at >= heartbeat_cutoff)
    running_jobs = db.query(JudgeJob).filter(JudgeJob.status == "RUNNING").order_by(JudgeJob.started_at.asc()).all()
    for job in running_jobs:
        row = workers.setdefault(job.worker_name or "-", {
            "worker_name": job.worker_name or "-",
            "last_heartbeat": None,
            "active": False,
            "current_jobs": [],
            "recent_done": 0,
            "recent_failed": 0,
            "avg_runtime_ms": 0,
        })
        row["current_jobs"].append(job)
    recent_cutoff = utc_now() - timedelta(hours=1)
    finished_jobs = db.query(JudgeJob).filter(JudgeJob.finished_at.isnot(None), JudgeJob.finished_at >= recent_cutoff).all()
    runtime_by_worker: dict[str, list[int]] = {}
    for job in finished_jobs:
        name = job.worker_name or "-"
        row = workers.setdefault(name, {
            "worker_name": name,
            "last_heartbeat": None,
            "active": False,
            "current_jobs": [],
            "recent_done": 0,
            "recent_failed": 0,
            "avg_runtime_ms": 0,
        })
        if job.status == "DONE":
            row["recent_done"] += 1
        elif job.status == "FAILED":
            row["recent_failed"] += 1
        if job.started_at and job.finished_at:
            runtime_by_worker.setdefault(name, []).append(max(int((job.finished_at - job.started_at).total_seconds() * 1000), 0))
    for name, values in runtime_by_worker.items():
        if values and name in workers:
            workers[name]["avg_runtime_ms"] = int(sum(values) / len(values))
    return sorted(workers.values(), key=lambda row: (not row["active"], row["worker_name"]))


def worker_operation_payload(db: Session) -> list[dict]:
    rows = []
    for row in build_worker_operation_rows(db):
        rows.append({
            "worker_name": row["worker_name"],
            "last_heartbeat": format_kst(row["last_heartbeat"]),
            "active": row["active"],
            "current_jobs": [job.submission_id for job in row["current_jobs"]],
            "recent_done": row["recent_done"],
            "recent_failed": row["recent_failed"],
            "avg_runtime_ms": row["avg_runtime_ms"],
        })
    return rows


@app.get("/api/worker/status")
def api_worker_status(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    auto_requeued = requeue_stuck_judging_submissions(db, actor="worker-status")
    stuck_cutoff = utc_now() - timedelta(minutes=10)
    heartbeat_cutoff = utc_now() - timedelta(minutes=2)
    recent_heartbeats = db.query(JudgeLog).filter(JudgeLog.event == "heartbeat").order_by(JudgeLog.id.desc()).limit(20).all()
    active_workers = len({log.worker_name for log in recent_heartbeats if log.created_at and log.created_at >= heartbeat_cutoff})
    return {
        "pending": db.query(Submission).filter(Submission.judge_status == "PENDING").count(),
        "judging": db.query(Submission).filter(Submission.judge_status == "JUDGING").count(),
        "queued_jobs": db.query(JudgeJob).filter(JudgeJob.status == "QUEUED").count(),
        "running_jobs": db.query(JudgeJob).filter(JudgeJob.status == "RUNNING").count(),
        "failed_jobs": current_failed_job_count(db),
        "stuck": count_stuck_running_judge_jobs(db),
        "failed": db.query(Submission).filter(Submission.judge_status == "FAILED").count(),
        "auto_requeued": auto_requeued,
        "active_workers": active_workers,
        "worker_rows": worker_operation_payload(db),
        "recent_heartbeats": [
            {"time": format_kst(log.created_at), "worker": log.worker_name, "message": log.message}
            for log in recent_heartbeats[:5]
        ],
        "recent_logs": [
            {"time": format_kst(log.created_at), "worker": log.worker_name, "event": log.event, "submission_id": log.submission_id, "message": log.message}
            for log in db.query(JudgeLog).order_by(JudgeLog.id.desc()).limit(20).all()
        ],
    }


@app.get("/admin/worker", response_class=HTMLResponse)
def admin_worker_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    requeue_stuck_judging_submissions(db, actor="worker-page")
    stuck_cutoff = utc_now() - timedelta(minutes=10)
    heartbeat_cutoff = utc_now() - timedelta(minutes=2)
    recent_heartbeats = db.query(JudgeLog).filter(JudgeLog.event == "heartbeat").order_by(JudgeLog.id.desc()).limit(20).all()
    active_workers = len({log.worker_name for log in recent_heartbeats if log.created_at and log.created_at >= heartbeat_cutoff})
    pending = db.query(Submission).filter(Submission.judge_status == "PENDING").count()
    judging = db.query(Submission).filter(Submission.judge_status == "JUDGING").count()
    queued_jobs = db.query(JudgeJob).filter(JudgeJob.status == "QUEUED").count()
    running_jobs = db.query(JudgeJob).filter(JudgeJob.status == "RUNNING").count()
    failed_jobs = current_failed_job_count(db)
    stuck = count_stuck_running_judge_jobs(db)
    failed = db.query(Submission).filter(Submission.judge_status == "FAILED").count()
    logs = db.query(JudgeLog).order_by(JudgeLog.id.desc()).limit(100).all()
    recent_jobs = db.query(JudgeJob).order_by(JudgeJob.id.desc()).limit(100).all()
    worker_rows = build_worker_operation_rows(db)
    return templates.TemplateResponse("worker_status.html", {"request": request, "user": user, "pending": pending, "judging": judging, "stuck": stuck, "failed": failed, "queued_jobs": queued_jobs, "running_jobs": running_jobs, "failed_jobs": failed_jobs, "recent_jobs": recent_jobs, "logs": logs, "active_workers": active_workers, "recent_heartbeats": recent_heartbeats[:5], "worker_rows": worker_rows})


@app.post("/admin/worker/requeue-stuck")
def admin_requeue_stuck_submissions(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    requeue_stuck_judging_submissions(db, actor="admin")
    return RedirectResponse(url="/admin/worker", status_code=303)


@app.get("/admin/judge-queue", response_class=HTMLResponse)
def admin_judge_queue_page(request: Request, status: str = Query(""), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    statuses = ["QUEUED", "RUNNING", "DONE", "FAILED", "CANCELED"]
    if status == "FAILED":
        query = current_failed_jobs_query(db).order_by(JudgeJob.id.desc())
    else:
        query = db.query(JudgeJob)
        if status in statuses:
            query = query.filter(JudgeJob.status == status)
            if status == "QUEUED":
                query = query.order_by(JudgeJob.priority.desc(), JudgeJob.id.asc())
            else:
                query = query.order_by(JudgeJob.id.desc())
        else:
            query = query.order_by(JudgeJob.id.desc())
            status = ""
    jobs = query.limit(300).all()
    counts = {key: (current_failed_job_count(db) if key == "FAILED" else db.query(JudgeJob).filter(JudgeJob.status == key).count()) for key in statuses}
    stuck_cutoff = utc_now() - timedelta(minutes=10)
    stuck_count = count_stuck_running_judge_jobs(db)
    heartbeat_cutoff = utc_now() - timedelta(minutes=2)
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
    failed_jobs = current_failed_job_count(db)
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
    freeze_cutoff = scoreboard_freeze_cutoff_for_user(user, contest)
    rankings = build_contest_rankings(db, contest, freeze_cutoff=freeze_cutoff)
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
        "scoreboard_frozen": freeze_cutoff is not None,
        "scoreboard_freeze_start": contest_freeze_start_time(contest),
        "ranking_links": sorted([link for link in contest.problem_links if not link.exclude_from_ranking], key=lambda link: (link.order_index, link.problem_id)),
        "score_mode": bool(getattr(contest, "score_enabled", False)),
        "ranking_visible": contest_ranking_visible_to(user, contest),
        "contest_closed": contest_is_closed(contest),
        "contest_started": contest_has_started(contest),
        "contest_status": contest_status,
        "phase_info": contest_phase_info(contest),
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
    return templates.TemplateResponse("problem.html", {
        "request": request,
        "user": user,
        "problem": link.problem,
        "contest": contest,
        "link": link,
        "now": now(),
        "contest_start_ms": to_app_epoch_ms(contest.start_time),
        "contest_end_ms": to_app_epoch_ms(contest.end_time),
        "server_now_ms": to_app_epoch_ms(now()),
        "phase_info": contest_phase_info(contest),
        "language_options": language_options_for_problem(link.problem),
        "problem_tag_names": problem_tag_names(db, link.problem),
    })


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
    return templates.TemplateResponse("problem.html", {"request": request, "user": user, "problem": problem, "contest": None, "link": None, "practice": practice, "group": group, "now": now(), "language_options": language_options_for_problem(problem), "problem_tag_names": problem_tag_names(db, problem)})




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
        empty_outputs = [path.name for path in output_files if path.exists() and path.stat().st_size == 0]
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
        if empty_outputs:
            issues.append("빈 출력 파일: " + ", ".join(empty_outputs[:5]))
        if p_dir.exists() and not (p_dir / "meta.json").exists():
            issues.append("meta.json 없음")
        rows.append({
            "problem": problem,
            "input_count": len(input_files),
            "output_count": len(output_files),
            "pair_count": len([path for path in input_files if path.with_suffix(".out").exists()]),
            "empty_output_count": len(empty_outputs),
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
    failed_jobs = current_failed_job_count(db)
    recent_24h = now() - timedelta(hours=24)
    heartbeat_cutoff = utc_now() - timedelta(minutes=2)
    recent_heartbeats = db.query(JudgeLog).filter(JudgeLog.event == "heartbeat", JudgeLog.created_at >= heartbeat_cutoff).all()
    docker_cli_found = shutil.which("docker") is not None
    active_worker_count = len({log.worker_name for log in recent_heartbeats})
    operational_checks = {
        "db_connection": "OK",
        "problems_dir_exists": Path("problems").exists(),
        "docker_cli_found": docker_cli_found,
        "active_worker_count": active_worker_count,
        "queued_jobs": queued_jobs,
        "running_jobs": running_jobs,
        "failed_jobs": failed_jobs,
        "stuck_jobs": count_stuck_running_judge_jobs(db),
        "problem_file_warning_count": sum(1 for row in file_rows if row["issues"]),
        "deleted_user_count": db.query(User).filter(User.is_deleted == True).count(),  # noqa: E712
        "recent_24h_submissions": db.query(Submission).filter(Submission.created_at >= recent_24h).count(),
    }

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
    if not docker_cli_found:
        warnings.append("docker CLI를 찾지 못했습니다. 실제 채점 worker가 실패할 수 있습니다.")
    if active_worker_count == 0:
        warnings.append("최근 2분 안에 heartbeat를 보낸 채점 worker가 없습니다.")

    return {
        "schema_checks": schema_checks,
        "problem_file_rows": file_rows,
        "orphan_problem_dirs": orphan_dirs,
        "operational_checks": operational_checks,
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


def coerce_model_value(column, value):
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))
    return value


def add_directory_to_zip(zf: zipfile.ZipFile, directory: Path, prefix: str) -> None:
    if not directory.exists():
        return
    for path in directory.rglob("*"):
        if path.is_file():
            zf.write(path, arcname=str(Path(prefix) / path.relative_to(directory)))


def safe_extract_prefix(zf: zipfile.ZipFile, prefix: str, target: Path) -> None:
    target = target.resolve()
    for member in zf.infolist():
        name = member.filename
        if not name.startswith(prefix + "/"):
            continue
        relative = Path(name).relative_to(prefix)
        if str(relative) in {".", ""}:
            continue
        destination = (target / relative).resolve()
        if target not in destination.parents and destination != target:
            raise ValueError("백업 파일 경로가 올바르지 않습니다.")
        if member.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst)


def build_backup_zip(db: Session) -> bytes:
    buffer = io.BytesIO()
    created_at = now().strftime("%Y%m%d_%H%M%S")
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "created_at": now().isoformat(sep=" "),
            "type": "online_judge_full_backup",
            "version": "v38_6_13",
            "note": "DB JSON data plus problems/uploads files. This archive can be restored from the admin backup page.",
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for model in BACKUP_MODEL_LIST:
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
    return buffer.getvalue()


def list_saved_backups():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backups = []
    for path in BACKUP_DIR.glob("online_judge_backup_*.zip"):
        if path.is_file():
            backups.append({
                "name": path.name,
                "size_mb": round(path.stat().st_size / (1024 * 1024), 2),
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    return sorted(backups, key=lambda item: item["name"], reverse=True)


def restore_backup_zip(db: Session, backup_bytes: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(backup_bytes), "r") as zf:
        if "manifest.json" not in zf.namelist():
            raise ValueError("manifest.json이 없는 백업 파일입니다.")
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        if manifest.get("type") not in {"online_judge_full_backup", "online_judge_basic_backup"}:
            raise ValueError("지원하지 않는 백업 형식입니다.")
        missing = [f"db/{model.__tablename__}.json" for model in BACKUP_MODEL_LIST if f"db/{model.__tablename__}.json" not in zf.namelist()]
        if missing:
            raise ValueError("백업에 일부 DB 테이블 데이터가 없습니다: " + ", ".join(missing[:5]))

        # DB 복구: FK 순서 문제를 피하기 위해 전체 테이블을 한 번에 TRUNCATE CASCADE 처리합니다.
        table_names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        db.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
        for model in BACKUP_MODEL_LIST:
            rows = json.loads(zf.read(f"db/{model.__tablename__}.json").decode("utf-8"))
            for row in rows:
                data = {}
                for column in model.__table__.columns:
                    if column.name in row:
                        data[column.name] = coerce_model_value(column, row[column.name])
                db.add(model(**data))
        db.commit()

        # 파일 복구: 기존 폴더는 safety copy로 보존한 뒤 백업 파일로 교체합니다.
        stamp = now().strftime("%Y%m%d_%H%M%S")
        restore_safety_dir = BACKUP_DIR / f"before_restore_files_{stamp}"
        restore_safety_dir.mkdir(parents=True, exist_ok=True)
        for folder_name in ["problems", "uploads"]:
            folder = Path(folder_name)
            if folder.exists():
                shutil.move(str(folder), str(restore_safety_dir / folder_name))
            folder.mkdir(parents=True, exist_ok=True)
            safe_extract_prefix(zf, folder_name, folder)


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
        "operational_checks": checks.get("operational_checks", {}),
        "problem_file_rows": [
            {
                "problem_id": row["problem"].id,
                "title": row["problem"].title,
                "input_count": row["input_count"],
                "output_count": row["output_count"],
                "pair_count": row.get("pair_count", 0),
                "empty_output_count": row.get("empty_output_count", 0),
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
        "users": db.query(User).filter(User.is_admin == False, User.is_deleted == False).count(),  # noqa: E712
        "problems": db.query(Problem).count(),
        "submissions": db.query(Submission).count(),
        "contests": db.query(Contest).count(),
        "groups": db.query(Group).count(),
        "board_posts": db.query(BoardPost).count(),
        "warnings": checks["warning_count"],
    }
    message = request.query_params.get("message", "")
    error = request.query_params.get("error", "")
    return templates.TemplateResponse("admin_backups.html", {
        "request": request,
        "user": user,
        "counts": counts,
        "now": now(),
        "backups": list_saved_backups(),
        "message": message,
        "error": error,
        "restore_confirm_text": BACKUP_RESTORE_CONFIRM_TEXT,
    })


@app.get("/admin/backups/download")
def download_full_backup(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    created_at = now().strftime("%Y%m%d_%H%M%S")
    filename = f"online_judge_backup_{created_at}.zip"
    return StreamingResponse(
        io.BytesIO(build_backup_zip(db)),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/admin/backups/create")
def create_saved_backup(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    created_at = now().strftime("%Y%m%d_%H%M%S")
    filename = f"online_judge_backup_{created_at}.zip"
    (BACKUP_DIR / filename).write_bytes(build_backup_zip(db))
    return RedirectResponse(f"/admin/backups?message={urlencode({'': '서버에 백업을 저장했습니다.'})[1:]}", status_code=303)


@app.get("/admin/backups/files/{filename}")
def download_saved_backup(filename: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    if not re.fullmatch(r"online_judge_backup_\d{8}_\d{6}\.zip", filename):
        raise HTTPException(status_code=400, detail="Invalid backup filename")
    path = (BACKUP_DIR / filename).resolve()
    if not path.exists() or path.parent != BACKUP_DIR.resolve():
        raise HTTPException(status_code=404, detail="Backup not found")
    return StreamingResponse(
        open(path, "rb"),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/admin/backups/restore")
async def restore_backup(request: Request, backup_file: UploadFile = File(...), confirm_text: str = Form(""), db: Session = Depends(get_db)):
    require_admin(request, db)
    if confirm_text.strip() != BACKUP_RESTORE_CONFIRM_TEXT:
        return RedirectResponse(f"/admin/backups?error={urlencode({'': '확인 문구가 일치하지 않아 복구를 취소했습니다.'})[1:]}", status_code=303)
    if not backup_file.filename.lower().endswith(".zip"):
        return RedirectResponse(f"/admin/backups?error={urlencode({'': 'ZIP 백업 파일만 복구할 수 있습니다.'})[1:]}", status_code=303)
    try:
        backup_bytes = await backup_file.read()
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        safety_name = f"before_restore_db_files_{now().strftime('%Y%m%d_%H%M%S')}.zip"
        (BACKUP_DIR / safety_name).write_bytes(build_backup_zip(db))
        restore_backup_zip(db, backup_bytes)
    except Exception as exc:
        db.rollback()
        return RedirectResponse(f"/admin/backups?error={urlencode({'': '복구 실패: ' + str(exc)})[1:]}", status_code=303)
    request.session.clear()
    return RedirectResponse("/login?error=백업 복구가 완료되었습니다. 다시 로그인해주세요.", status_code=303)


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
        "user_count": db.query(User).filter(User.is_admin == False, User.is_deleted == False).count(),  # noqa: E712
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
        "judge_queue_counts": {key: (current_failed_job_count(db) if key == "FAILED" else db.query(JudgeJob).filter(JudgeJob.status == key).count()) for key in ["QUEUED", "RUNNING", "FAILED"]},
        "active_worker_count": len({log.worker_name for log in db.query(JudgeLog).filter(JudgeLog.event == "heartbeat", JudgeLog.created_at >= utc_now() - timedelta(minutes=2)).order_by(JudgeLog.id.desc()).limit(20).all()}),
        "ranking_cache_size": len(RANKING_CACHE),
        "slow_request_count": len(SLOW_REQUESTS),
        "recent_audit_logs": db.query(AuditLog).order_by(AuditLog.id.desc()).limit(8).all(),
    })



@app.get("/admin/algorithm-tags", response_class=HTMLResponse)
def admin_algorithm_tags_page(request: Request, message: str = "", error: str = "", db: Session = Depends(get_db)):
    user = require_admin(request, db)
    tags = db.query(AlgorithmTag).order_by(AlgorithmTag.order_index.asc(), AlgorithmTag.name.asc(), AlgorithmTag.id.asc()).all()
    return templates.TemplateResponse("admin_algorithm_tags.html", {"request": request, "user": user, "tags": tags, "message": message, "error": error})


@app.post("/admin/algorithm-tags")
def admin_create_algorithm_tag(request: Request, name: str = Form(...), key: str = Form(""), description: str = Form(""), order_index: int = Form(0), is_active: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    name = name.strip()[:100]
    key = normalize_algorithm_tag_key(key or name)
    if not name or not key:
        return RedirectResponse("/admin/algorithm-tags?error=태그 이름과 키를 입력해야 합니다.", status_code=303)
    if db.query(AlgorithmTag).filter(AlgorithmTag.key == key).first() is not None:
        return RedirectResponse("/admin/algorithm-tags?error=이미 존재하는 태그 키입니다.", status_code=303)
    tag = AlgorithmTag(key=key, name=name, description=description.strip()[:500], order_index=order_index, is_active=is_active == "on")
    db.add(tag)
    audit_log(db, request, user, "algorithm_tag_create", "algorithm_tag", None, f"알고리즘 태그 생성: {name} ({key})")
    db.commit()
    return RedirectResponse("/admin/algorithm-tags?message=태그를 추가했습니다.", status_code=303)


@app.post("/admin/algorithm-tags/{tag_id}/edit")
def admin_edit_algorithm_tag(tag_id: int, request: Request, name: str = Form(...), description: str = Form(""), order_index: int = Form(0), is_active: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    tag = db.query(AlgorithmTag).filter(AlgorithmTag.id == tag_id).first()
    if tag is None:
        raise HTTPException(status_code=404, detail="Algorithm tag not found")
    tag.name = name.strip()[:100]
    tag.description = description.strip()[:500]
    tag.order_index = order_index
    tag.is_active = is_active == "on"
    audit_log(db, request, user, "algorithm_tag_update", "algorithm_tag", tag.id, f"알고리즘 태그 수정: {tag.name}")
    db.commit()
    return RedirectResponse("/admin/algorithm-tags?message=태그를 수정했습니다.", status_code=303)


@app.post("/admin/algorithm-tags/{tag_id}/delete")
def admin_delete_algorithm_tag(tag_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    tag = db.query(AlgorithmTag).filter(AlgorithmTag.id == tag_id).first()
    if tag is None:
        raise HTTPException(status_code=404, detail="Algorithm tag not found")
    summary = f"알고리즘 태그 삭제: {tag.name} ({tag.key})"
    db.delete(tag)
    audit_log(db, request, user, "algorithm_tag_delete", "algorithm_tag", tag_id, summary)
    db.commit()
    return RedirectResponse("/admin/algorithm-tags?message=태그를 삭제했습니다.", status_code=303)


@app.get("/admin/ratings", response_class=HTMLResponse)
def admin_ratings_page(request: Request, message: str = "", db: Session = Depends(get_db)):
    user = require_admin(request, db)
    users = db.query(User).filter(User.is_deleted == False).order_by(User.ac_rating.desc(), User.id.asc()).all()  # noqa: E712
    return templates.TemplateResponse("admin_ratings.html", {"request": request, "user": user, "users": users, "message": message})


@app.post("/admin/ratings/recalculate-all")
def admin_recalculate_all_ratings(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    count = recalculate_all_user_ratings(db)
    audit_log(db, request, user, "rating_recalculate_all", "user", None, f"전체 유저 레이팅 재계산: {count}명")
    db.commit()
    return RedirectResponse(f"/admin/ratings?message={count}명의 레이팅을 재계산했습니다.", status_code=303)


@app.post("/admin/ratings/users/{user_id}/recalculate")
def admin_recalculate_user_rating(user_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    target = db.query(User).filter(User.id == user_id, User.is_deleted == False).first()  # noqa: E712
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    summary = recalculate_user_rating(db, target)
    audit_log(db, request, user, "rating_recalculate", "user", target.id, f"{target.username} 레이팅 재계산: {summary['rating']}")
    db.commit()
    return RedirectResponse(f"/admin/ratings?message={target.username}의 레이팅을 재계산했습니다.", status_code=303)


@app.get("/admin/security", response_class=HTMLResponse)
def admin_security_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    admins = db.query(User).filter(User.is_admin == True, User.is_deleted == False).order_by(User.id.asc()).all()  # noqa: E712
    all_users = db.query(User).filter(User.is_deleted == False).order_by(User.id.asc()).all()
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
    username_error = validate_username_value(username)
    if username_error:
        raise HTTPException(status_code=400, detail=username_error)
    password_error = validate_password_value(password)
    if password_error:
        raise HTTPException(status_code=400, detail=password_error)
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
    password_error = validate_password_value(new_password)
    if password_error:
        raise HTTPException(status_code=400, detail=password_error)
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
        {"name": "users", "label": "회원", "count": db.query(User).filter(User.is_admin == False, User.is_deleted == False).count()},  # noqa: E712
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
    failed_jobs = current_failed_job_count(db)
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

    allowed_types = {"all", "regular", "contest_only", "group_contest", "review_pending", "public", "private", "judge_ready", "judge_not_ready", "testcase_warning"}
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
    if registration_type == "testcase_warning":
        all_candidates = query.order_by(Problem.id.asc()).all()
        filtered = [problem for problem in all_candidates if not testcase_status_for_problem(problem.id)["ok"]]
        total_count = len(filtered)
        total_pages = max((total_count + per_page - 1) // per_page, 1)
        if page > total_pages:
            page = total_pages
        problems = filtered[(page - 1) * per_page: page * per_page]
    else:
        total_count = query.count()
        total_pages = max((total_count + per_page - 1) // per_page, 1)
        if page > total_pages:
            page = total_pages
        problems = query.order_by(Problem.id.asc()).offset((page - 1) * per_page).limit(per_page).all()

    problem_tags_display = problem_tag_display_map(db, problems)

    return templates.TemplateResponse("admin_problems.html", {
        "request": request,
        "user": user,
        "problems": problems,
        "problem_tags_display": problem_tags_display,
        "total_count": total_count,
        "problem_id": (problem_id or "").strip(),
        "registration_type": registration_type if registration_type else "all",
        "keyword": (keyword or "").strip(),
        "tag": (tag or "").strip(),
        "source": (source or "").strip(),
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "language_options": [{"value": key, "label": language_label(key)} for key in SUPPORTED_LANGUAGES],
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
    confirm_text: str = Form(""),
    rejudge_languages: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    require_confirm_text(confirm_text, "rejudge")
    selected_languages = normalize_rejudge_language_filters(rejudge_languages)
    query = build_admin_problem_query(db, problem_id, registration_type, keyword, difficulty, tag, source)
    problem_ids = [row[0] for row in query.with_entities(Problem.id).all()]
    if not problem_ids:
        return templates.TemplateResponse("rejudge_result.html", {"request": request, "user": user, "problem": None, "count": 0, "message": "조건에 맞는 문제가 없습니다."})

    submissions = db.query(Submission).filter(Submission.problem_id.in_(problem_ids)).order_by(Submission.id.asc()).all()
    count = rejudge_submissions_in_batches(db, submissions, selected_languages)
    language_note = f" / 언어: {', '.join(language_label(lang) for lang in selected_languages)}" if selected_languages else ""
    create_message(db, user.id, "대량 재채점 등록 완료", f"검색 조건에 해당하는 {len(problem_ids)}개 문제의 제출 {count}건을 재채점 큐에 등록했습니다.{language_note}", "rejudge_notice")
    audit_log(db, request, user, "bulk_rejudge", "problem", None, f"{len(problem_ids)}개 문제, {count}건 재채점 등록{language_note}")
    db.commit()
    return templates.TemplateResponse("rejudge_result.html", {
        "request": request,
        "user": user,
        "problem": None,
        "count": count,
        "message": f"검색 조건에 해당하는 {len(problem_ids)}개 문제의 제출을 재채점 큐에 등록했습니다.{language_note}",
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
        python_time_limit=getattr(source_problem, "python_time_limit", None),
        c_time_limit=getattr(source_problem, "c_time_limit", None),
        cpp_time_limit=getattr(source_problem, "cpp_time_limit", None),
        java_time_limit=getattr(source_problem, "java_time_limit", None),
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
    return templates.TemplateResponse("problem_form.html", problem_form_context(db, request=request, user=user, problem=None, test_inputs="1 2\n---\n10 20", test_outputs="3\n---\n30", testcases=[], sample_inputs="1 2", sample_outputs="3", action="/admin/problems/new", notes_text="", hints_text=""))


@app.post("/admin/problems/new")
def create_problem(request: Request, problem_id: int = Form(...), title: str = Form(...), description: str = Form(...), input_description: str = Form(...), output_description: str = Form(...), time_limit: int = Form(...), memory_limit: int = Form(...), python_time_limit: str = Form(""), c_time_limit: str = Form(""), cpp_time_limit: str = Form(""), java_time_limit: str = Form(""), difficulty: str = Form(""), tier: int = Form(0), judge_priority: int = Form(0), algorithm_tags: list[str] = Form([]), source: str = Form(""), problem_author: str = Form(""), error_finder: str = Form(""), typo_finder: str = Form(""), test_inputs: str = Form(...), test_outputs: str = Form(...), sample_inputs: str = Form(""), sample_outputs: str = Form(""), notes_text: str = Form(""), hints_text: str = Form(""), is_public: str | None = Form(None), is_judge_ready: str | None = Form(None), force_private_submission: str | None = Form(None), allowed_languages: str = Form("python,c,cpp,java"), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if db.query(Problem).filter(Problem.id == problem_id).first():
        return templates.TemplateResponse("problem_form.html", problem_form_context(db, request=request, user=user, error="이미 존재하는 문제 번호입니다.", problem=None, test_inputs=test_inputs, test_outputs=test_outputs, testcases=[], sample_inputs=sample_inputs, sample_outputs=sample_outputs, action="/admin/problems/new", notes_text=notes_text, hints_text=hints_text, selected_tag_keys=set(algorithm_tags or [])))
    try:
        if time_limit < 1 or time_limit > 60 or memory_limit < 16 or memory_limit > 4096:
            raise ValueError("시간 제한은 1~60초, 메모리 제한은 16~4096MB 범위여야 합니다.")
        save_problem_files(problem_id, title, description, input_description, output_description, time_limit, memory_limit, test_inputs, test_outputs, ",".join(parse_allowed_languages(allowed_languages)))
    except ValueError as e:
        return templates.TemplateResponse("problem_form.html", problem_form_context(db, request=request, user=user, error=str(e), problem=None, test_inputs=test_inputs, test_outputs=test_outputs, testcases=[], sample_inputs=sample_inputs, sample_outputs=sample_outputs, action="/admin/problems/new", notes_text=notes_text, hints_text=hints_text, selected_tag_keys=set(algorithm_tags or [])))
    problem = Problem(
        id=problem_id,
        title=title,
        description=description,
        input_description=input_description,
        output_description=output_description,
        time_limit=time_limit,
        memory_limit=memory_limit,
        python_time_limit=optional_time_limit(python_time_limit),
        c_time_limit=optional_time_limit(c_time_limit),
        cpp_time_limit=optional_time_limit(cpp_time_limit),
        java_time_limit=optional_time_limit(java_time_limit),
        difficulty=tier_name(tier),
        tier=max(0, min(30, int(tier or 0))),
        judge_priority=max(-100, min(100, int(judge_priority or 0))),
        tags=selected_algorithm_tags(db, algorithm_tags),
        source=source.strip(),
        problem_author=problem_author.strip(),
        error_finder=error_finder.strip(),
        typo_finder=typo_finder.strip(),
        allowed_languages=",".join(parse_allowed_languages(allowed_languages)),
        is_contest_only=False,
        is_public=is_public == "on",
        is_judge_ready=is_judge_ready == "on",
        force_private_submission=force_private_submission == "on",
    )
    db.add(problem)
    db.flush()
    try:
        save_problem_examples(db, problem, sample_inputs, sample_outputs)
        save_problem_notes_and_hints(db, problem, notes_text, hints_text)
    except ValueError as e:
        db.rollback()
        return templates.TemplateResponse("problem_form.html", problem_form_context(db, request=request, user=user, error=str(e), problem=None, test_inputs=test_inputs, test_outputs=test_outputs, testcases=[], sample_inputs=sample_inputs, sample_outputs=sample_outputs, action="/admin/problems/new", notes_text=notes_text, hints_text=hints_text, selected_tag_keys=set(algorithm_tags or [])))
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
    return templates.TemplateResponse("problem_form.html", problem_form_context(db, request=request, user=user, problem=problem, test_inputs=test_inputs, test_outputs=test_outputs, testcases=read_problem_testcases(problem.id), sample_inputs=sample_inputs, sample_outputs=sample_outputs, action=f"/admin/problems/{problem.id}/edit", notes_text=read_problem_notes(problem), hints_text=read_problem_hints(problem), can_manage_public_settings=can_manage_problem_public_settings(user, problem)))


@app.post("/admin/problems/{problem_id}/edit")
def edit_problem(problem_id: int, request: Request, title: str = Form(...), description: str = Form(...), input_description: str = Form(...), output_description: str = Form(...), time_limit: int = Form(...), memory_limit: int = Form(...), python_time_limit: str = Form(""), c_time_limit: str = Form(""), cpp_time_limit: str = Form(""), java_time_limit: str = Form(""), difficulty: str = Form(""), tier: int = Form(0), judge_priority: int = Form(0), algorithm_tags: list[str] = Form([]), source: str = Form(""), problem_author: str = Form(""), error_finder: str = Form(""), typo_finder: str = Form(""), test_inputs: str = Form(...), test_outputs: str = Form(...), sample_inputs: str = Form(""), sample_outputs: str = Form(""), notes_text: str = Form(""), hints_text: str = Form(""), is_public: str | None = Form(None), is_judge_ready: str | None = Form(None), force_private_submission: str | None = Form(None), allowed_languages: str = Form("python,c,cpp,java"), promote: str | None = Form(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")
    if not can_edit_problem(user, problem, db):
        raise HTTPException(status_code=403, detail="문제 수정 권한이 없습니다.")
    try:
        if time_limit < 1 or time_limit > 60 or memory_limit < 16 or memory_limit > 4096:
            raise ValueError("시간 제한은 1~60초, 메모리 제한은 16~4096MB 범위여야 합니다.")
        save_problem_files(problem_id, title, description, input_description, output_description, time_limit, memory_limit, test_inputs, test_outputs, ",".join(parse_allowed_languages(allowed_languages)))
    except ValueError as e:
        return templates.TemplateResponse("problem_form.html", problem_form_context(db, request=request, user=user, error=str(e), problem=problem, test_inputs=test_inputs, test_outputs=test_outputs, testcases=read_problem_testcases(problem.id), sample_inputs=sample_inputs, sample_outputs=sample_outputs, action=f"/admin/problems/{problem.id}/edit", notes_text=read_problem_notes(problem), hints_text=read_problem_hints(problem), selected_tag_keys=set(algorithm_tags or [])))
    problem.title = title
    problem.description = description
    problem.input_description = input_description
    problem.output_description = output_description
    problem.time_limit = time_limit
    problem.memory_limit = memory_limit
    problem.python_time_limit = optional_time_limit(python_time_limit)
    problem.c_time_limit = optional_time_limit(c_time_limit)
    problem.cpp_time_limit = optional_time_limit(cpp_time_limit)
    problem.java_time_limit = optional_time_limit(java_time_limit)
    problem.tier = max(0, min(30, int(tier or 0)))
    problem.judge_priority = max(-100, min(100, int(judge_priority or 0)))
    problem.difficulty = tier_name(problem.tier)
    problem.tags = selected_algorithm_tags(db, algorithm_tags)
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
        return templates.TemplateResponse("problem_form.html", problem_form_context(db, request=request, user=user, error=str(e), problem=problem, test_inputs=test_inputs, test_outputs=test_outputs, testcases=read_problem_testcases(problem.id), sample_inputs=sample_inputs, sample_outputs=sample_outputs, action=f"/admin/problems/{problem.id}/edit", notes_text=read_problem_notes(problem), hints_text=read_problem_hints(problem), selected_tag_keys=set(algorithm_tags or [])))
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
def rejudge_problem(problem_id: int, request: Request, confirm_text: str = Form(""), rejudge_languages: list[str] = Form([]), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    require_confirm_text(confirm_text, "rejudge")
    selected_languages = normalize_rejudge_language_filters(rejudge_languages)
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")

    submissions = db.query(Submission).filter(Submission.problem_id == problem.id).order_by(Submission.id.asc()).all()
    count = rejudge_submissions_in_batches(db, submissions, selected_languages, commit_every=50)
    language_note = f" / 언어: {', '.join(language_label(lang) for lang in selected_languages)}" if selected_languages else ""
    create_message(db, user.id, "문제 재채점 등록 완료", f"{problem.id}번 문제의 제출 {count}건을 재채점 큐에 등록했습니다.{language_note}", "rejudge_notice")
    audit_log(db, request, user, "problem_rejudge", "problem", problem.id, f"{count}건 재채점 등록{language_note}")
    db.commit()
    return templates.TemplateResponse("rejudge_result.html", {
        "request": request,
        "user": user,
        "problem": problem,
        "count": count,
        "message": f"{problem.id}번 문제의 제출을 재채점 큐에 등록했습니다.{language_note}",
    })


@app.post("/admin/submissions/rejudge-after")
def rejudge_submissions_after(
    request: Request,
    start_submission_id: int = Form(...),
    confirm_text: str = Form(""),
    rejudge_languages: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    require_confirm_text(confirm_text, "rejudge")
    selected_languages = normalize_rejudge_language_filters(rejudge_languages)
    submissions = db.query(Submission).filter(Submission.id >= start_submission_id).order_by(Submission.id.asc()).all()
    count = rejudge_submissions_in_batches(db, submissions, selected_languages)
    language_note = f" / 언어: {', '.join(language_label(lang) for lang in selected_languages)}" if selected_languages else ""
    create_message(db, user.id, "범위 재채점 등록 완료", f"제출 #{start_submission_id} 이후 제출 {count}건을 재채점 큐에 등록했습니다.{language_note}", "rejudge_notice")
    audit_log(db, request, user, "submission_range_rejudge", "submission", start_submission_id, f"제출 #{start_submission_id} 이후 {count}건 재채점 등록{language_note}")
    db.commit()
    return templates.TemplateResponse("rejudge_result.html", {
        "request": request,
        "user": user,
        "problem": None,
        "count": count,
        "message": f"제출 #{start_submission_id} 이후 제출을 재채점 큐에 등록했습니다.{language_note}",
    })


@app.post("/admin/submissions/{submission_id}/rejudge")
def rejudge_single_submission(submission_id: int, request: Request, confirm_text: str = Form(""), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    require_confirm_text(confirm_text, "rejudge")
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    rejudge_submission(submission, db=db)
    audit_log(db, request, user, "submission_rejudge", "submission", submission.id, f"제출 #{submission.id} 재채점 등록")
    db.commit()
    return RedirectResponse(url=f"/submissions/{submission.id}", status_code=303)


@app.post("/admin/submissions/{submission_id}/delete")
def delete_submission_admin(submission_id: int, request: Request, confirm_text: str = Form(""), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    require_confirm_text(confirm_text, "delete_submission")
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    audit_log(db, request, user, "submission_delete", "submission", submission.id, f"제출 삭제: #{submission.id}")
    delete_submission_tree(db, submission)
    db.commit()
    return RedirectResponse(url="/submissions", status_code=303)


@app.post("/admin/problems/{problem_id}/delete")
def delete_problem_admin(problem_id: int, request: Request, confirm_text: str = Form(""), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    require_confirm_text(confirm_text, "delete_problem")
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
def delete_testcase_route(problem_id: int, case_index: int, request: Request, confirm_text: str = Form(""), db: Session = Depends(get_db)):
    require_admin(request, db)
    require_confirm_text(confirm_text, "delete_testcase")
    problem = db.query(Problem).filter(Problem.id == problem_id).first()
    if problem is None:
        raise HTTPException(status_code=404, detail="Problem not found")
    try:
        delete_problem_testcase(problem.id, case_index)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url=f"/admin/problems/{problem.id}/edit#testcases", status_code=303)


@app.post("/admin/contests/{contest_id}/rejudge")
def rejudge_contest(contest_id: int, request: Request, confirm_text: str = Form(""), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    require_confirm_text(confirm_text, "rejudge")
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
def rejudge_group_practice(practice_id: int, request: Request, confirm_text: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    require_confirm_text(confirm_text, "rejudge")
    practice = db.query(GroupPractice).filter(GroupPractice.id == practice_id).first()
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice not found")
    group = db.query(Group).filter(Group.id == practice.group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    submissions = db.query(Submission).filter(Submission.practice_id == practice.id).order_by(Submission.id.asc()).all()
    for submission in submissions:
        rejudge_submission(submission, db=db)
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
def group_detail(group_id: int, request: Request, board_type: str = Query("all"), invite_error: str = Query(""), invite_message: str = Query(""), db: Session = Depends(get_db)):
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
    can_school_group_member_admin = can_manage_school_group_members(user, group, db)
    return templates.TemplateResponse("group_detail.html", {
        "request": request,
        "user": user,
        "group": group,
        "membership": membership,
        "can_manage": can_manage,
        "can_manage_members": can_manage_members,
        "can_school_group_member_admin": can_school_group_member_admin,
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
        "invite_error": invite_error,
        "invite_message": invite_message,
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
    if target is None or getattr(target, "is_deleted", False):
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
    username = username.strip()
    target = db.query(User).filter(User.username == username).first()
    if target is None or getattr(target, "is_deleted", False):
        query = urlencode({"invite_error": f"존재하지 않는 사용자입니다: {username}"})
        return RedirectResponse(url=f"/groups/{group.id}?{query}#overview", status_code=303)
    exists = db.query(GroupMember).filter(GroupMember.group_id == group.id, GroupMember.user_id == target.id).first()
    if exists is None:
        create_message(db, target.id, "그룹 초대", f"{group.name} 그룹에 초대되었습니다. 수락하면 그룹 회원이 됩니다.", "group_invite", related_group_id=group.id, action_status="pending")
        db.commit()
        query = urlencode({"invite_message": f"{target.username} 사용자에게 그룹 초대 메세지를 보냈습니다."})
        return RedirectResponse(url=f"/groups/{group.id}?{query}#overview", status_code=303)
    query = urlencode({"invite_message": f"{target.username} 사용자는 이미 그룹 회원입니다."})
    return RedirectResponse(url=f"/groups/{group.id}?{query}#overview", status_code=303)


@app.post("/groups/{group_id}/members/bulk-add")
def bulk_add_existing_group_members(group_id: int, request: Request, csv_text: str = Form(""), csv_file: UploadFile | None = File(None), db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    require_school_group_member_admin(user, group, db)
    added_members = 0
    skipped = 0
    for row in parse_user_bulk_csv(read_csv_text(csv_text, csv_file)):
        target = db.query(User).filter(User.username == row["username"]).first()
        if target is None or getattr(target, "is_deleted", False):
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
def bulk_create_group_members(group_id: int, request: Request, csv_text: str = Form(""), csv_file: UploadFile | None = File(None), confirm_text: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    require_confirm_text(confirm_text, "bulk_create")
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    require_school_group_member_admin(user, group, db)
    created_users = 0
    added_members = 0
    skipped = 0
    for row in parse_user_bulk_csv(read_csv_text(csv_text, csv_file)):
        username_error = validate_username_value(row["username"])
        password_error = validate_password_value(row["password"])
        if username_error or password_error:
            skipped += 1
            continue
        target = db.query(User).filter(User.username == row["username"]).first()
        if target is None:
            target = User(username=row["username"], password_hash=hash_password(row["password"]), full_name=row["full_name"], student_id=row["student_id"], must_change_password=True)
            db.add(target)
            db.flush()
            created_users += 1
        elif getattr(target, "is_deleted", False):
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


@app.get("/groups/{group_id}/school-group/attachment")
def download_school_group_attachment(group_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_site_admin(user, group)
    stored_value = group.school_group_request_file_path or ""
    # Compatibility with rows created before private attachment routing was added.
    if stored_value.startswith("/uploads/school_group_requests/"):
        stored_value = stored_value.lstrip("/")
    stored_path = Path(stored_value)
    allowed_root = Path("uploads/school_group_requests").resolve()
    try:
        resolved_path = stored_path.resolve(strict=True)
        resolved_path.relative_to(allowed_root)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="Attachment not found")
    return FileResponse(
        resolved_path,
        filename=group.school_group_request_file_name or resolved_path.name,
        media_type="application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


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
        stored_path.write_bytes(read_limited_upload(attachment, MAX_PRIVATE_ATTACHMENT_BYTES, "첨부"))
        group.school_group_request_file_path = str(stored_path)
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
def rejudge_group_contest(group_contest_id: int, request: Request, confirm_text: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    require_confirm_text(confirm_text, "rejudge")
    group_contest = db.query(GroupContest).filter(GroupContest.id == group_contest_id).first()
    if group_contest is None or group_contest.contest_id is None:
        raise HTTPException(status_code=404, detail="Group contest not found")
    group = db.query(Group).filter(Group.id == group_contest.group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    submissions = db.query(Submission).filter(Submission.contest_id == group_contest.contest_id).order_by(Submission.id.asc()).all()
    for submission in submissions:
        rejudge_submission(submission, db=db)
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
def create_group_contest_shell(group_id: int, request: Request, title: str = Form(...), description: str = Form(""), start_time: str = Form(""), end_time: str = Form(""), problem_order: str = Form(""), contest_id: str = Form(""), is_exam_mode: str | None = Form(None), hide_ranking: str | None = Form(None), is_public: str | None = Form(None), score_enabled: str | None = Form(None), scoreboard_freeze_enabled: str | None = Form(None), scoreboard_freeze_minutes: int = Form(0), result_display_mode: str = Form("full"), db: Session = Depends(get_db)):
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
            linked_contest.hide_ranking = hide_ranking == "on"
            linked_contest.is_public = is_public == "on"
            linked_contest.score_enabled = score_enabled == "on"
            linked_contest.scoreboard_freeze_enabled = scoreboard_freeze_enabled == "on"
            linked_contest.scoreboard_freeze_minutes = max(0, int(scoreboard_freeze_minutes or 0))
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
        group_contest.contest.scoreboard_freeze_enabled = scoreboard_freeze_enabled == "on"
        group_contest.contest.scoreboard_freeze_minutes = max(0, int(scoreboard_freeze_minutes or 0))
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
def delete_group_contest_route(group_id: int, group_contest_id: int, request: Request, confirm_text: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    require_confirm_text(confirm_text, "delete_contest")
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
def create_contest(request: Request, title: str = Form(...), description: str = Form(...), start_time: str = Form(...), end_time: str = Form(...), problem_ids: list[int] = Form(default=[]), problem_order: str = Form(""), is_exam_mode: str | None = Form(None), hide_ranking: str | None = Form(None), score_enabled: str | None = Form(None), scoreboard_freeze_enabled: str | None = Form(None), scoreboard_freeze_minutes: int = Form(0), result_display_mode: str = Form("full"), db: Session = Depends(get_db)):
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
        contest.hide_ranking = hide_ranking == "on"
        contest.scoreboard_freeze_enabled = scoreboard_freeze_enabled == "on"
        contest.scoreboard_freeze_minutes = max(0, int(scoreboard_freeze_minutes or 0))
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
def update_contest_settings(contest_id: int, request: Request, is_exam_mode: str | None = Form(None), hide_ranking: str | None = Form(None), is_public: str | None = Form(None), score_enabled: str | None = Form(None), scoreboard_freeze_enabled: str | None = Form(None), scoreboard_freeze_minutes: int = Form(0), start_time: str = Form(""), end_time: str = Form(""), db: Session = Depends(get_db)):
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
        contest.hide_ranking = hide_ranking == "on"
    else:
        # 그룹 대회의 시험 모드는 생성 시에만 결정한다. 이후에는 공개 여부/순위표 숨김만 조정한다.
        contest.hide_ranking = hide_ranking == "on"
        contest.is_public = is_public == "on"
    contest.score_enabled = score_enabled == "on"
    contest.scoreboard_freeze_enabled = scoreboard_freeze_enabled == "on"
    contest.scoreboard_freeze_minutes = max(0, int(scoreboard_freeze_minutes or 0))
    contest.result_display_mode = "full"
    audit_log(db, request, user, "contest_settings", "contest", contest.id, f"대회 설정 변경: {contest.title}")
    db.commit()
    return RedirectResponse(url=f"/contests/{contest.id}", status_code=303)


@app.post("/admin/contests/{contest_id}/end")
def end_contest(contest_id: int, request: Request, confirm_text: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    require_confirm_text(confirm_text, "end_contest")
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
def add_new_contest_problem(contest_id: int, request: Request, problem_id: str = Form(""), title: str = Form(...), description: str = Form(...), input_description: str = Form(...), output_description: str = Form(...), time_limit: int = Form(2), memory_limit: int = Form(256), python_time_limit: str = Form(""), c_time_limit: str = Form(""), cpp_time_limit: str = Form(""), java_time_limit: str = Form(""), score: int = Form(100), sample_inputs: str = Form(""), sample_outputs: str = Form(""), test_inputs: str = Form(...), test_outputs: str = Form(...), is_judge_ready: str | None = Form("on"), publish_notice_agree: str | None = Form(None), db: Session = Depends(get_db)):
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
        python_time_limit=optional_time_limit(python_time_limit),
        c_time_limit=optional_time_limit(c_time_limit),
        cpp_time_limit=optional_time_limit(cpp_time_limit),
        java_time_limit=optional_time_limit(java_time_limit),
        difficulty="미지정",
        tier=0,
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
    if getattr(user, "is_deleted", False):
        return "탈퇴한 사용자"
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
    members = db.query(GroupMember).filter(GroupMember.group_id == group.id).join(User, User.id == GroupMember.user_id).filter(User.is_deleted == False, User.is_admin == False).order_by(User.student_id.asc(), User.username.asc()).all()
    rows = [["username", "full_name", "student_id", "role", "joined_at"]]
    for member in members:
        rows.append([csv_display_username(member.user) if member.user else member.user_id, "" if (member.user and member.user.is_deleted) else (member.user.full_name if member.user else ""), "" if (member.user and member.user.is_deleted) else (member.user.student_id if member.user else ""), member.role, member.joined_at])
    return csv_response(f"group_{group.id}_members.csv", rows)


@app.post("/groups/{group_id}/members/bulk-delete")
def bulk_delete_group_members(group_id: int, request: Request, member_ids: list[int] = Form([]), confirm_text: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    require_confirm_text(confirm_text, "delete_user")
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
def bulk_reset_group_member_passwords(group_id: int, request: Request, member_ids: list[int] = Form([]), new_password: str = Form("changeme1234"), confirm_text: str = Form(""), db: Session = Depends(get_db)):
    user = require_login(request, db)
    require_confirm_text(confirm_text, "bulk_reset")
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_group_owner_or_admin(user, group)
    require_school_group_member_admin(user, group, db)
    password_error = validate_password_value(new_password)
    if password_error:
        raise HTTPException(status_code=400, detail=password_error)
    member_user_ids = {member.user_id for member in db.query(GroupMember).filter(GroupMember.group_id == group.id).all()}
    for member_user_id in member_ids:
        if member_user_id not in member_user_ids:
            continue
        target = db.query(User).filter(User.id == member_user_id).first()
        if target and not getattr(target, "is_deleted", False):
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
    members = db.query(GroupMember).filter(GroupMember.group_id == group.id).join(User, User.id == GroupMember.user_id).filter(User.is_deleted == False, User.is_admin == False).all()
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
    members = db.query(GroupMember).filter(GroupMember.group_id == group.id).join(User, User.id == GroupMember.user_id).filter(User.is_deleted == False, User.is_admin == False).all()
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
    members = db.query(GroupMember).filter(GroupMember.group_id == group.id).join(User, User.id == GroupMember.user_id).filter(User.is_deleted == False, User.is_admin == False).all()
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
        rows.append([s.id, csv_display_username(s.user) if s.user else "", s.problem_id, s.contest_id or "", s.language, s.result, s.runtime_ms, s.memory_kb, s.created_at])
    return csv_response("submissions.csv", rows)


@app.get("/admin/contests/{contest_id}/ranking.csv")
def export_contest_ranking_csv(contest_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    contest = db.query(Contest).filter(Contest.id == contest_id).first()
    if contest is None:
        raise HTTPException(status_code=404, detail="Contest not found")
    require_contest_manager(user, contest, db)
    return csv_response(f"contest_{contest.id}_ranking.csv", build_contest_score_rows(db, contest))


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
        rows.append([s.id, csv_display_username(s.user) if s.user else "", s.problem_id, link.label if link else "", s.language, s.result, s.runtime_ms, s.memory_kb, s.created_at])
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
    return csv_response(f"group_contest_{group_contest.id}_ranking.csv", build_contest_score_rows(db, group_contest.contest))


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
    members = db.query(GroupMember).filter(GroupMember.group_id == practice.group_id).join(User, User.id == GroupMember.user_id).filter(User.is_deleted == False, User.is_admin == False).order_by(GroupMember.user_id.asc()).all()
    items = db.query(GroupPracticeProblem).filter(GroupPracticeProblem.practice_id == practice.id).order_by(GroupPracticeProblem.order_index.asc(), GroupPracticeProblem.id.asc()).all()
    board = build_practice_board(db, practice, members, items)
    header = ["username", "solved_count"] + [str(item.problem_id) for item in items]
    rows = [header]
    for row in board:
        rows.append([csv_display_username(row["user"]) if row["user"] else "", row["solved_count"]] + [f"AC/{cell['attempts']}" if cell["solved"] else (f"TRY/{cell['attempts']}" if cell["attempts"] else "") for cell in row["cells"]])
    return csv_response(f"group_practice_{practice.id}_board.csv", rows)




@app.get("/admin/profile-assets", response_class=HTMLResponse)
def admin_profile_assets_page(request: Request, message: str = "", error: str = "", db: Session = Depends(get_db)):
    user = require_admin(request, db)
    assets = db.query(ProfileAsset).order_by(ProfileAsset.asset_type.asc(), ProfileAsset.id.desc()).all()
    return templates.TemplateResponse("admin_profile_assets.html", {"request": request, "user": user, "assets": assets, "message": message, "error": error})


@app.post("/admin/profile-assets")
def admin_create_profile_asset(
    request: Request,
    asset_type: str = Form("badge"),
    title: str = Form(...),
    description: str = Form(""),
    image_file: UploadFile | None = File(None),
    icon_text: str = Form(""),
    condition_type: str = Form("single"),
    condition_problem_ids: str = Form(""),
    condition_value: str = Form(""),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    asset_type = asset_type if asset_type in {"badge", "background"} else "badge"
    condition_type = condition_type if condition_type in {"single", "all", "any", "default", "period_solve", "streak", "tag_rating"} else "single"
    asset = ProfileAsset(
        asset_type=asset_type,
        title=title.strip(),
        description=description.strip(),
        image_url=save_profile_asset_upload(image_file),
        icon_text=icon_text.strip(),
        condition_type=condition_type,
        condition_problem_ids=condition_problem_ids.strip(),
        condition_value=condition_value.strip(),
        is_default=bool(is_default) or condition_type == "default",
        is_active=bool(is_active),
    )
    db.add(asset)
    db.commit()
    audit_log(db, request, user, "profile_asset_create", "profile_asset", asset.id, f"프로필 {asset_type} 생성: {asset.title}")
    return RedirectResponse("/admin/profile-assets?message=프로필 보상을 추가했습니다.", status_code=303)


@app.post("/admin/profile-assets/{asset_id}/edit")
def admin_edit_profile_asset(
    asset_id: int,
    request: Request,
    asset_type: str = Form("badge"),
    title: str = Form(...),
    description: str = Form(""),
    image_file: UploadFile | None = File(None),
    icon_text: str = Form(""),
    condition_type: str = Form("single"),
    condition_problem_ids: str = Form(""),
    condition_value: str = Form(""),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    asset = db.query(ProfileAsset).filter(ProfileAsset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="프로필 보상을 찾을 수 없습니다.")
    asset.asset_type = asset_type if asset_type in {"badge", "background"} else "badge"
    asset.title = title.strip()
    asset.description = description.strip()
    new_image_path = save_profile_asset_upload(image_file)
    if new_image_path:
        asset.image_url = new_image_path
    asset.icon_text = icon_text.strip()
    asset.condition_type = condition_type if condition_type in {"single", "all", "any", "default", "period_solve", "streak", "tag_rating"} else "single"
    asset.condition_problem_ids = condition_problem_ids.strip()
    asset.condition_value = condition_value.strip()
    asset.is_default = bool(is_default) or asset.condition_type == "default"
    asset.is_active = bool(is_active)
    db.commit()
    audit_log(db, request, user, "profile_asset_update", "profile_asset", asset.id, f"프로필 보상 수정: {asset.title}")
    return RedirectResponse("/admin/profile-assets?message=프로필 보상을 수정했습니다.", status_code=303)


@app.post("/admin/profile-assets/{asset_id}/delete")
def admin_delete_profile_asset(asset_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    asset = db.query(ProfileAsset).filter(ProfileAsset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="프로필 보상을 찾을 수 없습니다.")
    title = asset.title
    db.delete(asset)
    db.commit()
    audit_log(db, request, user, "profile_asset_delete", "profile_asset", asset_id, f"프로필 보상 삭제: {title}")
    return RedirectResponse("/admin/profile-assets?message=프로필 보상을 삭제했습니다.", status_code=303)

@app.get("/users/{username}", response_class=HTMLResponse)
def user_profile(username: str, request: Request, db: Session = Depends(get_db)):
    viewer = get_current_user(request, db)
    target = db.query(User).filter(User.username == username).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    return templates.TemplateResponse("user_profile.html", build_user_profile_context(db, request, viewer, target))


@app.post("/users/{username}/profile/edit")
def edit_user_profile(
    username: str,
    request: Request,
    profile_message: str = Form(""),
    selected_profile_badge_id: int = Form(0),
    selected_profile_background_id: int = Form(0),
    profile_image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    user = require_login(request, db)
    target = db.query(User).filter(User.username == username).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id != target.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="프로필을 수정할 권한이 없습니다.")
    target.profile_message = profile_message.strip()[:256]
    if profile_image and profile_image.filename:
        target.profile_image_url = save_user_profile_upload(profile_image, "avatar")
    solved_ids = {row[0] for row in db.query(Submission.problem_id).filter(Submission.user_id == target.id, Submission.result == "AC").all()}
    solved_dates = {}
    for submission in db.query(Submission).filter(Submission.user_id == target.id, Submission.result == "AC").all():
        if submission.created_at:
            key = streak_day_key(submission.created_at)
            solved_dates[key] = solved_dates.get(key, 0) + 1
    today_key_dt = (datetime.now(APP_TIMEZONE) - timedelta(hours=6)).date()
    longest_for_asset = 0
    current_run_for_asset = 0
    for i in range(371):
        day = today_key_dt - timedelta(days=370 - i)
        if solved_dates.get(day.isoformat(), 0) > 0:
            current_run_for_asset += 1
            longest_for_asset = max(longest_for_asset, current_run_for_asset)
        else:
            current_run_for_asset = 0
    solved_problems_for_asset = db.query(Problem).filter(Problem.id.in_(solved_ids)).all() if solved_ids else []
    tag_rating_for_asset = {}
    for tag in active_algorithm_tags(db):
        values = [max(0, min(30, int(problem.tier or 0))) for problem in solved_problems_for_asset if tag.key in problem_tag_keys(problem)]
        if values:
            tag_rating_for_asset[tag.key] = sum(sorted(values, reverse=True)[:100]) + rating_solved_count_bonus(len(values))
    if selected_profile_badge_id:
        badge = db.query(ProfileAsset).filter(ProfileAsset.id == selected_profile_badge_id, ProfileAsset.asset_type == "badge", ProfileAsset.is_active == True).first()  # noqa: E712
        if badge and profile_asset_earned(badge, solved_ids, solved_dates=solved_dates, longest_streak=longest_for_asset, tag_ratings=tag_rating_for_asset):
            target.selected_profile_badge_id = badge.id
        else:
            target.selected_profile_badge_id = 0
    else:
        target.selected_profile_badge_id = 0
    if selected_profile_background_id:
        background = db.query(ProfileAsset).filter(ProfileAsset.id == selected_profile_background_id, ProfileAsset.asset_type == "background", ProfileAsset.is_active == True).first()  # noqa: E712
        if background and profile_asset_earned(background, solved_ids, solved_dates=solved_dates, longest_streak=longest_for_asset, tag_ratings=tag_rating_for_asset):
            target.selected_profile_background_id = background.id
            target.profile_background_url = ""
        else:
            target.selected_profile_background_id = 0
    else:
        target.selected_profile_background_id = 0
    db.commit()
    return RedirectResponse(f"/users/{target.username}?message=프로필을 수정했습니다.", status_code=303)



@app.post("/users/{username}/delete")
def delete_my_account(username: str, request: Request, password: str = Form(...), confirm_text: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    target = db.query(User).filter(User.username == username).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id != target.id:
        raise HTTPException(status_code=403, detail="본인 계정만 탈퇴할 수 있습니다.")
    if target.is_admin and db.query(User).filter(User.is_admin == True, User.id != target.id).count() == 0:  # noqa: E712
        raise HTTPException(status_code=400, detail="마지막 관리자 계정은 탈퇴할 수 없습니다.")
    if confirm_text.strip() != "탈퇴하겠습니다.":
        return templates.TemplateResponse("user_profile.html", build_user_profile_context(db, request, user, target, delete_error="확인 문구를 정확히 입력해야 합니다. 문구: 탈퇴하겠습니다."))
    if not verify_password(password, target.password_hash):
        raise HTTPException(status_code=400, detail="비밀번호가 올바르지 않습니다.")
    audit_log(db, request, user, "delete_account", "user", target.id, f"사용자 탈퇴: {target.username}")
    delete_user_account(db, target)
    db.commit()
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


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
    if getattr(target, "is_deleted", False):
        raise HTTPException(status_code=400, detail="탈퇴한 계정은 수정할 수 없습니다.")
    target.full_name = full_name.strip()[:100]
    target.student_id = student_id.strip()[:50]
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/reset-password")
def admin_reset_user_password(user_id: int, request: Request, new_password: str = Form(...), confirm_text: str = Form(""), db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    require_confirm_text(confirm_text, "reset_password")
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if getattr(target, "is_deleted", False):
        raise HTTPException(status_code=400, detail="탈퇴한 계정은 수정할 수 없습니다.")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="본인 비밀번호는 이 화면에서 초기화하지 않습니다.")
    password_error = validate_password_value(new_password)
    if password_error:
        raise HTTPException(status_code=400, detail=password_error)
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
def admin_bulk_create_users(request: Request, csv_text: str = Form(""), csv_file: UploadFile | None = File(None), confirm_text: str = Form(""), db: Session = Depends(get_db)):
    user = require_admin(request, db)
    require_confirm_text(confirm_text, "bulk_create")
    created = 0
    skipped = 0
    for row in parse_user_bulk_csv(read_csv_text(csv_text, csv_file)):
        if validate_username_value(row["username"]) or validate_password_value(row["password"]):
            skipped += 1
            continue
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
        if target is None or getattr(target, "is_deleted", False):
            skipped += 1
            continue
        target.full_name = row["full_name"]
        target.student_id = row["student_id"]
        updated += 1
    db.commit()
    users = db.query(User).order_by(User.id.asc()).all()
    return templates.TemplateResponse("admin_users.html", {"request": request, "user": user, "users": users, "now": now(), "message": f"이름/학번 일괄 등록 완료: 수정 {updated}명, 건너뜀 {skipped}명"})


@app.post("/admin/users/bulk-reset-password")
def admin_bulk_reset_user_passwords(request: Request, user_ids: list[int] = Form([]), new_password: str = Form("changeme1234"), confirm_text: str = Form(""), db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    require_confirm_text(confirm_text, "bulk_reset")
    password_error = validate_password_value(new_password)
    if password_error:
        raise HTTPException(status_code=400, detail=password_error)
    for user_id in user_ids:
        if user_id == admin.id:
            continue
        target = db.query(User).filter(User.id == user_id).first()
        if target and not getattr(target, "is_deleted", False):
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
    if getattr(target, "is_deleted", False):
        raise HTTPException(status_code=400, detail="탈퇴한 계정은 수정할 수 없습니다.")
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
    if getattr(target, "is_deleted", False):
        raise HTTPException(status_code=400, detail="탈퇴한 계정은 수정할 수 없습니다.")
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
    if getattr(target, "is_deleted", False):
        raise HTTPException(status_code=400, detail="탈퇴한 계정은 수정할 수 없습니다.")
    target.submit_banned_until = None
    target.ban_reason = None
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)

@app.post("/admin/users/{user_id}/delete")
def admin_delete_user(user_id: int, request: Request, confirm_text: str = Form(...), db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if getattr(target, "is_deleted", False):
        raise HTTPException(status_code=400, detail="이미 탈퇴 처리된 계정입니다.")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="본인 계정은 회원 관리 화면에서 삭제할 수 없습니다.")
    if target.is_admin and db.query(User).filter(User.is_admin == True, User.id != target.id).count() == 0:  # noqa: E712
        raise HTTPException(status_code=400, detail="마지막 관리자 계정은 삭제할 수 없습니다.")
    if confirm_text.strip() != target.username:
        raise HTTPException(status_code=400, detail="삭제 확인을 위해 대상 아이디를 정확히 입력해야 합니다.")
    audit_log(db, request, admin, "admin_delete_user", "user", target.id, f"관리자 회원 삭제: {target.username}")
    delete_user_account(db, target)
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)

