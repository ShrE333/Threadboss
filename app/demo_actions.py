from __future__ import annotations

import re
from dataclasses import dataclass


PNQ_TO_BLR_URL = (
    "https://www.ixigo.com/search/result/flight?from=PNQ&to=BLR&date=15092026"
    "&adults=1&children=0&infants=0&class=e&source=Search+Form"
    "&utm_source=Google_Search&utm_medium=paid_search_google"
)

BLR_TO_PNQ_URL = (
    "https://www.ixigo.com/search/result/flight?from=BLR&to=PNQ&date=15092026"
    "&adults=1&children=0&infants=0&class=e&source=Search+Form"
    "&utm_source=Google_Search&utm_medium=paid_search_google"
)


@dataclass(frozen=True)
class DemoFlightAction:
    origin: str
    destination: str
    date_label: str
    url: str


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def match_demo_flight_action(text: str) -> DemoFlightAction | None:
    """Deterministic demo trigger for the live pitch.

    It intentionally bypasses the LLM so the two showcase flight commands are
    guaranteed to produce the expected Ixigo deep link.
    """
    t = _normalize(text)
    if not t:
        return None

    # Keep this narrow so ordinary travel questions still go through ThreadBoss.
    is_booking = any(word in t.split() for word in ("book", "booking"))
    is_flight = any(word in t.split() for word in ("flight", "ticket", "tickets"))
    has_date = bool(re.search(r"\b15\s+(sep|sept|september)(\s+2026)?\b", t))
    if not (is_booking and is_flight and has_date):
        return None

    pune = r"(?:pune|pnq)"
    bangalore = r"(?:bangalore|banglore|bengaluru|blr)"

    if re.search(rf"\bfrom\s+{pune}\s+to\s+{bangalore}\b", t):
        return DemoFlightAction(
            origin="Pune",
            destination="Bangalore",
            date_label="15 September 2026",
            url=PNQ_TO_BLR_URL,
        )

    if re.search(rf"\bfrom\s+{bangalore}\s+to\s+{pune}\b", t):
        return DemoFlightAction(
            origin="Bangalore",
            destination="Pune",
            date_label="15 September 2026",
            url=BLR_TO_PNQ_URL,
        )

    return None
