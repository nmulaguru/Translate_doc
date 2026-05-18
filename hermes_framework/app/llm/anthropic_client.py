from __future__ import annotations

from typing import Optional

from anthropic import AsyncAnthropic

from app.config import settings

_client: Optional[AsyncAnthropic] = None


def get_async_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client
