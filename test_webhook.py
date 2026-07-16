"""
Fires sample payloads for all 4 Snappic webhook types at a running
local instance of the dashboard, so you can see it working without
ngrok or a real Snappic event.

Usage:
    python test_webhook.py
    python test_webhook.py --url http://localhost:8000/webhook/snappic
"""

import argparse
import json
import time
import urllib.request

SESSION_ID = "test-session-001"

EVENTS = [
    {
        "type": "session",
        "event_id": 1,
        "session": {
            "id": SESSION_ID,
            "direct_url": "https://picsum.photos/seed/a/400/300",
            "site_url": "https://example.com/view/test-session-001",
            "type": "still",
            "device": {"id": 1, "name": "Test Booth", "device_info": "iOS 17"},
        },
    },
    {
        "type": "share",
        "event_id": 2,
        "session": {
            "id": SESSION_ID,
            "direct_url": "https://picsum.photos/seed/b/400/300",
            "site_url": "https://example.com/view/test-session-001",
            "type": "still",
        },
        "share": {"id": "share1", "type": "email", "value": "guest@example.com"},
    },
    {
        "type": "survey",
        "event_id": 3,
        "session": {
            "id": SESSION_ID,
            "direct_url": "https://picsum.photos/seed/c/400/300",
            "site_url": "https://example.com/view/test-session-001",
            "type": "still",
        },
        "survey": {
            "data_capture": {
                "sections": [
                    {
                        "title": "Feedback",
                        "fields": [
                            {"field_title": "How was your experience?", "field_type": "rating", "value": "5"},
                            {"field_title": "Comments", "field_type": "text", "value": "Loved it!"},
                        ],
                    }
                ]
            }
        },
    },
    {
        "type": "competition_win",
        "event_id": 4,
        "session": {
            "id": SESSION_ID,
            "direct_url": "https://picsum.photos/seed/d/400/300",
            "site_url": "https://example.com/view/test-session-001",
            "type": "still",
        },
        "prize": {"id": 1, "name": "Free Coffee"},
        "winner": {"type": "email", "value": "guest@example.com"},
    },
]


def send(url: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        print(f"{payload['type']:16s} -> {resp.status}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000/webhook/snappic")
    parser.add_argument("--delay", type=float, default=1.5, help="seconds between events")
    args = parser.parse_args()

    for event in EVENTS:
        send(args.url, event)
        time.sleep(args.delay)

    print("\nDone — check the dashboard.")
