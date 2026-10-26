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

JMAP objects are returned as the server sent them (``dict``s of JSON
values), since the journal keeps unknown properties verbatim.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import quote

from geniza.config import secure_url

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

#: Bytes read at a time from a download.
CHUNK = 1 << 20

#: Longest server text quoted in an error.
MAX_DETAIL = 200

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


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 1] + "…"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn a redirect into an ``HTTPError``, so the token is not sent to a
    URL the configuration or session did not name."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


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
    """A connection to one JMAP account. Not thread-safe."""

    session: Session

    def __init__(self, token: str, *, timeout: float) -> None:
        self._token = token
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self) -> str:
        return f"Client(account={self.session.account_id!r})"

    @classmethod
    def connect(cls, url: str, token: str, *, timeout: float = 60.0) -> Self:
        """Fetch the session at ``url`` (the configured session URL)."""
        if not secure_url(url):
            raise JmapError(f"insecure URL {url!r}")
        client = cls(token, timeout=timeout)
        client.session = Session.parse(client._json(client._open(url)))
        return client

    # --- HTTP -------------------------------------------------------------

    def _open(self, url: str, body: bytes | None = None) -> Any:
        request = urllib.request.Request(url, data=body)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/json")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            return self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as e:
            raise self._http_error(url, e) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise JmapError(f"{url}: {reason}") from None

    @staticmethod
    def _http_error(url: str, e: urllib.error.HTTPError) -> JmapError:
        if 300 <= e.code < 400:
            target = e.headers.get("Location", "?")
            return JmapError(f"{url}: redirected to {target!r}; not followed")
        if e.code in (401, 403):
            return JmapError(f"{url}: HTTP {e.code}, authentication failed")
        detail = ""
        try:
            problem = json.loads(e.read(1 << 16))
            if isinstance(problem, dict):
                parts = [problem.get("type"), problem.get("detail")]
                detail = ": " + _short(" ".join(p for p in parts if isinstance(p, str)))
        except OSError, ValueError:
            pass
        return JmapError(f"{url}: HTTP {e.code}{detail}")

    @staticmethod
    def _json(response: Any) -> Any:
        with response:
            try:
                return json.loads(response.read())
            except (OSError, ValueError) as e:
                raise JmapError(f"{response.url}: bad JSON reply ({e})") from None

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
        reply = self._json(self._open(self.session.api_url, json.dumps(body).encode()))
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
        chunks = []
        with self._open(url) as response:
            try:
                while chunk := response.read(CHUNK):
                    chunks.append(chunk)
            except OSError as e:
                raise JmapError(f"download of {blob_id}: {e}") from None
        return b"".join(chunks)


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
