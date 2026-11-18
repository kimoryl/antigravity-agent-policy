"""Local CLI for the same runtime delivery operations exposed by MCP.

Example: python scripts/delivery_cli.py --data-dir PATH get TASK_ID
"""

from __future__ import annotations

import argparse
import json
import os

import delivery_state as delivery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', help='Use the same data directory as the Dashboard')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('list')
    get = commands.add_parser('get'); get.add_argument('task_id')
    submit = commands.add_parser('submit'); submit.add_argument('task_id')
    submit.add_argument('--expected-revision', type=int, required=True)
    submit.add_argument('--expected-version', type=int, required=True)
    submit.add_argument('--summary', required=True)
    submit.add_argument('--artifact', action='append', required=True)
    submit.add_argument('--report-summary', required=True)
    submit.add_argument('--report-path')
    reopen = commands.add_parser('reopen'); reopen.add_argument('task_id')
    reopen.add_argument('--expected-revision', type=int, required=True)
    reopen.add_argument('--version', type=int, required=True)
    reopen.add_argument('--reason', required=True)
    args = parser.parse_args()
    if args.data_dir:
        os.environ['CODEX_AGENT_POLICY_DATA'] = args.data_dir
    try:
        if args.action == 'list': result = delivery.board()
        elif args.action == 'get': result = delivery.detail(args.task_id)
        elif args.action == 'reopen': result = delivery.reopen(args.task_id, args.expected_revision, args.version, args.reason)
        else:
            result = delivery.submit(args.task_id, args.expected_revision, args.expected_version, args.summary,
                                     [{'path': p} for p in args.artifact], {'summary': args.report_summary, 'path': args.report_path})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, delivery.DeliveryError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
