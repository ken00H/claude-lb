from __future__ import annotations

from datetime import datetime

from app.modules.shared.schemas import DashboardModel


class RuntimeVersionResponse(DashboardModel):
    current_version: str
    latest_version: str | None = None
    update_available: bool = False
    checked_at: datetime
    source: str | None = None
    release_url: str = "https://github.com/ken00H/claude-lb/releases/latest"
