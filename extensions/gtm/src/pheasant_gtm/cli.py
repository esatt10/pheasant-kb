"""Command-line orchestration and progress reporting for GTM runs."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

from pheasant.services import ServiceError
from pheasant_gtm import run_service as gtm
from pheasant_gtm.catalog import Request, ReviewRequest, RunControl
from pheasant_gtm.run_store import RunStore
from pheasant_gtm.runtime import IsolationUnavailable


def register(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="gtm_command", required=True)
    start = actions.add_parser("start", help="Create a checkpoint and print its run ID")
    start.add_argument("company", nargs="?")
    start.add_argument("--website")
    start.add_argument("--url", action="append", default=[], dest="urls")
    start.add_argument("--fields", type=Path, help="YAML or JSON list of custom field definitions")
    start.add_argument("--acquisition", type=Path, help="YAML or JSON acquisition settings")
    start.add_argument("--depth", choices=("standard", "deep"), default="standard")
    start.add_argument("--page-limit", type=int)
    start.add_argument("--job-limit", type=int)
    start.add_argument("--signal-days", type=int, default=365)
    start.add_argument("--no-search", action="store_false", dest="search")
    start.add_argument("--review", action="store_true")
    start.add_argument("--decisions", action="store_true")
    start.add_argument("--model", help="Pheasant chat model catalog ID")
    start.add_argument("--decision-model", help="Pheasant Decisions model catalog ID")
    start.add_argument(
        "--run", action="store_true", help="Continue to completion and print the profile"
    )
    for name, description in (
        ("status", "Inspect progress"),
        ("step", "Advance one checkpoint"),
        ("control", "Change a run's pause state, acquisition policy or pending URLs"),
        ("persistence", "Inspect saved checkpoint counts and model token usage"),
        ("resume", "Advance to completion and print the agent-safe summary"),
        ("report", "Print the summary or export full evidence for an analyst"),
        ("review-packet", "List passage IDs or export excerpts for an analyst"),
        ("apply-review", "Validate and save IDE observations"),
    ):
        action = actions.add_parser(name, help=description)
        action.add_argument("run_id")
        if name == "apply-review":
            action.add_argument("observations", type=Path)
        if name == "control":
            action.add_argument("update", type=Path, help="YAML or JSON run control update")
        if name in {"report", "review-packet"}:
            action.add_argument("--evidence", action="store_true")
            action.add_argument("--output", type=Path, help="New file for the analyst export")


def _drive(store: RunStore, run_id: str, *, config) -> dict:
    while True:
        try:
            progress = gtm.step(store, run_id, config=config)
        except gtm.BusyRun:
            time.sleep(2)
            continue
        if progress["phase"] == "complete":
            return gtm.report(store, run_id)
        if progress.get("paused"):
            return progress
        print(json.dumps(progress, ensure_ascii=True), file=sys.stderr)
        if retry_at := progress.get("next_step_at"):
            time.sleep(max(0, min(2, retry_at - time.time())))


def run(args: argparse.Namespace) -> int:
    from pheasant.config.loader import load_config
    from pheasant.connections import resolve as connections
    from pheasant.persistence.state_store import StateStore
    from pheasant.telemetry.spend import GUARD
    from pheasant.telemetry.usage import METER, channel

    try:
        config = load_config(Path(args.config))
        state = StateStore.from_config(config)
        try:
            store = RunStore(state, config.knowledge_base_id, None)
            command = args.gtm_command
            if command in {"step", "resume"} or (command == "start" and args.run):
                connections.install(config, state)
                METER.bind_for(config, state)
            if command == "start":
                fields = (
                    yaml.safe_load(args.fields.read_text(encoding="utf-8-sig"))
                    if args.fields
                    else []
                )
                policy = (
                    {
                        "acquisition": yaml.safe_load(
                            args.acquisition.read_text(encoding="utf-8-sig")
                        )
                    }
                    if args.acquisition
                    else {}
                )
                result = gtm.start(
                    store,
                    Request.model_validate(
                        {
                            key: getattr(args, key)
                            for key in (
                                "company",
                                "website",
                                "urls",
                                "depth",
                                "page_limit",
                                "job_limit",
                                "signal_days",
                                "search",
                                "review",
                                "decisions",
                                "model",
                                "decision_model",
                            )
                            if getattr(args, key) is not None
                        }
                        | {"fields": fields}
                        | policy
                    ),
                    config=config,
                )
                if args.run:
                    print(f"Research run {result['run_id']}", file=sys.stderr)
                    with channel("cli"):
                        result = _drive(store, result["run_id"], config=config)
            elif command == "resume":
                if gtm.status(store, args.run_id).get("paused"):
                    gtm.control(store, args.run_id, RunControl(paused=False))
                with channel("cli"):
                    result = _drive(store, args.run_id, config=config)
            elif command == "step":
                with channel("cli"):
                    result = gtm.step(store, args.run_id, config=config)
            elif command == "control":
                update = RunControl.model_validate(
                    yaml.safe_load(args.update.read_text(encoding="utf-8-sig"))
                )
                result = gtm.control(store, args.run_id, update)
            elif command == "apply-review":
                packet = ReviewRequest.model_validate_json(
                    args.observations.read_text(encoding="utf-8-sig")
                )
                result = gtm.apply_review(
                    store, args.run_id, packet.model_dump(mode="json")["observations"]
                )
            elif command in {"report", "review-packet"}:
                action = gtm.report if command == "report" else gtm.review_packet
                if args.evidence != bool(args.output):
                    raise ValueError(
                        "use --evidence and --output together to export publisher text"
                    )
                result = action(store, args.run_id, evidence=args.evidence)
                if args.evidence:
                    with args.output.open("x", encoding="utf-8") as output:
                        json.dump(result, output, indent=2, ensure_ascii=True)
                    result = {"saved_to": str(args.output)}
            else:
                action = {
                    "status": gtm.status,
                    "persistence": gtm.persistence,
                }[command]
                result = action(store, args.run_id)
        finally:
            if connections.installed_state() is state:
                connections.uninstall()
            METER.unbind(state)
            GUARD.unbind(state)
            state.close()
    except (
        OSError,
        ValueError,
        yaml.YAMLError,
        gtm.UnknownRun,
        gtm.BusyRun,
        gtm.LegacyRun,
        IsolationUnavailable,
        ServiceError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0
