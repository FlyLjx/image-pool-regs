"""Application time helpers using China Standard Time (UTC+8)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now() -> datetime:
    return datetime.now(CHINA_TZ)


def iso_now() -> str:
    return now().isoformat()


def today() -> str:
    return now().date().isoformat()
