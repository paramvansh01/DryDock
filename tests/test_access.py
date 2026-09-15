"""The optional access token (DRYDOCK_ACCESS_TOKEN). Offline: the check runs before anything touches Exasol."""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from drydock import orchestrator


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "SETTINGS", dataclasses.replace(orchestrator.SETTINGS, access_token="s3cret-Token"))
    return TestClient(orchestrator.app)          # no `with`: the lifespan (and its database connection) never starts


def test_actions_and_downloads_need_the_token(client):
    for method, path in (("GET", "/runs"), ("POST", "/merges/1/approve"), ("GET", "/export/golden.csv"),
                         ("GET", "/uploads"), ("GET", "/examples/sample_system_a.csv")):
        r = client.request(method, path)
        assert r.status_code == 401 and r.json()["error"] == "AUTH", path


def test_the_right_token_opens_the_door_by_cookie_or_header(client):
    assert client.get("/examples/sample_system_a.csv", headers={"x-drydock-token": "s3cret-Token"}).status_code == 200
    client.cookies.set("drydock_token", "wrong")
    assert client.get("/examples/sample_system_a.csv").status_code == 401
    client.cookies.set("drydock_token", "s3cret-Token")
    assert client.get("/examples/sample_system_a.csv").status_code == 200


def test_the_page_itself_loads_so_it_can_ask(client):
    if not (orchestrator.ROOT / "ui" / "dist" / "index.html").exists():
        pytest.skip("UI not built")
    assert client.get("/").status_code == 200


def test_the_live_stream_refuses_without_the_token(client):
    with pytest.raises(WebSocketDisconnect) as e, client.websocket_connect("/ws") as ws:
        ws.receive_text()
    assert e.value.code == 4401


def test_no_token_configured_means_open(monkeypatch):
    monkeypatch.setattr(orchestrator, "SETTINGS", dataclasses.replace(orchestrator.SETTINGS, access_token=""))
    assert TestClient(orchestrator.app).get("/examples/sample_system_a.csv").status_code == 200
