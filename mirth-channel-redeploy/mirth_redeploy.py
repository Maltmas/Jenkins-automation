#!/usr/bin/env python3
"""Post-deployment Mirth step automation (written for Mirth 4.5.x).

Replaces the manual steps:
  open channel -> Source tab -> "Poll Once on Start" = Yes -> Save -> Redeploy
  -> channel goes Idle -> Reading -> Idle

Flow (per server, per channel):
  1. Log in to the Mirth REST API
  2. Resolve the channel ID from the channel NAME
  3. Set <pollOnStart>true</pollOnStart> on the source connector (skipped if already Yes)
  4. Redeploy the channel and wait until it is STARTED
  5. Report the received-count change and write an audit record

Standalone project: uses its OWN config.json (next to this script), not the
reprocess automation's config. Runs against the single "server" of the chosen
environment. Use --base-url to target a different server.

Dry run by default; add --execute to save and redeploy.
Credentials come from MIRTH_USERNAME / MIRTH_PASSWORD (same as the reprocess script).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

HERE = Path(__file__).resolve().parent
HDR = {"X-Requested-With": "OpenAPI"}
XML_ACCEPT = {**HDR, "Accept": "application/xml"}
TIMEOUT = (10, 60)


class MirthError(Exception):
    pass


class Mirth:
    def __init__(self, base_url: str, verify: bool = True) -> None:
        self.base = base_url.rstrip("/") + "/api"
        self.s = requests.Session()
        self.s.verify = verify

    # ---- session ----
    def login(self, user: str, pwd: str) -> None:
        r = self.s.post(f"{self.base}/users/_login",
                        data={"username": user, "password": pwd},
                        headers=HDR, timeout=TIMEOUT)
        r.raise_for_status()
        if "SUCCESS" not in r.text.upper():
            raise MirthError("Mirth login was not successful")

    def logout(self) -> None:
        try:
            self.s.post(f"{self.base}/users/_logout", headers=HDR, timeout=TIMEOUT)
        except requests.RequestException:
            pass

    # ---- lookups ----
    def channels(self) -> dict[str, str]:
        """Return {channel name: channel id}."""
        r = self.s.get(f"{self.base}/channels/idsAndNames", headers=XML_ACCEPT, timeout=TIMEOUT)
        r.raise_for_status()
        out: dict[str, str] = {}
        for entry in ET.fromstring(r.content).iter("entry"):
            vals = [x.text for x in entry.findall("string")]
            if len(vals) == 2 and vals[0] and vals[1]:
                out[vals[1]] = vals[0]
        return out

    def channel_xml(self, channel_id: str) -> str:
        r = self.s.get(f"{self.base}/channels/{channel_id}", headers=XML_ACCEPT, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text

    def state(self, channel_id: str) -> str | None:
        """Deployed state: STARTED / STOPPED / PAUSED / DEPLOYING ... or None if not deployed."""
        r = self.s.get(f"{self.base}/channels/{channel_id}/status", headers=XML_ACCEPT, timeout=TIMEOUT)
        if r.status_code != 200 or not r.content.strip():
            return None
        return ET.fromstring(r.content).findtext("state")

    def received(self, channel_id: str) -> int | None:
        r = self.s.get(f"{self.base}/channels/{channel_id}/statistics", headers=XML_ACCEPT, timeout=TIMEOUT)
        if r.status_code != 200 or not r.content.strip():
            return None
        el = ET.fromstring(r.content).find(".//received")
        return int(el.text) if el is not None and el.text and el.text.strip().isdigit() else None

    # ---- actions ----
    def save_channel(self, channel_id: str, xml: str) -> None:
        r = self.s.put(f"{self.base}/channels/{channel_id}",
                       params={"override": "true"},
                       data=xml.encode("utf-8"),
                       headers={**XML_ACCEPT, "Content-Type": "application/xml"},
                       timeout=TIMEOUT)
        r.raise_for_status()
        if "<boolean>false</boolean>" in r.text:
            raise MirthError("Mirth refused to save the channel")

    def deploy(self, channel_id: str) -> None:
        r = self.s.post(f"{self.base}/channels/{channel_id}/_deploy",
                        params={"returnErrors": "true"},
                        headers=HDR, timeout=(10, 180))
        r.raise_for_status()


def enable_poll_on_start(xml: str) -> tuple[str, str]:
    """Flip pollOnStart to true inside <sourceConnector> only.

    Text edit (not XML re-serialise) so scripts/transformers are saved back unchanged.
    Returns (new_xml, 'changed' | 'already' | 'missing').
    """
    start, end = xml.find("<sourceConnector"), xml.find("</sourceConnector>")
    if start == -1 or end == -1:
        return xml, "missing"
    src = xml[start:end]
    if re.search(r"<pollOnStart>\s*true\s*</pollOnStart>", src):
        return xml, "already"
    new_src, n = re.subn(r"<pollOnStart>\s*false\s*</pollOnStart>",
                         "<pollOnStart>true</pollOnStart>", src, count=1)
    if n == 0:
        return xml, "missing"
    return xml[:start] + new_src + xml[end:], "changed"


def write_audit(record: dict) -> Path:
    audit_dir = HERE / "audit"
    audit_dir.mkdir(exist_ok=True)
    path = audit_dir / datetime.now(timezone.utc).strftime("redeploy-%Y%m%dT%H%M%S%fZ.json")
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def resolve_server(args: argparse.Namespace) -> str:
    # MIRTH_URL is deliberately NOT read, so a value set for another
    # Mirth automation on the same Jenkins agent can't redirect this one.
    if args.base_url:
        return args.base_url
    p = Path(args.config)
    if not p.is_absolute():
        p = HERE / p
    if not p.exists():
        raise MirthError(f"Config not found: {p}")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    if not args.env:
        raise MirthError(f"--env is required with config.json (available: {', '.join(cfg)})")
    env = cfg.get(args.env)
    if not env:
        raise MirthError(f"Environment '{args.env}' not in config (available: {', '.join(cfg)})")
    server = env.get("server")
    if not server:
        raise MirthError(f"No 'server' for '{args.env}' in config")
    return server


def process(mirth: Mirth, server: str, name: str, cid: str, args, audit_rows: list) -> None:
    row = {"server": server, "channel_name": name, "channel_id": cid}
    audit_rows.append(row)
    print(f"\n--- {name} on {server}")

    new_xml, status = enable_poll_on_start(mirth.channel_xml(cid))
    row["poll_on_start_before"] = {"changed": "No", "already": "Yes", "missing": "n/a"}[status]
    if status == "missing":
        row["result"] = "FAILED_NO_POLL_SETTING"
        raise MirthError("Source connector has no 'Poll Once on Start' setting (not a polling connector?)")

    print(f"Poll Once on Start currently: {row['poll_on_start_before']}")
    print(f"Deployed state: {mirth.state(cid)}")
    if not args.execute:
        plan = "set Yes, save, redeploy" if status == "changed" else "redeploy only"
        print(f"DRY RUN: would {plan}. Add --execute to run.")
        row["result"] = "DRY_RUN_OK"
        return

    if status == "changed":
        mirth.save_channel(cid, new_xml)
        print("Saved: Poll Once on Start = Yes")

    before = mirth.received(cid)
    mirth.deploy(cid)
    print("Redeploy requested, waiting for STARTED...")

    state, deadline = None, time.time() + args.wait
    while time.time() < deadline:
        state = mirth.state(cid)
        if state == "STARTED":
            break
        time.sleep(3)
    row["state_after"] = state
    if state != "STARTED":
        row["result"] = "FAILED_NOT_STARTED"
        raise MirthError(f"Channel not STARTED after {args.wait}s (last state: {state})")

    time.sleep(args.settle)  # let the poll-on-start read finish (Idle -> Reading -> Idle)
    after = mirth.received(cid)
    row["received"] = [before, after]
    if before is not None and after is not None:
        print(f"Received: {before} -> {after} (+{after - before})")
    print("STARTED - redeploy OK")
    row["result"] = "REDEPLOYED"


def main() -> int:
    p = argparse.ArgumentParser(description="Mirth poll-on-start + redeploy automation")
    p.add_argument("--channel", action="append", required=True,
                   help="Exact channel name. Repeat or comma-separate for several.")
    p.add_argument("--env", help="Environment key in config.json, e.g. CHS")
    p.add_argument("--config", default="config.json")
    p.add_argument("--base-url", help="Single server, overrides config (e.g. http://100.132.0.38:9090)")
    p.add_argument("--execute", action="store_true", help="Actually save and redeploy (default is dry run)")
    p.add_argument("--wait", type=int, default=120, help="Seconds to wait for STARTED (default 120)")
    p.add_argument("--settle", type=int, default=10, help="Seconds to wait for the first poll (default 10)")
    p.add_argument("--insecure", action="store_true", help="Skip TLS check (HTTPS dev servers only)")
    args = p.parse_args()

    names = [c.strip() for raw in args.channel for c in raw.split(",") if c.strip()]
    user = os.getenv("MIRTH_USERNAME", "").strip()
    pwd = os.getenv("MIRTH_PASSWORD", "").strip()
    audit = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "env": args.env,
             "mode": "execute" if args.execute else "dry-run", "channels": names, "rows": []}
    failures: list[str] = []

    try:
        if not user or not pwd:
            raise MirthError("Set MIRTH_USERNAME and MIRTH_PASSWORD environment variables")
        if args.insecure:
            urllib3.disable_warnings()
        for server in [resolve_server(args)]:
            mirth = Mirth(server, verify=not args.insecure)
            try:
                mirth.login(user, pwd)
                ids = mirth.channels()
                for name in names:
                    cid = ids.get(name)
                    if not cid:
                        print(f"\nERROR: channel '{name}' not found on {server}", file=sys.stderr)
                        audit["rows"].append({"server": server, "channel_name": name, "result": "NOT_FOUND"})
                        failures.append(f"{name}@{server}")
                        continue
                    try:
                        process(mirth, server, name, cid, args, audit["rows"])
                    except (MirthError, requests.RequestException, ET.ParseError) as e:
                        print(f"ERROR: {e}", file=sys.stderr)
                        audit["rows"][-1].setdefault("result", "FAILED")
                        failures.append(f"{name}@{server}")
            except (MirthError, requests.RequestException) as e:
                print(f"\nERROR on {server}: {e}", file=sys.stderr)
                failures.append(server)
            finally:
                mirth.logout()
    except MirthError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        failures.append("setup")
    finally:
        audit["result"] = "FAILED" if failures else "OK"
        print(f"\nAudit written: {write_audit(audit).name}")

    if failures:
        print(f"Failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("All done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
