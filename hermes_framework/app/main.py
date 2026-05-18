"""Uvicorn entry point: `python -m app.main` or `uvicorn app.main:app`."""

from __future__ import annotations

import uvicorn

from app.api.server import app  # noqa: F401 — re-export for uvicorn
from app.config import settings


def main() -> None:
    uvicorn.run(
        "app.api.server:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
