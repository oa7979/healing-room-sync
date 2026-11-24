#!/usr/bin/env python3
"""알림 메일 경로 자체 점검용: 로컬에 가짜 SMTP 서버를 띄우고 실제로 한 통 보낸다.

  python3 tools/smtp_sink_test.py

Gmail 자격증명 없이 '메일을 만들어 SMTP로 넘기는 코드 경로'가 동작하는지 확인한다.
(실제 Gmail 발송은 SMTP_USER/SMTP_PASS 를 넣으면 같은 코드가 그대로 쓴다.)
"""
from __future__ import annotations

import os
import socketserver
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hrsync import notify  # noqa: E402

received: list[str] = []


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 sink ESMTP\r\n")
        data_mode = False
        buf: list[str] = []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("utf-8", "ignore").rstrip("\r\n")
            if data_mode:
                if text == ".":
                    data_mode = False
                    received.append("\n".join(buf))
                    buf = []
                    self.wfile.write(b"250 OK queued\r\n")
                else:
                    buf.append(text)
                continue
            cmd = text.upper()
            if cmd.startswith("EHLO") or cmd.startswith("HELO"):
                self.wfile.write(b"250-sink\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
            elif cmd.startswith("AUTH"):
                self.wfile.write(b"235 Authentication successful\r\n")
            elif cmd.startswith("MAIL FROM") or cmd.startswith("RCPT TO"):
                self.wfile.write(b"250 OK\r\n")
            elif cmd == "DATA":
                data_mode = True
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
            elif cmd == "QUIT":
                self.wfile.write(b"221 Bye\r\n")
                return
            else:
                self.wfile.write(b"250 OK\r\n")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


def main() -> int:
    srv = Server(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    os.environ.update({
        "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(port), "SMTP_SECURE": "none",
        "SMTP_USER": "oa7979@gmail.com", "SMTP_PASS": "dummy-app-password",
        "ALERT_TO": "oa7979@gmail.com",
    })
    ok, detail = notify.send("테스트: 로그인 실패 경로 점검",
                             "이것은 동기화 프로그램의 알림 경로 점검용 메일입니다.")
    time.sleep(0.3)
    srv.shutdown()
    print("send() ->", ok, detail)
    if not received:
        print("FAIL: SMTP 서버가 메일을 받지 못했습니다")
        return 1
    body = received[0]
    import email
    from email.header import decode_header
    msg = email.message_from_string(body)
    subject = "".join(p.decode(enc or "utf-8") if isinstance(p, bytes) else p
                      for p, enc in decode_header(msg["Subject"]))
    checks = {
        "제목 태그": subject.startswith("[힐링룸동기화]"),
        "받는 사람": "oa7979@gmail.com" in str(msg["To"]),
        "본문": "알림 경로 점검용" in body,
    }
    print("  디코드된 제목:", subject)
    for k, v in checks.items():
        print(f"  {k}: {'OK' if v else 'FAIL'}")
    print("---- 수신 원문(머리) ----")
    print("\n".join(body.splitlines()[:12]))
    return 0 if ok and all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
