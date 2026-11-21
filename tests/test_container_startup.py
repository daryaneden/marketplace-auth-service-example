import os
import subprocess

import pytest


@pytest.mark.parametrize("migration_status", [0, 7])
def test_entrypoint_migrates_before_exec(tmp_path, pytestconfig, migration_status):
    events = tmp_path / "events"
    python = tmp_path / "python"
    python.write_text(
        "#!/bin/sh\n"
        'printf "%s:%s\n" "$$" "$*" >> "$EVENTS"\n'
        'if [ "$2" = alembic ]; then exit "$MIGRATION_STATUS"; fi\n'
    )
    python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "EVENTS": str(events),
        "MIGRATION_STATUS": str(migration_status),
    }
    entrypoint = pytestconfig.rootpath / "run.sh"
    with subprocess.Popen(["bash", str(entrypoint)], env=env) as process:
        assert process.wait(timeout=5) == migration_status
    commands = events.read_text().splitlines()
    assert commands[0].endswith(":-m alembic upgrade head")
    if migration_status == 0:
        assert commands[1:] == [f"{process.pid}:-m bin.api"]
    else:
        assert len(commands) == 1
