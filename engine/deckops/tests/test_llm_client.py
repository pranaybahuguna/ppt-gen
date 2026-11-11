"""The real model client (HTTP, JSON mode) against a local fake server: request shape and parsing."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import fake_llm  # noqa: E402
from controlplane.planner import OpenAICompatPlanner  # noqa: E402


def test_outline_and_render_over_http(tmp_path):
    srv, url = fake_llm.start()
    try:
        cfg = tmp_path / "llm.json"
        cfg.write_text(json.dumps({"base_url": url, "model": "test-model", "api_key": "k"}))
        p = OpenAICompatPlanner.from_file(str(cfg))
        outline = p.outline("brief", {"facts": []}, 8)
        assert outline["slides"][0]["kind"] == "cover"
        opts = [{"pattern": "steps", "fields": ["title"], "items": {"min": 1, "max": 8, "each": ["heading", "body"]}}]
        ans = p.render_slide(outline["slides"][1], opts, {"slide": 2, "of": 3})
        assert ans["pattern"] == "steps" and ans["items"][0] == {"heading": "Q1", "body": "Two more teams."}
        call = fake_llm.Handler.calls[-1]
        assert call["auth"] == "Bearer k" and call["body"]["model"] == "test-model"
        assert call["body"]["response_format"] == {"type": "json_object"}
    finally:
        srv.shutdown()


def test_model_that_rejects_temperature(tmp_path):
    srv, url = fake_llm.start()
    fake_llm.Handler.reject_temperature = True
    try:
        p = OpenAICompatPlanner(url, "strict-model", "k")
        assert p.outline("brief", {"facts": []}, 8)["slides"]
        assert p.send_temperature is False and "temperature" not in fake_llm.Handler.calls[-1]["body"]
    finally:
        fake_llm.Handler.reject_temperature = False
        srv.shutdown()
