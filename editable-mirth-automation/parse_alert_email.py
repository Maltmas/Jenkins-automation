#!/usr/bin/env python3
"""Read a 'MISSING PDF ALERT' email and extract the Message Control ID (MSH-10).

Usage:
    python parse_alert_email.py --file email.txt
    python parse_alert_email.py --text "<pasted email body>"
    type email.txt | python parse_alert_email.py          (reads stdin)

Prints JSON, for example:
    {"alert_type": "MISSING_PDF", "control_id": "2014595951289196", "msh_control_id": "2014595951289196"}

Exit code 0 = OK, 1 = nothing safe to act on (unknown alert, no ID, or the two IDs disagree).
Only the control ID is printed. No other message content is shown.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

ALERT_TYPES = {
    "MISSING_PDF": re.compile(r"MISSING\s+PDF\s+ALERT", re.I),
}
CONTROL_ID_RE = re.compile(r"Message\s+Control\s+ID\s*:\s*([A-Za-z0-9._\-]+)", re.I)
MSH_RE = re.compile(r"^\s*MSH\|.*$", re.M)


def detect_alert_type(text: str) -> str | None:
    for name, pattern in ALERT_TYPES.items():
        if pattern.search(text):
            return name
    return None


def control_id_from_line(text: str) -> str | None:
    m = CONTROL_ID_RE.search(text)
    return m.group(1) if m else None


def control_id_from_msh(text: str) -> str | None:
    """MSH-10 is the 10th field after splitting the segment on '|' (MSH itself is field 0)."""
    m = MSH_RE.search(text)
    if not m:
        return None
    fields = m.group(0).strip().split("|")
    if len(fields) > 9 and fields[9].strip():
        return fields[9].strip()
    return None


def parse(text: str) -> dict:
    alert = detect_alert_type(text)
    line_id = control_id_from_line(text)
    msh_id = control_id_from_msh(text)
    return {
        "alert_type": alert,
        "control_id": line_id or msh_id,
        "msh_control_id": msh_id,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Extract the Message Control ID from an alert email")
    p.add_argument("--file", help="Text file containing the email body")
    p.add_argument("--text", help="Email body as text")
    args = p.parse_args()

    if args.file:
        with open(args.file, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    elif args.text:
        text = args.text
    else:
        text = sys.stdin.read()

    result = parse(text)
    print(json.dumps(result))

    if result["alert_type"] is None:
        print("ERROR: not a recognised alert email (expected 'MISSING PDF ALERT').", file=sys.stderr)
        return 1
    if not result["control_id"]:
        print("ERROR: no Message Control ID found.", file=sys.stderr)
        return 1
    if result["msh_control_id"] and result["msh_control_id"] != result["control_id"]:
        print("ERROR: the Message Control ID line and the MSH segment disagree. Not safe to act.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
