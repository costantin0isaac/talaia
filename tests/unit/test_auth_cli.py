"""Argument handling of the user-management CLI, with no database involved."""

from collections.abc import Iterator

import pytest

from talaia.auth import __main__ as cli


@pytest.fixture
def typed(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Feed scripted answers to the masked password prompt."""
    answers: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        return answers.pop(0)

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    yield answers


class TestPromptPassword:
    def test_returns_the_password_when_both_entries_match(self, typed: list[str]) -> None:
        typed += ["correct horse battery", "correct horse battery"]

        assert cli.prompt_password() == "correct horse battery"

    def test_refuses_a_mismatch(self, typed: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        typed += ["one thing", "another thing"]

        assert cli.prompt_password() is None
        assert "do not match" in capsys.readouterr().err


class TestMain:
    @pytest.fixture(autouse=True)
    def no_database(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
        """Record what would have been run instead of opening a database."""
        calls: list[tuple[str, str]] = []

        async def fake_run(command: str, username: str) -> int:
            calls.append((command, username))
            return 0

        monkeypatch.setattr(cli, "run", fake_run)
        return calls

    def test_no_arguments_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main([]) == 2
        assert cli.USAGE in capsys.readouterr().err

    def test_an_unknown_command_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["frobnicate"]) == 2
        assert cli.USAGE in capsys.readouterr().err

    @pytest.mark.parametrize("command", ["add", "passwd", "disable", "enable"])
    def test_commands_that_need_a_username_refuse_to_run_without_one(
        self, command: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main([command]) == 2
        assert cli.USAGE in capsys.readouterr().err

    def test_list_needs_no_username(self, no_database: list[tuple[str, str]]) -> None:
        assert cli.main(["list"]) == 0
        assert no_database == [("list", "")]

    def test_the_username_is_passed_through(self, no_database: list[tuple[str, str]]) -> None:
        assert cli.main(["add", "isaac"]) == 0
        assert no_database == [("add", "isaac")]

    def test_every_documented_command_is_wired(self) -> None:
        """The module docstring is the user manual; it must not promise a missing command."""
        for command in ("add", "list", "passwd", "disable"):
            assert command in cli.COMMANDS
