"""Tool registrations for the airline MCP server.

The goal of this module is to stay approachable for anyone extending the
codebase. The `register_tools` function is the single place where every tool is
declared. Each tool maps closely to an operation on the
``AirlineDatabase``—think of it as the contract between the MCP surface area and
your underlying data access layer.

When you add new behaviour, prefer creating small helper functions (similar to
``_search_direct_flight``) and keep the tool definitions focused on:

* validating parameters
* calling database helpers
* returning serialisable payloads (usually JSON strings)
"""

from __future__ import annotations

import json
import hashlib
import logging
import secrets
import time
import copy
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Annotated, List
from typing import Any

from fastmcp import FastMCP

from .database import AirlineDatabase

__all__ = ["register_tools"]
logger = logging.getLogger(__name__)

CONFIRMATION_REQUIRED_TOOLS = {
    "book_reservation",
    "cancel_reservation",
    "update_reservation_baggages",
    "update_reservation_flights",
    "update_reservation_passengers",
    "send_certificate",
}


def _action_hash(tool_name: str, arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        {"tool_name": tool_name, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _consume_confirmation(
    db: AirlineDatabase,
    tool_name: str,
    arguments: dict[str, Any],
    confirmation_id: str | None,
) -> str:
    """Consume a one-use grant for the exact action a user confirmed."""
    if not confirmation_id:
        raise ValueError("This action requires explicit user confirmation")

    grant = db.get_state()["_security"]["confirmations"].get(confirmation_id)
    if grant is None:
        raise ValueError("Confirmation is missing or has already been used")
    if grant["expires_at"] < time.time():
        raise ValueError("Confirmation has expired")
    if grant["tool_name"] != tool_name or grant["arguments_hash"] != _action_hash(
        tool_name, arguments
    ):
        raise ValueError("Action does not match the confirmed details")
    if grant["used"]:
        raise ValueError("Confirmation has already been used")

    grant["used"] = True
    db.save()
    return confirmation_id


def _record_action(
    db: AirlineDatabase,
    action_id: str,
    tool_name: str,
    user_id: str,
    summary: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    receipt = {
        "action_id": action_id,
        "action": tool_name,
        "status": "succeeded",
        "user_id": user_id,
        "summary": summary,
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    db.get_state()["_security"]["actions"].append(receipt)
    db.save()
    return receipt


def _with_receipt(payload: dict[str, Any], receipt: dict[str, Any]) -> str:
    result = dict(payload)
    result["_action_receipt"] = receipt
    return json.dumps(result, indent=2)


def _reservation_belongs_to_user(
    db: AirlineDatabase, reservation_id: str, user_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    reservation = db.get_reservation(reservation_id)
    if reservation["user_id"] != user_id:
        raise ValueError("Reservation does not belong to this user")
    return reservation, db.get_user(user_id)


def _flight_status(db: AirlineDatabase, flight: dict[str, Any]) -> str:
    return db.get_flight_instance(flight["flight_number"], flight["date"])["status"]


def _cancellation_denial(
    db: AirlineDatabase, reservation: dict[str, Any], reason: str
) -> str | None:
    if reservation.get("status") == "cancelled":
        return "Reservation is already cancelled"

    statuses = [_flight_status(db, flight) for flight in reservation["flights"]]
    if any(status in {"flying", "landed"} for status in statuses):
        return "A portion of this itinerary has already been flown"

    now = datetime.fromisoformat(db.get_date_time())
    created_at = datetime.fromisoformat(reservation["created_at"])
    within_24_hours = timedelta(0) <= now - created_at <= timedelta(hours=24)
    airline_cancelled = "cancelled" in statuses
    insured_reason = reservation.get("insurance") == "yes" and reason in {
        "health",
        "weather",
    }
    if not (
        within_24_hours
        or airline_cancelled
        or reservation.get("cabin") == "business"
        or insured_reason
    ):
        return "Cancellation is not allowed by the airline policy"
    return None


def _remember_delayed_itinerary(
    db: AirlineDatabase, reservation: dict[str, Any], action_id: str
) -> None:
    delayed = [
        {"flight_number": flight["flight_number"], "date": flight["date"]}
        for flight in reservation["flights"]
        if _flight_status(db, flight) == "delayed"
    ]
    if delayed:
        events = db.get_state()["_security"]["delay_events"].setdefault(
            reservation["reservation_id"], []
        )
        events.append({"action_id": action_id, "flights": delayed})


def _database_locked(db: AirlineDatabase, rollback_on_error: bool = False):
    """Serialize a tool operation against other mutations in this process."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with db.lock:
                if not rollback_on_error:
                    return function(*args, **kwargs)

                snapshot = copy.deepcopy(db.get_state())
                try:
                    return function(*args, **kwargs)
                except Exception:
                    confirmation_id = kwargs.get("confirmation_id")
                    current_grants = db.get_state()["_security"]["confirmations"]
                    consumed = bool(
                        confirmation_id
                        and current_grants.get(confirmation_id, {}).get("used")
                    )
                    state = db.get_state()
                    state.clear()
                    state.update(snapshot)
                    if consumed:
                        state["_security"]["confirmations"][confirmation_id][
                            "used"
                        ] = True
                        db.save()
                    raise

        return wrapped

    return decorate


def _search_direct_flight(
    db: AirlineDatabase,
    date: str,
    origin: str | None = None,
    destination: str | None = None,
    leave_after: str | None = None,
) -> List[dict]:
    """Internal helper to search for direct flights."""

    results: List[dict] = []
    db_state = db.get_state()

    for flight in db_state["flights"].values():
        matches_query = (
            (origin is None or flight["origin"] == origin)
            and (destination is None or flight["destination"] == destination)
            and (date in flight["dates"])
            and (flight["dates"][date]["status"] == "available")
            and (
                leave_after is None
                or flight["scheduled_departure_time_est"] >= leave_after
            )
        )

        if not matches_query:
            continue

        flight_date_data = flight["dates"][date]
        results.append(
            {
                "flight_number": flight["flight_number"],
                "origin": flight["origin"],
                "destination": flight["destination"],
                "status": "available",
                "scheduled_departure_time_est": flight["scheduled_departure_time_est"],
                "scheduled_arrival_time_est": flight["scheduled_arrival_time_est"],
                "available_seats": flight_date_data["available_seats"],
                "prices": flight_date_data["prices"],
            }
        )

    return results


def _payment_for_update(
    user: dict,
    payment_id: str,
    total_price: float,
) -> dict | None:
    """Process payment for a reservation update."""

    if payment_id not in user["payment_methods"]:
        raise ValueError("Payment method not found")

    payment_method = user["payment_methods"][payment_id]

    if payment_method["source"] == "certificate":
        raise ValueError("Certificate cannot be used to update reservation")

    if (
        payment_method["source"] == "gift_card"
        and payment_method["amount"] < total_price
    ):
        raise ValueError("Gift card balance is not enough")

    if payment_method["source"] == "gift_card":
        payment_method["amount"] -= total_price

    if total_price != 0:
        return {
            "payment_id": payment_id,
            "amount": total_price,
        }

    return None


def _parse_json_argument(raw_value: str, argument_name: str) -> Any:
    """Parse a JSON string and raise a friendly ``ValueError`` on failure."""

    try:
        return json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{argument_name} must be valid JSON") from exc


def register_tools(mcp: FastMCP, db: AirlineDatabase) -> None:
    """Register all airline tools with the MCP server."""

    @mcp.tool()
    def authorize_action(
        tool_name: Annotated[str, "The protected MCP action to authorize"],
        arguments: Annotated[str, "Canonical JSON object containing the exact action arguments"],
    ) -> str:
        """Create a short-lived, single-use grant after the trusted UI confirms an action."""
        if tool_name not in CONFIRMATION_REQUIRED_TOOLS:
            raise ValueError("This action cannot be authorized")

        action_arguments = _parse_json_argument(arguments, "arguments")
        if not isinstance(action_arguments, dict) or "confirmation_id" in action_arguments:
            raise ValueError("arguments must be an object without a confirmation_id")

        with db.lock:
            confirmation_id = secrets.token_urlsafe(32)
            grants = db.get_state()["_security"]["confirmations"]
            now = time.time()
            for existing_id in [
                key for key, grant in grants.items() if grant["expires_at"] < now
            ]:
                del grants[existing_id]
            grants[confirmation_id] = {
                "tool_name": tool_name,
                "arguments_hash": _action_hash(tool_name, action_arguments),
                "expires_at": now + 300,
                "used": False,
            }
            db.save()
            return json.dumps({"confirmation_id": confirmation_id})

    # ------------------------------------------------------------------
    # Reservation lifecycle tools
    # ------------------------------------------------------------------
    @mcp.tool()
    @_database_locked(db, rollback_on_error=True)
    def book_reservation(
        user_id: Annotated[
            str,
            "The ID of the user to book the reservation such as 'sara_doe_496'",
        ],
        origin: Annotated[
            str,
            "The IATA code for the origin city such as 'SFO'",
        ],
        destination: Annotated[
            str,
            "The IATA code for the destination city such as 'JFK'",
        ],
        flight_type: Annotated[
            str,
            "The type of flight such as 'one_way' or 'round_trip'",
        ],
        cabin: Annotated[
            str,
            "The cabin class such as 'basic_economy', 'economy', or 'business'",
        ],
        flights: Annotated[
            str,
            "JSON array of objects containing flight_number and date for each flight segment",
        ],
        passengers: Annotated[
            str,
            "JSON array of objects containing first_name, last_name, and dob for each passenger",
        ],
        payment_methods: Annotated[
            str,
            "JSON array of objects containing payment_id and amount for each payment",
        ],
        total_baggages: Annotated[
            int,
            "The total number of baggage items to book",
        ],
        nonfree_baggages: Annotated[
            int,
            "The number of non-free baggage items to book",
        ],
        insurance: Annotated[str, "Whether the reservation has insurance: 'yes' or 'no'"],
        confirmation_id: Annotated[
            str | None,
            "Internal one-use confirmation grant; do not supply this value",
        ] = None,
    ) -> str:
        """Create a brand-new reservation and record the full payment detail."""

        action_arguments = {
            "user_id": user_id,
            "origin": origin,
            "destination": destination,
            "flight_type": flight_type,
            "cabin": cabin,
            "flights": flights,
            "passengers": passengers,
            "payment_methods": payment_methods,
            "total_baggages": total_baggages,
            "nonfree_baggages": nonfree_baggages,
            "insurance": insurance,
        }
        flights_list = _parse_json_argument(flights, "flights")
        if not isinstance(flights_list, list):
            raise ValueError("flights must be a JSON array")

        passengers_list = _parse_json_argument(passengers, "passengers")
        if not isinstance(passengers_list, list):
            raise ValueError("passengers must be a JSON array")
        if not 1 <= len(passengers_list) <= 5:
            raise ValueError("A reservation must have between one and five passengers")

        payment_methods_list = _parse_json_argument(
            payment_methods,
            "payment_methods",
        )
        if not isinstance(payment_methods_list, list):
            raise ValueError("payment_methods must be a JSON array")

        user = db.get_user(user_id)
        reservation_id = db.get_new_reservation_id()
        db_state = db.get_state()

        reservation = {
            "reservation_id": reservation_id,
            "user_id": user_id,
            "origin": origin,
            "destination": destination,
            "flight_type": flight_type,
            "cabin": cabin,
            "flights": [],
            "passengers": json.loads(json.dumps(passengers_list)),
            "payment_history": json.loads(json.dumps(payment_methods_list)),
            "created_at": db.get_date_time(),
            "total_baggages": total_baggages,
            "nonfree_baggages": nonfree_baggages,
            "insurance": insurance,
        }

        total_price = 0.0
        all_flights_date_data = []

        for flight_info in flights_list:
            flight_number = flight_info["flight_number"]
            flight = db.get_flight(flight_number)
            flight_date_data = db.get_flight_instance(
                flight_number, flight_info["date"]
            )

            if flight_date_data["status"] != "available":
                raise ValueError(
                    f"Flight {flight_number} not available on date {flight_info['date']}"
                )

            if flight_date_data["available_seats"][cabin] < len(passengers_list):
                raise ValueError(f"Not enough seats on flight {flight_number}")

            price = flight_date_data["prices"][cabin]

            reservation["flights"].append(
                {
                    "origin": flight["origin"],
                    "destination": flight["destination"],
                    "flight_number": flight_number,
                    "date": flight_info["date"],
                    "price": price,
                }
            )

            all_flights_date_data.append(flight_date_data)
            total_price += price * len(passengers_list)

        if insurance == "yes":
            total_price += 30 * len(passengers_list)

        total_price += 50 * nonfree_baggages

        for payment_method in payment_methods_list:
            payment_id = payment_method["payment_id"]
            amount = payment_method["amount"]

            if payment_id not in user["payment_methods"]:
                raise ValueError(f"Payment method {payment_id} not found")

            user_payment_method = user["payment_methods"][payment_id]
            if user_payment_method["source"] in ["gift_card", "certificate"] and user_payment_method["amount"] < amount:
                raise ValueError(
                    f"Not enough balance in payment method {payment_id}"
                )

        total_payment = sum(p["amount"] for p in payment_methods_list)
        if total_payment != total_price:
            raise ValueError(
                "Payment amount does not add up, total price is"
                f" {total_price}, but paid {total_payment}"
            )

        action_id = _consume_confirmation(
            db, "book_reservation", action_arguments, confirmation_id
        )
        for payment_method in payment_methods_list:
            payment_id = payment_method["payment_id"]
            amount = payment_method["amount"]
            user_payment_method = user["payment_methods"][payment_id]

            if user_payment_method["source"] == "gift_card":
                user_payment_method["amount"] -= amount
            elif user_payment_method["source"] == "certificate":
                del user["payment_methods"][payment_id]

        for flight_date_data in all_flights_date_data:
            flight_date_data["available_seats"][cabin] -= len(passengers_list)

        db_state["reservations"][reservation_id] = reservation
        user["reservations"].append(reservation_id)

        receipt = _record_action(
            db,
            action_id,
            "book_reservation",
            user_id,
            f"Created reservation {reservation_id}",
            {"reservation_id": reservation_id, "total_price": total_price},
        )
        return _with_receipt(reservation, receipt)

    @mcp.tool()
    @_database_locked(db, rollback_on_error=True)
    def cancel_reservation(
        user_id: Annotated[str, "The booking user's private user ID"],
        reservation_id: Annotated[str, "The reservation ID, such as 'ZFA04Y'"],
        reason: Annotated[
            str,
            "Cancellation reason: change_of_plan, airline_cancelled, health, weather, or other",
        ],
        confirmation_id: Annotated[
            str | None,
            "Internal one-use confirmation grant; do not supply this value",
        ] = None,
    ) -> str:
        """Cancel an existing reservation and write refund records."""

        if reason not in {
            "change_of_plan",
            "airline_cancelled",
            "health",
            "weather",
            "other",
        }:
            raise ValueError("Invalid cancellation reason")

        action_arguments = {
            "user_id": user_id,
            "reservation_id": reservation_id,
            "reason": reason,
        }
        reservation, _ = _reservation_belongs_to_user(db, reservation_id, user_id)
        denial = _cancellation_denial(db, reservation, reason)
        if denial:
            raise ValueError(denial)

        action_id = _consume_confirmation(
            db, "cancel_reservation", action_arguments, confirmation_id
        )
        _remember_delayed_itinerary(db, reservation, action_id)

        refunds = [
            {
                "payment_id": payment["payment_id"],
                "amount": -payment["amount"],
            }
            for payment in reservation["payment_history"]
        ]

        reservation["payment_history"].extend(refunds)
        reservation["status"] = "cancelled"

        logger.warning("Seats release not implemented for cancellation")

        receipt = _record_action(
            db,
            action_id,
            "cancel_reservation",
            user_id,
            f"Cancelled reservation {reservation_id}",
            {
                "reservation_id": reservation_id,
                "reason": reason,
                "refunds": refunds,
            },
        )
        return _with_receipt(reservation, receipt)

    @mcp.tool()
    @_database_locked(db)
    def get_reservation_details(
        user_id: Annotated[str, "The booking user's private user ID"],
        reservation_id: Annotated[str, "The reservation ID, such as '8JX2WO'"],
    ) -> str:
        """Return the reservation payload so MCP clients can render it."""

        reservation, _ = _reservation_belongs_to_user(db, reservation_id, user_id)
        return json.dumps(reservation, indent=2)

    @mcp.tool()
    @_database_locked(db, rollback_on_error=True)
    def update_reservation_baggages(
        user_id: Annotated[str, "The booking user's private user ID"],
        reservation_id: Annotated[str, "The reservation ID, such as 'ZFA04Y'"],
        total_baggages: Annotated[int, "The updated total number of baggage items"],
        nonfree_baggages: Annotated[
            int, "The updated number of non-free baggage items"
        ],
        payment_id: Annotated[
            str,
            "The payment id stored in user profile, such as 'credit_card_7815826'",
        ],
        confirmation_id: Annotated[
            str | None,
            "Internal one-use confirmation grant; do not supply this value",
        ] = None,
    ) -> str:
        """Adjust baggage counts while collecting any additional payment."""

        action_arguments = {
            "user_id": user_id,
            "reservation_id": reservation_id,
            "total_baggages": total_baggages,
            "nonfree_baggages": nonfree_baggages,
            "payment_id": payment_id,
        }
        reservation, user = _reservation_belongs_to_user(db, reservation_id, user_id)
        if total_baggages < reservation["total_baggages"]:
            raise ValueError("Checked bags cannot be removed")
        if nonfree_baggages < reservation["nonfree_baggages"]:
            raise ValueError("Paid checked bags cannot be removed")
        if nonfree_baggages > total_baggages:
            raise ValueError("Paid checked bags cannot exceed total checked bags")

        total_price = 50 * max(0, nonfree_baggages - reservation["nonfree_baggages"])

        action_id = _consume_confirmation(
            db, "update_reservation_baggages", action_arguments, confirmation_id
        )
        payment = _payment_for_update(user, payment_id, total_price)
        if payment is not None:
            reservation["payment_history"].append(payment)

        reservation["total_baggages"] = total_baggages
        reservation["nonfree_baggages"] = nonfree_baggages

        receipt = _record_action(
            db,
            action_id,
            "update_reservation_baggages",
            user_id,
            f"Updated baggage for reservation {reservation_id}",
            {
                "reservation_id": reservation_id,
                "total_baggages": total_baggages,
                "nonfree_baggages": nonfree_baggages,
                "payment": payment,
            },
        )
        return _with_receipt(reservation, receipt)

    @mcp.tool()
    @_database_locked(db, rollback_on_error=True)
    def update_reservation_flights(
        user_id: Annotated[str, "The booking user's private user ID"],
        reservation_id: Annotated[str, "The reservation ID, such as 'ZFA04Y'"],
        cabin: Annotated[
            str,
            "The cabin class: 'basic_economy', 'economy', or 'business'",
        ],
        flights: Annotated[
            str,
            "JSON array of flight info objects with flight_number and date for ALL flights in reservation",
        ],
        payment_id: Annotated[
            str,
            "The payment id stored in user profile, such as 'credit_card_7815826'",
        ],
        confirmation_id: Annotated[
            str | None,
            "Internal one-use confirmation grant; do not supply this value",
        ] = None,
    ) -> str:
        """Swap flights in a reservation, charging the fare difference."""

        action_arguments = {
            "user_id": user_id,
            "reservation_id": reservation_id,
            "cabin": cabin,
            "flights": flights,
            "payment_id": payment_id,
        }
        flights_list = _parse_json_argument(flights, "flights")
        if not isinstance(flights_list, list):
            raise ValueError("flights must be a JSON array")

        reservation, user = _reservation_belongs_to_user(db, reservation_id, user_id)
        if reservation.get("status") == "cancelled":
            raise ValueError("Cancelled reservations cannot be modified")
        if cabin not in {"basic_economy", "economy", "business"}:
            raise ValueError("Invalid cabin class")
        if len({(f.get("flight_number"), f.get("date")) for f in flights_list}) != len(
            flights_list
        ):
            raise ValueError("A flight instance cannot appear more than once")

        old_flight_keys = {
            (flight["flight_number"], flight["date"])
            for flight in reservation["flights"]
        }
        new_flight_keys = {
            (flight["flight_number"], flight["date"]) for flight in flights_list
        }
        flights_are_unchanged = (
            old_flight_keys == new_flight_keys and len(flights_list) == len(old_flight_keys)
        )
        if reservation["cabin"] == "basic_economy" and not flights_are_unchanged:
            raise ValueError("Basic economy reservations cannot change flights")
        if cabin != reservation["cabin"] and any(
            _flight_status(db, flight) in {"flying", "landed"}
            for flight in reservation["flights"]
        ):
            raise ValueError("Cabin cannot be changed after a flight has been flown")

        total_price = 0.0
        reservation_flights = []

        for flight_info in flights_list:
            matching_flight = next(
                (
                    rf
                    for rf in reservation["flights"]
                    if rf["flight_number"] == flight_info["flight_number"]
                    and rf["date"] == flight_info["date"]
                    and cabin == reservation["cabin"]
                ),
                None,
            )

            if matching_flight:
                total_price += matching_flight["price"] * len(
                    reservation["passengers"]
                )
                reservation_flights.append(matching_flight)
                continue

            flight = db.get_flight(flight_info["flight_number"])
            flight_date_data = db.get_flight_instance(
                flight_info["flight_number"], flight_info["date"]
            )

            if flight_date_data["status"] != "available":
                raise ValueError(
                    f"Flight {flight_info['flight_number']} not available on date {flight_info['date']}"
                )

            if flight_date_data["available_seats"][cabin] < len(reservation["passengers"]):
                raise ValueError(
                    f"Not enough seats on flight {flight_info['flight_number']}"
                )

            reservation_flight = {
                "flight_number": flight_info["flight_number"],
                "date": flight_info["date"],
                "price": flight_date_data["prices"][cabin],
                "origin": flight["origin"],
                "destination": flight["destination"],
            }
            total_price += reservation_flight["price"] * len(
                reservation["passengers"]
            )
            reservation_flights.append(reservation_flight)

        original_price = (
            sum(f["price"] for f in reservation["flights"])
            * len(reservation["passengers"])
        )
        total_price -= original_price

        action_id = _consume_confirmation(
            db, "update_reservation_flights", action_arguments, confirmation_id
        )
        payment = _payment_for_update(user, payment_id, total_price)
        if payment is not None:
            reservation["payment_history"].append(payment)

        for flight in reservation["flights"]:
            key = (flight["flight_number"], flight["date"])
            if key not in new_flight_keys or cabin != reservation["cabin"]:
                status = db.get_flight_instance(*key)
                status["available_seats"][reservation["cabin"]] += len(
                    reservation["passengers"]
                )
        for flight in reservation_flights:
            key = (flight["flight_number"], flight["date"])
            if key not in old_flight_keys or cabin != reservation["cabin"]:
                status = db.get_flight_instance(*key)
                status["available_seats"][cabin] -= len(reservation["passengers"])

        _remember_delayed_itinerary(db, reservation, action_id)
        reservation["flights"] = reservation_flights
        reservation["cabin"] = cabin

        receipt = _record_action(
            db,
            action_id,
            "update_reservation_flights",
            user_id,
            f"Updated flights for reservation {reservation_id}",
            {
                "reservation_id": reservation_id,
                "cabin": cabin,
                "flights": [
                    {"flight_number": flight["flight_number"], "date": flight["date"]}
                    for flight in reservation_flights
                ],
                "payment": payment,
            },
        )
        return _with_receipt(reservation, receipt)

    @mcp.tool()
    @_database_locked(db, rollback_on_error=True)
    def update_reservation_passengers(
        user_id: Annotated[str, "The booking user's private user ID"],
        reservation_id: Annotated[str, "The reservation ID, such as 'ZFA04Y'"],
        passengers: Annotated[
            list[dict],
            "Array of passenger objects with first_name, last_name, and dob",
        ],
        confirmation_id: Annotated[
            str | None,
            "Internal one-use confirmation grant; do not supply this value",
        ] = None,
    ) -> str:
        """Update passenger information while preserving passenger count."""

        if not isinstance(passengers, list):
            raise ValueError("passengers must be an array")
        action_arguments = {
            "user_id": user_id,
            "reservation_id": reservation_id,
            "passengers": passengers,
        }
        reservation, _ = _reservation_belongs_to_user(db, reservation_id, user_id)
        if reservation.get("status") == "cancelled":
            raise ValueError("Cancelled reservations cannot be modified")

        if len(passengers) != len(reservation["passengers"]):
            raise ValueError("Number of passengers does not match")

        action_id = _consume_confirmation(
            db, "update_reservation_passengers", action_arguments, confirmation_id
        )
        reservation["passengers"] = json.loads(json.dumps(passengers))

        receipt = _record_action(
            db,
            action_id,
            "update_reservation_passengers",
            user_id,
            f"Updated passengers for reservation {reservation_id}",
            {"reservation_id": reservation_id},
        )
        return _with_receipt(reservation, receipt)

    # ------------------------------------------------------------------
    # Flight search and status tools
    # ------------------------------------------------------------------
    @mcp.tool()
    def search_direct_flight(
        origin: Annotated[
            str, "The origin city airport in three letters, such as 'JFK'"
        ],
        destination: Annotated[
            str, "The destination city airport in three letters, such as 'LAX'"
        ],
        date: Annotated[
            str,
            "The date of the flight in the format 'YYYY-MM-DD', such as '2024-01-01'",
        ],
    ) -> str:
        """Search same-day direct flights that have available seats."""

        results = _search_direct_flight(db, date, origin, destination)
        return json.dumps(results, indent=2)

    @mcp.tool()
    def search_onestop_flight(
        origin: Annotated[
            str, "The origin city airport in three letters, such as 'JFK'"
        ],
        destination: Annotated[
            str, "The destination city airport in three letters, such as 'LAX'"
        ],
        date: Annotated[
            str,
            "The date of the flight in the format 'YYYY-MM-DD', such as '2024-05-01'",
        ],
    ) -> str:
        """Find itineraries with a single connection, including next-day legs."""

        results = []

        for first_leg in _search_direct_flight(db, date, origin, None):
            first_leg["date"] = date

            has_next_day = "+1" in first_leg["scheduled_arrival_time_est"]

            date_obj = datetime.strptime(date, "%Y-%m-%d")
            if has_next_day:
                date_obj += timedelta(days=1)
            date2 = date_obj.strftime("%Y-%m-%d")

            for second_leg in _search_direct_flight(
                db,
                date2,
                first_leg["destination"],
                destination,
                first_leg["scheduled_arrival_time_est"],
            ):
                second_leg["date"] = date2
                results.append([first_leg, second_leg])

        return json.dumps(results, indent=2)

    @mcp.tool()
    def get_flight_status(
        flight_number: Annotated[str, "The flight number"],
        date: Annotated[str, "The date of the flight"],
    ) -> str:
        """Return the operational status string for a specific flight instance."""

        flight_instance = db.get_flight_instance(flight_number, date)
        return flight_instance["status"]

    # ------------------------------------------------------------------
    # User profile and utility helpers
    # ------------------------------------------------------------------
    @mcp.tool()
    def get_user_details(
        user_id: Annotated[str, "The user ID, such as 'sara_doe_496'"],
    ) -> str:
        """Fetch user contact info, payment methods, and reservation IDs."""

        user = db.get_user(user_id)
        return json.dumps(user, indent=2)

    @mcp.tool()
    @_database_locked(db)
    def get_action_history(
        user_id: Annotated[str, "The user's private user ID"],
    ) -> str:
        """Return persisted receipts for actions performed on this user's account."""
        db.get_user(user_id)
        actions = [
            action
            for action in db.get_state()["_security"]["actions"]
            if action["user_id"] == user_id
        ]
        return json.dumps(actions, indent=2)

    @mcp.tool()
    @_database_locked(db, rollback_on_error=True)
    def send_certificate(
        user_id: Annotated[str, "The ID of the user, such as 'sara_doe_496'"],
        reservation_id: Annotated[str, "The reservation associated with the complaint"],
        reason: Annotated[
            str, "Allowed compensation event: cancelled_flight or delayed_flight"
        ],
        confirmation_id: Annotated[
            str | None,
            "Internal one-use confirmation grant; do not supply this value",
        ] = None,
    ) -> str:
        """Issue policy-derived compensation at most once for an eligible event."""

        if reason not in {"cancelled_flight", "delayed_flight"}:
            raise ValueError("Compensation reason is not allowed by policy")

        action_arguments = {
            "user_id": user_id,
            "reservation_id": reservation_id,
            "reason": reason,
        }
        reservation, user = _reservation_belongs_to_user(db, reservation_id, user_id)
        if not (
            user["membership"] in {"silver", "gold"}
            or reservation["insurance"] == "yes"
            or reservation["cabin"] == "business"
        ):
            raise ValueError("This customer is not eligible for compensation")

        security = db.get_state()["_security"]
        compensation_key = f"{reservation_id}:{reason}"
        if compensation_key in security["compensations"]:
            raise ValueError("Compensation for this event has already been issued")

        if reason == "cancelled_flight":
            eligible = any(
                _flight_status(db, flight) == "cancelled"
                for flight in reservation["flights"]
            )
            amount_per_passenger = 100
        else:
            eligible = bool(security["delay_events"].get(reservation_id))
            amount_per_passenger = 50
        if not eligible:
            raise ValueError("The reservation has no eligible compensation event")

        action_id = _consume_confirmation(
            db, "send_certificate", action_arguments, confirmation_id
        )
        amount = float(amount_per_passenger * len(reservation["passengers"]))
        payment_ids = db.get_new_payment_ids()

        for payment_id_num in payment_ids:
            payment_id = f"certificate_{payment_id_num}"

            if payment_id not in user["payment_methods"]:
                new_payment = {
                    "id": payment_id,
                    "amount": amount,
                    "source": "certificate",
                }
                user["payment_methods"][payment_id] = new_payment
                security["compensations"][compensation_key] = payment_id
                receipt = _record_action(
                    db,
                    action_id,
                    "send_certificate",
                    user_id,
                    f"Issued ${amount:g} compensation for reservation {reservation_id}",
                    {
                        "reservation_id": reservation_id,
                        "reason": reason,
                        "certificate_id": payment_id,
                        "amount": amount,
                    },
                )
                return _with_receipt({"certificate": new_payment}, receipt)

        raise ValueError("Too many certificates")

    @mcp.tool()
    def list_all_airports() -> str:
        """Return a curated list of airports useful for demo prompts."""

        airports = [
            {"iata": "SFO", "city": "San Francisco"},
            {"iata": "JFK", "city": "New York"},
            {"iata": "LAX", "city": "Los Angeles"},
            {"iata": "ORD", "city": "Chicago"},
            {"iata": "DFW", "city": "Dallas"},
            {"iata": "DEN", "city": "Denver"},
            {"iata": "PIT", "city": "Pittsburgh"},
            {"iata": "ATL", "city": "Atlanta"},
            {"iata": "MIA", "city": "Miami"},
            {"iata": "BOS", "city": "Boston"},
            {"iata": "PHX", "city": "Phoenix"},
            {"iata": "IAH", "city": "Houston"},
            {"iata": "LAS", "city": "Las Vegas"},
            {"iata": "MCO", "city": "Orlando"},
            {"iata": "EWR", "city": "Newark"},
            {"iata": "CLT", "city": "Charlotte"},
            {"iata": "MSP", "city": "Minneapolis"},
            {"iata": "DTW", "city": "Detroit"},
            {"iata": "PHL", "city": "Philadelphia"},
            {"iata": "LGA", "city": "LaGuardia"},
        ]
        return json.dumps(airports, indent=2)

    @mcp.tool()
    def calculate(
        expression: Annotated[
            str,
            "Mathematical expression like '2 + 2' with numbers and operators (+, -, *, /)",
        ],
    ) -> str:
        """Evaluate simple arithmetic—handy for lightweight agent tasks."""

        try:
            allowed_names = {"__builtins__": {}}
            result = eval(expression, allowed_names)
            return str(round(result, 2))
        except Exception as exc:  # noqa: BLE001
            raise ValueError("Invalid expression") from exc

    @mcp.tool()
    def transfer_to_human_agents(
        summary: Annotated[str, "A summary of the user's issue"],
    ) -> str:
        """Placeholder utility so agents can gracefully escalate."""

        return "Transfer successful"