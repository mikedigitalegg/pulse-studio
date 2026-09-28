"""
Tests for the AI provider switch: OpenAI, Anthropic and local OpenAI-compatible servers all
feed _call_ai_async. Network calls are faked.

    python -m pytest tests/test_ai_providers.py -q
"""
import asyncio
import types

import anthropic
import httpx

import ableton_web_poc_server as srv


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else ""

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://x")
            raise httpx.HTTPStatusError("err", request=req, response=self)

    def json(self):
        return self._body


def _fake_http(monkeypatch, responses, sent):
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent.append({"url": url, "json": dict(json), "headers": dict(headers or {})})
            return responses.pop(0)

    monkeypatch.setattr(srv.httpx, "AsyncClient", Client)


def _ok(content):
    return _Resp(200, {"choices": [{"message": {"content": content}}]})


def _fake_anthropic(monkeypatch, sent, text='{"a": 1}'):
    class Messages:
        async def create(self, **kw):
            sent.append(kw)
            return types.SimpleNamespace(
                stop_reason="end_turn",
                content=[types.SimpleNamespace(type="text", text=text)],
            )

    class Client:
        def __init__(self, api_key=None, **k):
            self.api_key = api_key
            self.messages = Messages()

        async def close(self):
            pass

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Client)


def test_defaults_to_openai_gpt_4o_mini(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    sent = []
    _fake_http(monkeypatch, [_ok('{"a": 1}')], sent)
    obj, meta = asyncio.run(srv._call_ai_async("sys", "user", 0.7))
    assert meta["ok"] and obj == {"a": 1}
    assert sent[0]["url"] == "https://api.openai.com/v1/chat/completions"
    assert sent[0]["json"]["model"] == "gpt-4o-mini"
    assert sent[0]["headers"]["Authorization"] == "Bearer sk-test"


def test_missing_key_names_the_provider(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    srv.set_ai_settings(srv.AISettingsRequest(provider="anthropic"))
    obj, meta = asyncio.run(srv._call_ai_async("sys", "user", 0.7))
    assert obj is None
    assert meta["error"] == "missing_ai_api_key" and meta["provider"] == "anthropic"
    assert not srv._ai_configured()


def test_openai_model_choice_is_used(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    srv.set_ai_settings(srv.AISettingsRequest(provider="openai", model="gpt-4.1"))
    sent = []
    _fake_http(monkeypatch, [_ok("{}")], sent)
    asyncio.run(srv._call_ai_async("sys", "user", 0.7))
    assert sent[0]["json"]["model"] == "gpt-4.1"


def test_unsupported_temperature_is_dropped_and_retried(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    sent = []
    bad = _Resp(400, "Unsupported value: 'temperature' does not support 0.7 with this model.")
    _fake_http(monkeypatch, [bad, _ok('{"a": 1}')], sent)
    obj, meta = asyncio.run(srv._call_ai_async("sys", "user", 0.7))
    assert meta["ok"] and obj == {"a": 1}
    assert "temperature" in sent[0]["json"] and "temperature" not in sent[1]["json"]


def test_local_uses_base_url_model_and_no_key(monkeypatch):
    monkeypatch.delenv("LOCAL_AI_API_KEY", raising=False)
    srv.set_ai_settings(srv.AISettingsRequest(
        provider="local", model="qwen2.5:7b", local_base_url="http://127.0.0.1:1234/v1/",
    ))
    assert srv._ai_configured()
    sent = []
    no_json_mode = _Resp(400, "'response_format.type' must be 'json_schema'")
    _fake_http(monkeypatch, [no_json_mode, _ok('{"a": 2}')], sent)
    obj, meta = asyncio.run(srv._call_ai_async("sys", "user", 0.7))
    assert meta["ok"] and obj == {"a": 2}
    assert sent[0]["url"] == "http://127.0.0.1:1234/v1/chat/completions"
    assert sent[0]["json"]["model"] == "qwen2.5:7b"
    assert "Authorization" not in sent[0]["headers"]
    assert "response_format" not in sent[1]["json"]


def test_anthropic_request_shape(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    srv.set_ai_settings(srv.AISettingsRequest(provider="anthropic"))
    sent = []
    _fake_anthropic(monkeypatch, sent, text='```json\n{"drums": 1}\n```')
    obj, meta = asyncio.run(srv._call_ai_async("sys", "user", 0.9))
    assert meta["ok"] and obj == {"drums": 1}
    kw = sent[0]
    assert kw["model"] == "claude-opus-5"
    assert kw["output_config"] == {"effort": "low"}
    assert "temperature" not in kw
    assert kw["messages"] == [{"role": "user", "content": "user"}]


def test_haiku_gets_no_effort(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    srv.set_ai_settings(srv.AISettingsRequest(provider="anthropic", model="claude-haiku-4-5"))
    sent = []
    _fake_anthropic(monkeypatch, sent)
    asyncio.run(srv._call_ai_async("sys", "user", 0.9))
    assert sent[0]["model"] == "claude-haiku-4-5"
    assert "output_config" not in sent[0]


def test_api_key_goes_to_env_file_not_back_to_page(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = srv.set_ai_settings(srv.AISettingsRequest(provider="anthropic", api_key="sk-ant-abcdef123456"))
    with open(srv.ENV_FILE, encoding="utf-8") as f:
        assert "ANTHROPIC_API_KEY=sk-ant-abcdef123456" in f.read()
    prov = next(p for p in out["providers"] if p["id"] == "anthropic")
    assert prov["key_set"] and prov["key_hint"] == "...3456"
    assert "sk-ant-abcdef123456" not in str(out)


def test_settings_remember_a_model_per_provider():
    srv.set_ai_settings(srv.AISettingsRequest(provider="openai", model="gpt-4o"))
    srv.set_ai_settings(srv.AISettingsRequest(provider="anthropic", model="claude-sonnet-5"))
    out = srv.get_ai_settings()
    models = {p["id"]: p["model"] for p in out["providers"]}
    assert out["provider"] == "anthropic"
    assert models["openai"] == "gpt-4o" and models["anthropic"] == "claude-sonnet-5"


def test_rejects_unknown_provider_and_bad_url():
    assert srv.set_ai_settings(srv.AISettingsRequest(provider="gemini"))["error"] == "unknown_provider"
    out = srv.set_ai_settings(srv.AISettingsRequest(provider="local", local_base_url="localhost:11434"))
    assert out["error"] == "bad_base_url"
