#!/usr/bin/env python3
"""Run the whole alert workflow for ONE alert.

  STEP 1  FIRST server : find the message in the channel and reprocess it (overwrite)
  STEP 2  SECOND server: clear ONLY the 'Errored' statistic of that one channel

It is a DRY RUN unless you add --execute. Step 2 is only run after step 1 finished
cleanly (message reprocessed and no connector left in ERROR).

Examples (dev test, reprocess Mirth message 5, second channel plays the 'other server'):
    python process_alert.py --env DEV --mirth-message-id 5 --insecure
    python process_alert.py --env DEV --mirth-message-id 5 --insecure --execute

Real alert email:
    python process_alert.py --env CHS --email-file email.txt
    python process_alert.py --env CHS --email-file email.txt --execute

Logins come from environment variables (never from files):
    MIRTH_USERNAME_<ENV> / MIRTH_PASSWORD_<ENV>   (for example MIRTH_USERNAME_CHS)
    or MIRTH_USERNAME / MIRTH_PASSWORD as a fallback
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

import mirth_reprocess as mr
import parse_alert_email as pe

HERE = Path(__file__).resolve().parent


def load_env(config_path: str, env: str) -> dict:
    p = Path(config_path)
    if not p.is_absolute():
        p = HERE / p
    if not p.exists():
        raise mr.MirthError(f"Config file not found: {p}")
    envs = json.loads(p.read_text(encoding="utf-8")).get("environments", {})
    cfg = envs.get(env)
    if not cfg:
        raise mr.MirthError(f"Environment '{env}' not in {p.name}. Available: {', '.join(envs) or 'none'}")
    for key in ("channel_name", "first_server", "second_server"):
        if not cfg.get(key) or "CHANGE_ME" in str(cfg[key]):
            raise mr.MirthError(f"Fill in '{key}' for environment {env} in {p.name}")
    return cfg


def credentials(env: str) -> tuple[str, str]:
    user = (os.getenv(f"MIRTH_USERNAME_{env}") or os.getenv("MIRTH_USERNAME") or "").strip()
    pwd = (os.getenv(f"MIRTH_PASSWORD_{env}") or os.getenv("MIRTH_PASSWORD") or "").strip()
    if not user or not pwd:
        raise mr.MirthError(
            f"Set MIRTH_USERNAME_{env} and MIRTH_PASSWORD_{env} (or MIRTH_USERNAME / MIRTH_PASSWORD)"
        )
    return user, pwd


def open_server(base_url: str, user: str, pwd: str, insecure: bool) -> mr.Mirth:
    if insecure:
        urllib3.disable_warnings()
    server = mr.Mirth(base_url, verify=not insecure)
    server.login(user, pwd)
    return server


def fmt(stats: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in stats.items()) or "(could not read)"


def step_clear_stats(cfg: dict, user: str, pwd: str, insecure: bool, execute: bool) -> dict:
    channel = cfg.get("second_channel_name") or cfg["channel_name"]
    server = open_server(cfg["second_server"], user, pwd, insecure)
    try:
        channel_id = server.channels().get(channel)
        if not channel_id:
            raise mr.MirthError(f"Channel not found on the second server: {channel}")
        dest_ids = sorted(server.destinations(channel_id).values())
        before = server.statistics(channel_id)
        print(f"Second server: {cfg['second_server']}")
        print(f"Channel: {channel}")
        print(f"    statistics now: {fmt(before)}")
        if not execute:
            print("DRY RUN: would clear ONLY the Errored statistic of this channel.")
            return {"cleared": False, "before": before}
        server.clear_error_statistics(channel_id, dest_ids)
        after = server.statistics(channel_id)
        print(f"Errored statistic cleared. Statistics now: {fmt(after)}")
        return {"cleared": True, "before": before, "after": after}
    finally:
        server.logout()


def write_summary(summary: dict) -> Path:
    audit_dir = HERE / "audit"
    audit_dir.mkdir(exist_ok=True)
    path = audit_dir / datetime.now(timezone.utc).strftime("alert-%Y%m%dT%H%M%S%fZ.json")
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return path


def run(args: argparse.Namespace) -> int:
    env = args.env.upper()
    cfg = load_env(args.config, env)
    user, pwd = credentials(env)

    control_id = args.control_id
    if args.email_file:
        text = Path(args.email_file).read_text(encoding="utf-8", errors="replace")
        parsed = pe.parse(text)
        problem = pe.problem_with(parsed)
        if problem:
            raise mr.MirthError(f"Email check failed: {problem}")
        control_id = parsed["control_id"]
        print(f"Alert email OK. Message Control ID: {control_id}")
    if not control_id and not args.mirth_message_id and not args.skip_reprocess:
        raise mr.MirthError("Give --email-file, --control-id or --mirth-message-id")

    summary: dict = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "environment": env,
        "mode": "execute" if args.execute else "dry-run",
        "control_id": control_id,
    }
    print(f"Environment: {env}   mode: {'EXECUTE' if args.execute else 'DRY RUN'}")

    if not args.skip_reprocess:
        print()
        print("=== STEP 1: reprocess on the FIRST server ===")
        os.environ["MIRTH_USERNAME"], os.environ["MIRTH_PASSWORD"] = user, pwd
        ns = argparse.Namespace(
            channel=cfg["channel_name"],
            message_id=control_id,
            mirth_message_id=args.mirth_message_id,
            destination=cfg.get("destination_names") or None,
            execute=args.execute,
            insecure=args.insecure,
            wait=args.wait,
            overwrite=True,
            any_status=False,
            base_url=cfg["first_server"],
            config=args.config,
        )
        rc = mr.do_reprocess(ns)
        result = dict(mr.LAST_AUDIT)
        summary["step1"] = {
            "result": result.get("result"),
            "mirth_message_id": result.get("mirth_message_id"),
            "statuses_after": result.get("statuses_after"),
        }
        if rc != 0:
            summary["outcome"] = "STEP1_FAILED"
            print(f"STOP: step 1 returned code {rc}. Statistics were NOT cleared.")
            print(f"Summary written: {write_summary(summary).name}")
            return 3
        if args.execute:
            final = result.get("statuses_after") or {}
            if result.get("result") != "REPROCESSED" or any(v == "ERROR" for v in final.values()):
                summary["outcome"] = "STEP1_NOT_CLEAN"
                print(
                    "STOP: the reprocess did not finish cleanly (still processing, or a connector is "
                    "in ERROR). Statistics were NOT cleared. Check the message in Mirth; when it is "
                    "fine, run again with --skip-reprocess to do only the statistics step."
                )
                print(f"Summary written: {write_summary(summary).name}")
                return 3

    print()
    print("=== STEP 2: clear Errored statistics on the SECOND server ===")
    summary["step2"] = step_clear_stats(cfg, user, pwd, args.insecure, args.execute)
    summary["outcome"] = "DONE" if args.execute else "DRY_RUN_OK"
    print()
    print(f"Summary written: {write_summary(summary).name}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Reprocess an alerted message, then clear Errored stats on the other server")
    p.add_argument("--env", required=True, help="Environment name from config.json (DEV, CHS, LABFLY)")
    p.add_argument("--email-file", help="Text file with the alert email body")
    p.add_argument("--control-id", help="Message Control ID (instead of an email file)")
    p.add_argument("--mirth-message-id", type=int, help="Use this Mirth message ID and skip the search (dev testing)")
    p.add_argument("--skip-reprocess", action="store_true", help="Only run step 2 (clear Errored stats)")
    p.add_argument("--execute", action="store_true", help="Actually make the changes (default is dry run)")
    p.add_argument("--insecure", action="store_true", help="Skip TLS certificate check (dev only)")
    p.add_argument("--wait", type=int, default=120, help="Seconds to wait for the reprocess to finish")
    p.add_argument("--config", default="config.json")
    args = p.parse_args()
    try:
        return run(args)
    except (mr.MirthError, requests.RequestException, ET.ParseError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
