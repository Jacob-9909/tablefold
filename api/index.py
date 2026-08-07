"""Vercel 서버리스 진입점.

Vercel 은 이 모듈에서 ``app`` 이라는 ASGI 객체를 찾는다. 로직은 아무것도 두지
않는다 — 여기서 하는 일은 번들 안에서 ``demo`` 와 ``src/tablefold`` 를 import
가능하게 만드는 것뿐이다. 로컬에서는 ``uv run`` 이 프로젝트를 설치해 주지만
서버리스 번들에는 설치 단계가 없어서 경로를 직접 얹어야 한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for path in (ROOT, ROOT / "src"):
    entry = str(path)
    if entry not in sys.path:
        sys.path.insert(0, entry)

from demo.app import app  # noqa: E402

__all__ = ["app"]
