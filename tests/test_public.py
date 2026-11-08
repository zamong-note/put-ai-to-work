"""밖에서 접속 (Cloudflare Tunnel + Access) — 가짜 키로 Access JWT 를 만들어 검증 경로를 시험 (네트워크 없음)."""
from __future__ import annotations

import time
from datetime import datetime

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from jangjung import timeutil
from jangjung.app import App
from jangjung.config import load_settings
from jangjung.web import AccessVerifier, Dashboard

HOST = "jangjung.example.com"
TEAM, AUD = "myteam", "a" * 64
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(email="me@example.com", aud=AUD, iss=f"https://{TEAM}.cloudflareaccess.com", exp=3600, key=KEY):
    now = int(time.time())
    return jwt.encode({"aud": [aud], "iss": iss, "email": email, "iat": now, "exp": now + exp, "sub": "u1"},
                      key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def make(tmp_path):
    timeutil.set_clock(datetime(2026, 11, 9, 10, 0, tzinfo=timeutil.KST))
    s = load_settings(tmp_path)
    s.broker = "sim"
    app = App(s, console=False)

    def _make(access=True, pin=None, allowed=None):
        verifier = None
        if access:
            verifier = AccessVerifier(f"https://{TEAM}.cloudflareaccess.com", AUD, allowed)
            verifier._key = lambda t: KEY.public_key()                  # 공개키 조회(인터넷) 대신
        return Dashboard(app, 8765, public_host=HOST, access=verifier, pin=pin)
    yield _make
    timeutil.set_clock(None)


def test_requires_access_or_pin(make):
    with pytest.raises(ValueError):
        make(access=False, pin=None)
    make(access=False, pin="246810")                                    # Access 없이 PIN 만으로는 가능


def test_access_jwt_guards_remote_requests(make):
    dash = make(allowed=["me@example.com"])
    with TestClient(dash.asgi(), base_url=f"https://{HOST}", follow_redirects=False) as c:
        assert c.get("/api/status").status_code == 403                  # 토큰 없음
        ok = {"Cf-Access-Jwt-Assertion": token()}
        assert c.get("/api/status", headers=ok).status_code == 200
        assert dash.token in c.get("/", headers=ok).text
        c.cookies.set("CF_Authorization", token())                      # 브라우저는 쿠키로도 옴
        assert c.get("/api/status").status_code == 200
        c.cookies.clear()
        for bad in (token(aud="b" * 64), token(iss="https://evil.cloudflareaccess.com"), token(exp=-60),
                    token(key=OTHER), token(email="stranger@example.com"), "not-a-jwt"):
            r = c.get("/api/status", headers={"Cf-Access-Jwt-Assertion": bad})
            assert r.status_code == 403, bad
        # 쓰기 요청: Access + 화면 토큰 + 같은 출처
        w = {**ok, "X-JJ-Token": dash.token, "Origin": f"https://{HOST}"}
        assert c.post("/api/stop", headers=w).status_code == 200
        assert c.post("/api/stop", headers={**w, "Origin": "https://evil.example"}).status_code == 403
        assert c.post("/api/stop", headers={**ok, "Origin": f"https://{HOST}"}).status_code == 403   # 화면 토큰 없음
        assert c.get("/login", headers={}).status_code == 403            # 로그인 화면도 Access 뒤에


def test_local_stays_open_but_tunnel_headers_do_not_bypass(make):
    dash = make()
    with TestClient(dash.asgi(), base_url="http://127.0.0.1:8765") as c:
        assert c.get("/api/status").status_code == 200                  # 이 PC에서 직접
        # Host 를 127.0.0.1 로 바꿔 보내도 Cloudflare 를 거친 요청(Cf-Ray)이면 인증 필요
        assert c.get("/api/status", headers={"Cf-Ray": "8f1-ICN"}).status_code == 403
        assert c.get("/api/status", headers={"Cf-Ray": "8f1-ICN", "Cf-Access-Jwt-Assertion": token()}).status_code == 200
        assert c.get("/api/status", headers={"Host": "other.example.com"}).status_code == 403


def test_pin_fallback_without_access(make):
    dash = make(access=False, pin="246810")
    with TestClient(dash.asgi(), base_url=f"https://{HOST}", follow_redirects=False) as c:
        assert c.get("/").status_code == 303 and c.get("/").headers["location"] == "/login"
        assert c.get("/api/status").status_code == 401
        r = c.post("/login", data={"pin": "246810"})
        assert r.status_code == 303 and "secure" in r.headers["set-cookie"].lower()   # HTTPS 전용 쿠키
        assert c.get("/api/status").status_code == 200
    with TestClient(dash.asgi(), base_url="http://127.0.0.1:8765") as c:
        assert c.get("/api/status").status_code == 200                  # 이 PC에서는 PIN 없이
