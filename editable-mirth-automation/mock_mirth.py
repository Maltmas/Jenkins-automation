from __future__ import annotations

import json
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent


class MockMirthClient:
    """
    Simulates the UAT Mirth server.

    This class does NOT connect to any real Mirth server.
    It is only for local development/testing.
    """

    def __init__(self):
        self.messages_file = PROJECT_DIR / "demo_messages.json"

    def _load_messages(self) -> dict:
        with self.messages_file.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def find_message(
        self,
        business_message_id: str,
        client_name: str,
    ) -> dict:
        messages = self._load_messages()

        record = messages.get(business_message_id)

        if not record:
            raise LookupError(
                f"Mirth message not found for Message ID "
                f"{business_message_id}"
            )

        if record["client"].upper() != client_name.upper():
            raise ValueError(
                "Message belongs to a different client"
            )

        print("MOCK MIRTH: Message found")
        print(f"MOCK MIRTH: Internal Mirth ID = "
              f"{record['mirth_message_id']}")
        print(f"MOCK MIRTH: Status = {record['status']}")
        print(f"MOCK MIRTH: Destination = {record['destination']}")

        return record

    def reprocess_message(
        self,
        mirth_message_id: int,
    ) -> dict:

        messages = self._load_messages()

        for message_id, record in messages.items():

            if record["mirth_message_id"] == mirth_message_id:

                if record["status"].upper() != "ERROR":
                    raise ValueError(
                        "MOCK MIRTH: Message is not in ERROR state"
                    )

                print()
                print("MOCK MIRTH: Reprocessing message...")
                print(
                    f"MOCK MIRTH: Message {mirth_message_id} "
                    f"reprocessed successfully"
                )

                return {
                    "status": "SUCCESS",
                    "message": "REPROCESS_REQUEST_ACCEPTED",
                    "mirth_message_id": mirth_message_id,
                }

        raise LookupError(
            f"MOCK MIRTH: Internal message ID "
            f"{mirth_message_id} not found"
        )