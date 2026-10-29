from pathlib import Path

INDEX = (Path(__file__).parent.parent / "index.html").read_text(encoding="utf-8")


def test_no_llm_down_alert_on_ops_screen():
    assert "LLM service is DOWN" not in INDEX
