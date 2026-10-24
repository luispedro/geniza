import pytest

from geniza import cli


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "sync" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["sync"],
        ["search", "foo"],
        ["show", "sha256:00"],
        ["reindex"],
        ["account", "rename", "a", "b"],
        ["secret", "keys"],
    ],
)
def test_unimplemented_commands_fail_cleanly(argv: list[str]) -> None:
    assert cli.main(argv) == 2


def test_global_options_before_and_after_subcommand() -> None:
    parser = cli.build_parser()
    a = parser.parse_args(["--json", "-v", "reindex"])
    assert a.json and a.verbose == 1
    b = parser.parse_args(["reindex", "--json", "-v"])
    assert b.json and b.verbose == 1
    c = parser.parse_args(["reindex"])
    assert not c.json and c.verbose == 0 and c.store is None
