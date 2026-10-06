from unittest.mock import MagicMock

import pytest

from migrations import migrate


def test_migration_commits_as_one_transaction():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None
    migrate(conn)
    assert any('profile_analysis_cache' in str(call) for call in cursor.execute.call_args_list)
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_migration_error_rolls_back():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = RuntimeError('simulated schema failure')
    with pytest.raises(RuntimeError):
        migrate(conn)
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()