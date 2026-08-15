from __future__ import annotations

import logging

from maitu_photo.logging_utils import PluginEventLogger, diagnostic_error, redact_text
from maitu_photo.provider import ProviderHTTPError


def test_redact_text_removes_common_provider_credentials() -> None:
    secret = "sk-this-is-a-sensitive-provider-key"
    text = redact_text(
        f"Bearer {secret} api_key={secret} https://user:pass@example.test/?token={secret}",
    )

    assert secret not in text
    assert "user:pass" not in text
    assert "[REDACTED]" in text


def test_redact_text_removes_json_style_credentials() -> None:
    secret = "json-secret-value"
    text = redact_text(f'{{"api_key":"{secret}","token":"{secret}"}}')

    assert secret not in text
    assert '"api_key":"[REDACTED]"' in text
    assert '"token":"[REDACTED]"' in text


def test_event_logger_emits_safe_structured_fields(caplog) -> None:
    logger = logging.getLogger("test.maitu.logging")
    secret = "sk-this-is-a-sensitive-provider-key"

    with caplog.at_level(logging.INFO, logger=logger.name):
        PluginEventLogger(logger).info(
            "任务处理失败",
            task_id="0123456789abcdef",
            error=f"Bearer {secret}",
            request={"api_key": secret},
            prompt="this must not be rendered as a full payload",
            scene_signature="this must not be rendered as a scene signature",
        )

    rendered = caplog.text
    assert "麦麦写真 | 任务处理失败" in rendered
    assert "task_id=0123456789abcdef" in rendered
    assert "request=<mapping:1>" in rendered
    assert secret not in rendered
    assert "error=<redacted>" in rendered
    assert "this must not be rendered" not in rendered


def test_event_logger_honors_minimum_level_and_enabled_flag(caplog) -> None:
    logger = logging.getLogger("test.maitu.logging.filtered")

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        PluginEventLogger(logger, minimum_level="WARNING").info("不会记录")
        PluginEventLogger(logger, enabled=False).error("不会记录")
        PluginEventLogger(logger, minimum_level="WARNING").warning("会记录")

    assert "不会记录" not in caplog.text
    assert "会记录" in caplog.text


def test_event_logger_preserves_zero_valued_diagnostics(caplog) -> None:
    logger = logging.getLogger("test.maitu.logging.zero")

    with caplog.at_level(logging.INFO, logger=logger.name):
        PluginEventLogger(logger).info("清理完成", deleted_tasks=0)

    assert "deleted_tasks=0" in caplog.text


def test_diagnostic_error_uses_provider_category_without_response_text() -> None:
    error = ProviderHTTPError(
        "provider echoed prompt: secret request content",
        status_code=503,
        retryable=True,
    )

    assert diagnostic_error(error) == "provider_http/http_503"


def test_event_logger_exception_does_not_emit_traceback_text(caplog) -> None:
    logger = logging.getLogger("test.maitu.logging.exception")

    with caplog.at_level(logging.ERROR, logger=logger.name):
        try:
            raise RuntimeError("upstream response included private prompt")
        except RuntimeError:
            PluginEventLogger(logger).exception("任务失败")

    assert "error_kind=RuntimeError" in caplog.text
    assert "private prompt" not in caplog.text
