"""Persistent memory and recall."""

from __future__ import annotations


def test_recall_ranks_relevant_memories_first(jarvis):
    jarvis.remember("the release branch is called stable", tags=["release"])
    jarvis.remember("the coffee machine is on the second floor", tags=["office"])
    jarvis.remember("releases are cut from stable on Fridays", tags=["release"])

    hits = jarvis.recall("when do we cut a release", limit=3)
    top = hits[0]["content"]

    assert "release" in top.lower()
    assert all("coffee" not in hit["content"].lower() for hit in hits[:2])


def test_recall_scores_are_deterministic(jarvis):
    jarvis.remember("nightly builds run at 02:00", tags=["build"])
    first = [(h["content"], h["score"]) for h in jarvis.recall("nightly build time")]
    second = [(h["content"], h["score"]) for h in jarvis.recall("nightly build time")]
    assert first == second


def test_goal_scoped_memories_are_boosted(jarvis):
    goal = jarvis.adopt_goal("G", "objective", run_first=False)
    jarvis.memory.remember("this project uses pytest", tags=["tooling"], goal_id=goal.id)
    jarvis.memory.remember("another project uses pytest", tags=["tooling"], goal_id="goal_other")

    hits = jarvis.memory.recall("pytest", goal_id=goal.id, limit=2)
    assert hits[0]["memory"].goal_id == goal.id


def test_importance_and_recency_change_ranking(jarvis):
    jarvis.remember("trivial note about tests", importance=0.05)
    jarvis.remember("critical rule about tests", importance=1.0)

    hits = jarvis.recall("tests", limit=2)
    assert hits[0]["content"].startswith("critical")


def test_memories_can_be_forgotten(jarvis):
    stored = jarvis.remember("remember to rotate the keys")
    assert jarvis.forget(stored["id"]) is True
    assert jarvis.forget(stored["id"]) is False
    assert jarvis.recall("rotate the keys") == []


def test_recall_touches_access_counters(jarvis):
    stored = jarvis.remember("the deploy runbook lives in docs")
    jarvis.recall("deploy runbook")
    refreshed = jarvis.memory.store.get_memory(stored["id"])
    assert refreshed.access_count >= 1


def test_context_block_is_prompt_ready(jarvis):
    jarvis.remember("the deploy runbook lives in docs", tags=["deploy"])
    block = jarvis.memory.context_block("how do we deploy")
    assert "deploy runbook" in block
    assert block.startswith("- (fact")


def test_prune_keeps_the_valuable_memories(jarvis):
    for index in range(30):
        jarvis.remember(f"noise number {index}", importance=0.01)
    jarvis.remember("the one thing that matters", importance=1.0)

    removed = jarvis.memory.prune(max_items=5)
    remaining = jarvis.memories()

    assert removed == 26
    assert len(remaining) == 5
    assert any("matters" in m["content"] for m in remaining)


def test_work_is_remembered_as_an_episode(permissive):
    permissive.request("run the tests")
    episodes = [m for m in permissive.memories() if m["kind"] == "episode"]

    assert episodes
    assert "completed/verified" in episodes[0]["content"]
