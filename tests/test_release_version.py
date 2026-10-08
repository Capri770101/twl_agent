from pathlib import Path
from backend.version import VERSION


def test_api_version_matches_release_file():
    from main import app
    assert app.version == VERSION == (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip()
