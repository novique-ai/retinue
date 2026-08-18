"""JMAP inbox read — list + read, token from env, no send path (issue #130)."""

from __future__ import annotations

import pytest

from . import jmap, jmap_tools

ACCOUNT_ID = "acct-1"
API_URL = "https://example.invalid/jmap/api/"
INBOX_ID = "mb-inbox"

SESSION = {
    "apiUrl": API_URL,
    "primaryAccounts": {"urn:ietf:params:jmap:mail": ACCOUNT_ID},
}


class FakeTransport:
    """Records every JMAP request instead of touching the network."""

    def __init__(self, posts=None, session=None):
        self.session = session if session is not None else SESSION
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[dict] = []
        self._responses = list(posts or [])

    def get_json(self, url, headers, timeout):
        self.gets.append((url, dict(headers)))
        return self.session

    def post_json(self, url, body, headers, timeout):
        self.posts.append({"url": url, "body": body, "headers": dict(headers)})
        if not self._responses:
            raise AssertionError(f"unexpected JMAP POST: {body}")
        return self._responses.pop(0)

    def install(self, monkeypatch):
        monkeypatch.setattr(jmap, "_http_get_json", self.get_json)
        monkeypatch.setattr(jmap, "_http_post_json", self.post_json)
        return self

    def method_calls(self, index):
        return self._call_names(self.posts[index]["body"]["methodCalls"])

    @staticmethod
    def _call_names(calls):
        return [c[0] for c in calls]

    def call_args(self, index, name):
        for call in self.posts[index]["body"]["methodCalls"]:
            if call[0] == name:
                return call[1]
        raise AssertionError(f"no {name} in POST #{index}")


def _mailbox_query_response(ids=(INBOX_ID,)):
    return {"methodResponses": [["Mailbox/query", {"ids": list(ids)}, "m0"]]}


def _envelope(email_id="em-1", subject="Quarterly numbers"):
    return {
        "id": email_id,
        "threadId": "th-1",
        "subject": subject,
        "from": [{"name": "Ada", "email": "ada@example.invalid"}],
        "to": [{"name": "Me", "email": "me@example.invalid"}],
        "receivedAt": "2026-08-18T09:30:00Z",
        "preview": "Numbers are attached",
        "keywords": {"$seen": True},
        "hasAttachment": True,
        "size": 4096,
    }


def _list_response(emails=None, ids=("em-1",)):
    emails = [_envelope()] if emails is None else emails
    return {
        "methodResponses": [
            ["Email/query", {"ids": list(ids)}, "q0"],
            ["Email/get", {"list": emails}, "g0"],
        ]
    }


def _read_response(email):
    return {"methodResponses": [["Email/get", {"list": [email]}, "g0"]]}


class NoNetwork:
    """Any network call is a test failure."""

    @staticmethod
    def install(monkeypatch):
        def _boom(*_a, **_kw):
            raise AssertionError("network call attempted with no/invalid credentials")

        monkeypatch.setattr(jmap, "_http_get_json", _boom)
        monkeypatch.setattr(jmap, "_http_post_json", _boom)


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv(jmap.TOKEN_ENV, "env-token")
    monkeypatch.delenv(jmap.SESSION_ENV, raising=False)
    return "env-token"


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------


class TestFailClosed:
    def test_missing_token_raises_before_any_network(self, monkeypatch):
        monkeypatch.delenv(jmap.TOKEN_ENV, raising=False)
        NoNetwork.install(monkeypatch)

        with pytest.raises(jmap.JmapError) as exc:
            jmap.list_inbox()
        assert jmap.TOKEN_ENV in str(exc.value)

    def test_blank_token_is_treated_as_missing(self, monkeypatch):
        monkeypatch.setenv(jmap.TOKEN_ENV, "   ")
        NoNetwork.install(monkeypatch)

        with pytest.raises(jmap.JmapError):
            jmap.read_email("em-1")

    def test_tools_report_missing_token_without_network(self, monkeypatch):
        monkeypatch.delenv(jmap.TOKEN_ENV, raising=False)
        NoNetwork.install(monkeypatch)

        for out in (
            jmap_tools.mail_list({}),
            jmap_tools.mail_read({"id": "em-1"}),
        ):
            assert out.startswith("Error:")
            assert jmap.TOKEN_ENV in out

    def test_token_never_appears_in_tool_output(self, monkeypatch):
        monkeypatch.setenv(jmap.TOKEN_ENV, "super-secret-token")
        FakeTransport(posts=[_mailbox_query_response(), _list_response()]).install(monkeypatch)

        out = jmap_tools.mail_list({})
        assert "super-secret-token" not in out


# --------------------------------------------------------------------------
# Credentials come from the environment, never from the model
# --------------------------------------------------------------------------


class TestArgsCannotSupplyCredentials:
    @pytest.mark.parametrize(
        "args",
        [
            {"token": "attacker-token"},
            {"api_token": "attacker-token"},
            {"bearer": "attacker-token"},
            {"account_id": "other-account"},
            {"accountId": "other-account"},
            {"session_url": "https://attacker.invalid/jmap/session"},
        ],
    )
    def test_credential_args_are_rejected_with_no_network(self, monkeypatch, token, args):
        NoNetwork.install(monkeypatch)

        for out in (jmap_tools.mail_list(dict(args)),
                    jmap_tools.mail_read({"id": "em-1", **args})):
            assert out.startswith("Error:")
            assert "environment" in out.lower()
            assert "attacker-token" not in out

    def test_schema_exposes_no_credential_parameters(self):
        for schema in jmap_tools._SCHEMAS.values():
            props = schema["function"]["parameters"].get("properties", {})
            for banned in ("token", "api_token", "bearer", "account_id",
                           "accountId", "session_url", "password"):
                assert banned not in props, f"{schema['function']['name']} exposes {banned}"

    def test_env_token_is_the_bearer_actually_sent(self, monkeypatch, token):
        fake = FakeTransport(posts=[_mailbox_query_response(), _list_response()]).install(monkeypatch)

        jmap.list_inbox()

        assert fake.gets[0][1]["Authorization"] == "Bearer env-token"
        for post in fake.posts:
            assert post["headers"]["Authorization"] == "Bearer env-token"

    def test_account_id_comes_from_the_session(self, monkeypatch, token):
        fake = FakeTransport(posts=[_mailbox_query_response(), _list_response()]).install(monkeypatch)

        jmap.list_inbox()

        assert fake.call_args(0, "Mailbox/query")["accountId"] == ACCOUNT_ID
        assert fake.call_args(1, "Email/query")["accountId"] == ACCOUNT_ID


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


class TestSession:
    def test_default_session_url(self, monkeypatch, token):
        fake = FakeTransport(posts=[_mailbox_query_response(), _list_response()]).install(monkeypatch)

        jmap.list_inbox()

        assert fake.gets[0][0] == jmap.DEFAULT_SESSION_URL

    def test_session_url_override_from_env(self, monkeypatch, token):
        monkeypatch.setenv(jmap.SESSION_ENV, "https://example.invalid/jmap/session")
        fake = FakeTransport(posts=[_mailbox_query_response(), _list_response()]).install(monkeypatch)

        jmap.list_inbox()

        assert fake.gets[0][0] == "https://example.invalid/jmap/session"

    def test_api_url_from_session_is_used_for_posts(self, monkeypatch, token):
        fake = FakeTransport(posts=[_mailbox_query_response(), _list_response()]).install(monkeypatch)

        jmap.list_inbox()

        assert [p["url"] for p in fake.posts] == [API_URL, API_URL]

    def test_session_without_mail_account_fails(self, monkeypatch, token):
        fake = FakeTransport(session={"apiUrl": API_URL, "primaryAccounts": {}})
        fake.install(monkeypatch)

        with pytest.raises(jmap.JmapError):
            jmap.list_inbox()
        assert fake.posts == []


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------


class TestListInbox:
    def test_resolves_inbox_then_queries_and_gets(self, monkeypatch, token):
        fake = FakeTransport(posts=[_mailbox_query_response(), _list_response()]).install(monkeypatch)

        result = jmap.list_inbox(limit=5)

        assert fake.method_calls(0) == ["Mailbox/query"]
        assert fake.call_args(0, "Mailbox/query")["filter"] == {"role": "inbox"}

        assert fake.method_calls(1) == ["Email/query", "Email/get"]
        query = fake.call_args(1, "Email/query")
        assert query["filter"] == {"inMailbox": INBOX_ID}
        assert query["limit"] == 5
        assert query["sort"] == [{"property": "receivedAt", "isAscending": False}]

        get = fake.call_args(1, "Email/get")
        assert get["#ids"] == {"resultOf": "q0", "name": "Email/query", "path": "/ids"}
        assert "bodyValues" not in (get.get("properties") or [])

        assert result["mailbox_id"] == INBOX_ID
        assert [e["id"] for e in result["emails"]] == ["em-1"]
        assert result["emails"][0]["subject"] == "Quarterly numbers"

    def test_uses_the_jmap_mail_capability(self, monkeypatch, token):
        fake = FakeTransport(posts=[_mailbox_query_response(), _list_response()]).install(monkeypatch)

        jmap.list_inbox()

        assert jmap.MAIL_CAPABILITY in fake.posts[0]["body"]["using"]

    def test_limit_is_clamped(self, monkeypatch, token):
        fake = FakeTransport(posts=[_mailbox_query_response(), _list_response()]).install(monkeypatch)

        jmap.list_inbox(limit=10_000)

        assert fake.call_args(1, "Email/query")["limit"] == jmap.MAX_LIST_LIMIT

    def test_missing_inbox_mailbox_fails(self, monkeypatch, token):
        fake = FakeTransport(posts=[_mailbox_query_response(ids=())]).install(monkeypatch)

        with pytest.raises(jmap.JmapError):
            jmap.list_inbox()
        assert len(fake.posts) == 1

    def test_jmap_method_error_is_raised(self, monkeypatch, token):
        error = {"methodResponses": [["error", {"type": "unknownMethod"}, "m0"]]}
        FakeTransport(posts=[error]).install(monkeypatch)

        with pytest.raises(jmap.JmapError) as exc:
            jmap.list_inbox()
        assert "unknownMethod" in str(exc.value)

    def test_tool_renders_envelopes(self, monkeypatch, token):
        FakeTransport(posts=[_mailbox_query_response(), _list_response()]).install(monkeypatch)

        out = jmap_tools.mail_list({})

        assert "Quarterly numbers" in out
        assert "ada@example.invalid" in out
        assert "em-1" in out

    def test_tool_reports_empty_inbox(self, monkeypatch, token):
        FakeTransport(
            posts=[_mailbox_query_response(), _list_response(emails=[], ids=())]
        ).install(monkeypatch)

        assert "no messages" in jmap_tools.mail_list({}).lower()


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


class TestReadEmail:
    def test_fetches_one_id_and_returns_text_body(self, monkeypatch, token):
        email = dict(
            _envelope(),
            textBody=[{"partId": "1", "type": "text/plain"}],
            htmlBody=[{"partId": "2", "type": "text/html"}],
            bodyValues={
                "1": {"value": "Plain text body.", "isTruncated": False},
                "2": {"value": "<p>HTML body.</p>", "isTruncated": False},
            },
        )
        fake = FakeTransport(posts=[_read_response(email)]).install(monkeypatch)

        result = jmap.read_email("em-1")

        assert fake.method_calls(0) == ["Email/get"]
        args = fake.call_args(0, "Email/get")
        assert args["ids"] == ["em-1"]
        assert args["fetchTextBodyValues"] is True
        assert args["maxBodyValueBytes"] == jmap.MAX_BODY_BYTES

        assert result["body"] == "Plain text body."
        assert "HTML body" not in result["body"]
        assert result["subject"] == "Quarterly numbers"

    def test_html_only_body_is_stripped_to_text(self, monkeypatch, token):
        email = dict(
            _envelope(),
            textBody=[],
            htmlBody=[{"partId": "2", "type": "text/html"}],
            bodyValues={
                "2": {
                    "value": "<style>p{color:red}</style><p>Hello&nbsp;&amp; welcome</p>"
                             "<script>evil()</script><div>Second line</div>",
                    "isTruncated": False,
                }
            },
        )
        FakeTransport(posts=[_read_response(email)]).install(monkeypatch)

        body = jmap.read_email("em-1")["body"]

        assert "Hello & welcome" in body
        assert "Second line" in body
        assert "<" not in body
        assert "evil()" not in body
        assert "color:red" not in body

    def test_no_body_parts_yields_placeholder_not_raw_mime(self, monkeypatch, token):
        email = dict(_envelope(), textBody=[], htmlBody=[], bodyValues={})
        FakeTransport(
            posts=[_read_response(email), _read_response(email)]
        ).install(monkeypatch)

        body = jmap.read_email("em-1")["body"]

        assert body == ""
        assert "no readable text body" in jmap_tools.mail_read({"id": "em-1"}).lower()

    def test_truncated_body_is_flagged(self, monkeypatch, token):
        email = dict(
            _envelope(),
            textBody=[{"partId": "1"}],
            bodyValues={"1": {"value": "start of body", "isTruncated": True}},
        )
        FakeTransport(posts=[_read_response(email)]).install(monkeypatch)

        result = jmap.read_email("em-1")

        assert result["truncated"] is True

    def test_unknown_id_fails(self, monkeypatch, token):
        FakeTransport(posts=[{"methodResponses": [["Email/get", {"list": []}, "g0"]]}]).install(monkeypatch)

        with pytest.raises(jmap.JmapError):
            jmap.read_email("nope")

    def test_tool_requires_an_id(self, monkeypatch, token):
        NoNetwork.install(monkeypatch)

        out = jmap_tools.mail_read({})
        assert out.startswith("Error:")
        assert "id" in out

    def test_tool_renders_header_and_body(self, monkeypatch, token):
        email = dict(
            _envelope(),
            textBody=[{"partId": "1"}],
            bodyValues={"1": {"value": "Plain text body.", "isTruncated": False}},
        )
        FakeTransport(posts=[_read_response(email)]).install(monkeypatch)

        out = jmap_tools.mail_read({"id": "em-1"})

        assert "Quarterly numbers" in out
        assert "Plain text body." in out
        assert "ada@example.invalid" in out


# --------------------------------------------------------------------------
# Read-only surface
# --------------------------------------------------------------------------


class TestReadOnly:
    def test_only_list_and_read_tools_exist(self):
        assert set(jmap_tools._SCHEMAS) == {"mail_list", "mail_read"}
        assert set(jmap_tools._HANDLERS) == {"mail_list", "mail_read"}

    def test_no_mutating_jmap_methods_in_the_module(self):
        import inspect

        source = inspect.getsource(jmap)
        for method in ("Email/set", "Email/import", "Email/copy",
                       "EmailSubmission/set", "Mailbox/set", "Identity/set"):
            assert method not in source, f"{method} must not appear in a read-only module"


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


class TestRegistration:
    def test_register_tools_registers_both(self):
        seen = {}

        class _Ctx:
            def register_tool(self, name, toolset, schema, handler, **kw):
                seen[name] = (toolset, handler)

        jmap_tools.register_tools(_Ctx())

        assert set(seen) == {"mail_list", "mail_read"}
        assert all(ts == "retinue_rooms" for ts, _ in seen.values())

    def test_plugin_register_registers_tools_even_when_adapter_fails(self, monkeypatch):
        from . import __init__ as rooms_init  # noqa: F401
        from . import register

        registered: list[str] = []

        class _Ctx:
            def register_tool(self, name, **kw):
                registered.append(name)

            def register_platform(self, **kw):
                raise RuntimeError("adapter unavailable")

        register(_Ctx())

        assert set(registered) == {"mail_list", "mail_read"}

    def test_handlers_accept_the_registry_dispatch_convention(self, monkeypatch):
        """registry.dispatch calls handler(args_dict) positionally."""
        monkeypatch.delenv(jmap.TOKEN_ENV, raising=False)
        NoNetwork.install(monkeypatch)

        for handler in jmap_tools._HANDLERS.values():
            assert handler({}).startswith("Error:")
