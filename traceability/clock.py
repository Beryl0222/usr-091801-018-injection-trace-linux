"""可注入时钟。业务代码不直接取系统时间，便于随访窗口的可复现验证。"""

from datetime import datetime, timedelta, timezone


class Clock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now

    def set(self, value: datetime) -> datetime:
        self._now = value
        return self._now


def to_dt(value: str) -> datetime:
    """解析 ISO-8601 字符串（可带 Z 后缀），统一为带时区时间。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
