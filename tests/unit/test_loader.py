"""Tests for reading the configuration file from disk."""

import re
from pathlib import Path

import pytest

from talaia.config.loader import ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]

VALID = """
defaults:
  interval: 30
monitors:
  - name: web
    type: http
    target: http://10.0.0.1
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "monitors.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestLoadConfig:
    def test_reads_and_validates_a_file(self, tmp_path: Path) -> None:
        config = load_config(write(tmp_path, VALID))

        assert config.defaults.interval == 30
        assert [m.name for m in config.resolve()] == ["web"]

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "absent.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_config(write(tmp_path, "monitors: [\n  - name: web\n"))

    def test_empty_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="empty"):
            load_config(write(tmp_path, ""))

    def test_top_level_list_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="mapping at the top level"):
            load_config(write(tmp_path, "- name: web\n"))

    def test_unreadable_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = write(tmp_path, VALID)

        def deny(*args: object, **kwargs: object) -> str:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "read_text", deny)

        with pytest.raises(ConfigError, match="could not read"):
            load_config(path)

    def test_schema_violation_names_the_field(self, tmp_path: Path) -> None:
        bad = "monitors:\n  - name: web\n    type: http\n    target: not-a-url\n"

        with pytest.raises(ConfigError, match=re.escape("monitors.0")):
            load_config(write(tmp_path, bad))

    def test_error_message_lists_every_problem(self, tmp_path: Path) -> None:
        bad = "monitors:\n  - name: WEB\n    type: nope\n    target: x\n"

        with pytest.raises(ConfigError) as raised:
            load_config(write(tmp_path, bad))

        assert str(raised.value).count("\n") >= 2


class TestCommittedConfigFiles:
    """The files in config/ are parsed by CI, so they must always be valid."""

    def test_committed_file_is_valid(self) -> None:
        config = load_config(REPO_ROOT / "config" / "monitors.yaml")

        assert config.resolve()

    def test_the_real_config_never_enters_an_image(self) -> None:
        """The Dockerfile copies config/ wholesale, so only .dockerignore keeps it out."""
        patterns = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

        assert "config/monitors.local.yaml" in patterns

    def test_committed_files_contain_no_real_addresses(self) -> None:
        text = (REPO_ROOT / "config" / "monitors.yaml").read_text(encoding="utf-8")

        assert "192.168." not in text
        assert "costantino.es" not in text
