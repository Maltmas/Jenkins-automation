#!/usr/bin/env python3
"""Reprocess an ERROR message in Mirth Connect (written for Mirth 4.5.x).

Flow:
  1. Read config.json (client -> channel name + destination name)
  2. Log in to the Mirth REST API
  3. Resolve channel ID and destination metaDataId by NAME (no manual IDs)
  4. Find the message by the ID from the alert email (text search, status ERROR)
  5. Check the destination is really in ERROR
  6. Dry run (default) or reprocess (--execute)

Credentials come from environment variables MIRTH_USERNAME / MIRTH_PASSWORD.
No message content (possible PHI) is ever printed or written to the audit log.
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
    def reprocess(self, channel_id: str, message_id: int, dest_meta_id: int) -> None:
        body = (
            f"<messageFilter><minMessageId>{message_id}</minMessageId>"
            f"<maxMessageId>{message_id}</maxMessageId></messageFilter>"
        )
        r = self.s.post(
            f"{self.base}/channels/{channel_id}/messages/_reprocess",
            params={"replace": "false", "filterDestinations": "true", "metaDataId": dest_meta_id},
            data=body,
            headers={**HDR, "Content-Type": "application/xml"},
            timeout=(10, 120),
        )
        r.raise_for_status()


def write_audit(record: dict) -> Path:
    audit_dir = HERE / "audit"
    audit_dir.mkdir(exist_ok=True)
    path = audit_dir / datetime.now(timezone.utc).strftime("audit-%Y%m%dT%H%M%S%fZ.json")
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = HERE / p
    if not p.exists():
        raise MirthError(f"Config file not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def connect(cfg: dict, insecure: bool) -> Mirth:
    user = os.getenv("MIRTH_USERNAME", "").strip()
    pwd = os.getenv("MIRTH_PASSWORD", "").strip()
    if not user or not pwd:
        raise MirthError("Set MIRTH_USERNAME and MIRTH_PASSWORD environment variables")
    if insecure:
        urllib3.disable_warnings()
    mirth = Mirth(cfg["base_url"], verify=not insecure)
    mirth.login(user, pwd)
    return mirth


def do_list(cfg: dict, insecure: bool) -> int:
    mirth = connect(cfg, insecure)
    try:
        for name, cid in sorted(mirth.channels().items()):
            print(f"Channel: {name}  (id={cid})")
            for dname, meta in sorted(mirth.destinations(cid).items(), key=lambda x: x[1]):
                print(f"    destination: {dname}  (metaDataId={meta})")
    finally:
        mirth.logout()
    return 0


def do_reprocess(args: argparse.Namespace, cfg: dict) -> int:
    client = args.client.upper()
    ccfg = cfg.get("clients", {}).get(client)
    if not ccfg:
        raise MirthError(f"Client {client} not found in config")
    channel_name = ccfg.get("channel_name", "")
    dest_name = ccfg.get("destination_name", "")
    if not channel_name or not dest_name or "CHANGE_ME" in (channel_name + dest_name):
        raise MirthError(f"Fill in channel_name / destination_name for {client} in config.json")

    audit = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "client": client,
        "channel_name": channel_name,
        "destination_name": dest_name,
        "business_message_id": args.message_id,
        "mode": "execute" if args.execute else "dry-run",
    }

    mirth = connect(cfg, args.insecure)
    try:
        channel_id = mirth.channels().get(channel_name)
        if not channel_id:
            raise MirthError(f"Channel not found in Mirth: {channel_name}")
        dest_meta = mirth.destinations(channel_id).get(dest_name)
        if dest_meta is None:
            raise MirthError(f"Destination '{dest_name}' not found in channel {channel_name}")
        audit.update(channel_id=channel_id, destination_meta_id=dest_meta)

        # Find the Mirth message
        if args.mirth_message_id:
            msg = mirth.get_message(channel_id, args.mirth_message_id)
            if not msg:
                raise MirthError(f"Mirth message {args.mirth_message_id} not found")
            candidates = [msg]
        else:
            candidates = mirth.find_error_messages(channel_id, args.message_id)
            if not candidates:
                raise MirthError(f"No ERROR message found in {channel_name} for ID {args.message_id}")
            if len(candidates) > 1:
                ids = ", ".join(str(c[0]) for c in candidates)
                raise MirthError(
                    f"{len(candidates)} messages matched (Mirth IDs: {ids}). "
                    "Re-run with --mirth-message-id to pick one."
                )

        mirth_id, statuses = candidates[0]
        audit["mirth_message_id"] = mirth_id
        dest_status = statuses.get(dest_meta, "MISSING")
        audit["destination_status_before"] = dest_status

        print(f"Client: {client}")
        print(f"Channel: {channel_name}")
        print(f"Business ID: {args.message_id}  ->  Mirth message ID: {mirth_id}")
        print(f"{dest_name} status: {dest_status}")

        if dest_status != "ERROR":
            audit["result"] = "SKIPPED_NOT_ERROR"
            raise MirthError(f"{dest_name} status is {dest_status}; only ERROR is eligible. Nothing done.")

        if not args.execute:
            print("DRY RUN: nothing reprocessed. Add --execute to reprocess.")
            audit["result"] = "DRY_RUN_OK"
            return 0

        mirth.reprocess(channel_id, mirth_id, dest_meta)
        print("Reprocess request accepted by Mirth.")

        after = mirth.get_message(channel_id, mirth_id)
        status_after = after[1].get(dest_meta, "UNKNOWN") if after else "UNKNOWN"
        print(f"{dest_name} status after reprocess: {status_after}")
        audit["destination_status_after"] = status_after
        audit["result"] = "REPROCESSED"
        return 0
    finally:
        mirth.logout()
        audit.setdefault("result", "FAILED")
        print(f"Audit written: {write_audit(audit).name}")


def main() -> int:
    p = argparse.ArgumentParser(description="Mirth error-message reprocess automation")
    p.add_argument("--config", default="config.json")
    p.add_argument("--list", action="store_true", help="List channels and destinations (to fill config.json)")
    p.add_argument("--client", help="Client key from config.json, e.g. CHS")
    p.add_argument("--message-id", help="Message ID from the alert email")
    p.add_argument("--mirth-message-id", type=int, help="Skip search; use this internal Mirth message ID")
    p.add_argument("--execute", action="store_true", help="Actually reprocess (default is dry run)")
    p.add_argument("--insecure", action="store_true", help="Skip TLS certificate check (dev only)")
    args = p.parse_args()

    try:
        cfg = load_config(args.config)
        if args.list:
            return do_list(cfg, args.insecure)
        if not args.client or not args.message_id:
            p.error("--client and --message-id are required (or use --list)")
        return do_reprocess(args, cfg)
    except (MirthError, requests.RequestException, ET.ParseError, KeyError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
