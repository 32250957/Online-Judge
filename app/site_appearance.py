"""Shared schedule rules for HTML, live updates and the admin preview."""
from datetime import datetime, timezone

from app.models import SiteAppearance
from app.time_utils import APP_TIMEZONE, local_to_utc, utc_naive

THEMES = {"default": "기본 디자인", "pepero": "빼빼로데이"}
COLOR_MODES = {"light": "크림 라이트", "dark": "초콜릿 다크"}


def utc_iso(value):
    return utc_naive(value).isoformat(timespec="milliseconds") + "Z" if value else None


def appearance_snapshot(row: SiteAppearance | None = None, at=None):
    at = utc_naive(at or datetime.now(timezone.utc))
    theme = row.theme if row and row.theme in THEMES else "default"
    start = utc_naive(row.starts_at) if row and row.starts_at else None
    end = utc_naive(row.ends_at) if row and row.ends_at else None
    status = "default"
    if theme != "default":
        if end and at >= end:
            status = "ended"
        elif start and at < start:
            status = "scheduled"
        else:
            status = "active"
    return {
        "configured_theme": theme,
        "theme": theme if status == "active" else "default",
        "color_mode": row.color_mode if row and row.color_mode in COLOR_MODES else "light",
        "motion_enabled": bool(row.motion_enabled) if row else True,
        "starts_at": utc_iso(start), "ends_at": utc_iso(end),
        "server_now": utc_iso(at), "status": status,
    }


def local_input(value):
    if not value:
        return ""
    return utc_naive(value).replace(tzinfo=timezone.utc).astimezone(APP_TIMEZONE).strftime("%Y-%m-%dT%H:%M")


def appearance_form(row):
    return {
        "theme": row.theme if row else "default",
        "color_mode": row.color_mode if row else "light",
        "motion_enabled": bool(row.motion_enabled) if row else True,
        "starts_at": local_input(row.starts_at) if row else "",
        "ends_at": local_input(row.ends_at) if row else "",
        "no_end": row is None or row.ends_at is None,
    }


def validate_appearance(values, at=None):
    if values["theme"] not in THEMES:
        raise ValueError("지원하지 않는 디자인입니다.")
    if values["color_mode"] not in COLOR_MODES:
        raise ValueError("라이트 또는 다크 색상을 선택해 주세요.")
    result = {key: values[key] for key in ("theme", "color_mode", "motion_enabled")}
    result.update(starts_at=None, ends_at=None)
    if values["theme"] == "default":
        return result
    for field, label in (("starts_at", "시작"), ("ends_at", "종료")):
        if field == "ends_at" and values["no_end"]:
            continue
        value = values[field].strip()
        if not value:
            if field == "ends_at":
                raise ValueError("종료 시각을 입력하거나 무기한 적용을 선택해 주세요.")
            continue
        try:
            parsed = datetime.fromisoformat(value)
            # datetime-local is a wall time in Asia/Seoul, never a UTC offset.
            if "T" not in value or parsed.tzinfo is not None:
                raise ValueError
            result[field] = local_to_utc(parsed)
        except (ValueError, OverflowError):
            raise ValueError(f"{label} 시각을 올바른 한국 시간으로 입력해 주세요.") from None
    at = utc_naive(at or datetime.now(timezone.utc))
    if result["ends_at"] and result["ends_at"] <= (result["starts_at"] or at):
        raise ValueError("종료 시각은 시작 시각보다 늦어야 합니다.")
    if result["ends_at"] and result["ends_at"] <= at:
        raise ValueError("종료 시각은 현재 시각보다 늦어야 합니다.")
    return result


def save_appearance(db, values):
    # Both PostgreSQL and the isolated SQLite test DB support this atomic upsert.
    # Concurrent first saves cannot insert a second singleton row.
    from sqlalchemy import text
    db.execute(text("""
        INSERT INTO site_appearance (id, theme, color_mode, motion_enabled, starts_at, ends_at)
        VALUES (1, :theme, :color_mode, :motion_enabled, :starts_at, :ends_at)
        ON CONFLICT (id) DO UPDATE SET theme = EXCLUDED.theme,
            color_mode = EXCLUDED.color_mode, motion_enabled = EXCLUDED.motion_enabled,
            starts_at = EXCLUDED.starts_at, ends_at = EXCLUDED.ends_at
    """), values)
