import re
import stat
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from geniza.config import (
    CACHE_FILES,
    CACHE_ID_FILE,
    CACHEDIR_TAG,
    FORMAT_VERSION,
    STORE_FILE,
    ConfigError,
    IncomingSettings,
    Secret,
    init_store,
    load,
    open_cache,
    parse,
    read_store,
)
from geniza.mime.recipes import Policy
from geniza.store.lock import StoreLock

DESIGN = Path(__file__).parent.parent / "DESIGN.md"
ENV = {"HOME": "/home/u"}
TOKEN = "sshcrypt:U1NIQ1JZUFQy"
CFG = Path("/cfg/config.toml")


def toml_blocks(section: str) -> list[str]:
    """The ```toml blocks of a DESIGN.md section."""
    text = DESIGN.read_text()
    start = text.index(f"\n## {section}\n")
    end = text.find("\n## ", start + 1)
    return re.findall(r"```toml\n(.*?)```", text[start:end], re.S)


def account(**kw: Any) -> dict[str, Any]:
    return {"store": "s", "type": "jmap", "url": "https://h/jmap", "token": TOKEN} | kw


def config(**top: Any) -> dict[str, Any]:
    return {"stores": {"s": {}}} | top


def error(data: dict[str, Any], env: dict[str, str] = ENV) -> str:
    with pytest.raises(ConfigError) as e:
        parse(data, CFG, env)
    return str(e.value)


# --- configuration -----------------------------------------------------------


def test_defaults_and_xdg_fallbacks() -> None:
    c = parse(config(accounts={"a": account()}), CFG, ENV)
    s = c.store()
    assert s.path == Path("/home/u/.local/share/geniza/s")
    assert s.cache == Path("/home/u/.cache/geniza/s")
    assert s.attachments == Policy()
    assert s.incoming == IncomingSettings()
    a = c.accounts["a"]
    assert (a.store, a.type, a.url) == ("s", "jmap", "https://h/jmap")
    assert a.token == Secret("sshcrypt", TOKEN)
    assert (a.exclude, a.no_stable_id_ok) == ((), False)
    assert c.accounts_of("s") == [a]


def test_xdg_variables() -> None:
    env = ENV | {"XDG_DATA_HOME": "/d", "XDG_CACHE_HOME": "relative/is/ignored"}
    s = parse(config(), CFG, env).store()
    assert s.path == Path("/d/geniza/s")
    assert s.cache == Path("/home/u/.cache/geniza/s")


def test_default_config_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no configuration file"):
        load(env={"HOME": str(tmp_path)})
    (tmp_path / "x" / "geniza").mkdir(parents=True)
    (tmp_path / "x" / "geniza" / "config.toml").write_text('[stores.s]\npath="~/m"\n')
    c = load(env={"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "x")})
    assert c.store("s").path == tmp_path / "m"


def test_toml_syntax_error(tmp_path: Path) -> None:
    f = tmp_path / "c.toml"
    f.write_text("[stores\n")
    with pytest.raises(ConfigError, match=re.escape(str(f))):
        load(f, ENV)


def test_merge_key_by_key() -> None:
    data = {
        "attachments": {"decoration_min_size": "1MiB"},
        "incoming": {"max_age": "3d", "grace": "1w"},
        "stores": {
            "a": {},
            "b": {
                "attachments": {"min_size": 10},
                "incoming": {"max_age": "14d", "mailboxes": ["inbox", "junk"]},
            },
        },
    }
    c = parse(data, CFG, ENV)
    assert c.stores["a"].attachments == Policy(decoration_min_size=1 << 20)
    assert c.stores["b"].attachments == Policy(min_size=10, decoration_min_size=1 << 20)
    assert c.stores["a"].incoming == IncomingSettings(
        max_age=timedelta(days=3), grace=timedelta(weeks=1)
    )
    assert c.stores["b"].incoming == IncomingSettings(
        mailboxes=("inbox", "junk"),
        max_age=timedelta(days=14),
        grace=timedelta(weeks=1),
    )


def test_design_example() -> None:
    (example,) = toml_blocks("Configuration")
    data = tomllib.loads(example)
    assert "IMAP accounts are not supported yet" in error(data)
    del data["accounts"]["uni"]
    c = parse(data, CFG, ENV)
    assert c.store().name == "personal"
    assert c.stores["work"].path == Path("/srv/archive/work")
    assert c.stores["work"].incoming.max_age == timedelta(days=14)
    assert c.stores["personal"].attachments == Policy()
    assert c.accounts["fastmail"].token.kind == "sshcrypt"


def test_design_incoming_defaults() -> None:
    """The [incoming] block in DESIGN.md lists the built-in defaults."""
    (block,) = toml_blocks("Incoming: recent INBOX mail")
    c = parse(config(**tomllib.loads(block)), CFG, ENV)
    assert c.store().incoming == IncomingSettings()


@pytest.mark.parametrize(
    "data, message",
    [
        (config(colour=1), "colour: unknown key"),
        ({"stores": {"s": {"pth": "/x"}}}, "stores.s.pth: unknown key"),
        (config(incoming={"max_ag": "1d"}), "incoming.max_ag: unknown key"),
        (config(accounts={"a": account(sync={"x": 1})}), "accounts.a.sync.x"),
        (config(accounts={"a": account(port=1)}), "accounts.a.port: unknown"),
        ({"stores": {"Big": {}}}, "stores.Big: store names may only"),
        ({"stores": {"a.b": {}}}, "store names may only"),
        (config(accounts={"x y": account()}), "account names may only"),
        (config(stores=[]), "stores: must be a table"),
        (config(accounts={"a": account(store="t")}), "accounts.a.store: no store"),
        (config(accounts={"a": account(type="pop")}), "must be 'jmap'"),
        (config(accounts={"a": account(type="imap")}), "not supported yet"),
        (config(accounts={"a": {"type": "jmap"}}), "accounts.a.store: missing"),
        (config(accounts={"a": account(url=None)}), "accounts.a.url: missing"),
        (config(default_store="t"), "default_store: no store 't'"),
        ({"stores": {"s": {"path": "rel/x"}}}, "must be an absolute path"),
        ({"stores": {"s": {"path": "~user/x"}}}, "must be an absolute path"),
        (config(attachments={"min_size": -1}), "bad size -1"),
        (config(attachments={"min_size": True}), "bad size True"),
        (config(attachments={"min_size": "64KB"}), "bad size '64KB'"),
        (config(incoming={"max_age": "7"}), "bad duration '7'"),
        (config(incoming={"max_age": 7}), "bad duration 7"),
        (config(incoming={"on_delete": "purge"}), "must be 'drop' or 'keep'"),
        (config(incoming={"max_drop_fraction": 1.5}), "between 0 and 1"),
        (config(incoming={"max_drop_count": 2.0}), "non-negative integer"),
        (config(incoming={"mailboxes": "INBOX"}), "list of non-empty strings"),
        (config(accounts={"a": account(no_stable_id_ok=1)}), "true or false"),
    ],
)
def test_invalid(data: dict[str, Any], message: str) -> None:
    assert message in error(data)


def test_error_names_file() -> None:
    assert error(config(colour=1)).startswith(f"{CFG}: ")


@pytest.mark.parametrize(
    "paths",
    [
        {"path": "/m", "cache": "/m"},
        {"path": "/m", "cache": "/m/cache"},
        {"path": "/c/store", "cache": "/c"},
    ],
)
def test_store_and_cache_must_not_nest(paths: dict[str, str]) -> None:
    assert "must not contain each other" in error({"stores": {"s": paths}})


def test_stores_must_not_overlap() -> None:
    data = {"stores": {"a": {"path": "/m/a"}, "b": {"cache": "/m/a/x"}}}
    assert "stores.b: /m/a/x overlaps with /m/a of store 'a'" in error(data)
    # Default paths of different stores are siblings.
    parse({"stores": {"a": {}, "b": {}}}, CFG, ENV)


def test_store_selection() -> None:
    two = {"stores": {"a": {}, "b": {}}}
    c = parse(two, CFG, ENV)
    assert c.store("b").name == "b"
    with pytest.raises(ConfigError, match="use --store"):
        c.store()
    with pytest.raises(ConfigError, match="no store 'c'"):
        c.store("c")
    assert parse(two | {"default_store": "b"}, CFG, ENV).store().name == "b"
    with pytest.raises(ConfigError, match="no stores configured"):
        parse({}, CFG, ENV).store()


# --- secrets -----------------------------------------------------------------


def test_secret_command() -> None:
    a = account(token_command="pass show mail")
    del a["token"]
    token = parse(config(accounts={"a": a}), CFG, ENV).accounts["a"].token
    assert token == Secret("command", "pass show mail")


def test_secret_repr_hides_value() -> None:
    assert TOKEN not in repr(parse(config(accounts={"a": account()}), CFG, ENV))


@pytest.mark.parametrize("value", ["hunter2-plain", "sshcrypt:", "SSHCRYPT:abc"])
def test_plain_secret_rejected_without_echo(value: str) -> None:
    message = error(config(accounts={"a": account(token=value)}))
    assert "geniza secret encrypt" in message
    assert value not in message


def test_secret_exactly_one() -> None:
    both = account(token_command="pass x")
    assert "either token or token_command" in error(config(accounts={"a": both}))
    none = account()
    del none["token"]
    assert "token: missing" in error(config(accounts={"a": none}))
    empty = account(token_command="")
    del empty["token"]
    assert "token_command: must be a non-empty" in error(config(accounts={"a": empty}))


@pytest.mark.parametrize(
    "url",
    ["https://api.fastmail.com/jmap/session", "http://127.0.0.1:8080/s",
     "http://localhost/s", "http://[::1]:1/s"],
)  # fmt: skip
def test_url_accepted(url: str) -> None:
    assert parse(config(accounts={"a": account(url=url)}), CFG, ENV).accounts["a"]


@pytest.mark.parametrize(
    "url", ["http://api.fastmail.com/jmap", "ftp://h/x", "https://", "api/jmap"]
)
def test_url_rejected(url: str) -> None:
    assert "accounts.a.url" in error(config(accounts={"a": account(url=url)}))


# --- sizes and durations -----------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [(0, 0), (1, 1), ("12", 12), ("12B", 12), ("64KiB", 65536), ("64 KiB", 65536),
     ("2MiB", 2 << 20), ("1GiB", 1 << 30)],
)  # fmt: skip
def test_sizes(value: int | str, expected: int) -> None:
    c = parse(config(attachments={"decoration_min_size": value}), CFG, ENV)
    assert c.store().attachments.decoration_min_size == expected


@given(
    st.lists(
        st.tuples(st.integers(0, 10_000), st.sampled_from("smhdw")),
        min_size=1,
        max_size=5,
    )
)
def test_durations(parts: list[tuple[int, str]]) -> None:
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    text = "".join(f"{n}{u}" for n, u in parts)
    c = parse(config(incoming={"grace": text}), CFG, ENV)
    expected = timedelta(seconds=sum(n * unit[u] for n, u in parts))
    assert c.store().incoming.grace == expected


# --- store.toml --------------------------------------------------------------


def test_init_store(tmp_path: Path) -> None:
    store = tmp_path / "store"
    t = datetime(2026, 10, 25, 9, 30, 1, 123, tzinfo=UTC)
    with StoreLock(store):
        info = init_store(store, clock=lambda: t)
        assert info.format == FORMAT_VERSION
        assert info.created == t.replace(microsecond=0)
        text = (store / STORE_FILE).read_text()
        # Written once: a second call reads it back unchanged.
        assert init_store(store) == info
    assert (store / STORE_FILE).read_text() == text
    assert read_store(store) == info
    assert sorted(p.name for p in store.iterdir()) == [".lock", STORE_FILE]
    (tmp_path / "other").mkdir()
    assert init_store(tmp_path / "other").id != info.id


def test_init_store_removes_temp_files(tmp_path: Path) -> None:
    (tmp_path / ".store.toml.0123456789abcdef.tmp").write_text("partial")
    init_store(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [STORE_FILE]
    # Also once the store exists (a crash after the link, or while writing
    # "initialized").
    (tmp_path / ".store.toml.0123456789abcdef.tmp").write_text("complete")
    (tmp_path / ".initialized.0123456789abcdef.tmp").write_text("partial")
    (tmp_path / ".other.0123456789abcdef.tmp").write_text("not ours")
    init_store(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        ".other.0123456789abcdef.tmp",
        STORE_FILE,
    ]


def test_init_store_refuses_nonempty_directory(tmp_path: Path) -> None:
    (tmp_path / "segments").mkdir()
    with pytest.raises(ConfigError, match="has files but no store"):
        init_store(tmp_path)
    assert not (tmp_path / STORE_FILE).exists()


def test_read_store_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not a geniza store"):
        read_store(tmp_path)


@pytest.mark.parametrize(
    "text, message",
    [
        ("format = 2\n", "newer than this geniza supports"),
        ("format = 0\n", "unknown store format 0"),
        ('format = "1"\n', "format: must be an integer"),
        ('id = "nope"\n', "id: must be a UUID"),
        ("created = 2026-10-25T09:00:00\n", "created: must be a date-time"),
        ("extra = 1\n", "extra: unknown key"),
    ],
)
def test_read_store_invalid(tmp_path: Path, text: str, message: str) -> None:
    good = {
        "format": f"format = {FORMAT_VERSION}\n",
        "id": f'id = "{uuid.uuid4()}"\n',
        "created": "created = 2026-10-25T09:00:00Z\n",
    }
    key = text.split(" ")[0]
    good[key] = text
    (tmp_path / STORE_FILE).write_text("".join(good.values()))
    with pytest.raises(ConfigError, match=message):
        read_store(tmp_path)


# --- cache -------------------------------------------------------------------


def test_new_cache(tmp_path: Path) -> None:
    cache = tmp_path / "c" / "personal"
    sid = uuid.uuid4()
    assert open_cache(cache, sid)
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert (
        (cache / CACHEDIR_TAG)
        .read_text()
        .startswith("Signature: 8a477f597d28d172789f06886806bc55")
    )
    assert (cache / CACHE_ID_FILE).read_text() == f"{sid}\n"
    assert not open_cache(cache, sid)


def test_cache_of_other_store_is_discarded(tmp_path: Path) -> None:
    open_cache(tmp_path, uuid.uuid4())
    for name in CACHE_FILES:
        (tmp_path / name).write_text("old")
    (tmp_path / "keep.txt").write_text("not ours")
    sid = uuid.uuid4()
    assert open_cache(tmp_path, sid)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [CACHEDIR_TAG, CACHE_ID_FILE, "keep.txt"]
    )
    assert (tmp_path / CACHE_ID_FILE).read_text() == f"{sid}\n"


def test_cache_without_id_is_discarded(tmp_path: Path) -> None:
    open_cache(tmp_path, sid := uuid.uuid4())
    (tmp_path / CACHE_ID_FILE).unlink()
    (tmp_path / "index.sqlite").write_text("old")
    assert open_cache(tmp_path, sid)
    assert not (tmp_path / "index.sqlite").exists()


def test_cache_refuses_untagged_directory(tmp_path: Path) -> None:
    (tmp_path / "photos").mkdir()
    with pytest.raises(ConfigError, match="not a geniza cache"):
        open_cache(tmp_path, uuid.uuid4())
    assert not (tmp_path / CACHEDIR_TAG).exists()


def test_cache_refuses_bad_tag(tmp_path: Path) -> None:
    (tmp_path / CACHEDIR_TAG).write_text("hello\n")
    with pytest.raises(ConfigError, match="not a cache directory tag"):
        open_cache(tmp_path, uuid.uuid4())
