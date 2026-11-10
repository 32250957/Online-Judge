"""Event form times are KST; submission/log timestamps are UTC."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

APP_TIMEZONE = ZoneInfo("Asia/Seoul")


def local_to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=APP_TIMEZONE)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
