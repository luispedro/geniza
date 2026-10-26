"""An in-process fake JMAP server (RFC 8620/8621, the read-only part).

It serves one account over loopback HTTP (stdlib ``http.server``): the
session object, ``Mailbox``/``Email`` ``get`` and ``changes``,
``Email/query`` (sorted by ``receivedAt``, with ``position`` or
``anchor``), ``get`` of the settings types (``Identity``,
``SieveScript``, ``VacationResponse``) once ``enable_settings`` gave the
account their capabilities, and blob downloads (emails and Sieve
scripts). A method whose capability the request's ``using`` or the
account lacks is ``unknownMethod``. Tests change the account through
``FakeJmap`` methods, which keep a change log so ``*/changes`` can answer
from any earlier state. Every method name received is recorded in
``methods``, every downloaded blob in ``downloads``; a method that would
modify the account is answered with ``unknownMethod`` and recorded too,
so tests can assert it never arrives.
"""

import hashlib
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.parser import BytesHeaderParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

TOKEN = "fake-jmap-token-0123456789"
ACCOUNT = "A1"

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
#: Capability of each settings type.
SETTINGS = {
    "Identity": "urn:ietf:params:jmap:submission",
    "SieveScript": "urn:ietf:params:jmap:sieve",
    "VacationResponse": "urn:ietf:params:jmap:vacationresponse",
}

EMAIL_PROPERTIES = (
    "id", "blobId", "threadId", "mailboxIds", "keywords", "receivedAt", "size",
    "messageId",
)  # fmt: skip
MAILBOX_PROPERTIES = (
    "id", "name", "parentId", "role", "sortOrder", "totalEmails", "unreadEmails",
    "totalThreads", "unreadThreads", "myRights", "isSubscribed",
)  # fmt: skip

type Json = dict[str, Any]


@dataclass
class FakeEmail:
    id: str
    blob: bytes
    mailboxes: set[str]
    keywords: set[str]
    received: datetime
    thread: str
    blob_id: str = ""

    def __post_init__(self) -> None:
        self.blob_id = "G" + hashlib.sha256(self.blob).hexdigest()[:40]

    def json(self) -> Json:
        headers = BytesHeaderParser().parsebytes(self.blob)
        mid = headers.get("Message-ID")
        return {
            "id": self.id,
            "blobId": self.blob_id,
            "threadId": self.thread,
            "mailboxIds": dict.fromkeys(sorted(self.mailboxes), True),
            "keywords": dict.fromkeys(sorted(self.keywords), True),
            "receivedAt": self.received.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size": len(self.blob),
            "messageId": [mid.strip().strip("<>")] if mid else None,
        }


@dataclass
class _Log:
    """Changes of one data type: entry ``n`` moved the state from ``n`` to
    ``n + 1``."""

    entries: list[tuple[str, str]] = field(default_factory=list)  # (kind, id)
    #: Oldest state ``*/changes`` can start from.
    floor: int = 0

    @property
    def state(self) -> int:
        return len(self.entries)


class FakeJmap:
    """The account and the server's knobs. Thread-safe."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mailboxes: dict[str, Json] = {}
        self.emails: dict[str, FakeEmail] = {}
        #: Settings objects by type and id; Sieve scripts' blobs by id.
        self.settings: dict[str, dict[str, Json]] = {t: {} for t in SETTINGS}
        self.sieve_blobs: dict[str, bytes] = {}
        self.logs = {"Mailbox": _Log(), "Email": _Log()}
        self.methods: list[str] = []
        self.downloads: list[str] = []
        self.get_sizes: list[int] = []
        self.requests = 0
        self._next = 0
        # Knobs.
        self.max_objects_in_get = 500
        self.max_calls_in_request = 16
        #: Largest ``maxChanges`` the server applies, whatever is asked.
        self.max_changes = 1000
        #: Largest ``Email/query`` page.
        self.query_limit = 1000
        self.extra_capabilities: dict[str, Json] = {}
        self.session_overrides: Json = {}
        #: Called (unlocked) before each ``Email/query`` is answered.
        self.before_query: Callable[[Json], None] | None = None
        #: Email ids whose download is one byte short.
        self.truncate: set[str] = set()
        #: Email and Sieve script ids whose download fails (HTTP 500).
        self.fail: set[str] = set()
        #: Method names answered with a ``forbidden`` error.
        self.forbidden: set[str] = set()
        self.url = ""

    def _id(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}{self._next:04d}"

    # --- changing the account ---------------------------------------------

    def add_mailbox(self, name: str, role: str | None = None,
                    parent: str | None = None, **extra: Any) -> str:  # fmt: skip
        with self.lock:
            id_ = self._id("M")
            self.mailboxes[id_] = {
                "id": id_, "name": name, "parentId": parent, "role": role,
                "sortOrder": 10, "totalEmails": 0, "unreadEmails": 0,
                "totalThreads": 0, "unreadThreads": 0, "isSubscribed": True,
                "myRights": {"mayReadItems": True},
            } | extra  # fmt: skip
            self.logs["Mailbox"].entries.append(("created", id_))
            return id_

    def update_mailbox(self, id_: str, **changes: Any) -> None:
        with self.lock:
            self.mailboxes[id_] |= changes
            self.logs["Mailbox"].entries.append(("updated", id_))

    def destroy_mailbox(self, id_: str) -> None:
        with self.lock:
            del self.mailboxes[id_]
            self.logs["Mailbox"].entries.append(("destroyed", id_))

    def add_email(self, blob: bytes, mailboxes: set[str],
                  keywords: set[str] | frozenset[str] = frozenset(),
                  received: datetime | None = None) -> str:  # fmt: skip
        with self.lock:
            id_ = self._id("E")
            when = received or datetime(2026, 1, 1, tzinfo=UTC)
            self.emails[id_] = FakeEmail(
                id_, blob, set(mailboxes), set(keywords), when, "T" + id_[1:]
            )
            self.logs["Email"].entries.append(("created", id_))
            return id_

    def update_email(self, id_: str, *, mailboxes: set[str] | None = None,
                     keywords: set[str] | None = None) -> None:  # fmt: skip
        with self.lock:
            e = self.emails[id_]
            if mailboxes is not None:
                e.mailboxes = set(mailboxes)
            if keywords is not None:
                e.keywords = set(keywords)
            self.logs["Email"].entries.append(("updated", id_))

    def destroy_email(self, id_: str) -> None:
        with self.lock:
            del self.emails[id_]
            self.logs["Email"].entries.append(("destroyed", id_))

    def enable_settings(self, *types: str) -> None:
        """Give the account the capabilities of settings types (all by
        default)."""
        for type_ in types or SETTINGS:
            self.extra_capabilities[SETTINGS[type_]] = {}

    def set_setting(self, type_: str, id_: str | None = None, **props: Any) -> str:
        """Create a settings object, or replace one's properties."""
        with self.lock:
            id_ = id_ or self._id(type_[0])
            self.settings[type_][id_] = {"id": id_} | props
            return id_

    def destroy_setting(self, type_: str, id_: str) -> None:
        with self.lock:
            del self.settings[type_][id_]
            self.sieve_blobs.pop(id_, None)

    def set_sieve(self, content: bytes, id_: str | None = None,
                  name: str = "script", active: bool = True) -> str:  # fmt: skip
        """Create a Sieve script, or replace one's text (a new blob)."""
        blob_id = "S" + hashlib.sha256(content).hexdigest()[:40]
        id_ = self.set_setting(
            "SieveScript", id_, name=name, blobId=blob_id, isActive=active
        )
        with self.lock:
            self.sieve_blobs[id_] = content
        return id_

    def forget_changes(self) -> None:
        """Make ``*/changes`` from any earlier state fail with
        ``cannotCalculateChanges``."""
        with self.lock:
            for log in self.logs.values():
                log.floor = log.state

    # --- the protocol -------------------------------------------------------

    def session(self) -> Json:
        base = self.url.rsplit("/", 1)[0]
        return {
            "capabilities": {
                CORE: {
                    "maxSizeUpload": 50_000_000, "maxConcurrentUpload": 4,
                    "maxSizeRequest": 10_000_000, "maxConcurrentRequests": 4,
                    "maxCallsInRequest": self.max_calls_in_request,
                    "maxObjectsInGet": self.max_objects_in_get,
                    "maxObjectsInSet": 500, "collationAlgorithms": [],
                },
                MAIL: {},
            } | self.extra_capabilities,
            "accounts": {
                ACCOUNT: {
                    "name": "user@example.org", "isPersonal": True,
                    "isReadOnly": False,
                    "accountCapabilities": {MAIL: {}} | self.extra_capabilities,
                }
            },
            "primaryAccounts": {MAIL: ACCOUNT},
            "username": "user@example.org",
            "apiUrl": f"{base}/api",
            "downloadUrl": base + "/download/{accountId}/{blobId}/{name}?type={type}",
            "uploadUrl": f"{base}/upload/{{accountId}}",
            "eventSourceUrl": f"{base}/events",
            "state": "S1",
        } | self.session_overrides  # fmt: skip

    def api(self, request: Json) -> Json:
        responses = []
        using = request.get("using") or []
        for name, args, call_id in request["methodCalls"]:
            self.methods.append(name)
            if name == "Email/query" and self.before_query:
                self.before_query(args)
            with self.lock:
                try:
                    type_ = name.split("/")[0]
                    capability = SETTINGS.get(type_, MAIL)
                    if capability not in using or (
                        type_ in SETTINGS and capability not in self.extra_capabilities
                    ):
                        raise _Error("unknownMethod")
                    if name in self.forbidden:
                        raise _Error("forbidden")
                    if args.get("accountId") != ACCOUNT:
                        raise _Error("accountNotFound")
                    result = self._method(name, args)
                    responses.append([name, result, call_id])
                except _Error as e:
                    responses.append(["error", {"type": e.args[0]}, call_id])
        return {"methodResponses": responses, "sessionState": "S1"}

    def _method(self, name: str, args: Json) -> Json:
        match name:
            case "Mailbox/get":
                return self._get("Mailbox", self.mailboxes, args, MAILBOX_PROPERTIES)
            case "Email/get":
                wanted = self.emails if args.get("ids") is None else args["ids"]
                objects = {k: self.emails[k].json() for k in wanted if k in self.emails}
                return self._get("Email", objects, args, EMAIL_PROPERTIES)
            case "Mailbox/changes" | "Email/changes":
                return self._changes(name.split("/")[0], args)
            case "Email/query":
                return self._query(args)
            case "Identity/get" | "SieveScript/get" | "VacationResponse/get":
                type_ = name.split("/")[0]
                return self._get(type_, self.settings[type_], args, None)
        raise _Error("unknownMethod")

    def _get(self, type_: str, objects: dict[str, Json], args: Json,
             known: tuple[str, ...] | None) -> Json:  # fmt: skip
        ids = args.get("ids")
        props = args.get("properties")
        if (
            props is not None
            and known is not None
            and any(p not in known for p in props)
        ):
            raise _Error("invalidArguments")
        if ids is None:
            ids = list(objects)
        if len(ids) > self.max_objects_in_get:
            raise _Error("requestTooLarge")
        self.get_sizes.append(len(ids))
        found = [objects[i] for i in ids if i in objects]
        if props is not None:
            found = [{p: o[p] for p in o if p == "id" or p in props} for o in found]
        return {
            "accountId": ACCOUNT,
            # Settings keep no change log: their state is unused.
            "state": str(self.logs[type_].state) if type_ in self.logs else "0",
            "list": found,
            "notFound": [i for i in ids if i not in objects],
        }

    def _changes(self, type_: str, args: Json) -> Json:
        log = self.logs[type_]
        try:
            since = int(args["sinceState"])
        except KeyError:
            raise _Error("invalidArguments") from None
        except ValueError:
            raise _Error("cannotCalculateChanges") from None
        if not log.floor <= since <= log.state:
            raise _Error("cannotCalculateChanges")
        limit = min(args.get("maxChanges") or self.max_changes, self.max_changes)
        end, touched = since, set[str]()
        while end < log.state and len(touched | {log.entries[end][1]}) <= limit:
            touched.add(log.entries[end][1])
            end += 1
        kinds: dict[str, list[str]] = {}
        for kind, id_ in log.entries[since:end]:
            kinds.setdefault(id_, []).append(kind)
        created, updated, destroyed = [], [], []
        for id_, ks in kinds.items():
            if "destroyed" in ks:
                if "created" not in ks:
                    destroyed.append(id_)
            elif "created" in ks:
                created.append(id_)
            else:
                updated.append(id_)
        return {
            "accountId": ACCOUNT, "oldState": str(since), "newState": str(end),
            "hasMoreChanges": end < log.state, "created": created,
            "updated": updated, "destroyed": destroyed,
        }  # fmt: skip

    def _query(self, args: Json) -> Json:
        if args.get("filter") or args.get("collapseThreads"):
            raise _Error("unsupportedFilter")
        sort = args.get("sort") or []
        if sort not in ([], [{"property": "receivedAt", "isAscending": True}]):
            raise _Error("unsupportedSort")
        ordered = sorted(self.emails.values(), key=lambda e: (e.received, e.id))
        ids = [e.id for e in ordered]
        limit = min(args.get("limit") or self.query_limit, self.query_limit)
        if "anchor" in args:
            if args["anchor"] not in ids:
                raise _Error("anchorNotFound")
            start = max(0, ids.index(args["anchor"]) + args.get("anchorOffset", 0))
        else:
            start = args.get("position", 0)
        return {
            "accountId": ACCOUNT, "queryState": str(self.logs["Email"].state),
            "canCalculateChanges": False, "position": start,
            "ids": ids[start : start + limit],
        }  # fmt: skip

    def blob(self, blob_id: str) -> bytes | None:
        """The bytes of a blob; None if unknown. Raises ``_Error`` for one
        set to fail."""
        with self.lock:
            for e in self.emails.values():
                if e.blob_id == blob_id:
                    self.downloads.append(blob_id)
                    if e.id in self.fail:
                        raise _Error("serverFail")
                    return e.blob[:-1] if e.id in self.truncate else e.blob
            for id_, script in self.settings["SieveScript"].items():
                if script["blobId"] == blob_id and id_ in self.sieve_blobs:
                    self.downloads.append(blob_id)
                    if id_ in self.fail:
                        raise _Error("serverFail")
                    return self.sieve_blobs[id_]
        return None


class _Error(Exception):
    pass


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _authorized(self) -> bool:
        self.server.fake.requests += 1
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        self._send(401, b'{"type": "about:blank", "status": 401}')
        return False

    def _send(self, code: int, body: bytes, type_: str = "application/json",
              headers: dict[str, str] | None = None) -> None:  # fmt: skip
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        fake = self.server.fake
        path = urlsplit(self.path).path
        if path == "/redirect":
            self._send(302, b"", headers={"Location": "http://127.0.0.1:1/x"})
            return
        if not self._authorized():
            return
        if path == "/jmap/session":
            self._send(200, json.dumps(fake.session()).encode())
            return
        parts = path.split("/")
        if len(parts) == 6 and parts[2] == "download" and parts[3] == ACCOUNT:
            try:
                blob = fake.blob(unquote(parts[4]))
            except _Error:
                self._send(500, b'{"type": "about:blank", "status": 500}')
                return
            if blob is not None:
                self._send(200, blob, "message/rfc822")
                return
        self._send(404, b'{"type": "about:blank", "status": 404}')

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if urlsplit(self.path).path != "/jmap/api":
            self._send(404, b"{}")
            return
        body = self.rfile.read(int(self.headers["Content-Length"]))
        try:
            request = json.loads(body)
            if not isinstance(request.get("methodCalls"), list):
                raise ValueError
        except ValueError:
            problem = {"type": "urn:ietf:params:jmap:error:notRequest",
                       "status": 400, "detail": "not a request"}  # fmt: skip
            self._send(400, json.dumps(problem).encode())
            return
        self._send(200, json.dumps(self.server.fake.api(request)).encode())


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, fake: FakeJmap) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.fake = fake


@contextmanager
def fake_jmap() -> Iterator[FakeJmap]:
    """A fake server on loopback; ``.url`` is its session URL."""
    fake = FakeJmap()
    server = _Server(fake)
    fake.url = f"http://127.0.0.1:{server.server_address[1]}/jmap/session"
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
