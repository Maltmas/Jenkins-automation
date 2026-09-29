"""Editable proof of concept for Jenkins + Mirth message reprocessing.

Run in demo mode today. Switch to real mode only after receiving approved
Jenkins, network, Mirth API, channel, and connector access.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mock_mirth import MockMirthClient

PROJECT_DIR = Path(__file__).resolve().parent


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def contains_placeholder(value: object) -> bool:
    return "CHANGE_REQUIRED" in str(value)


def validate_client_configuration(client_name: str, client: dict[str, Any]) -> None:
    required = (
        "mirth_base_url",
        "channel_id",
        "pcp_outbound_metadata_id",
    )
    missing = [name for name in required if name not in client]
    if missing:
        raise ValueError(
            f"{client_name} configuration is missing: {', '.join(missing)}"
        )


def resolve_demo_message(
    business_message_id: str,
    requested_client: str,
) -> dict[str, Any]:
    """Replace this later with an approved SQL or Mirth metadata lookup."""
    records = load_json(PROJECT_DIR / "demo_messages.json")
    record = records.get(business_message_id)
    if not record:
        raise LookupError(
            f"No record found for business Message ID {business_message_id}"
        )
    if record["client"].upper() != requested_client.upper():
        raise ValueError(
            "Message belongs to a different client; reprocessing is blocked"
        )
    return record


def validate_reprocessing_candidate(record: dict[str, Any]) -> None:
    if record.get("destination") != "PCP_Outbound":
        raise ValueError("Only PCP_Outbound messages are allowed")

    status = str(record.get("status", "")).upper()
    if status != "ERROR":
        raise ValueError(
            f"Message status is {status}; only ERROR messages are eligible"
        )

    mirth_message_id = record.get("mirth_message_id")
    if not isinstance(mirth_message_id, int) or mirth_message_id <= 0:
        raise ValueError("Resolved Mirth message ID must be a positive integer")

def run_mock(
    business_message_id: str,
    client_name: str,
) -> dict[str, Any]:
    """Run the complete workflow against Mock Mirth."""

    print("MODE: MOCK")
    print("No real Mirth server will be contacted.")
    print()

    mirth = MockMirthClient()

    # Step 1: Find the message
    record = mirth.find_message(
        business_message_id,
        client_name,
    )

    # Step 2: Validate message
    if record.get("status", "").upper() != "ERROR":
        raise ValueError(
            f"Message status is {record.get('status')}; "
            "only ERROR messages can be reprocessed"
        )

    if record.get("destination") != "PCP_Outbound":
        raise ValueError(
            "Only PCP_Outbound messages are allowed"
        )

    # Step 3: Reprocess the message
    result = mirth.reprocess_message(
        record["mirth_message_id"]
    )

    print()
    print("MOCK MIRTH RESULT:")
    print(result["status"])
    print(result["message"])

    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "mock",
        "client": client_name,
        "business_message_id": business_message_id,
        "mirth_message_id": record["mirth_message_id"],
        "destination": record["destination"],
        "requested_action": "reprocess",
        "result": result["status"],
    }

def run_demo(
    business_message_id: str,
    client_name: str,
    client_config: dict[str, Any],
    execute: bool,
) -> dict[str, Any]:
    print("MODE: DEMO (no Mirth request will be made)")
    record = resolve_demo_message(business_message_id, client_name)
    validate_reprocessing_candidate(record)

    result = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "demo",
        "client": client_name,
        "business_message_id": business_message_id,
        "mirth_message_id": record["mirth_message_id"],
        "channel_id": client_config["channel_id"],
        "destination": "PCP_Outbound",
        "destination_metadata_id": client_config[
            "pcp_outbound_metadata_id"
        ],
        "requested_action": "reprocess" if execute else "dry-run",
        "result": "SIMULATED_SUCCESS" if execute else "DRY_RUN_SUCCESS",
    }

    print(f"Client: {client_name}")
    print(f"Email/business Message ID: {business_message_id}")
    print(f"Resolved internal Mirth ID: {record['mirth_message_id']}")
    print("Current status: ERROR")
    print("Destination: PCP_Outbound")
    if execute:
        print("SIMULATION: a destination-only reprocess would be requested.")
    else:
        print("DRY RUN: no reprocessing was performed.")
    return result


class MirthClient:
    """Minimal real Mirth client. This path remains blocked by default."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        try:
            import requests
        except ImportError as error:
            raise RuntimeError(
                "Install dependencies: python -m pip install -r requirements.txt"
            ) from error

        self.requests = requests
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update(
            {"X-Requested-With": "OpenAPI", "Accept": "application/xml"}
        )

    def login(self) -> None:
        response = self.session.post(
            f"{self.base_url}/api/users/_login",
            data={"username": self.username, "password": self.password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=(10, 30),
        )
        response.raise_for_status()
        if "SUCCESS" not in response.text.upper():
            raise RuntimeError("Mirth login was not successful")

    def inspect_message(self, channel_id: str, mirth_message_id: int) -> None:
        response = self.session.get(
            f"{self.base_url}/api/channels/{channel_id}/messages/{mirth_message_id}",
            params={"metaDataId": 0},
            timeout=(10, 60),
        )
        response.raise_for_status()
        print(f"Mirth message lookup: HTTP {response.status_code}")
        print("Message payload was not printed because it may contain PHI.")

    def reprocess_destination(
        self,
        channel_id: str,
        mirth_message_id: int,
        destination_metadata_id: int,
    ) -> None:
        response = self.session.post(
            (
                f"{self.base_url}/api/channels/{channel_id}/messages/"
                f"{mirth_message_id}/_reprocess"
            ),
            params={
                "replace": "false",
                "filterDestinations": "true",
                "metaDataId": destination_metadata_id,
            },
            timeout=(10, 120),
        )
        response.raise_for_status()
        print(f"Reprocess request: HTTP {response.status_code}")


def run_real(
    mirth_message_id: int | None,
    client_config: dict[str, Any],
    execute: bool,
) -> dict[str, Any]:
    if any(contains_placeholder(value) for value in client_config.values()):
        raise ValueError("Real mode is blocked: CHANGE_REQUIRED values remain")
    if mirth_message_id is None or mirth_message_id <= 0:
        raise ValueError("--mirth-message-id is required in real mode")

    username = os.getenv("MIRTH_USERNAME", "").strip()
    password = os.getenv("MIRTH_PASSWORD", "").strip()
    if not username or not password:
        raise ValueError("MIRTH_USERNAME and MIRTH_PASSWORD are required")

    client = MirthClient(
        client_config["mirth_base_url"],
        username,
        password,
    )
    client.login()
    client.inspect_message(client_config["channel_id"], mirth_message_id)

    if execute:
        if os.getenv("CONFIRM_REPROCESS", "NO") != "YES":
            raise ValueError("Set CONFIRM_REPROCESS=YES to allow execution")
        client.reprocess_destination(
            client_config["channel_id"],
            mirth_message_id,
            int(client_config["pcp_outbound_metadata_id"]),
        )
        result_text = "REPROCESS_REQUEST_ACCEPTED"
    else:
        print("DRY RUN: message was inspected but not reprocessed.")
        result_text = "DRY_RUN_SUCCESS"

    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "real",
        "mirth_message_id": mirth_message_id,
        "result": result_text,
    }


def write_audit(result: dict[str, Any]) -> Path:
    audit_dir = PROJECT_DIR / "audit"
    audit_dir.mkdir(exist_ok=True)
    filename = datetime.now(timezone.utc).strftime("audit-%Y%m%dT%H%M%SZ.json")
    output = audit_dir / filename
    with output.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mirth automation proof of concept")
    parser.add_argument("--client", required=True, choices=("CHS", "LABFLY"))
    parser.add_argument("--message-id", required=True, help="ID received in email")
    parser.add_argument(
        "--mirth-message-id",
        type=int,
        help="Internal numeric Mirth ID; required only in real mode",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Request a reprocess; without this flag the run is read-only",
    )
    parser.add_argument(
        "--config",
        default="config.example.json",
        help="Path to JSON configuration",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        config = load_json(Path(args.config))
        client_name = args.client.upper()
        client_config = config["clients"][client_name]

        validate_client_configuration(
            client_name,
            client_config,
        )

        mode = config.get("mode", "demo").lower()

        if mode == "demo":
            result = run_demo(
                args.message_id,
                client_name,
                client_config,
                args.execute,
            )

        elif mode == "mock":
            result = run_mock(
                args.message_id,
                client_name,
            )

        else:
            result = run_real(
                args.mirth_message_id,
                client_config,
                args.execute,
            )

        audit_path = write_audit(result)

        print(f"Audit written: {audit_path.name}")

        return 0

    except (
        KeyError,
        ValueError,
        LookupError,
        FileNotFoundError,
        RuntimeError,
    ) as error:

        print(f"ERROR: {error}", file=sys.stderr)

        return 1

    except Exception as error:

        print(
            f"UNEXPECTED ERROR: {error}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())

