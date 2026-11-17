import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "fixtures")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import osmface  # noqa: E402


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


@pytest.fixture
def loaded(tmp_path):
    """Load a fixture into a fresh staging db; return the db path."""
    def _load(name: str) -> str:
        db = str(tmp_path / (name.replace(".jsonl", "") + ".db"))
        osmface.load_database(db, [fixture(name)], force=True)
        return db
    return _load


def relation(db: str, rid: int) -> dict:
    return osmface.inspect_database(db, relation_id=rid)["relation"]


def run_cli(*args) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "osmface", *args],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
