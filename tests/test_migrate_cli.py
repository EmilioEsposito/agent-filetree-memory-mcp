import pytest

from agent_filetree_memory import migrate_cli


def test_missing_database_setting_exits_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        migrate_cli.main(["check"])
    assert exc.value.code == 1
    assert "DATABASE_URL" in capsys.readouterr().err


def test_driver_errors_do_not_disclose_credentials(monkeypatch, capsys):
    async def failing(*args):
        raise RuntimeError("postgresql://user:sensitive-password@server/database")

    monkeypatch.setattr(migrate_cli, "run_command", failing)
    with pytest.raises(SystemExit) as exc:
        migrate_cli.main(["upgrade"])
    assert exc.value.code == 1
    message = capsys.readouterr().err
    assert "sensitive-password" not in message
    assert "RuntimeError" in message
