"""``pheasant harness``: run the coordinator as its own worker, reap, inspect."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def register(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="harness_command", required=True)
    worker = sub.add_parser("worker", help="Advance runs fairly across tenants and reap sandboxes")
    worker.add_argument("--once", action="store_true", help="One pass, then exit")
    worker.add_argument("--interval", type=float, default=0.5)
    sub.add_parser("reap", help="Dispose of abandoned sandbox generations")
    status = sub.add_parser("status", help="Print one run's status")
    status.add_argument("run_id")
    sub.add_parser("contract", help="What this build supports")
    sub.add_parser("isolation", help="The declared sandbox isolation manifest")


def _services(args):
    from pheasant.config.loader import load_config
    from pheasant.persistence.state_store import StateStore
    from pheasant.services import ServiceContext

    config = load_config(Path(args.config))
    state = StateStore.from_config(config)
    return ServiceContext(config=config, state=state, searcher=None)


def run(args: argparse.Namespace) -> int:
    from pheasant_harness import service
    from pheasant_harness.context import harness_for
    from pheasant_harness.runner import Runner
    from pheasant_harness.sandbox import isolation_manifest

    if args.harness_command == "isolation":
        print(json.dumps(isolation_manifest(), indent=2))
        return 0
    services = _services(args)
    if args.harness_command == "reap":
        print(json.dumps(service.reap(services), indent=2))
    elif args.harness_command == "status":
        print(json.dumps(service.run_status(services, args.run_id), indent=2, default=str))
    elif args.harness_command == "contract":
        print(json.dumps(service.contract(services), indent=2, default=str))
    elif args.harness_command == "worker":
        runner = Runner(harness_for(services), llm=service._llm(services))
        if args.once:
            print(json.dumps({"advanced": runner.pass_once()}))
            runner._pool.shutdown(wait=True)
            return 0
        while True:  # pragma: no cover - a long-running process
            runner.pass_once()
            time.sleep(args.interval)
    return 0
