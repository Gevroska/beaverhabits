"""Exercise production session middleware in fresh application processes."""

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest


@pytest.mark.parametrize("secure", [True, False])
def test_production_session_cookie_policy(secure, tmp_path):
    env = os.environ.copy()
    env.update(
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/habits.db",
        HABITS_STORAGE="DATABASE",
        NICEGUI_STORAGE_PATH=str(tmp_path / "nicegui"),
        NICEGUI_STORAGE_SECRET="disposable-session-cookie-test-secret",
        JWT_SECRET="disposable-session-cookie-test-jwt-secret",
        ENABLE_DAILY_BACKUP="false",
        ENABLE_PLAN="false",
        TRUSTED_LOCAL_EMAIL="",
        REQUIRE_ADMIN_FOR_REGISTRATION="false",
        MAX_USER_COUNT="1",
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
    )
    # Exercise the secure default, rather than setting it explicitly.
    env.pop("SESSION_COOKIE_SECURE", None)
    if not secure:
        env["SESSION_COOKIE_SECURE"] = "false"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(secure)],
        env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def probe_session_cookie_policy(secure):
    from http.cookies import SimpleCookie

    import httpx
    from fastapi.testclient import TestClient
    from itsdangerous import TimestampSigner
    from nicegui import app as gui_app
    from nicegui.storage import request_contextvar
    from starlette.requests import Request

    from beaverhabits.app.auth import user_logout
    from beaverhabits.configs import settings
    from beaverhabits.main import app

    signer = TimestampSigner(settings.NICEGUI_STORAGE_SECRET)
    policy = "secure-v1" if secure else "http-v1"
    scheme = "https" if secure else "http"

    def signed(payload):
        return signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()

    def decoded(value):
        return json.loads(base64.b64decode(signer.unsign(value)))

    def session_cookie(response):
        cookies = SimpleCookie()
        for header in response.headers.get_list("set-cookie"):
            cookies.load(header)
        cookie = cookies.get("session")
        if cookie is not None:
            assert bool(cookie["secure"]) is secure
            assert cookie["httponly"]
            assert cookie["samesite"].lower() == "lax"
            assert cookie["path"] == "/"
            assert cookie["max-age"] == str(14 * 24 * 60 * 60)
        return cookie

    with TestClient(
        app, base_url=f"{scheme}://bh.test", follow_redirects=False
    ) as client:
        response = client.get("/login")
        assert response.status_code == 200
        cookie = session_cookie(response)
        assert cookie is not None
        initial = decoded(cookie.value)
        assert initial["bh_cookie_policy"] == policy
        assert session_cookie(client.get("/login")) is None

        # Use the cookie jar, not a manually supplied Cookie header, to check
        # browser-like transport behavior after receiving a Secure cookie.
        http_request = httpx.Request("GET", "http://bh.test/login")
        client.cookies.set_cookie_header(http_request)
        assert ("session=" in http_request.headers.get("cookie", "")) is not secure

        credentials = {"email": "cookie-test@example.com", "password": "CookieTest123!"}
        assert client.post("/auth/register", json=credentials).status_code == 201
        login = client.post(
            "/auth/login",
            data={
                "username": credentials["email"],
                "password": credentials["password"],
            },
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        habit = client.post(
            "/api/v1/habits",
            json={"name": "Existing session habit"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert habit.status_code == 200

        # None models an old deployment. The two explicit policies exercise
        # both directions of the opt-out/rollback configuration lifecycle.
        for previous_policy in [None, "http-v1", "secure-v1"]:
            session_id = str(uuid4())
            payload = {"id": session_id, "preserved": "legacy-browser-state"}
            if previous_policy is not None:
                payload["bh_cookie_policy"] = previous_policy

            async def seed_user_storage():
                await gui_app.storage._create_user_storage(session_id)
                gui_app.storage._users[session_id].update(
                    {"auth_token": token, "sentinel": "existing-user-data"}
                )

            client.portal.call(seed_user_storage)
            client.cookies.clear()
            client.cookies.set("session", signed(payload), domain="bh.test", path="/")
            response = client.get("/")
            assert response.status_code == 200  # Real protected GUI route.
            replacement = session_cookie(response)
            assert (replacement is not None) is (previous_policy != policy)
            migrated = decoded(client.cookies.get("session"))
            assert migrated == {**payload, "bh_cookie_policy": policy}
            assert gui_app.storage._users[session_id]["auth_token"] == token
            assert (
                gui_app.storage._users[session_id]["sentinel"] == "existing-user-data"
            )
            assert session_cookie(client.get("/")) is None

        # Exercise the existing logout helper in the same session context,
        # then verify that the protected GUI no longer accepts that session.
        async def logout():
            context_token = request_contextvar.set(
                Request({"type": "http", "session": {"id": session_id}})
            )
            try:
                assert user_logout()
            finally:
                request_contextvar.reset(context_token)

        client.portal.call(logout)
        response = client.get("/")
        assert response.status_code == 307
        assert response.headers["location"] == "/login"

        class ExpiredSigner(TimestampSigner):
            def get_timestamp(self):
                return 0

        expired_id = str(uuid4())
        expired_cookie = (
            ExpiredSigner(settings.NICEGUI_STORAGE_SECRET)
            .sign(base64.b64encode(json.dumps({"id": expired_id}).encode()))
            .decode()
        )
        for invalid_cookie in [
            "tampered.invalid",
            signed({"id": str(uuid4())}) + "x",
            expired_cookie,
        ]:
            client.cookies.clear()
            client.cookies.set("session", invalid_cookie, domain="bh.test", path="/")
            response = client.get("/login")
            assert response.status_code == 200
            assert session_cookie(response) is not None
            replacement = decoded(client.cookies.get("session"))
            assert replacement["bh_cookie_policy"] == policy
            assert replacement["id"] != expired_id

    print(
        f"secure={secure}: new, legacy, policy transition, auth, logout and invalid-cookie checks passed"
    )


if __name__ == "__main__":
    probe_session_cookie_policy(sys.argv[1] == "True")
