from pathlib import Path


def test_render_blueprint_contains_web_service_and_secret_config():
    text = Path("render.yaml").read_text(encoding="utf-8")
    assert "type: web" in text
    assert "runtime: python" in text
    assert "branch: main" in text
    assert "pip install -r requirements.txt && pip install ." in text
    assert "uvicorn app:app --host 0.0.0.0 --port $PORT" in text
    assert "healthCheckPath: /health" in text
    assert "key: GEMINI_API_KEY" in text
    assert "key: SECRET_PASSPHRASE" in text
    assert "sync: false" in text


def test_dockerfile_uses_render_port_and_web_command():
    text = Path("Dockerfile").read_text(encoding="utf-8")
    assert "EXPOSE 8000" in text
    assert "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}" in text


def test_compose_keeps_cli_override_and_secret_variable():
    text = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "SECRET_PASSPHRASE" in text
    assert 'command: ["python3", "main.py"]' in text
