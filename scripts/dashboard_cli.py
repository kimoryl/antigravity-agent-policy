"""Run the local dashboard directly while developing the plugin."""

from __future__ import annotations

import time
import argparse
import os

from dashboard import start_dashboard
from skill_resolver import SkillService


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', help='Optional isolated preview data directory')
    args = parser.parse_args()
    if args.data_dir:
        os.environ['CODEX_AGENT_POLICY_DATA'] = args.data_dir
    result = start_dashboard(SkillService())
    print(result["url"], flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
