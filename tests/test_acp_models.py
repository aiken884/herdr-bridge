# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""acp.models: frozen dataclass contract and the two-tier vocabulary (D-3)."""

from __future__ import annotations

import pytest

from herdr_bridge.acp.models import (
    AcpAgentSpec,
    AcpEvent,
    AcpPolicy,
    AcpSessionInfo,
    PromptResult,
)


def test_acp_agent_spec_frozen():
    spec = AcpAgentSpec(name="oc-dspro", command="/tmp/wrapper.sh", builtin=False)
    assert spec.name == "oc-dspro"
    with pytest.raises(AttributeError):
        spec.name = "other"  # type: ignore[misc]


def test_acp_session_info_frozen():
    info = AcpSessionInfo(
        session_name="flt-1-dspro",
        agent="oc-dspro",
        workdir="/tmp/acp-m0/workdirs/m0-1",
        acp_session_id="ses_abc123",
        closed=False,
    )
    assert info.acp_session_id == "ses_abc123"
    with pytest.raises(AttributeError):
        info.closed = True  # type: ignore[misc]


def test_acp_event_transparent_unknown_type():
    """R4 tolerant reader: an unknown type doesn't blow up, raw passes through."""
    ev = AcpEvent(type="some_future_event_type", session_id="ses_1",
                  text=None, raw={"weird": "shape"})
    assert ev.type == "some_future_event_type"
    assert ev.raw == {"weird": "shape"}


def test_prompt_result_reason_is_bridge_frozen_vocabulary():
    """D-3: reason is bridge's own frozen vocabulary, and can only be one of four values."""
    result = PromptResult(
        reason="stop", stop_reason="end_turn", text="PONG",
        session_name="t1", usage=None,
    )
    assert result.reason == "stop"


@pytest.mark.parametrize("reason", ["stop", "timeout", "error", "canceled"])
def test_prompt_result_reason_all_frozen_values(reason):
    result = PromptResult(reason=reason, stop_reason=None, text="",
                          session_name="t1", usage=None)
    assert result.reason == reason


def test_prompt_result_stop_reason_is_transparent_str_not_literal():
    """D-3: protocol vocabulary passes through as a plain str, not locked to a
    Literal -- unknown values can be stuffed in too."""
    result = PromptResult(reason="stop", stop_reason="some_new_v2_value",
                          text="", session_name="t1", usage=None)
    assert result.stop_reason == "some_new_v2_value"


def test_acp_policy_frozen_defaults():
    policy = AcpPolicy()
    assert policy.mode == "approve-all"
    assert policy.policy_enforced is None


def test_acp_policy_explicit_fields():
    policy = AcpPolicy(mode="approve-reads", policy_enforced=True)
    assert policy.mode == "approve-reads"
    assert policy.policy_enforced is True
