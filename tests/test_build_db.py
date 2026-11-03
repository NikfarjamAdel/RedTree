"""build_db.py --clean --azure builds brand-new databases only: it must
refuse any database that already has a table, before running anything."""
import build_db
import pytest

pytestmark = pytest.mark.db      # every test in this file needs a built database

class FakeDatabase:
    """Answers refuse_unless_empty's one query with the given table count."""
    def __init__(self, tables):
        self.tables = tables

    def cursor(self):
        return self

    def execute(self, sql):
        assert "sys.tables" in sql

    def fetchone(self):
        return ("newdb", self.tables)

    def close(self):
        pass


def test_an_empty_database_may_be_built():
    build_db.refuse_unless_empty(FakeDatabase(0))           # no exit


def test_a_database_with_tables_is_refused():
    with pytest.raises(SystemExit, match="already has 3 tables"):
        build_db.refuse_unless_empty(FakeDatabase(3))


@pytest.mark.parametrize("mode", ["--example", "--demo"])
def test_azure_builds_clean_only(monkeypatch, mode):
    monkeypatch.setattr(build_db, "connect_db", lambda target: pytest.fail("connected"))
    with pytest.raises(SystemExit, match="--clean only"):
        build_db.main([mode, "--azure", "--yes"])


def test_azure_needs_yes(monkeypatch):
    monkeypatch.setattr(build_db, "connect_db", lambda target: pytest.fail("connected"))
    with pytest.raises(SystemExit, match="AZURE database"):
        build_db.main(["--clean", "--azure"])


def test_azure_mode_refuses_a_real_database_with_tables(monkeypatch, local_db):
    """The whole --clean --azure path against a database that has tables
    (the local one stands in for Azure): refused before any SQL file runs."""
    targets = []

    class Unclosable:                    # main() closes it; the fixture does that
        def __getattr__(self, name):
            return getattr(local_db, name)

        def close(self):
            pass

    def connect(target):
        targets.append(target)
        return Unclosable()

    monkeypatch.setattr(build_db, "connect_db", connect)
    monkeypatch.setattr(build_db, "run_sql_file", lambda *a: pytest.fail("a SQL file ran"))
    with pytest.raises(SystemExit, match="Refusing to build: .* already has \\d+ tables"):
        build_db.main(["--clean", "--azure", "--yes"])
    assert targets == ["azure"]
