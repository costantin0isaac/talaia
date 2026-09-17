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
    def no_database(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
        """Record what would have been run instead of opening a database."""
        calls: list[tuple[str, ...]] = []

        async def fake_run(command: str, *args: str) -> int:
            calls.append((command, *args))
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

    def test_list_needs_no_username(self, no_database: list[tuple[str, ...]]) -> None:
        assert cli.main(["list"]) == 0
        assert no_database == [("list",)]

    def test_the_username_is_passed_through(self, no_database: list[tuple[str, ...]]) -> None:
        assert cli.main(["add", "isaac"]) == 0
        assert no_database == [("add", "isaac")]

    def test_revoke_needs_a_user_and_a_session_id(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["revoke", "isaac"]) == 2
        assert cli.USAGE in capsys.readouterr().err

    def test_too_many_arguments_is_also_wrong(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A stray extra word is more likely a mistake than an intention."""
        assert cli.main(["list", "isaac"]) == 2
        assert cli.USAGE in capsys.readouterr().err

    def test_revoke_passes_both_arguments(self, no_database: list[tuple[str, ...]]) -> None:
        assert cli.main(["revoke", "isaac", "3f9a"]) == 0
        assert no_database == [("revoke", "isaac", "3f9a")]

    def test_every_documented_command_is_wired(self) -> None:
        """The module docstring is the user manual; it must not promise a missing command."""
        for command in ("add", "list", "passwd", "disable", "sessions", "revoke"):
            assert command in cli.COMMANDS
