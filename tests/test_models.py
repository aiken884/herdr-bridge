# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

import dataclasses

import pytest

import herdr_bridge
from herdr_bridge.errors import (
    AgentNotFoundError,
    HerdrApiError,
    HerdrBridgeError,
)
from herdr_bridge.models import AgentInfo, WaitResult, normalize_read_source


def test_version():
    assert herdr_bridge.__version__ == "0.2.2"


def test_agent_info_is_frozen():
    info = AgentInfo(
        agent_id="term_1", brand="claude", status="idle",
        pane_id="w1:p1", workspace_id="w1", tab_id="w1:t1",
        cwd="/tmp", session_ref={"kind": "id", "value": "u-1"}, focused=False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.status = "working"  # type: ignore[misc]


def test_api_error_carries_code():
    err = HerdrApiError(code="pane_not_found", message="pane x not found")
    assert isinstance(err, HerdrBridgeError)
    assert err.code == "pane_not_found"


def test_agent_not_found_is_api_error():
    assert issubclass(AgentNotFoundError, HerdrApiError)


def test_normalize_read_source():
    assert normalize_read_source("recent-unwrapped") == "recent_unwrapped"
    assert normalize_read_source("recent_unwrapped") == "recent_unwrapped"
    assert normalize_read_source("visible") == "visible"
    with pytest.raises(ValueError):
        normalize_read_source("bogus")


def test_wait_result_reason_literal():
    r = WaitResult(success=False, agent_id="term_1", reason="timeout",
                   elapsed_sec=60.0, last_output=None, error=None)
    assert r.reason == "timeout"


def test_subscription_type_to_event_name():
    from herdr_bridge.models import subscription_type_to_event_name
    assert subscription_type_to_event_name(
        "pane.agent_status_changed") == "pane_agent_status_changed"
    assert subscription_type_to_event_name("workspace.created") == "workspace_created"
    assert subscription_type_to_event_name("layout.updated") == "layout_updated"


def test_normalized_text_joins_pty_hard_wrap():
    # 0.1.1 Fix B: a narrow pane's PTY hard-wrap splits a marker across two
    # lines -- normalized_text joins the stray newline so marker matching
    # works; the semantics of `text` stay frozen and untouched
    from herdr_bridge.models import AgentOutput
    out = AgentOutput(agent_id="term_1", text="TEST_MARKER_STA\nRT",
                      source="recent_unwrapped", status_at_read="idle")
    assert "TEST_MARKER_START" in out.normalized_text
    assert out.text == "TEST_MARKER_STA\nRT"      # text stays untouched


def test_normalized_text_preserves_blank_line_paragraphs():
    from herdr_bridge.models import AgentOutput
    out = AgentOutput(agent_id="term_1", text="a\n\nb",
                      source="recent_unwrapped", status_at_read="idle")
    assert "\n\n" in out.normalized_text          # blank-line paragraph separators are preserved
    assert out.normalized_text == "a\n\nb"
