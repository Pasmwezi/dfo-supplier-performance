from pathlib import Path


def test_run_script_listens_on_loopback_by_default():
    script = (Path(__file__).parents[1] / "run.sh").read_text()
    assert '${HOST:-127.0.0.1}' in script
    assert 'app.main:create_app --factory' in script
    assert 'source .env' in script
