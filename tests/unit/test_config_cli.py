"""Tests for `python -m talaia.config`, the command the CI validate stage runs."""

from pathlib import Path

import pytest

from talaia.config.__main__ import main

VALID = "monitors:\n  - name: web\n    type: http\n    target: http://10.0.0.1\n"


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestMain:
    def test_valid_file_succeeds(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = write(tmp_path, "monitors.yaml", VALID)

        assert main([str(path)]) == 0
        assert "ok" in capsys.readouterr().out

    def test_invalid_file_fails_with_a_message_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write(tmp_path, "monitors.yaml", VALID.replace("name: web", "name: WEB"))

        assert main([str(path)]) == 1
        assert "INVALID" in capsys.readouterr().err

    def test_one_bad_file_fails_the_whole_run(self, tmp_path: Path) -> None:
        good = write(tmp_path, "good.yaml", VALID)
        bad = write(tmp_path, "bad.yaml", "monitors: []\n")

        assert main([str(good), str(bad)]) == 1

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        assert main([str(tmp_path / "absent.yaml")]) == 1

    def test_no_arguments_returns_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([]) == 2
        assert "usage" in capsys.readouterr().err
