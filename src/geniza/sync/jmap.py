"""A read-only JMAP client (RFC 8620, RFC 8621): what sync needs and
nothing else (DESIGN.md, "JMAP").

``Client.connect`` fetches the session object and picks the primary mail
account; every limit (``maxObjectsInGet``, ``maxCallsInRequest``) comes
from the session, never from here. Methods are allow-listed by suffix
(``/get``, ``/changes``, ``/query``, ``/queryChanges``), so no code path
can send ``*/set``, ``*/copy`` or ``Email/import``: geniza never modifies
the server.

The token is sent as a Bearer token, only to https URLs (or http to a
loopback host), and redirects are refused rather than followed, so it
never goes anywhere the session did not name. It is never put in an
exception or a log line.

Connections are kept open and reused (HTTP/1.1 keep-alive): a new one
costs round trips for TCP and TLS, and starts slow. ``downloads`` fetches
blobs over several connections at once, since a far server's latency,
not its bandwidth, is what limits one download after another.

A request answered with HTTP 429, 502, 503 or 504, or cut off by a
network error (a connection refused, reset or timed out, a reply cut
short), is sent again after a pause (``Retry-After`` when the server
gives one, else a doubling backoff), up to ``RETRIES`` times. Every
request only reads, so sending one twice is harmless.

JMAP objects are returned as the server sent them (``dict``s of JSON
values), since the journal keeps unknown properties verbatim.

Retries are logged at ``-v``; method calls with their time, and new
connections, at ``-vv``.
"""

import http.client
import json
import logging
import ssl
import threading
import time
from collections import deque
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Self
from urllib.parse import SplitResult, quote, urlsplit

from geniza.config import secure_url

log = logging.getLogger(__name__)

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"

#: Method name suffixes that only read. Anything else is refused before
#: a request is made.
READ_ONLY = ("/get", "/changes", "/query", "/queryChanges")

#: Capability each data type needs in ``using``.
CAPABILITY = {
    "Mailbox": MAIL,
    "Email": MAIL,
    "Thread": MAIL,
    "Identity": "urn:ietf:params:jmap:submission",
    "SieveScript": "urn:ietf:params:jmap:sieve",
    "VacationResponse": "urn:ietf:params:jmap:vacationresponse",
}

#: Ids asked for per ``Email/query`` page; a server may return fewer.
QUERY_PAGE = 4096

#: Bytes (by the sizes the server gave) that ``downloads`` fetches ahead
#: of the one it is waiting for, so its memory stays bounded.
IN_FLIGHT = 64 << 20

#: Longest server text quoted in an error.
MAX_DETAIL = 200

#: HTTP statuses that mean "try again later": rate limiting, and a proxy
#: or server that is briefly unavailable.
TRANSIENT = frozenset({429, 502, 503, 504})

#: Times a request is sent again after a transient status or a network
#: error.
RETRIES = 5

#: First pause without ``Retry-After``, in seconds; it doubles each time.
BACKOFF = 1.0

#: Longest pause, in seconds. A ``Retry-After`` asking for more is an
#: error rather than a pause.
MAX_WAIT = 300.0

type Json = dict[str, Any]


class JmapError(Exception):
    """The server could not be reached, refused a request, or sent
    something that is not JMAP. Messages never contain the token."""


class MethodError(JmapError):
    """A method call returned an ``error`` response (RFC 8620, 3.6.2)."""

    def __init__(self, method: str, type_: str, description: str | None) -> None:
        detail = f": {_short(description)}" if description else ""
        super().__init__(f"{method}: {type_}{detail}")
        self.method = method
        self.type = type_


class CannotCalculateChanges(MethodError):
    """``*/changes`` cannot go back to the given state; the caller falls
    back to a full listing."""


class _Retry(Exception):
    """A request to send again: what went wrong, and the pause the server
    asked for (None: back off)."""

    def __init__(self, problem: str, wait: float | None) -> None:
        self.problem = problem
        self.wait = wait


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 1] + "…"


class _Pool:
    """Idle connections by origin, kept for reuse. Thread-safe; each
    connection is used by one thread at a time."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._context = ssl.create_default_context()
        self._lock = threading.Lock()
        self._idle: dict[tuple[str, str], list[http.client.HTTPConnection]] = {}

    def take(self, url: SplitResult) -> tuple[http.client.HTTPConnection, bool]:
        """An idle connection to the origin of ``url``, or a new one; and
        whether it was used before."""
        with self._lock:
            idle = self._idle.get((url.scheme, url.netloc))
            if idle:
                return idle.pop(), True
        log.debug("connecting to %s", url.netloc)
        if url.scheme == "https":
            return http.client.HTTPSConnection(
                url.netloc, timeout=self._timeout, context=self._context
            ), False
        return http.client.HTTPConnection(url.netloc, timeout=self._timeout), False

    def give(self, url: SplitResult, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            self._idle.setdefault((url.scheme, url.netloc), []).append(conn)

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, {}
        for conns in idle.values():
            for conn in conns:
                conn.close()


@dataclass(frozen=True)
class Session:
    """The parts of the session object (RFC 8620, 2) that sync uses."""

    api_url: str
    download_url: str
    account_id: str
    #: Capabilities of the account (``accountCapabilities``), by URI.
    capabilities: Mapping[str, Json]
    max_objects_in_get: int
    max_calls_in_request: int
    state: str

    @classmethod
    def parse(cls, data: Any) -> Self:
        try:
            core = data["capabilities"][CORE]
            account_id = data["primaryAccounts"][MAIL]
            account = data["accounts"][account_id]
            session = cls(
                api_url=data["apiUrl"],
                download_url=data["downloadUrl"],
                account_id=account_id,
                capabilities=dict(account["accountCapabilities"]),
                max_objects_in_get=core["maxObjectsInGet"],
                max_calls_in_request=core["maxCallsInRequest"],
                state=data["state"],
            )
        except (KeyError, TypeError) as e:
            raise JmapError(f"bad session object (no {e})") from None
        for name in ("api_url", "download_url", "account_id", "state"):
            if not isinstance(getattr(session, name), str):
                raise JmapError(f"bad session object ({name} is not a string)")
        for name in ("max_objects_in_get", "max_calls_in_request"):
            value = getattr(session, name)
            if type(value) is not int or value < 1:
                raise JmapError(f"bad session object ({name} is {value!r})")
        for url in (session.api_url, session.download_url):
            if not secure_url(url):
                raise JmapError(f"session names an insecure URL {url!r}")
        if MAIL not in session.capabilities:
            raise JmapError(f"account {account_id!r} has no mail capability")
        return session

    def has(self, type_: str) -> bool:
        """Whether the account offers the capability of a data type."""
        return CAPABILITY[type_] in self.capabilities


@dataclass(frozen=True)
class Got:
    """The result of a ``*/get``, across batches."""

    state: str
    objects: list[Json]
    not_found: list[str]


@dataclass
class Changes:
    """The result of ``*/changes`` from ``old_state`` to ``new_state``,
    across pages. An id is in at most one list; one created and destroyed
    in between may be in ``destroyed`` or in none (RFC 8620, 5.2)."""

    old_state: str
    new_state: str
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    destroyed: list[str] = field(default_factory=list)


class Client:
    """A connection to one JMAP account. Thread-safe."""

    session: Session

    def __init__(
        self,
        token: str,
        *,
        timeout: float,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token
        self._sleep = sleep
        self._pool = _Pool(timeout)

    def __repr__(self) -> str:
        return f"Client(account={self.session.account_id!r})"

    @classmethod
    def connect(
        cls,
        url: str,
        token: str,
        *,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Self:
        """Fetch the session at ``url`` (the configured session URL).
        ``sleep`` makes the pauses between retries."""
        if not secure_url(url):
            raise JmapError(f"insecure URL {url!r}")
        client = cls(token, timeout=timeout, sleep=sleep)
        try:
            client.session = Session.parse(client._json(url))
        except BaseException:
            client.close()
            raise
        return client

    def close(self) -> None:
        """Close the idle connections."""
        self._pool.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- HTTP -------------------------------------------------------------

    def _fetch(self, url: str, body: bytes | None = None) -> bytes:
        """The body of a successful (2xx) response to a GET, or to a POST
        of ``body``. Transient statuses and network errors are retried."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise JmapError(f"{url}: not an http URL")
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        backoff = BACKOFF
        attempt = 0
        while True:
            try:
                return self._attempt(
                    url, parts, target, body, headers, last=attempt == RETRIES
                )
            except _Retry as r:
                problem, wait = r.problem, r.wait
            if wait is None:
                wait, backoff = backoff, backoff * 2
            attempt += 1
            log.info(
                "%s: %s; trying again in %.1f s (%d of %d)",
                url, problem, wait, attempt, RETRIES,
            )  # fmt: skip
            self._sleep(wait)

    def _attempt(
        self,
        url: str,
        parts: SplitResult,
        target: str,
        body: bytes | None,
        headers: dict[str, str],
        *,
        last: bool,
    ) -> bytes:
        """Send a request once and read its reply. Raises ``_Retry`` for a
        transient status or a network error, unless this is the ``last``
        attempt. The connection goes back to the pool if the reply was
        read to the end."""
        try:
            conn, response = self._send(parts, target, body, headers)
            if 200 <= response.status < 300:
                try:
                    data = response.read()
                except BaseException:
                    conn.close()
                    raise
                if response.will_close:
                    conn.close()
                else:
                    self._pool.give(parts, conn)
                return data
        except (http.client.HTTPException, OSError) as e:
            problem = str(e) or type(e).__name__
            # A certificate that does not verify will not verify next time.
            if last or isinstance(e, ssl.SSLCertVerificationError):
                raise JmapError(f"{url}: {problem}") from None
            raise _Retry(problem, None) from None
        try:
            if response.status not in TRANSIENT or last:
                raise self._http_error(url, response)
            wait = _retry_after(response.getheader("Retry-After"))
        finally:
            conn.close()
        if wait is not None and wait > MAX_WAIT:
            raise JmapError(
                f"{url}: HTTP {response.status}, retry after {wait:.0f} s;"
                " not waiting that long"
            )
        raise _Retry(f"HTTP {response.status}", wait)

    def _send(
        self,
        parts: SplitResult,
        target: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        """Send a request and read the reply's status and headers. A kept
        connection the server closed while it was idle is replaced by a new
        one at once; other errors are raised (the connection closed)."""
        while True:
            conn, reused = self._pool.take(parts)
            try:
                conn.request("GET" if body is None else "POST", target, body, headers)
                return conn, conn.getresponse()
            except (http.client.HTTPException, OSError) as e:
                conn.close()
                if reused and isinstance(e, ConnectionError):
                    continue
                raise

    @staticmethod
    def _http_error(url: str, response: http.client.HTTPResponse) -> JmapError:
        code = response.status
        if 300 <= code < 400:
            target = response.getheader("Location", "?")
            return JmapError(f"{url}: redirected to {target!r}; not followed")
        if code in (401, 403):
            return JmapError(f"{url}: HTTP {code}, authentication failed")
        detail = ""
        try:
            problem = json.loads(response.read(1 << 16))
            if isinstance(problem, dict):
                parts = [problem.get("type"), problem.get("detail")]
                detail = ": " + _short(" ".join(p for p in parts if isinstance(p, str)))
        except http.client.HTTPException, OSError, ValueError:
            pass
        return JmapError(f"{url}: HTTP {code}{detail}")

    def _json(self, url: str, body: bytes | None = None) -> Any:
        try:
            return json.loads(self._fetch(url, body))
        except ValueError as e:
            raise JmapError(f"{url}: bad JSON reply ({e})") from None

    # --- method calls -----------------------------------------------------

    def call(self, calls: Sequence[tuple[str, Json]]) -> list[Json]:
        """Make method calls in one request and return their arguments, in
        order. An ``error`` response raises ``MethodError`` (or
        ``CannotCalculateChanges``)."""
        using = {CORE}
        for method, _ in calls:
            if not method.endswith(READ_ONLY):
                raise JmapError(f"{method}: not a read-only method; refused")
            type_ = method.split("/", 1)[0]
            if type_ not in CAPABILITY:
                raise JmapError(f"{method}: unknown data type")
            using.add(CAPABILITY[type_])
        if len(calls) > self.session.max_calls_in_request:
            raise JmapError(f"{len(calls)} calls in one request; too many")
        body = {
            "using": sorted(using),
            "methodCalls": [[m, args, str(i)] for i, (m, args) in enumerate(calls)],
        }
        start = time.monotonic()
        reply = self._json(self.session.api_url, json.dumps(body).encode())
        log.debug(
            "%s: %.2f s",
            ", ".join(m for m, _ in calls), time.monotonic() - start,
        )  # fmt: skip
        try:
            responses = reply["methodResponses"]
            by_id = {r[2]: (r[0], r[1]) for r in responses}
        except KeyError, TypeError, IndexError:
            raise JmapError("bad API reply (no methodResponses)") from None
        results = []
        for i, (method, _) in enumerate(calls):
            name, args = by_id.get(str(i), (None, None))
            if not isinstance(args, dict):
                raise JmapError(f"{method}: no response")
            if name == "error":
                kind = args.get("type")
                text = args.get("description")
                if not isinstance(kind, str):
                    raise JmapError(f"{method}: bad error response")
                cls = (
                    CannotCalculateChanges
                    if kind == "cannotCalculateChanges"
                    else MethodError
                )
                raise cls(method, kind, text if isinstance(text, str) else None)
            if name != method:
                raise JmapError(f"{method}: answered by {name!r}")
            results.append(args)
        return results

    def _one(self, method: str, args: Json) -> Json:
        return self.call([(method, {"accountId": self.session.account_id} | args)])[0]

    def get(
        self,
        type_: str,
        ids: Sequence[str] | None = None,
        properties: Iterable[str] | None = None,
    ) -> Got:
        """``Foo/get`` of ``ids`` (all objects when ``None``), in batches of
        ``maxObjectsInGet`` ids, up to ``maxCallsInRequest`` per request.
        The state is that of the last batch."""
        props = {} if properties is None else {"properties": list(properties)}
        account = {"accountId": self.session.account_id}
        if ids is None:
            batches: list[Json] = [account | props | {"ids": None}]
        else:
            n = self.session.max_objects_in_get
            batches = [
                account | props | {"ids": list(ids[i : i + n])}
                for i in range(0, len(ids), n)
            ] or [account | props | {"ids": []}]
        objects: list[Json] = []
        not_found: list[str] = []
        state = ""
        per = self.session.max_calls_in_request
        for i in range(0, len(batches), per):
            calls = [(f"{type_}/get", b) for b in batches[i : i + per]]
            for result in self.call(calls):
                objects.extend(_list(result, "list", dict))
                not_found.extend(_list(result, "notFound", str, optional=True))
                state = _str(result, "state")
        return Got(state, objects, not_found)

    def changes(
        self, type_: str, since: str, *, max_changes: int | None = None
    ) -> Changes:
        """``Foo/changes`` from ``since`` to the current state, following
        ``hasMoreChanges``. Raises ``CannotCalculateChanges`` when the
        server cannot go back that far."""
        out = Changes(since, since)
        created: dict[str, None] = {}
        updated: dict[str, None] = {}
        destroyed: dict[str, None] = {}
        while True:
            args: Json = {"sinceState": out.new_state}
            if max_changes is not None:
                args["maxChanges"] = max_changes
            result = self._one(f"{type_}/changes", args)
            for id_ in _list(result, "created", str):
                created[id_] = None
            for id_ in _list(result, "updated", str):
                if id_ not in created:
                    updated[id_] = None
            for id_ in _list(result, "destroyed", str):
                created.pop(id_, None)
                updated.pop(id_, None)
                destroyed[id_] = None
            new_state = _str(result, "newState")
            more = result.get("hasMoreChanges")
            if not isinstance(more, bool):
                raise JmapError(f"{type_}/changes: bad hasMoreChanges")
            if more and new_state == out.new_state:
                raise JmapError(f"{type_}/changes: no progress")
            out.new_state = new_state
            if not more:
                break
        out.created = list(created)
        out.updated = list(updated)
        out.destroyed = list(destroyed)
        return out

    def query_emails(self) -> tuple[str, list[str]]:
        """Every email id of the account, oldest first, and an ``Email``
        state from before the listing: ``Email/changes`` from it covers
        whatever changed while the listing ran.

        Pages are anchored at the last id of the previous page, so a
        message destroyed meanwhile does not shift the rest of the listing
        and skip one (``receivedAt`` never changes, so neither does the
        order). If the anchor itself is destroyed, the listing restarts."""
        while True:
            state = self.get("Email", []).state
            try:
                return state, self._query_pages()
            except MethodError as e:
                if e.type != "anchorNotFound":
                    raise

    def _query_pages(self) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        args: Json = {
            "sort": [{"property": "receivedAt", "isAscending": True}],
            "collapseThreads": False,
            "position": 0,
            "limit": QUERY_PAGE,
        }
        while True:
            page = _list(self._one("Email/query", args), "ids", str)
            if not page:
                return ids
            for id_ in page:
                if id_ in seen:
                    raise JmapError("Email/query: an id listed twice")
                seen.add(id_)
                ids.append(id_)
            args = args | {"anchor": page[-1], "anchorOffset": 1}
            args.pop("position", None)

    # --- blobs ------------------------------------------------------------

    def download(self, blob_id: str, *, name: str = "message.eml",
                 type_: str = "message/rfc822") -> bytes:  # fmt: skip
        """The bytes of a blob, exactly as the server has them."""
        url = self.session.download_url
        for var, value in (
            ("accountId", self.session.account_id),
            ("blobId", blob_id),
            ("name", name),
            ("type", type_),
        ):
            url = url.replace("{" + var + "}", quote(value, safe=""))
        if not secure_url(url):
            raise JmapError(f"insecure download URL {url!r}")
        return self._fetch(url)

    def downloads(
        self, blobs: Iterable[tuple[str, int]], *, connections: int
    ) -> Generator[bytes | JmapError]:
        """Download emails given as ``(blobId, size)``, up to
        ``connections`` at a time, yielding in order each one's bytes or the
        error that stopped it. Downloads run ahead of the one yielded next
        by up to ``IN_FLIGHT`` bytes of the given sizes. Closing the
        iterator early cancels those not started."""
        pool = ThreadPoolExecutor(connections, thread_name_prefix="jmap-download")
        pending: deque[tuple[int, Future[bytes]]] = deque()
        ahead = 0
        items = iter(blobs)
        try:
            item = next(items, None)
            while item is not None or pending:
                while item is not None and (
                    not pending or ahead + item[1] <= IN_FLIGHT
                ):
                    blob_id, size = item
                    pending.append((size, pool.submit(self.download, blob_id)))
                    ahead += size
                    item = next(items, None)
                size, future = pending.popleft()
                ahead -= size
                try:
                    data: bytes | JmapError = future.result()
                except JmapError as e:
                    data = e
                yield data
        finally:
            pool.shutdown(cancel_futures=True)


def _retry_after(value: str | None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delay-seconds or an
    HTTP date, RFC 9110, 10.2.3); None when absent or unreadable."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _list[T](
    result: Json, key: str, item: type[T], *, optional: bool = False
) -> list[T]:
    value = result.get(key)
    if value is None and optional:
        return []
    if not isinstance(value, list) or not all(isinstance(v, item) for v in value):
        raise JmapError(f"bad {key!r} in a method response")
    return value


def _str(result: Json, key: str) -> str:
    value = result.get(key)
    if not isinstance(value, str):
        raise JmapError(f"bad {key!r} in a method response")
    return value
