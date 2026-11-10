"""Where the host sends a model request, and with whose key.

The server's ``LLM_API_KEY`` only ever goes to the server's ``LLM_BASE_URL``: a request
that names its own endpoint brings its own key. A browser-supplied endpoint must be a
well-formed https URL on a public address (``LLM_ALLOW_HTTP`` lifts both rules for a
model on the operator's own network).
"""
from __future__ import annotations

import httpx
import pytest

from backend import llm as llm_module
from backend.config import WebSettings
from backend.llm import LLMError, LLMSettings
from tests.fake_model import FakeModel, text_turn

SERVER_ENV = {"LLM_BASE_URL": "https://srv.example/v1", "LLM_MODEL": "srv-model",
              "LLM_API_KEY": "srv-key-not-real"}
SERVER = WebSettings.from_env(SERVER_ENV)
RELAXED = WebSettings.from_env({**SERVER_ENV, "LLM_ALLOW_HTTP": "1"})
PUBLIC = "93.184.215.14"          # a global unicast address; the transport is mocked, nothing connects


class Recorder:
    """The scripted model behind httpx.MockTransport, also keeping where each request went."""

    def __init__(self):
        self.model = FakeModel([])
        self.sent: list[tuple[str, str]] = []           # (url, Authorization header)
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append((str(request.url), request.headers.get("authorization", "")))
        return self.model._handle(request)


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = rec.transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", patched)
    return rec


@pytest.fixture
def server_model(make_client, env):
    """A host whose operator configured a model and a key."""
    for name, value in SERVER_ENV.items():
        env.setenv(name, value)
    return make_client()


def answers(monkeypatch, table: dict[str, list[str]]):
    """Name resolution for the endpoint check: ``{host: [addresses]}``; anything else fails."""
    asked: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        asked.append(host)
        if host not in table:
            raise OSError(f"no such host {host}")
        return table[host]

    monkeypatch.setattr(llm_module, "resolve_addresses", resolve)
    return asked


# -- A1: the server's key never leaves for a visitor's endpoint ------------------------------

def test_a_browser_endpoint_without_its_own_key_is_refused():
    for headers in ({"x-llm-base-url": "https://attacker.example/v1"},
                    {"x-llm-base-url": "https://attacker.example/v1", "x-llm-model": "m"},
                    # the server's own URL, typed into the browser, is still the browser's
                    {"x-llm-base-url": "https://srv.example/v1/"}):
        with pytest.raises(LLMError) as exc:
            LLMSettings.resolve(headers, SERVER)
        assert exc.value.status == 400
        assert "X-LLM-Key" in str(exc.value) and "srv-key-not-real" not in str(exc.value)


def test_the_leak_request_is_refused_before_anything_is_sent(server_model, recorder):
    recorder.model.turns.append(text_turn("never"))
    resp = server_model.post("/api/chat", json={"message": "hi"},
                             headers={"X-LLM-Base-Url": "https://attacker.example/v1"})
    assert resp.status_code == 400
    assert "X-LLM-Key" in resp.json()["detail"]
    assert recorder.sent == []
    assert "srv-key-not-real" not in resp.text


@pytest.mark.parametrize("headers, expected", [
    # (browser headers) -> (url, model, key) the request goes out with
    ({}, ("https://srv.example/v1", "srv-model", "srv-key-not-real")),
    ({"x-llm-key": "visitor-key"}, ("https://srv.example/v1", "srv-model", "visitor-key")),
    ({"x-llm-model": "other"}, ("https://srv.example/v1", "other", "srv-key-not-real")),
    ({"x-llm-model": "other", "x-llm-key": "visitor-key"}, ("https://srv.example/v1", "other", "visitor-key")),
    ({"x-llm-base-url": "https://mine.example/v1", "x-llm-key": "visitor-key"},
     ("https://mine.example/v1", "srv-model", "visitor-key")),
    ({"x-llm-base-url": "https://mine.example/v1/", "x-llm-model": "m", "x-llm-key": "visitor-key"},
     ("https://mine.example/v1", "m", "visitor-key")),
])
def test_the_allowed_combinations(headers, expected):
    resolved = LLMSettings.resolve(headers, SERVER)
    assert (resolved.base_url, resolved.model, resolved.api_key) == expected
    assert resolved.source == ("browser" if headers else "server")
    assert resolved.custom_endpoint is ("x-llm-base-url" in headers)


def test_a_visitor_endpoint_on_a_host_without_a_server_model():
    bare = WebSettings.from_env({})
    resolved = LLMSettings.resolve({"x-llm-base-url": "https://mine.example/v1", "x-llm-model": "m",
                                    "x-llm-key": "visitor-key"}, bare)
    assert resolved.configured and resolved.base_url == "https://mine.example/v1"
    with pytest.raises(LLMError):
        LLMSettings.resolve({"x-llm-base-url": "https://mine.example/v1", "x-llm-model": "m"}, bare)


def test_each_request_goes_where_its_key_belongs(server_model, recorder, monkeypatch):
    answers(monkeypatch, {"mine.example": [PUBLIC]})
    recorder.model.turns += [text_turn("one"), text_turn("two"), text_turn("three")]

    assert server_model.post("/api/chat", json={"message": "a", "bridge": False}).status_code == 200
    assert server_model.post("/api/chat", json={"message": "b", "bridge": False},
                             headers={"X-LLM-Key": "visitor-key"}).status_code == 200
    assert server_model.post("/api/chat", json={"message": "c", "bridge": False},
                             headers={"X-LLM-Base-Url": "https://mine.example/v1",
                                      "X-LLM-Key": "visitor-key"}).status_code == 200
    assert recorder.sent == [
        ("https://srv.example/v1/chat/completions", "Bearer srv-key-not-real"),
        ("https://srv.example/v1/chat/completions", "Bearer visitor-key"),
        ("https://mine.example/v1/chat/completions", "Bearer visitor-key"),
    ]


def test_config_json_announces_a_server_model_only_when_it_can_run_alone():
    """Key and model without a server URL could only ever run with a visitor's URL - the
    leak above - so that host has no server model to offer."""
    assert not WebSettings.from_env({"LLM_MODEL": "m", "LLM_API_KEY": "k"}).server_model_configured
    assert SERVER.server_model_configured
    assert SERVER.config_json()["features"]["serverModel"] is True


# -- B1: a browser endpoint must be on a public address -------------------------------------

@pytest.mark.parametrize("url", [
    "https://10.0.0.5/v1", "https://192.168.1.20/v1", "https://172.16.0.1/v1",
    "https://127.0.0.1:8443/v1", "https://169.254.169.254/latest", "https://0.0.0.0/v1",
    "https://224.0.0.1/v1", "https://240.0.0.1/v1", "https://100.64.0.1/v1",
    "https://[::1]/v1", "https://[fe80::1]/v1", "https://[fc00::1]/v1", "https://[::]/v1",
    "https://[::ffff:127.0.0.1]/v1", "https://[::ffff:10.0.0.5]/v1", "https://[ff02::1]/v1",
    # The forms the system resolver reads as IPv4 (inet_aton): no DNS needed to refuse them.
    "https://127.1/v1", "https://0x7f.1/v1", "https://0x7f000001/v1", "https://2130706433/v1",
    "https://017700000001/v1", "https://127.0.0.1./v1", "https://10.1/v1",
    # localhost is loopback by definition (RFC 6761), whatever a resolver says.
    "https://localhost/v1", "https://localhost./v1", "https://LOCALHOST:8443/v1", "https://api.localhost/v1",
    # IPv4 carried inside IPv6: mapped, NAT64, 6to4.
    "https://[::ffff:7f00:1]/v1", "https://[64:ff9b::7f00:1]/v1", "https://[64:ff9b::a00:5]/v1",
    "https://[2002:7f00:1::]/v1", "https://[2002:a00:5::1]/v1",
    # User info is not the host.
    "https://user@10.0.0.1/v1", "https://1.1.1.1@127.0.0.1/v1", "https://user:pw@[::1]/v1",
])
def test_literal_private_addresses_are_refused(url):
    with pytest.raises(LLMError) as exc:
        LLMSettings.resolve({"x-llm-base-url": url, "x-llm-key": "k"}, SERVER)
    assert exc.value.status == 400 and "address" in str(exc.value)


@pytest.mark.parametrize("url", ["https://8.8.8.8/v1", "https://[2001:4860:4860::8888]/v1",
                                 "https://user@8.8.8.8/v1", "https://api.example.com/v1",
                                 "https://localhost.example.com/v1"])
def test_public_addresses_and_names_pass_the_literal_check(url):
    assert LLMSettings.resolve({"x-llm-base-url": url, "x-llm-key": "k"}, SERVER).custom_endpoint


def test_a_redirect_from_the_model_endpoint_is_not_followed(monkeypatch):
    """A public endpoint answering 302 to an internal address must not become a request there."""
    import asyncio

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        assert kwargs.get("follow_redirects") is False          # pinned, not left to a default
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", patched)

    async def collect():
        settings = LLMSettings(base_url="https://8.8.8.8/v1", model="m", api_key="k")
        return [t async for t in llm_module.stream_completion(settings, [{"role": "user", "content": "x"}])]

    with pytest.raises(LLMError) as exc:
        asyncio.run(collect())
    assert seen == ["https://8.8.8.8/v1/chat/completions"]
    assert exc.value.status == 502 and "302" in str(exc.value) and "169.254" not in str(exc.value)


def test_a_literal_private_address_is_never_requested(server_model, recorder):
    recorder.model.turns.append(text_turn("never"))
    resp = server_model.post("/api/chat", json={"message": "hi", "bridge": False},
                             headers={"X-LLM-Base-Url": "https://10.0.0.5/v1", "X-LLM-Key": "k"})
    assert resp.status_code == 400 and recorder.sent == []


def test_a_name_that_resolves_to_a_private_address_is_refused(server_model, recorder, monkeypatch):
    asked = answers(monkeypatch, {"localhost": ["127.0.0.1", "::1"], "internal.example": ["10.1.2.3"],
                                  "mixed.example": [PUBLIC, "10.1.2.3"], "public.example": [PUBLIC]})
    recorder.model.turns.append(text_turn("public"))

    def chat(base_url: str):
        return server_model.post("/api/chat", json={"message": "hi", "bridge": False},
                                 headers={"X-LLM-Base-Url": base_url, "X-LLM-Key": "k"})

    for url in ("https://localhost:8443/v1", "https://internal.example/v1", "https://mixed.example/v1",
                "https://unknown.example/v1"):
        resp = chat(url)
        assert resp.status_code == 400, url
    assert recorder.sent == []
    assert chat("https://public.example/v1").status_code == 200
    assert recorder.sent == [("https://public.example/v1/chat/completions", "Bearer k")]
    # localhost is refused before any lookup (RFC 6761); the others were resolved.
    assert asked == ["internal.example", "mixed.example", "unknown.example", "public.example"]


def test_the_server_endpoint_is_not_checked(make_client, env, recorder, monkeypatch):
    """LLM_BASE_URL is the operator's choice: a model on the host's own network is fine."""
    asked = answers(monkeypatch, {})
    env.setenv("LLM_BASE_URL", "http://10.0.0.5:11434/v1")
    env.setenv("LLM_MODEL", "local")
    env.setenv("LLM_API_KEY", "k")
    client = make_client()
    recorder.model.turns.append(text_turn("local"))
    assert client.post("/api/chat", json={"message": "hi", "bridge": False}).status_code == 200
    assert recorder.sent == [("http://10.0.0.5:11434/v1/chat/completions", "Bearer k")]
    assert asked == []


def test_llm_allow_http_also_lifts_the_address_check(make_client, env, recorder, monkeypatch):
    asked = answers(monkeypatch, {})
    for url in ("http://localhost:11434/v1", "https://10.0.0.5/v1", "http://[::1]:8000/v1"):
        assert LLMSettings.resolve({"x-llm-base-url": url, "x-llm-key": "k"}, RELAXED).base_url == url
    env.setenv("LLM_ALLOW_HTTP", "1")
    client = make_client()
    recorder.model.turns.append(text_turn("local"))
    resp = client.post("/api/chat", json={"message": "hi", "bridge": False},
                       headers={"X-LLM-Base-Url": "http://localhost:11434/v1", "X-LLM-Model": "m",
                                "X-LLM-Key": "k"})
    assert resp.status_code == 200
    assert recorder.sent == [("http://localhost:11434/v1/chat/completions", "Bearer k")]
    assert asked == []                      # nothing resolved: the opt-in covers names too


# -- R1: what httpx would refuse to request is refused at the gate ---------------------------

UNREQUESTABLE = ["https://8.8.8.8/a\tb", "https://8.8.8.8/a\x00b", "https://8.8.8.8/a\x1fb",
                 "https://8.8.8.8/a\x7fb", "https://exa mple/v1", "https://exa%20mple/v1",
                 "https://8.8.8.8:99999/v1", "https://a..b/v1"]


@pytest.mark.parametrize("url", UNREQUESTABLE)
def test_a_url_httpx_would_refuse_is_a_400_at_the_gate(url):
    for settings in (SERVER, RELAXED):           # malformed is malformed, opt-in or not
        with pytest.raises(LLMError) as exc:
            LLMSettings.resolve({"x-llm-base-url": url, "x-llm-key": "k"}, settings)
        assert exc.value.status == 400 and "not a valid URL" in str(exc.value)


@pytest.mark.parametrize("url", ["https://8.8.8.8/a\tb", "https://exa mple/v1", "http://exa mple/v1"])
def test_a_url_httpx_would_refuse_never_reaches_the_transport(make_client, env, recorder, url):
    env.setenv("LLM_ALLOW_HTTP", "1" if url.startswith("http:") else "0")
    client = make_client()
    recorder.model.turns.append(text_turn("never"))
    resp = client.post("/api/chat", json={"message": "hi", "bridge": False},
                       headers={"X-LLM-Base-Url": url, "X-LLM-Model": "m", "X-LLM-Key": "k"})
    assert resp.status_code == 400, resp.text
    assert "not a valid URL" in resp.json()["detail"]
    assert recorder.sent == []


def test_an_unrequestable_server_url_is_a_model_error_not_a_crash(recorder):
    """LLM_BASE_URL is not checked at the gate; the request itself still fails as an LLMError."""
    import asyncio

    async def collect():
        settings = LLMSettings(base_url="https://8.8.8.8/a\tb", model="m", api_key="k")
        return [t async for t in llm_module.stream_completion(settings, [{"role": "user", "content": "x"}])]

    with pytest.raises(LLMError) as exc:
        asyncio.run(collect())
    assert "not a valid URL" in str(exc.value) and recorder.sent == []


# -- B2: a malformed endpoint is a 400, not a crash ------------------------------------------

@pytest.mark.parametrize("url", ["https://[::1", "https://example.com:99999/v1", "https://[not-an-ip]/v1"])
def test_a_malformed_base_url_is_a_400(server_model, recorder, url):
    with pytest.raises(LLMError) as exc:
        LLMSettings.resolve({"x-llm-base-url": url, "x-llm-key": "k"}, SERVER)
    assert exc.value.status == 400
    resp = server_model.post("/api/chat", json={"message": "hi", "bridge": False},
                             headers={"X-LLM-Base-Url": url, "X-LLM-Key": "k"})
    assert resp.status_code == 400 and recorder.sent == []
