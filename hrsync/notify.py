"""예외 알림. 정상 동작일 때는 아무것도 남기지 않는다.

기본 방식: 이 저장소의 GitHub Issue (라벨 '힐링룸동기화')
  - 같은 제목의 열린 이슈가 있으면 댓글만 추가한다(중복 생성 방지).
  - 이후 실행이 깨끗하면 그 열린 이슈를 스스로 닫는다.
  - 아침 점검에서는 `gh issue list -R <repo> -l 힐링룸동기화 --state open` 한 줄로 본다.
보조 방식: SMTP_USER/SMTP_PASS 가 있으면 메일도 함께 보낸다(선택).
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate

TAG = "[힐링룸동기화]"
LABEL = "힐링룸동기화"
API = "https://api.github.com"


# --------------------------------------------------------------------- github
def _api(method: str, path: str, payload=None, token=None, repo=None):
    token = token or os.environ.get("GITHUB_TOKEN")
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return None, "GITHUB_TOKEN/GITHUB_REPOSITORY 없음"
    url = f"{API}/repos/{repo}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "healing-room-sync")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "ignore")
            return (json.loads(body) if body else {}), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}"
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _ensure_label():
    ok, err = _api("GET", f"/labels/{urllib.parse.quote(LABEL)}")
    if ok:
        return
    _api("POST", "/labels", {"name": LABEL, "color": "d93f0b",
                             "description": "용산힐링룸 동기화 예외"})


def open_alert(title: str, body: str, key: str | None = None) -> tuple[bool, str]:
    """이슈를 열거나, 같은 key 의 열린 이슈에 댓글을 단다."""
    key = key or title
    issues, err = _api("GET", f"/issues?state=open&labels={urllib.parse.quote(LABEL)}&per_page=50")
    if issues is None:
        _print_fallback(title, body)
        return False, f"이슈 생성 실패: {err}"
    marker = f"<!-- sync-key: {key} -->"
    for it in issues:
        if marker in (it.get("body") or ""):
            _api("POST", f"/issues/{it['number']}/comments", {"body": body})
            return True, f"이슈 #{it['number']} 에 댓글 추가"
    _ensure_label()
    created, err = _api("POST", "/issues", {
        "title": f"{TAG} {title}",
        "body": f"{marker}\n{body}\n\n---\n자동 생성: healing-room-sync",
        "labels": [LABEL],
    })
    if created is None:
        _print_fallback(title, body)
        return False, f"이슈 생성 실패: {err}"
    return True, f"이슈 #{created['number']} 생성"


def close_alerts(note: str = "정상 동작으로 확인되어 자동 종료합니다.") -> tuple[int, str]:
    """열린 동기화 이슈를 모두 닫는다(정상 회차에만 호출)."""
    issues, err = _api("GET", f"/issues?state=open&labels={LABEL}&per_page=50")
    if issues is None:
        return 0, f"이슈 조회 실패: {err}"
    n = 0
    for it in issues:
        if "pull_request" in it:
            continue
        ok, _ = _api("POST", f"/issues/{it['number']}/comments", {"body": note})
        if ok is not None:
            _api("PATCH", f"/issues/{it['number']}", {"state": "closed"})
            n += 1
    return n, "ok" if not err else err


def _print_fallback(title: str, body: str) -> None:
    print(f"{TAG} {title}\n{body}\n(이슈 API 를 쓸 수 없어 표준출력에만 남겼습니다)")


# --------------------------------------------------------------------- smtp (선택)
def build_message(subject: str, body: str) -> EmailMessage:
    user = os.environ.get("SMTP_USER", "")
    to = os.environ.get("ALERT_TO") or user or "noreply@example.com"
    msg = EmailMessage()
    msg["Subject"] = f"{TAG} {subject}"
    msg["From"] = user or "healing-room-sync@localhost"
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    return msg


def send_mail(subject: str, body: str, *, dry: bool = False) -> tuple[bool, str]:
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    secure = os.environ.get("SMTP_SECURE", "ssl").lower()
    msg = build_message(subject, body)
    if dry:
        return False, "DRY-RUN"
    if not (user and password):
        return False, "SMTP 설정 없음"
    try:
        ctx = ssl.create_default_context()
        if secure == "ssl":
            server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            if secure == "starttls":
                server.starttls(context=ctx)
        with server as s:
            s.login(user, password)
            s.send_message(msg)
        return True, f"{host}:{port} → {msg['To']}"
    except Exception as e:  # noqa: BLE001
        return False, f"발송 실패: {e}"
