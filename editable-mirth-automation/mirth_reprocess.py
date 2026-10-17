#!/usr/bin/env python3
"""Reprocess an ERROR message in Mirth Connect (written for Mirth 4.5.x).

Flow:
  1. Log in to the Mirth REST API
  2. Resolve the channel ID from the channel NAME
  3. Find the message (by the ID from the alert email, or by Mirth message ID)
  4. See which connectors are in ERROR:
       - Source connector in ERROR  -> reprocess the whole message
       - only destinations in ERROR -> reprocess just those destinations
  5. Dry run (default) or reprocess (--execute): uses the single-message endpoint, so only that one message is touched and it is overwritten in place

Credentials come from environment variables MIRTH_USERNAME / MIRTH_PASSWORD.
No message content (possible PHI) is ever printed or written to the audit log.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import sys
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
        r = self.s.post(
            f"{self.base}/users/_login",
            data={"username": user, "password": pwd},
            headers=HDR,
            timeout=TIMEOUT,
        )
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

    def destinations(self, channel_id: str) -> dict[str, int]:
        """Return {destination name: metaDataId} (source connector excluded)."""
        r = self.s.get(f"{self.base}/channels/{channel_id}", headers=XML_ACCEPT, timeout=TIMEOUT)
        r.raise_for_status()
        out: dict[str, int] = {}
        for conn in ET.fromstring(r.content).iter("connector"):
            name = conn.findtext("name")
            meta = conn.findtext("metaDataId")
            if name and meta and meta.isdigit() and int(meta) != 0:
                out[name] = int(meta)
        return out

    @staticmethod
    def _parse_messages(content: bytes) -> list[tuple[int, dict[int, str]]]:
        """Return [(messageId, {metaDataId: STATUS})] from a message or list XML."""
        root = ET.fromstring(content)
        msgs = [root] if root.tag == "message" else list(root.iter("message"))
        result = []
        for m in msgs:
            mid = m.findtext("messageId")
            if not mid or not mid.isdigit():
                continue
            statuses: dict[int, str] = {}
            for cm in m.iter("connectorMessage"):
                meta = cm.findtext("metaDataId")
                if meta is not None and meta.isdigit():
                    statuses[int(meta)] = (cm.findtext("status") or "").upper()
            result.append((int(mid), statuses))
        return result

    def find_error_messages(self, channel_id: str, text: str):
        r = self.s.get(
            f"{self.base}/channels/{channel_id}/messages",
            params={"textSearch": text, "status": "ERROR", "includeContent": "false", "limit": 20},
            headers=XML_ACCEPT,
            timeout=(10, 120),
        )
        r.raise_for_status()
        return self._parse_messages(r.content)

    def get_message(self, channel_id: str, message_id: int):
        r = self.s.get(
            f"{self.base}/channels/{channel_id}/messages/{message_id}",
            params={"includeContent": "false"},
            headers=XML_ACCEPT,
            timeout=TIMEOUT,
        )
        if r.status_code == 404 or not r.content.strip():
            return None
        r.raise_for_status()
        found = self._parse_messages(r.content)
        return found[0] if found else None

    # ---- action ----
    def reprocess(self, channel_id: str, message_id: int, dest_meta_ids: list[int] | None) -> None:
        """Reprocess ONE message and overwrite it in place.

        Uses POST /channels/{channelId}/messages/{messageId}/_reprocess. The message ID is
        part of the URL, so this can never touch any other message in the channel.
        dest_meta_ids=None -> reprocess the whole message (all connectors).
        """
        params: dict = {}
        if dest_meta_ids is None:
            params["filterDestinations"] = "false"
        else:
            params["filterDestinations"] = "true"
            params["metaDataId"] = dest_meta_ids  # repeated query parameter
        r = self.s.post(
            f"{self.base}/channels/{channel_id}/messages/{message_id}/_reprocess",
            params=params,
            headers=HDR,
            timeout=(10, 120),
        )
        r.raise_for_status()


def write_audit(record: dict) -> Path:
    audit_dir = HERE / "audit"
    audit_dir.mkdir(exist_ok=True)
    path = audit_dir / datetime.now(timezone.utc).strftime("audit-%Y%m%dT%H%M%S%fZ.json")
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def resolve_base_url(args: argparse.Namespace) -> str:
    if args.base_url:
        return args.base_url
    if os.getenv("MIRTH_URL"):
        return os.environ["MIRTH_URL"]
    p = Path(args.config)
    if not p.is_absolute():
        p = HERE / p
    if p.exists():
        url = json.loads(p.read_text(encoding="utf-8")).get("base_url")
        if url:
            return url
    raise MirthError("No Mirth URL: use --base-url, MIRTH_URL, or base_url in config.json")


def connect(base_url: str, insecure: bool) -> Mirth:
    user = os.getenv("MIRTH_USERNAME", "").strip()
    pwd = os.getenv("MIRTH_PASSWORD", "").strip()
    if not user or not pwd:
        raise MirthError("Set MIRTH_USERNAME and MIRTH_PASSWORD environment variables")
    if insecure:
        urllib3.disable_warnings()
    mirth = Mirth(base_url, verify=not insecure)
    mirth.login(user, pwd)
    return mirth


def do_list(args: argparse.Namespace) -> int:
    mirth = connect(resolve_base_url(args), args.insecure)
    try:
        for name, cid in sorted(mirth.channels().items()):
            print(f"Channel: {name}  (id={cid})")
            for dname, meta in sorted(mirth.destinations(cid).items(), key=lambda x: x[1]):
                print(f"    destination: {dname}  (metaDataId={meta})")
    finally:
        mirth.logout()
    return 0


def do_reprocess(args: argparse.Namespace) -> int:
    audit = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "channel_name": args.channel,
        "business_message_id": args.message_id,
        "mode": "execute" if args.execute else "dry-run",
    }
    mirth = connect(resolve_base_url(args), args.insecure)
    try:
        channel_id = mirth.channels().get(args.channel)
        if not channel_id:
            raise MirthError(f"Channel not found in Mirth: {args.channel}")
        audit["channel_id"] = channel_id
        dest_names = {meta: name for name, meta in mirth.destinations(channel_id).items()}
        dest_names[0] = "Source"

        # 1. Find the Mirth message
        if args.mirth_message_id:
            msg = mirth.get_message(channel_id, args.mirth_message_id)
            if not msg:
                raise MirthError(f"Mirth message {args.mirth_message_id} not found")
            candidates = [msg]
        else:
            candidates = mirth.find_error_messages(channel_id, args.message_id)
            if not candidates:
                raise MirthError(f"No ERROR message found in {args.channel} for ID {args.message_id}")
            if len(candidates) > 1:
                ids = ", ".join(str(c[0]) for c in candidates)
                raise MirthError(
                    f"{len(candidates)} messages matched (Mirth IDs: {ids}). "
                    "Re-run with --mirth-message-id to pick one."
                )

        mirth_id, statuses = candidates[0]
        audit["mirth_message_id"] = mirth_id
        audit["statuses_before"] = {dest_names.get(k, str(k)): v for k, v in statuses.items()}

        # 2. Decide what to reprocess
        error_ids = sorted(k for k, v in statuses.items() if v == "ERROR")
        print(f"Channel: {args.channel}")
        print(f"Mirth message ID: {mirth_id}")
        for k in sorted(statuses):
            print(f"    {dest_names.get(k, k)}: {statuses[k]}")

        if not error_ids:
            audit["result"] = "SKIPPED_NOT_ERROR"
            raise MirthError("No connector of this message is in ERROR. Nothing done.")

        selected = None
        if args.destination:
            by_name = {n.lower(): m for m, n in dest_names.items() if m != 0}
            selected = []
            for raw in args.destination:
                for d in raw.split(","):
                    d = d.strip()
                    if not d:
                        continue
                    meta = by_name.get(d.lower())
                    if meta is None:
                        avail = ", ".join(sorted(n for m, n in dest_names.items() if m != 0))
                        raise MirthError(
                            f"Destination '{d}' not found in {args.channel}. Available: {avail}"
                        )
                    if meta not in selected:
                        selected.append(meta)
            audit["selected_destinations"] = [dest_names[m] for m in selected]

        if selected is not None:
            names = ", ".join(dest_names[m] for m in selected)
            if 0 in error_ids:
                plan_ids = selected
                print(f"Plan: Source is in ERROR -> reprocess the message, sending ONLY to: {names}")
            else:
                plan_ids = [m for m in selected if statuses.get(m) == "ERROR"]
                if not plan_ids:
                    audit["result"] = "SKIPPED_NOT_ERROR"
                    raise MirthError(f"Selected destination(s) ({names}) are not in ERROR. Nothing done.")
                print(
                    "Plan: reprocess only: "
                    + ", ".join(dest_names[m] for m in plan_ids)
                )
        elif 0 in error_ids:
            plan_ids = None
            print("Plan: Source connector is in ERROR -> reprocess the WHOLE message.")
        else:
            plan_ids = error_ids
            names = ", ".join(dest_names.get(k, str(k)) for k in plan_ids)
            print(f"Plan: only destination(s) in ERROR -> reprocess: {names}")
        audit["plan"] = "whole-message" if plan_ids is None else f"destinations {plan_ids}"

        if not args.execute:
            print("DRY RUN: nothing reprocessed. Add --execute to reprocess.")
            audit["result"] = "DRY_RUN_OK"
            return 0

        # 3. Reprocess and report
        mirth.reprocess(channel_id, mirth_id, plan_ids)
        print("Reprocess request accepted by Mirth (the message is overwritten in place).")

        # Wait up to ~30 s for Mirth to finish, then report the final statuses
        final = statuses
        for _ in range(15):
            time.sleep(2)
            cur = mirth.get_message(channel_id, mirth_id)
            if cur:
                final = cur[1]
                if not any(v in ("RECEIVED", "PENDING") for v in final.values()):
                    break
        state = {dest_names.get(k, str(k)): v for k, v in sorted(final.items())}
        print(f"Statuses after reprocess: {state}")
        audit["statuses_after"] = state
        audit["result"] = "REPROCESSED"
        return 0
    finally:
        mirth.logout()
        audit.setdefault("result", "FAILED")
        print(f"Audit written: {write_audit(audit).name}")


def main() -> int:
    p = argparse.ArgumentParser(description="Mirth error-message reprocess automation")
    p.add_argument("--list", action="store_true", help="List channels and destinations")
    p.add_argument("--channel", help="Exact channel name, e.g. 4_0_Inbound_CCDA_CERNER")
    p.add_argument("--message-id", help="Message ID from the alert email (text search in the channel)")
    p.add_argument("--mirth-message-id", type=int, help="Skip search; use this internal Mirth message ID")
    p.add_argument(
        "--destination",
        action="append",
        help="Reprocess only this destination name (e.g. HTTPS). Repeat or comma-separate for several.",
    )
    p.add_argument("--execute", action="store_true", help="Actually reprocess (default is dry run)")
    p.add_argument("--insecure", action="store_true", help="Skip TLS certificate check (dev only)")
    p.add_argument("--base-url", help="e.g. https://10.15.10.102:9091 (default: MIRTH_URL or config.json)")
    p.add_argument("--config", default="config.json")
    args = p.parse_args()

    try:
        if args.list:
            return do_list(args)
        if not args.channel or not (args.message_id or args.mirth_message_id):
            p.error("--channel and (--message-id or --mirth-message-id) are required, or use --list")
        return do_reprocess(args)
    except (MirthError, requests.RequestException, ET.ParseError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
