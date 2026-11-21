import logging
import os
import re
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path

import httpx
import pytest

from src.trace_context import configure_uvicorn_logging

TRACE_ID = "550e8400-e29b-41d4-a716-446655440000"
QUERY_MARKER = "SYNTHETIC_PRIVATE_QUERY_8482"
ERROR_MARKER = "SYNTHETIC_DRIVER_DETAIL_3721"
PARAM_MARKER = "SYNTHETIC_SQL_PARAM_4163"

APP_SOURCE = """
from bin.api import app
from src.infrastructure.persistence.database import create_engine
from src.settings import Settings

@app.get("/__probe/health")
async def health():
    engine = create_engine(Settings())
    try:
        return {"hide_parameters": engine.sync_engine.hide_parameters}
    finally:
        await engine.dispose()

from types import SimpleNamespace
from asyncpg import UniqueViolationError
from sqlalchemy.exc import IntegrityError
from src.presentation.api.dependencies import get_register_user

async def fail_register(name, email, password):
    engine = create_engine(Settings())
    try:
        driver_text = "constraint violation"
        params = ("normal-parameter",)
        if name == "driver":
            driver_text += ": SYNTHETIC_DRIVER_DETAIL_3721"
        else:
            params = ("SYNTHETIC_SQL_PARAM_4163",)
        raise IntegrityError(
            "INSERT INTO users VALUES ($1)", params,
            UniqueViolationError(driver_text),
            hide_parameters=engine.sync_engine.hide_parameters,
        )
    finally:
        await engine.dispose()

app.dependency_overrides[get_register_user] = lambda: SimpleNamespace(
    execute=fail_register,
)

"""


@pytest.fixture(scope="module")
def server_capture(tmp_path_factory):
    directory = tmp_path_factory.mktemp("uvicorn-log-safety")
    (directory / "probe.py").write_text(APP_SOURCE)
    log_path = directory / "server.log"
    env = {
        **os.environ,
        "PYTHONPATH": str(Path.cwd()),
        "PYTHONDONTWRITEBYTECODE": "1",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USERNAME": "probe",
        "POSTGRES_PASSWORD": "probe",
        "POSTGRES_DATABASE_NAME": "probe",
    }
    with (
        log_path.open("w") as output,
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "probe:app",
                "--app-dir",
                str(directory),
                "--host",
                "127.0.0.1",
                "--port",
                "0",
            ],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        ) as server,
    ):
        try:
            deadline = time.monotonic() + 10
            address = None
            while time.monotonic() < deadline and server.poll() is None:
                address = re.search(
                    r"Uvicorn running on http://127\.0\.0\.1:(\d+)",
                    log_path.read_text(),
                )
                if address:
                    break
                time.sleep(0.05)
            assert address, log_path.read_text()
            headers = {"X-Trace-Id": TRACE_ID, "Connection": "close"}
            with httpx.Client(
                base_url=f"http://127.0.0.1:{address[1]}", timeout=5, trust_env=False
            ) as client:
                success = client.get(
                    "/__probe/health", headers=headers, params={"secret": QUERY_MARKER}
                )
                not_found = client.get("/__probe/not-found", headers=headers)
                errors = {
                    mode: client.post(
                        "/auth/register",
                        headers=headers,
                        json={
                            "name": mode,
                            "email": "probe@example.com",
                            "password": "probe-only-password",
                        },
                    )
                    for mode in ("params", "driver")
                }
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)

    return success, not_found, errors, log_path.read_text()


def test_real_server_omits_query_from_access_logs(server_capture):
    success, not_found, _, output = server_capture
    assert success.status_code == 200
    assert not_found.status_code == 404
    assert not_found.json() == {"detail": "Not Found"}
    assert success.headers["X-Trace-Id"] == not_found.headers["X-Trace-Id"] == TRACE_ID
    assert (
        f"trace_id={TRACE_ID} http request method='GET' "
        "path='/__probe/health' status=200" in output
    )
    assert QUERY_MARKER not in output


def test_engine_hides_sql_parameters(server_capture):
    success, _, _, _ = server_capture
    assert success.json()["hide_parameters"] is True


@pytest.mark.parametrize(
    ("mode", "marker"), [("params", PARAM_MARKER), ("driver", ERROR_MARKER)]
)
def test_real_server_omits_sql_exception_secrets(server_capture, mode, marker):
    _, _, errors, output = server_capture
    response = errors[mode]
    assert response.status_code == 500
    assert "X-Trace-Id" not in response.headers
    assert response.text == "Internal Server Error"
    assert (
        f"trace_id={TRACE_ID} http request failed method='POST' "
        "path='/auth/register' status=500" in output
    )
    assert re.search(r"^ERROR: +Exception in ASGI application", output, re.MULTILINE)
    assert marker not in output


def test_uvicorn_console_redacts_a_copy_without_changing_other_handlers(monkeypatch):
    class Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    class Console(Capture, logging.StreamHandler):
        def emit(self, record):
            Capture.emit(self, record)
            logging.StreamHandler.emit(self, record)

    stream = StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    safe = Console()
    sibling = Capture()
    parent = logging.getLogger("uvicorn")
    errors = logging.getLogger("uvicorn.error")
    access = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(parent, "handlers", [safe, sibling])
    monkeypatch.setattr(parent, "propagate", False)
    monkeypatch.setattr(errors, "handlers", [])
    monkeypatch.setattr(errors, "filters", [])
    monkeypatch.setattr(errors, "propagate", True)
    monkeypatch.setattr(access, "disabled", False)

    configure_uvicorn_logging()
    configure_uvicorn_logging()

    try:
        raise RuntimeError(ERROR_MARKER)
    except RuntimeError:
        exc_info = sys.exc_info()
        original = errors.makeRecord(
            errors.name,
            logging.ERROR,
            __file__,
            0,
            "Exception in ASGI application\n",
            (),
            exc_info,
        )
        original.exc_text = f"cached exception: {ERROR_MARKER}"
        original_text = original.exc_text
        errors.handle(original)

    assert original.exc_info is exc_info
    assert original.exc_text == original_text
    assert errors.filters == []
    assert sibling.records == [original]
    assert safe.records[0] is not original
    assert safe.records[0].exc_info is None
    assert safe.records[0].exc_text is None
    assert ERROR_MARKER not in stream.getvalue()
    assert "Exception in ASGI application" in stream.getvalue()
    assert len(safe.filters) == 1
    assert sibling.filters == []

    try:
        raise RuntimeError("unrelated Uvicorn failure")
    except RuntimeError:
        errors.exception("Uvicorn startup failed")

    assert safe.records[1] is sibling.records[1]
    assert safe.records[1].exc_info is not None
    assert "Traceback (most recent call last)" in stream.getvalue()
    assert "RuntimeError: unrelated Uvicorn failure" in stream.getvalue()
