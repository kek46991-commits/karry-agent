from pathlib import Path


def test_render_blueprint_contains_web_service_and_secret_config():
    text = Path("render.yaml").read_text(encoding="utf-8")
    assert "type: web" in text
    assert "runtime: python" in text
    assert "branch: main" in text
    assert "uvicorn app:app --host 0.0.0.0 --port $PORT" in text
    assert "healthCheckPath: /health" in text
    assert "key: GEMINI_API_KEY" in text
    assert "sync: false" in text
