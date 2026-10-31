"""Check the built production container with disposable data, before and after restart."""

import os
import sys
import time

import httpx
import nicegui


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
        assert client.get("/login").status_code == 200
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
        print(f"{sys.argv[1]}: health, version, login, and habit data passed")


if __name__ == "__main__":
    main()
