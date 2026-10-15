import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """엑셀 원본에서 새로 이관한 임시 DB로 테스트한다 (실제 fleet.db는 건드리지 않음)."""
    import data
    import services
    monkeypatch.setattr(data, "DB_FILE", tmp_path / "test.db")
    monkeypatch.setattr(data, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(data, "_initialized", False)
    monkeypatch.setattr(services, "_last_tick", 0.0)
    data.ensure_db()
    yield data
