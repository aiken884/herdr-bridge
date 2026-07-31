# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""#60: AcpRouter.dispatch_with_memory_confirm() used to return `ok=True`,
`confirmed_via="none"` on echo fallback (when nothing was actually sent to
any downstream target), sharing the same value with "dispatch actually
happened but PONG hasn't come back yet" -- a caller who only checks the `ok`
field would be misled into thinking dispatch succeeded. This is exactly the
silent-success pattern explicitly banned by
docs/governance/acp-direct-communication-pipeline.md line 282.

The first fix round only added three marker fields --
`echo_fallback`/`acp_unavailable`/`confirmed_via="echo-fallback"` -- but `ok`
was still `True` (see the first version of
docs/decisions/acp-layer-status-20260725.md). After this architectural
decision went through PPLX review, a stricter consensus emerged: echo
fallback should **not** report "success" -- `ok` must reflect whether this
dispatch actually reached a downstream target, not "success with a caveat
flag attached." This fix: `ok` now becomes `False` on echo fallback, plus two
new fields, `degraded: True` and `delivery_status: "not_attempted"` (the
degraded-state semantics PPLX recommended).
"""

from __future__ import annotations

import os

import pytest

from herdr_bridge.acp import router as router_module
from herdr_bridge.acp.router import create_herdr_router


@pytest.fixture(autouse=True)
def _isolate_remagraph_env(monkeypatch):
    """Avoid touching real RemaGraph state / cross-test pollution when constructing AcpRouter."""
    monkeypatch.setattr(router_module, "_rg", None)
    keys = ["REMAGRAPH_STATE_DIR", "REMAGRAPH_PROJECT"]
    saved = {k: os.environ.pop(k, None) for k in keys}
    yield
    for k in keys:
        if saved[k] is not None:
            os.environ[k] = saved[k]
        else:
            os.environ.pop(k, None)


def test_echo_fallback_when_acp_sdk_unavailable_is_explicitly_flagged(monkeypatch):
    """When acp-sdk is globally unavailable, dispatch to any target must fall
    into echo fallback -- the return value must be explicitly flagged, not
    left for the caller to figure out by parsing the response string itself.

    2026-07-25: acp-sdk has been promoted from an optional extra to a core
    dependency (see pyproject.toml), so this test environment now has
    `agent-client-protocol` installed by default -- we can no longer rely on
    "this test environment just happens to not have it installed" as the
    setup. Instead, use an explicit monkeypatch to simulate the "SDK globally
    unavailable" scenario itself, consistent with the approach used in
    `test_echo_fallback_still_flagged_even_when_acp_sdk_available_but_target_unregistered`
    in this same file."""
    monkeypatch.setattr(router_module, "ACP_SDK_AVAILABLE", False)

    router = create_herdr_router(project="test-echo-fallback-unavailable")
    result = router.dispatch_with_memory_confirm(
        "test message",
        target="not-a-registered-agent",
    )

    assert result["echo_fallback"] is True, "confirmed nothing was sent to any downstream target, must flag echo_fallback"
    assert result["acp_unavailable"] is True, "ACP SDK is globally unavailable, must flag acp_unavailable"
    assert result["confirmed_via"] == "echo-fallback", (
        "when it's confirmed to be echo fallback, confirmed_via should no longer be the overloaded 'none'"
    )
    # PPLX consensus: echo fallback should not report "success" -- a caller that only checks ok must not be misled
    assert result["ok"] is False, "echo fallback never actually reached any downstream target, ok cannot be True"
    assert result["degraded"] is True
    assert result["delivery_status"] == "not_attempted"


def test_echo_fallback_still_flagged_even_when_acp_sdk_available_but_target_unregistered(monkeypatch):
    """Even when the ACP SDK is available, if the target isn't a downstream
    agent previously registered via register_agent() (e.g. passing a raw
    Herdr pane_id directly -- currently the most common real-world usage),
    it will still fall into echo fallback. In this case acp_unavailable
    should be False (the SDK itself is fine), but echo_fallback should still
    be True, letting the caller know that "nothing was actually sent to any
    downstream target" this time -- acp_unavailable and echo_fallback are two
    independent diagnostic dimensions.
    """
    monkeypatch.setattr(router_module, "ACP_SDK_AVAILABLE", True)

    router = create_herdr_router(project="test-echo-fallback-available-unregistered")
    result = router.dispatch_with_memory_confirm(
        "test message",
        target="wT:p18",  # raw pane_id syntax, not in registered_agents
    )

    assert result["echo_fallback"] is True
    assert result["acp_unavailable"] is False, "the SDK itself is available, should not flag acp_unavailable"
    assert result["confirmed_via"] == "echo-fallback"
    assert result["ok"] is False, "SDK being available doesn't mean this dispatch actually got delivered, still cannot report success"
    assert result["degraded"] is True
    assert result["delivery_status"] == "not_attempted"


def test_real_pong_confirmation_is_not_mislabeled_as_echo_fallback(monkeypatch):
    """Regression guard: the normal path where a real PONG confirmation was
    received must not get mislabeled as echo_fallback by this fix.

    Note: this deliberately fakes `wait_for_pong` returning "PONG received"
    on a target that is actually echo fallback, creating a contradictory
    scenario purely to test the priority order of `confirmed_via` (PONG
    outranks the weaker echo-fallback signal). `ok` reflects whether this
    dispatch, at the `_run()` level, actually attempted delivery to a
    downstream target -- that fact doesn't change just because we later fake
    a PONG that couldn't really happen -- in the real world echo fallback and
    a real PONG never occur together; this contradiction only exists in this
    synthetic test.
    """
    router = create_herdr_router(project="test-echo-fallback-real-pong")

    def _fake_wait_for_pong(*args, **kwargs):
        return {"ok": True, "pong": {"correlation": "fake"}}

    monkeypatch.setattr(router, "wait_for_pong", _fake_wait_for_pong)

    result = router.dispatch_with_memory_confirm(
        "test message",
        target="not-a-registered-agent",
    )

    assert result["confirmed_via"] == "pong"
    assert result["echo_fallback"] is True, (
        "this target still never actually reached any downstream target (the underlying echo-fallback fact hasn't changed), "
        "but confirmed_via should prioritize the stronger 'PONG received' signal over echo-fallback"
    )
    assert result["ok"] is False, (
        "ok reflects whether this dispatch actually got delivered at the _run() level, and doesn't change with confirmed_via afterward"
    )


def test_echo_fallback_does_not_self_poison_side_channel_confirmation(monkeypatch):
    """Regression test (#73): on echo fallback, pong_confirmed/side_confirmed
    used to incorrectly end up True.

    Root cause: `dispatch_with_memory_confirm()` used to still call
    `_send_task_report()` to send a "task complete" report to the router's
    own side-channel listener (the one opened by
    `_start_report_side_channel()`) even on echo fallback (when it's certain
    nothing was sent to any downstream target). That report would get picked
    up by the same router and written into `self._side_reports`; later, the
    side-channel confirmation-check logic would then find this self-sent
    report via `_has_side_report()` and misjudge it as
    `side_confirmed=True` -- the same class of "the tower's own action gets
    misjudged as a downstream confirmation" bug as #40 (`wait_for_pong`
    misjudging the tower's own delivery-state bookkeeping as a downstream
    PONG), just a different instance.

    This test doesn't set up a real side-channel socket (that requires
    project="herdr-bridge" to trigger the background listener/Herdr event
    thread, which is heavier and prone to flakiness in a test environment).
    Instead it uses monkeypatch to directly simulate "what happens if
    `_send_task_report()` gets called": it writes the task_id into
    `self._side_reports`, faithfully reproducing the state a real
    self-connected listener would end up in after receiving a report. If the
    call to `_send_task_report()` isn't gated by the echo-fallback check (the
    pre-fix behavior), this monkeypatch gets triggered and pollutes
    `_side_reports`, causing the later `_has_side_report()` check to
    misjudge side_confirmed=True -- this is how we prove that after the fix,
    `_send_task_report()` is never called at all on echo fallback.
    """
    router = create_herdr_router(project="test-echo-fallback-self-poisoning")

    def _poison_side_reports(task_id, agent_id, result):
        # simulate the real effect of "_send_task_report being called": the
        # report loops back through the router's own side-channel listener
        # and gets written into self._side_reports.
        router._side_reports[task_id] = {
            "summary": "self-sent report",
            "tags": ["side-channel", "complete"],
        }

    monkeypatch.setattr(router, "_send_task_report", _poison_side_reports)

    result = router.dispatch_with_memory_confirm(
        "test message",
        target="not-a-registered-agent",
    )

    assert result["echo_fallback"] is True
    assert result["ok"] is False, "echo fallback never actually reached any downstream target, ok cannot be True"
    assert result["pong_confirmed"] is False, "echo fallback has no downstream target, so there can't be a real PONG"
    assert result["side_confirmed"] is False, (
        "_send_task_report should not be called on echo fallback; "
        "the router's own self-sent report must not be misjudged as a downstream side-channel confirmation"
    )
    assert result["task_id"] not in router._side_reports, (
        "_send_task_report should not be called on echo fallback (if the monkeypatch were triggered, "
        "it would write task_id into _side_reports -- this assertion directly proves it was never called)"
    )
