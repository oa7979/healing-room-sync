"""상태 파일(JSON) 읽기/쓰기. 반쯤 쓰인 파일이 남지 않게 원자적으로 교체한다."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

KST = timezone.utc  # 저장은 UTC ISO 문자열


def load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"version": 1}
    except json.JSONDecodeError:
        return {"version": 1, "corrupt": True}


def save(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    state["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
