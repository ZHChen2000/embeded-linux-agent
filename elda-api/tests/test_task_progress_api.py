"""API-side progress payload tests."""

from elda.task_progress import compute_progress


def test_plan_progress_stages_order() -> None:
    p = compute_progress("plan", "plan_agent", "规划驱动方案")
    ids = [s["id"] for s in p["stages"]]
    assert ids.index("load_context") < ids.index("plan_agent")
    assert any(s["status"] == "running" and s["id"] == "plan_agent" for s in p["stages"])
