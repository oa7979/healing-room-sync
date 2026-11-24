"""아주 작은 HTTP 클라이언트 (표준 라이브러리만 사용).

- 도메인별 쿠키 자동 보관/전송 (http.cookiejar)
- 실패(5xx, 타임아웃) 시 짧게 재시도
- JSON 요청/응답 편의 함수
"""
from __future__ import annotations

import http.cookiejar
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class Response:
    __slots__ = ("status", "body", "headers", "url")

    def __init__(self, status: int, body: str, headers: dict, url: str):
        self.status = status
        self.body = body
        self.headers = headers
        self.url = url

    def json(self):
        return json.loads(self.body)

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<Response {self.status} {self.url} {len(self.body)}b>"


class Client:
    def __init__(self, timeout: int = 45, retries: int = 2):
        self.timeout = timeout
        self.retries = retries
        self.jar = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx),
        )

    # -- cookies ---------------------------------------------------------
    def cookies(self, domain_substr: str) -> dict:
        out = {}
        for c in self.jar:
            if domain_substr in c.domain:
                out[c.name] = c.value
        return out

    def set_cookie_header(self, cookie_header: str) -> None:
        """'a=b; c=d' 형태의 고정 쿠키를 모든 요청에 붙인다(세션 주입용)."""
        self._fixed_cookie = cookie_header

    # -- core ------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        json_body=None,
        data: bytes | None = None,
        headers: dict | None = None,
    ) -> Response:
        hdrs = {
            "User-Agent": UA,
            "Accept-Language": "ko-KR,ko;q=0.9",
        }
        if getattr(self, "_fixed_cookie", None):
            hdrs["Cookie"] = self._fixed_cookie
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)

        last_err = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, method=method.upper())
            for k, v in hdrs.items():
                req.add_header(k, v)
            try:
                with self.opener.open(req, timeout=self.timeout) as r:
                    return Response(
                        r.status,
                        r.read().decode("utf-8", "ignore"),
                        dict(r.headers.items()),
                        url,
                    )
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore")
                resp = Response(e.code, body, dict(e.headers.items()), url)
                # 5xx만 재시도. 4xx는 호출부가 판단.
                if e.code < 500 or attempt == self.retries:
                    return resp
                last_err = resp
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                if attempt == self.retries:
                    raise
            time.sleep(1.5 * (attempt + 1))
        if isinstance(last_err, Response):
            return last_err
        raise RuntimeError(f"request failed: {last_err}")

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> Response:
        return self.request("POST", url, **kw)


def qs(**params) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
