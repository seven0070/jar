"""Desktop interface: the HTTP surface the packaged Windows app serves."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis.interfaces.desktop.app import create_app  # noqa: E402


@pytest.fixture
def client(permissive):
    app = create_app(permissive, scheduler_interval=0)
    with TestClient(app) as test_client:
        yield test_client


def test_health_and_status(client):
    health = client.get("/api/health").json()
    assert health["ok"] is True and health["halted"] is False

    status = client.get("/api/status").json()
    assert status["banner"].startswith("JARVIS")
    assert len(status["tools"]) > 20
    assert status["policy"]["mode"] == "standard"


def test_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "JARVIS" in response.text
    assert "/static/app.js" in response.text


def test_a_request_can_be_queued_and_then_run(client, permissive):
    created = client.post("/api/request", json={"intent": "run the tests", "run": False})
    assert created.status_code == 200
    task_id = created.json()["id"]
    assert created.json()["state"] == "queued"

    permissive.executive.run(max_tasks=5)
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["verdict"] == "verified"
    assert detail["plan"][0]["state"] == "succeeded"


def test_the_approval_queue_is_visible_and_actionable(client, jarvis):
    app = create_app(jarvis, scheduler_interval=0)
    with TestClient(app) as guarded:
        task = guarded.post("/api/request", json={"intent": "run the tests",
                                                  "run": False}).json()
        guarded.post(f"/api/tasks/{task['id']}/resume")
        approvals = guarded.get("/api/approvals").json()
        assert len(approvals) == 1
        assert approvals[0]["scope"] == "code.run"

        decided = guarded.post(f"/api/approvals/{approvals[0]['id']}/decide",
                               json={"approve": True, "note": "ui"}).json()
        assert decided["state"] == "approved"
        assert guarded.get(f"/api/tasks/{task['id']}").json()["verdict"] == "verified"


def test_halt_and_resume(client, permissive):
    assert client.post("/api/halt").json()["halted"] is True
    assert client.get("/api/status").json()["halted"] is True

    resumed = client.post("/api/resume").json()
    assert resumed["halted"] is False
    assert client.get("/api/status").json()["halted"] is False


def test_goals_can_be_adopted_from_the_ui(client, permissive):
    created = client.post("/api/goals", json={
        "title": "Reach a tested release",
        "objective": "Ship with a green test suite",
        "milestones": ["Run the test suite"],
        "monitors": [],
    }).json()
    assert created["title"] == "Reach a tested release"

    permissive.advance()
    report = client.get(f"/api/goals/{created['goal_id']}").json()
    assert report["completed"] == 1

    goals = client.get("/api/goals").json()
    assert goals[0]["id"] == created["goal_id"]

    cancelled = client.post(f"/api/goals/{created['goal_id']}/cancel").json()
    assert cancelled["state"] == "cancelled"


def test_memory_round_trip(client):
    stored = client.post("/api/memory", json={"content": "the build needs Python 3.11",
                                              "kind": "fact"}).json()
    assert stored["id"].startswith("mem_")

    listed = client.get("/api/memory").json()
    assert any("Python 3.11" in item["content"] for item in listed)

    recalled = client.get("/api/memory", params={"query": "python version"}).json()
    assert recalled and recalled[0]["score"] > 0

    assert client.delete(f"/api/memory/{stored['id']}").json()["forgotten"] is True


def test_events_can_be_injected(client, permissive):
    permissive.adopt_goal("Keep main green", "React to failures", run_first=False,
                          monitors=[{"name": "ci", "trigger": "event",
                                     "kind": "ci.failure", "intent": "Investigate",
                                     "cooldown_seconds": 0}])
    response = client.post("/api/events", json={"kind": "ci.failure",
                                                "payload": {"run": 1}}).json()
    assert len(response["created"]) == 1


def test_mode_can_be_changed(client):
    assert client.post("/api/mode", json={"mode": "readonly"}).json()["mode"] == "readonly"
    assert client.get("/api/status").json()["policy"]["mode"] == "readonly"
    assert client.post("/api/mode", json={"mode": "nonsense"}).status_code == 400


def test_review_and_proposals_endpoints(client, permissive):
    permissive.request("run the tests")
    review = client.post("/api/review").json()
    assert review["evaluated"] == 1
    assert isinstance(client.get("/api/proposals").json(), list)


def test_unknown_task_is_a_404(client):
    assert client.get("/api/tasks/task_nope").status_code == 404


def test_validation_errors_are_reported(client):
    assert client.post("/api/request", json={"intent": ""}).status_code == 400
    assert client.post("/api/goals", json={"title": "", "objective": ""}).status_code == 400
    assert client.post("/api/events", json={"kind": ""}).status_code == 400
