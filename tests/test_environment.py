import sqlite3


def test_sqlite_has_fts5() -> None:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
    con.execute("INSERT INTO t(body) VALUES ('quarterly report')")
    rows = con.execute("SELECT body FROM t WHERE t MATCH 'report'").fetchall()
    assert rows == [("quarterly report",)]


def test_stdlib_zstd() -> None:
    from compression import zstd

    assert zstd.decompress(zstd.compress(b"geniza")) == b"geniza"
