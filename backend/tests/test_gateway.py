import json

import pytest
from fake_gateway import text_reply, tool_reply

from tsf_sizer.agent import config
from tsf_sizer.agent.gateway import GatewayClient, GatewayError

SPEC = {
    "name": "get_evidence",
    "description": "Read evidence",
    "parameters": {
        "type": "object",
        "properties": {"topic": {"type": "string"}},
        "required": ["topic"],
    },
}
MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]


def cfg(gw, **extra):
    env = {
        "TSF_SIZER_LLM_MODE": "gateway",
        "TSF_SIZER_GATEWAY_URL": gw.url,
        "TSF_SIZER_GATEWAY_API_KEY": "secret-key",
        "TSF_SIZER_GATEWAY_MODEL": "fake-model",
        "TSF_SIZER_GATEWAY_MAX_RETRIES": "2",
        **extra,
    }
    return config.from_env(env)


def test_plain_chat_sends_key_model_and_extra_headers(gateway):
    gateway.script = [text_reply("hello", request_id="abc")]
    c = GatewayClient(cfg(gateway, TSF_SIZER_GATEWAY_HEADERS='{"x-profile": "sizing"}'))
    reply = c.chat(MSGS)
    assert reply.content == "hello" and reply.request_id == "abc"
    req = gateway.requests[0]
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["authorization"] == "Bearer secret-key"
    assert req["headers"]["x-profile"] == "sizing"
    assert req["body"]["model"] == "fake-model" and "tools" not in req["body"]


def test_custom_auth_header_without_scheme(gateway):
    gateway.script = [text_reply("x")]
    c = GatewayClient(
        cfg(
            gateway,
            TSF_SIZER_GATEWAY_AUTH_HEADER="x-api-key",
            TSF_SIZER_GATEWAY_AUTH_SCHEME="",
            TSF_SIZER_GATEWAY_CHAT_PATH="/chat",
        )
    )
    c.chat(MSGS)
    req = gateway.requests[0]
    assert req["headers"]["x-api-key"] == "secret-key" and "authorization" not in req["headers"]
    assert req["path"] == "/v1/chat"


def test_native_tool_calls(gateway):
    gateway.script = [tool_reply(("get_evidence", {"topic": "overview"}))]
    reply = GatewayClient(cfg(gateway)).chat(MSGS, [SPEC])
    assert [(t.name, t.arguments) for t in reply.tool_calls] == [
        ("get_evidence", {"topic": "overview"})
    ]
    body = gateway.requests[0]["body"]
    assert body["tools"][0]["function"]["name"] == "get_evidence"
    assert body["tool_choice"] == "auto"


def test_json_tool_mode_rewrites_history_and_parses_calls(gateway):
    gateway.script = [
        text_reply('Sure.\n{"tool": "get_evidence", "arguments": {"topic": "usage"}}')
    ]
    history = MSGS + [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "get_evidence", "arguments": '{"topic": "overview"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"result_id": "r1"}'},
    ]
    c = GatewayClient(cfg(gateway, TSF_SIZER_GATEWAY_TOOL_MODE="json"))
    reply = c.chat(history, [SPEC])
    assert reply.tool_calls[0].name == "get_evidence"
    assert reply.tool_calls[0].arguments == {"topic": "usage"}
    sent = gateway.requests[0]["body"]
    assert "tools" not in sent
    assert "get_evidence(topic: string)" in sent["messages"][0]["content"]
    assert any("Result of get_evidence" in m["content"] for m in sent["messages"])
    assert all("tool_calls" not in m for m in sent["messages"])


def test_json_mode_plain_text_is_a_plain_answer(gateway):
    gateway.script = [text_reply("Which port speed do you need?")]
    reply = GatewayClient(cfg(gateway, TSF_SIZER_GATEWAY_TOOL_MODE="json")).chat(MSGS, [SPEC])
    assert reply.tool_calls == [] and "port speed" in reply.content


def test_auto_falls_back_to_json_when_tools_are_refused(gateway):
    gateway.script = [
        (400, {"error": {"message": "tools are not supported by this model"}}),
        text_reply('{"tool": "get_evidence", "arguments": {"topic": "ha"}}'),
    ]
    c = GatewayClient(cfg(gateway))
    reply = c.chat(MSGS, [SPEC])
    assert reply.tool_calls[0].arguments == {"topic": "ha"} and reply.tool_mode == "json"
    assert c.fell_back and c.tool_mode == "json"
    assert "tools" in gateway.requests[0]["body"] and "tools" not in gateway.requests[1]["body"]


def test_native_only_does_not_fall_back(gateway):
    gateway.script = [(400, {"error": "tools are not supported"})]
    with pytest.raises(GatewayError) as e:
        GatewayClient(cfg(gateway, TSF_SIZER_GATEWAY_TOOL_MODE="native")).chat(MSGS, [SPEC])
    assert e.value.kind == "http"


@pytest.mark.parametrize("code", [401, 403])
def test_auth_failure_is_final(gateway, code):
    gateway.script = [(code, {"error": "bad key"})]
    with pytest.raises(GatewayError) as e:
        GatewayClient(cfg(gateway)).chat(MSGS)
    assert e.value.kind == "auth" and "TSF_SIZER_GATEWAY_API_KEY" in str(e.value)
    assert gateway.calls == 1  # no retry


@pytest.mark.parametrize(
    "status,body",
    [
        (400, {"error": {"message": "Request blocked by guardrail: prompt injection"}}),
        (403, {"error": "Blocked by AI security profile"}),
        (200, {"error": {"message": "blocked by policy"}}),
        (200, {"choices": [{"finish_reason": "content_filter", "message": {"content": ""}}]}),
    ],
)
def test_blocked_by_policy_is_final_and_never_retried(gateway, status, body):
    gateway.script = [(status, body, {"x-request-id": "blk-9"})]
    with pytest.raises(GatewayError) as e:
        GatewayClient(cfg(gateway)).chat(MSGS)
    assert e.value.kind == "blocked"
    assert gateway.calls == 1


def test_blocked_error_carries_the_request_id(gateway):
    gateway.script = [(403, {"error": "Blocked by AI security profile"}, {"x-request-id": "blk-9"})]
    with pytest.raises(GatewayError) as e:
        GatewayClient(cfg(gateway)).chat(MSGS)
    assert e.value.request_id == "blk-9"


def test_rate_limit_retries_then_succeeds(gateway):
    gateway.script = [(429, {"error": "slow down"}), (503, {"error": "busy"}), text_reply("ok")]
    assert GatewayClient(cfg(gateway)).chat(MSGS).content == "ok"
    assert gateway.calls == 3


def test_rate_limit_gives_up(gateway):
    gateway.script = [(429, {"error": "slow down"})] * 5
    gateway.repeat_last = True
    with pytest.raises(GatewayError) as e:
        GatewayClient(cfg(gateway, TSF_SIZER_GATEWAY_MAX_RETRIES="1")).chat(MSGS)
    assert e.value.kind == "rate" and gateway.calls == 2


def test_unreachable(gateway):
    c = cfg(
        gateway, TSF_SIZER_GATEWAY_URL="http://127.0.0.1:9/v1", TSF_SIZER_GATEWAY_MAX_RETRIES="0"
    )
    with pytest.raises(GatewayError) as e:
        GatewayClient(c).chat(MSGS)
    assert e.value.kind == "unreachable"


def test_not_json_and_empty_choices_are_bad_responses(gateway):
    gateway.script = [(200, b"<html>proxy login</html>")]
    with pytest.raises(GatewayError) as e:
        GatewayClient(cfg(gateway)).chat(MSGS)
    assert e.value.kind == "bad_response"
    gateway.script = [(200, {"choices": []})]
    with pytest.raises(GatewayError) as e:
        GatewayClient(cfg(gateway)).chat(MSGS)
    assert e.value.kind == "bad_response"


def test_connection_test_reports_tools_and_request_id(gateway):
    gateway.script = [
        text_reply("OK", request_id="t1"),
        tool_reply(("ping", {"message": "pong"})),
    ]
    out = GatewayClient(cfg(gateway)).test()
    assert out["answer"] == "OK" and out["tool_calls_work"] is True
    assert out["request_id"] == "t1" and out["tool_mode"] == "native"


def test_connection_test_when_tools_do_not_work(gateway):
    gateway.script = [text_reply("OK"), text_reply("I cannot call tools")]
    out = GatewayClient(cfg(gateway, TSF_SIZER_GATEWAY_TOOL_MODE="json")).test()
    assert out["tool_calls_work"] is False


def test_unparsable_native_arguments_do_not_crash(gateway):
    reply = tool_reply(("get_evidence", {}))
    reply["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
    gateway.script = [reply]
    out = GatewayClient(cfg(gateway)).chat(MSGS, [SPEC])
    assert out.tool_calls[0].arguments == {"_unparsed": "{not json"}


def test_key_is_not_in_error_text(gateway):
    gateway.script = [(500, {"error": "boom"})]
    with pytest.raises(GatewayError) as e:
        GatewayClient(cfg(gateway, TSF_SIZER_GATEWAY_MAX_RETRIES="0")).chat(MSGS)
    assert "secret-key" not in str(e.value)
    assert json.dumps(gateway.requests[0]["body"]).count("secret-key") == 0
