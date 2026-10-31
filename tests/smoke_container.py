"""Check the built production container with disposable data, before and after restart."""

import base64
import json
import os
import sys
import time
from http.cookies import SimpleCookie
from uuid import uuid4

import httpx
import nicegui
from itsdangerous import TimestampSigner


def check_session_cookies(client):
    """Check new and legacy browser cookies on the actual production image."""
    signer = TimestampSigner(os.environ["SMOKE_STORAGE_SECRET"])

    def cookie_from(response):
        cookies = SimpleCookie()
        for header in response.headers.get_list("set-cookie"):
            cookies.load(header)
        cookie = cookies["session"]
        assert cookie["secure"] and cookie["httponly"]
        assert cookie["samesite"].lower() == "lax"
        return cookie.value

    response = client.get("/login")
    assert response.status_code == 200
    cookie_from(response)
    session = {"id": str(uuid4()), "preserved": "legacy-smoke-session"}
    legacy = signer.sign(base64.b64encode(json.dumps(session).encode())).decode()
    response = client.get("/login", headers={"Cookie": f"session={legacy}"})
    migrated = cookie_from(response)
    decoded = json.loads(base64.b64decode(signer.unsign(migrated)))
    assert decoded == {**session, "bh_cookie_policy": "secure-v1"}
    response = client.get("/login", headers={"Cookie": f"session={migrated}"})
    assert response.status_code == 200
    assert not response.headers.get_list("set-cookie")


def main():
    with httpx.Client(base_url="http://127.0.0.1:18080", timeout=5) as client:
        deadline = time.monotonic() + 90
        while True:
            try:
                response = client.get("/health")
                response.raise_for_status()
                break
            except httpx.HTTPError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
        health = response.json()
        assert health["nicegui_version"] == nicegui.__version__
        assert (
            health["identity"] == f"beaverhabits/fork@{os.environ['EXPECTED_REVISION']}"
        )
        check_session_cookies(client)
        credentials = {
            "email": "smoke@example.com",
            "password": "DisposableSmokePassword123!",
        }
        if sys.argv[1] == "seed":
            response = client.post("/auth/register", json=credentials)
            assert response.status_code == 201, response.text
        response = client.post(
            "/auth/login",
            data={
                "username": credentials["email"],
                "password": credentials["password"],
            },
        )
        response.raise_for_status()
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        if sys.argv[1] == "seed":
            response = client.post(
                "/api/v1/habits", json={"name": "Persistent smoke habit"}
            )
            response.raise_for_status()
        response = client.get("/api/v1/habits")
        response.raise_for_status()
        habits = response.json()
        assert (
            len(habits) == 1 and habits[0]["name"] == "Persistent smoke habit"
        ), habits
        print(
            f"{sys.argv[1]}: health, version, secure/legacy cookies, login, and habit data passed"
        )


if __name__ == "__main__":
    main()
