# SPDX-FileCopyrightText: 2026 The herdr-bridge Authors
#
# SPDX-License-Identifier: Apache-2.0

from herdr_bridge.light.cli import build_parser, main


def test_parser_run_dry_run():
    p = build_parser()
    args = p.parse_args(["run", "--dry-run"])
    assert args.command == "run"
    assert args.dry_run is True


def test_parser_start():
    p = build_parser()
    args = p.parse_args(["start"])
    assert args.command == "start"


def test_parser_status():
    p = build_parser()
    args = p.parse_args(["status"])
    assert args.command == "status"


def test_main_no_args_exits():
    # required subcommand → SystemExit
    try:
        main([])
    except SystemExit as e:
        assert e.code != 0
    else:
        raise AssertionError("expected SystemExit")
