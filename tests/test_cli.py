"""End-to-end user commands, with explicitly fictional data only."""
import json

from stock_validator.cli import main


def test_demo_round_trip_is_explicitly_synthetic(tmp_path, capsys):
    directory = tmp_path / "fictional"
    assert main(["demo", "--output", str(directory), "--days", "80"]) == 0
    assert "합성" in capsys.readouterr().out
    assert main(["inspect-data", "--data", str(directory)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["meta"]["synthetic"] is True
    assert summary["bar_count"] == 160
    assert "SYNTHETIC" in summary["meta"]["source"]


def test_demo_preserves_existing_user_data(tmp_path, capsys):
    directory = tmp_path / "existing"
    directory.mkdir()
    (directory / "notes.txt").write_text("retain", encoding="utf-8")
    assert main(["demo", "--output", str(directory), "--days", "20"]) == 1
    assert (directory / "notes.txt").read_text(encoding="utf-8") == "retain"
    assert "작업 실패" in capsys.readouterr().err


def test_absent_real_data_does_not_fall_back_to_demo(tmp_path, capsys):
    assert main(["inspect-data", "--data", str(tmp_path / "missing")]) == 1
    assert "작업 실패" in capsys.readouterr().err
    assert not (tmp_path / "missing").exists()
