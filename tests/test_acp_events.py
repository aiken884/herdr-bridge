# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

"""acp.events: NDJSON tolerant reader (R4).

M0 spike evidence (m0-acp-spike-evidence.md §5.2): the envelope described in
the acpx README (`eventVersion` and other fields) simply doesn't exist in the
real 0.12.0 build -- `--format json` output is the raw ACP JSON-RPC message
passed straight through. This module therefore parses standard JSON-RPC/ACP
fields directly and doesn't assume any acpx-specific wrapper exists.
"""

from __future__ import annotations

from herdr_bridge.acp.events import parse_line, parse_stream


def test_parse_line_session_update_message_chunk():
    line = ('{"jsonrpc":"2.0","method":"session/update","params":'
           '{"sessionId":"ses_1","update":{"sessionUpdate":"agent_message_chunk",'
           '"content":{"type":"text","text":"PONG"}}}}')
    ev = parse_line(line)
    assert ev is not None
    assert ev.type == "agent_message_chunk"
    assert ev.session_id == "ses_1"
    assert ev.text == "PONG"


def test_parse_line_session_update_unknown_variant_transparent():
    """Unknown sessionUpdate variant: type passes through, no exception raised (R4)."""
    line = ('{"jsonrpc":"2.0","method":"session/update","params":'
           '{"sessionId":"ses_1","update":{"sessionUpdate":"some_future_variant",'
           '"weird":"shape"}}}')
    ev = parse_line(line)
    assert ev is not None
    assert ev.type == "some_future_variant"
    assert ev.text is None
    assert ev.raw["params"]["update"]["weird"] == "shape"


def test_parse_line_top_level_method_no_session_update_wrapper():
    line = '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":1}}'
    ev = parse_line(line)
    assert ev is not None
    assert ev.type == "initialize"
    assert ev.session_id is None


def test_parse_line_final_result_with_stop_reason():
    line = ('{"jsonrpc":"2.0","id":2,"result":{"stopReason":"end_turn",'
           '"usage":{"inputTokens":5,"outputTokens":3}}}')
    ev = parse_line(line)
    assert ev is not None
    assert ev.type == "result"
    assert ev.raw["result"]["stopReason"] == "end_turn"


def test_parse_line_error_envelope():
    line = ('{"jsonrpc":"2.0","id":null,"error":{"code":-32002,'
           '"message":"No acpx session found"}}')
    ev = parse_line(line)
    assert ev is not None
    assert ev.type == "error"
    assert ev.raw["error"]["code"] == -32002


def test_parse_line_non_json_cli_chrome_line_tolerant():
    """acpx's own status lines (non-JSON) mixed into the output must not blow up
    the reader (a case actually observed in testing)."""
    line = ("[acpx] session t1 (ses_081c243e) · /private/tmp/acp-m0 · "
           "agent needs reconnect")
    ev = parse_line(line)
    assert ev is not None
    assert ev.type == "cli_status"
    assert ev.text == line


def test_parse_line_valid_json_but_not_object_tolerant():
    """Valid JSON but not an object (e.g. a bare array) -- must not be
    misjudged as any shape other than chrome."""
    ev = parse_line("[1, 2, 3]")
    assert ev is not None
    assert ev.type == "cli_status"


def test_parse_line_valid_object_without_method_result_error():
    """A valid JSON object, but without method/result/error -- unknown shapes
    pass through as-is; we don't guess at the semantics."""
    ev = parse_line('{"foo": "bar"}')
    assert ev is not None
    assert ev.type == "unknown"
    assert ev.raw == {"foo": "bar"}


def test_parse_line_blank_line_returns_none():
    assert parse_line("") is None
    assert parse_line("   \n") is None


def test_parse_line_malformed_json_tolerant_not_raise():
    """Malformed JSON (truncated, etc.) must not raise -- this is the core
    promise of the tolerant reader."""
    ev = parse_line('{"jsonrpc":"2.0","method":')
    assert ev is not None
    assert ev.type == "cli_status"  # falls back to passing through the raw text; caller can decide what to do with it


def test_parse_stream_multiple_lines_skips_blanks_and_chrome():
    text = (
        "[acpx] session t1 · agent needs reconnect\n"
        '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{}}\n'
        "\n"
        '{"jsonrpc":"2.0","method":"session/update","params":'
        '{"sessionId":"ses_1","update":{"sessionUpdate":"agent_message_chunk",'
        '"content":{"type":"text","text":"P"}}}}\n'
    )
    events = list(parse_stream(text.splitlines()))
    assert len(events) == 3  # chrome lines pass through too (type cli_status); blank lines are dropped
    assert events[0].type == "cli_status"
    assert events[1].type == "initialize"
    assert events[2].type == "agent_message_chunk"


def test_parse_stream_accumulates_message_text():
    """Typical usage: concatenate agent_message_chunk.text to get the full
    response (the M0-1/O3 code-word test technique)."""
    lines = [
        (
            '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s",'
            '"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"PO"}}}}'
        ),
        (
            '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s",'
            '"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"NG"}}}}'
        ),
    ]
    events = list(parse_stream(lines))
    text = "".join(e.text or "" for e in events if e.type == "agent_message_chunk")
    assert text == "PONG"


def test_extract_stop_reason_from_events():
    from herdr_bridge.acp.events import extract_final_result

    lines = [
        (
            '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s",'
            '"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"hi"}}}}'
        ),
        (
            '{"jsonrpc":"2.0","id":2,"result":{"stopReason":"cancelled",'
            '"usage":{"inputTokens":1,"outputTokens":1}}}'
        ),
    ]
    events = list(parse_stream(lines))
    result = extract_final_result(events)
    assert result is not None
    assert result["stopReason"] == "cancelled"


def test_extract_final_result_none_when_absent():
    from herdr_bridge.acp.events import extract_final_result

    events = list(parse_stream(['{"jsonrpc":"2.0","method":"initialize","params":{}}']))
    assert extract_final_result(events) is None


# -- golden corpus regression (real output captured from the M0 spike against
# acpx 0.12.0 + opencode 1.18.3) --
# R1/R11 mitigation: if a future acpx version changes the output shape, this
# blows up here rather than silently breaking downstream in the M1 consumer.

_FIXTURES_DIR = __import__("pathlib").Path(__file__).parent / "fixtures" / "acpx"


def test_golden_corpus_normal_completion_dsflash():
    from herdr_bridge.acp.events import extract_final_result

    lines = (_FIXTURES_DIR / "oc-dsflash-model-check.ndjson").read_text().splitlines()
    events = list(parse_stream(lines))
    result = extract_final_result(events)
    assert result is not None
    assert result["stopReason"] == "end_turn"
    text = "".join(e.text or "" for e in events if e.type == "agent_message_chunk")
    assert text == "PONGFLASH"


def test_golden_corpus_in_generation_cancel_dsflash():
    """M0 spike §11.2 follow-up test: in-generation cancel correctly reports
    cancelled, with the content truncated."""
    from herdr_bridge.acp.events import extract_final_result

    lines = (_FIXTURES_DIR / "oc-dsflash-cancel-ingeneration.ndjson").read_text().splitlines()
    events = list(parse_stream(lines))
    result = extract_final_result(events)
    assert result is not None
    assert result["stopReason"] == "cancelled"
    text = "".join(e.text or "" for e in events if e.type == "agent_message_chunk")
    assert 0 < len(text) < 600  # confirm it was truncated, not the full 600-word essay
