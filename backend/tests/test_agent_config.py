from tsf_sizer.agent import config


def env(**kw):
    return {k.replace("__", "_"): v for k, v in kw.items()}


def test_off_by_default():
    cfg = config.from_env({})
    assert cfg.mode == "off" and not cfg.enabled and cfg.label == "Off"


def test_gateway_variables():
    cfg = config.from_env(
        {
            "TSF_SIZER_LLM_MODE": "gateway",
            "TSF_SIZER_GATEWAY_URL": "https://gw.example/v1/",
            "TSF_SIZER_GATEWAY_API_KEY": "hunter2key",
            "TSF_SIZER_GATEWAY_MODEL": "m",
            "TSF_SIZER_GATEWAY_AUTH_HEADER": "x-api-key",
            "TSF_SIZER_GATEWAY_AUTH_SCHEME": "",
            "TSF_SIZER_GATEWAY_HEADERS": '{"x-profile": "sizing"}',
            "TSF_SIZER_GATEWAY_VERIFY_TLS": "false",
            "TSF_SIZER_GATEWAY_TIMEOUT_S": "30",
            "TSF_SIZER_GATEWAY_MAX_RETRIES": "5",
            "TSF_SIZER_GATEWAY_TOOL_MODE": "json",
            "TSF_SIZER_AGENT_MAX_STEPS": "12",
        }
    )
    assert cfg.enabled and cfg.mode == "gateway"
    assert cfg.endpoint == "https://gw.example/v1/chat/completions"
    assert (cfg.auth_header, cfg.auth_scheme) == ("x-api-key", "")
    assert cfg.headers == {"x-profile": "sizing"}
    assert cfg.verify_tls is False and cfg.timeout_s == 30 and cfg.max_retries == 5
    assert cfg.tool_mode == "json" and cfg.max_steps == 12 and not cfg.problems
    assert "hunter2key" not in repr(cfg)  # the key never shows up in a log line


def test_default_auth_is_bearer_in_authorization():
    cfg = config.from_env(
        {
            "TSF_SIZER_LLM_MODE": "gateway",
            "TSF_SIZER_GATEWAY_URL": "https://gw/v1",
            "TSF_SIZER_GATEWAY_MODEL": "m",
        }
    )
    assert (cfg.auth_header, cfg.auth_scheme, cfg.chat_path) == (
        "Authorization",
        "Bearer",
        "/chat/completions",
    )
    assert cfg.verify_tls and cfg.tool_mode == "auto" and cfg.max_steps == 40


def test_missing_url_or_model_means_not_enabled():
    cfg = config.from_env({"TSF_SIZER_LLM_MODE": "gateway", "TSF_SIZER_GATEWAY_URL": "https://g"})
    assert not cfg.enabled


def test_secret_from_file(tmp_path):
    f = tmp_path / "key"
    f.write_text("from-file\n")
    cfg = config.from_env(
        {
            "TSF_SIZER_LLM_MODE": "gateway",
            "TSF_SIZER_GATEWAY_URL": "https://g/v1",
            "TSF_SIZER_GATEWAY_MODEL": "m",
            "TSF_SIZER_GATEWAY_API_KEY_FILE": str(f),
        }
    )
    assert cfg.api_key == "from-file"


def test_unreadable_secret_file_is_reported_not_raised():
    cfg = config.from_env(
        {
            "TSF_SIZER_LLM_MODE": "gateway",
            "TSF_SIZER_GATEWAY_URL": "https://g/v1",
            "TSF_SIZER_GATEWAY_MODEL": "m",
            "TSF_SIZER_GATEWAY_API_KEY_FILE": "/nonexistent/key",
        }
    )
    assert cfg.api_key is None
    assert any("API_KEY_FILE" in p for p in cfg.problems)


def test_mistakes_become_problems_and_safe_defaults():
    cfg = config.from_env(
        {
            "TSF_SIZER_LLM_MODE": "gateway",
            "TSF_SIZER_GATEWAY_URL": "gw.example/v1",
            "TSF_SIZER_GATEWAY_MODEL": "m",
            "TSF_SIZER_GATEWAY_HEADERS": "[1, 2]",
            "TSF_SIZER_GATEWAY_TIMEOUT_S": "soon",
            "TSF_SIZER_GATEWAY_TOOL_MODE": "magic",
        }
    )
    assert not cfg.enabled  # an unusable URL is dropped
    assert cfg.headers == {} and cfg.timeout_s == 120 and cfg.tool_mode == "auto"
    text = " ".join(cfg.problems)
    for word in ("GATEWAY_URL", "GATEWAY_HEADERS", "TIMEOUT_S", "TOOL_MODE"):
        assert word in text
    assert config.from_env({"TSF_SIZER_LLM_MODE": "sideways"}).problems


def test_direct_mode():
    cfg = config.from_env(
        {
            "TSF_SIZER_LLM_MODE": "direct",
            "TSF_SIZER_DIRECT_URL": "http://ollama:11434/v1",
            "TSF_SIZER_DIRECT_MODEL": "qwen2.5:14b",
        }
    )
    assert cfg.enabled and cfg.mode == "direct" and "direct" in cfg.label


def test_status_never_shows_secrets():
    e = {
        "TSF_SIZER_LLM_MODE": "gateway",
        "TSF_SIZER_GATEWAY_URL": "https://g/v1",
        "TSF_SIZER_GATEWAY_MODEL": "m",
        "TSF_SIZER_GATEWAY_API_KEY": "topsecret",
        "TSF_SIZER_GATEWAY_HEADERS": '{"x-token": "alsosecret"}',
    }
    rows = config.status(config.from_env(e), e)
    by = {r["name"]: r for r in rows}
    assert by["TSF_SIZER_GATEWAY_API_KEY"]["set"] and by["TSF_SIZER_GATEWAY_API_KEY"]["secret"]
    assert by["TSF_SIZER_GATEWAY_HEADERS"]["set"]
    assert "topsecret" not in repr(rows) and "alsosecret" not in repr(rows)
    assert by["TSF_SIZER_GATEWAY_URL"]["value"] == "https://g/v1"
