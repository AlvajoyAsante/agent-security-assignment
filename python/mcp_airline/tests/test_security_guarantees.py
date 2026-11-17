import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_airline.database import AirlineDatabase
from mcp_airline.server import create_mcp_server


USER_ID = "alice_123"
RESERVATION_ID = "R1"
FLIGHT_NUMBER = "F1"
FLIGHT_DATE = "2024-05-16"


def make_database(tmp_path, *, flight_status="available", with_reservation=True):
    database_path = tmp_path / "db.json"
    users = {
        USER_ID: {
            "user_id": USER_ID,
            "membership": "gold",
            "payment_methods": {
                "card_1": {"id": "card_1", "source": "credit_card"},
                "card_2": {"id": "card_2", "source": "credit_card"},
            },
            "reservations": [RESERVATION_ID] if with_reservation else [],
        },
        "bob_456": {
            "user_id": "bob_456",
            "membership": "regular",
            "payment_methods": {
                "card_1": {"id": "card_1", "source": "credit_card"},
                "card_2": {"id": "card_2", "source": "credit_card"},
            },
            "reservations": [],
        },
    }
    reservations = {}
    if with_reservation:
        reservations[RESERVATION_ID] = {
            "reservation_id": RESERVATION_ID,
            "user_id": USER_ID,
            "origin": "AAA",
            "destination": "BBB",
            "flight_type": "one_way",
            "cabin": "economy",
            "flights": [
                {
                    "flight_number": FLIGHT_NUMBER,
                    "date": FLIGHT_DATE,
                    "price": 100,
                }
            ],
            "passengers": [
                {"first_name": "Alice", "last_name": "Test", "dob": "1990-01-01"}
            ],
            "payment_history": [{"payment_id": "card_1", "amount": 100}],
            "created_at": "2024-05-15T10:00:00",
            "total_baggages": 1,
            "nonfree_baggages": 0,
            "insurance": "no",
        }
    data = {
        "users": users,
        "reservations": reservations,
        "flights": {
            FLIGHT_NUMBER: {
                "flight_number": FLIGHT_NUMBER,
                "origin": "AAA",
                "destination": "BBB",
                "scheduled_departure_time_est": "10:00:00",
                "scheduled_arrival_time_est": "12:00:00",
                "dates": {
                    FLIGHT_DATE: {
                        "status": flight_status,
                        "available_seats": {
                            "basic_economy": 1,
                            "economy": 1,
                            "business": 1,
                        },
                        "prices": {
                            "basic_economy": 100,
                            "economy": 100,
                            "business": 100,
                        },
                    }
                },
            }
        },
    }
    database_path.write_text(json.dumps(data), encoding="utf-8")
    return AirlineDatabase(database_path)


async def authorize(client, tool_name, arguments):
    result = await client.call_tool(
        "authorize_action",
        {
            "tool_name": tool_name,
            "arguments": json.dumps(arguments, sort_keys=True, separators=(",", ":")),
        },
    )
    assert not result.is_error
    return json.loads(result.content[0].text)["confirmation_id"]


async def expect_tool_error(client, tool_name, arguments):
    with pytest.raises(ToolError):
        await client.call_tool(tool_name, arguments)


def result_json(result):
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_payment_mutation_requires_exact_single_use_confirmation(tmp_path):
    database = make_database(tmp_path)
    server = create_mcp_server(database)
    arguments = {
        "user_id": USER_ID,
        "reservation_id": RESERVATION_ID,
        "total_baggages": 2,
        "nonfree_baggages": 1,
        "payment_id": "card_1",
    }

    async with Client(server) as client:
        await expect_tool_error(
            client, "update_reservation_baggages", arguments
        )

        confirmation_id = await authorize(
            client, "update_reservation_baggages", arguments
        )
        await expect_tool_error(
            client,
            "update_reservation_baggages",
            {**arguments, "total_baggages": 3, "confirmation_id": confirmation_id},
        )

        result = await client.call_tool(
            "update_reservation_baggages",
            {**arguments, "confirmation_id": confirmation_id},
        )
        assert not result.is_error
        updated = result_json(result)
        assert updated["total_baggages"] == 2
        assert updated["_action_receipt"]["details"]["nonfree_baggages"] == 1

        await expect_tool_error(
            client,
            "update_reservation_baggages",
            {**arguments, "confirmation_id": confirmation_id},
        )


@pytest.mark.asyncio
async def test_persistence_failure_rolls_back_action_and_keeps_grant_consumed(tmp_path):
    database = make_database(tmp_path)
    server = create_mcp_server(database)
    arguments = {
        "user_id": USER_ID,
        "reservation_id": RESERVATION_ID,
        "total_baggages": 2,
        "nonfree_baggages": 1,
        "payment_id": "card_1",
    }

    async with Client(server) as client:
        confirmation_id = await authorize(
            client, "update_reservation_baggages", arguments
        )
        original_save = database.save
        save_calls = 0

        def fail_once():
            nonlocal save_calls
            save_calls += 1
            if save_calls == 2:
                raise OSError("simulated runtime-state write failure")
            original_save()

        database.save = fail_once
        await expect_tool_error(
            client,
            "update_reservation_baggages",
            {**arguments, "confirmation_id": confirmation_id},
        )
        database.save = original_save

        reservation = database.get_reservation(RESERVATION_ID)
        assert reservation["total_baggages"] == 1
        assert reservation["nonfree_baggages"] == 0
        assert database.get_state()["_security"]["actions"] == []
        assert (
            database.get_state()["_security"]["confirmations"][confirmation_id][
                "used"
            ]
        )
        await expect_tool_error(
            client,
            "update_reservation_baggages",
            {**arguments, "confirmation_id": confirmation_id},
        )


@pytest.mark.asyncio
async def test_cancel_policy_is_enforced_and_reservation_owner_is_required(tmp_path):
    database = make_database(tmp_path)
    server = create_mcp_server(database)

    async with Client(server) as client:
        await expect_tool_error(
            client,
            "get_reservation_details",
            {"user_id": "bob_456", "reservation_id": RESERVATION_ID},
        )

        arguments = {
            "user_id": USER_ID,
            "reservation_id": RESERVATION_ID,
            "reason": "change_of_plan",
        }
        confirmation_id = await authorize(client, "cancel_reservation", arguments)
        result = await client.call_tool(
            "cancel_reservation",
            {**arguments, "confirmation_id": confirmation_id},
        )
        assert not result.is_error
        assert result_json(result)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancellation_rejects_ineligible_and_already_flown_reservations(tmp_path):
    cases = [
        ("ineligible", "available", "regular", "2024-05-01T10:00:00"),
        ("flown", "landed", "gold", "2024-05-15T10:00:00"),
    ]
    for folder, flight_status, membership, created_at in cases:
        case_dir = tmp_path / folder
        case_dir.mkdir()
        database = make_database(case_dir, flight_status=flight_status)
        database.get_user(USER_ID)["membership"] = membership
        database.get_reservation(RESERVATION_ID)["created_at"] = created_at
        server = create_mcp_server(database)
        arguments = {
            "user_id": USER_ID,
            "reservation_id": RESERVATION_ID,
            "reason": "change_of_plan",
        }

        async with Client(server) as client:
            confirmation_id = await authorize(
                client, "cancel_reservation", arguments
            )
            await expect_tool_error(
                client,
                "cancel_reservation",
                {**arguments, "confirmation_id": confirmation_id},
            )

        reservation = database.get_reservation(RESERVATION_ID)
        assert reservation.get("status") != "cancelled"
        assert len(reservation["payment_history"]) == 1


@pytest.mark.asyncio
async def test_compensation_is_derived_once_and_persists_after_restart(tmp_path):
    database = make_database(tmp_path, flight_status="cancelled")
    server = create_mcp_server(database)
    arguments = {
        "user_id": USER_ID,
        "reservation_id": RESERVATION_ID,
        "reason": "cancelled_flight",
    }

    async with Client(server) as client:
        confirmation_ids = [
            await authorize(client, "send_certificate", arguments)
            for _ in range(2)
        ]
        results = await asyncio.gather(
            *[
                client.call_tool(
                    "send_certificate",
                    {**arguments, "confirmation_id": confirmation_id},
                )
                for confirmation_id in confirmation_ids
            ],
            return_exceptions=True,
        )
        successful_results = [
            result for result in results if not isinstance(result, BaseException)
        ]
        assert len(successful_results) == 1
        issued = result_json(successful_results[0])
        assert issued["certificate"]["amount"] == 100
        assert sum(isinstance(result, BaseException) for result in results) == 1

        second_confirmation = await authorize(client, "send_certificate", arguments)
        await expect_tool_error(
            client,
            "send_certificate",
            {**arguments, "confirmation_id": second_confirmation},
        )

    restarted_database = AirlineDatabase(database._db_path)
    restarted_server = create_mcp_server(restarted_database)
    async with Client(restarted_server) as restarted_client:
        history = await restarted_client.call_tool(
            "get_action_history", {"user_id": USER_ID}
        )
        assert len(result_json(history)) == 1
        replay_confirmation = await authorize(
            restarted_client, "send_certificate", arguments
        )
        await expect_tool_error(
            restarted_client,
            "send_certificate",
            {**arguments, "confirmation_id": replay_confirmation},
        )


@pytest.mark.asyncio
async def test_delayed_compensation_requires_a_successful_itinerary_change(tmp_path):
    database = make_database(tmp_path, flight_status="delayed")
    database.get_state()["flights"]["F2"] = {
        "flight_number": "F2",
        "origin": "AAA",
        "destination": "BBB",
        "scheduled_departure_time_est": "11:00:00",
        "scheduled_arrival_time_est": "13:00:00",
        "dates": {
            FLIGHT_DATE: {
                "status": "available",
                "available_seats": {
                    "basic_economy": 1,
                    "economy": 1,
                    "business": 1,
                },
                "prices": {
                    "basic_economy": 100,
                    "economy": 100,
                    "business": 100,
                },
            }
        },
    }
    server = create_mcp_server(database)
    compensation = {
        "user_id": USER_ID,
        "reservation_id": RESERVATION_ID,
        "reason": "delayed_flight",
    }

    async with Client(server) as client:
        before_change_id = await authorize(client, "send_certificate", compensation)
        await expect_tool_error(
            client,
            "send_certificate",
            {**compensation, "confirmation_id": before_change_id},
        )

        update = {
            "user_id": USER_ID,
            "reservation_id": RESERVATION_ID,
            "cabin": "economy",
            "flights": json.dumps(
                [{"flight_number": "F2", "date": FLIGHT_DATE}]
            ),
            "payment_id": "card_1",
        }
        update_id = await authorize(client, "update_reservation_flights", update)
        changed = await client.call_tool(
            "update_reservation_flights",
            {**update, "confirmation_id": update_id},
        )
        assert not changed.is_error

        after_change_id = await authorize(client, "send_certificate", compensation)
        issued = await client.call_tool(
            "send_certificate",
            {**compensation, "confirmation_id": after_change_id},
        )
        assert not issued.is_error
        assert result_json(issued)["certificate"]["amount"] == 50


@pytest.mark.asyncio
async def test_concurrent_bookings_cannot_oversell_last_seat(tmp_path):
    database = make_database(tmp_path, with_reservation=False)
    server = create_mcp_server(database)

    def booking_args(user_id, payment_id):
        return {
            "user_id": user_id,
            "origin": "AAA",
            "destination": "BBB",
            "flight_type": "one_way",
            "cabin": "economy",
            "flights": json.dumps(
                [{"flight_number": FLIGHT_NUMBER, "date": FLIGHT_DATE}]
            ),
            "passengers": json.dumps(
                [{"first_name": user_id, "last_name": "Test", "dob": "1990-01-01"}]
            ),
            "payment_methods": json.dumps(
                [{"payment_id": payment_id, "amount": 100}]
            ),
            "total_baggages": 0,
            "nonfree_baggages": 0,
            "insurance": "no",
        }

    async with Client(server) as client:
        first_args = booking_args(USER_ID, "card_1")
        second_args = booking_args("bob_456", "card_2")
        first_confirmation = await authorize(
            client, "book_reservation", first_args
        )
        second_confirmation = await authorize(
            client, "book_reservation", second_args
        )
        results = await asyncio.gather(
            client.call_tool(
                "book_reservation",
                {**first_args, "confirmation_id": first_confirmation},
            ),
            client.call_tool(
                "book_reservation",
                {**second_args, "confirmation_id": second_confirmation},
            ),
            return_exceptions=True,
        )

    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert (
        database.get_flight_instance(FLIGHT_NUMBER, FLIGHT_DATE)["available_seats"][
            "economy"
        ]
        == 0
    )


@pytest.mark.asyncio
async def test_booking_rejects_more_than_five_passengers(tmp_path):
    database = make_database(tmp_path, with_reservation=False)
    server = create_mcp_server(database)
    arguments = {
        "user_id": USER_ID,
        "origin": "AAA",
        "destination": "BBB",
        "flight_type": "one_way",
        "cabin": "economy",
        "flights": json.dumps(
            [{"flight_number": FLIGHT_NUMBER, "date": FLIGHT_DATE}]
        ),
        "passengers": json.dumps(
            [
                {"first_name": f"Passenger{i}", "last_name": "Test", "dob": "1990-01-01"}
                for i in range(6)
            ]
        ),
        "payment_methods": json.dumps([{"payment_id": "card_1", "amount": 600}]),
        "total_baggages": 0,
        "nonfree_baggages": 0,
        "insurance": "no",
    }

    async with Client(server) as client:
        await expect_tool_error(client, "book_reservation", arguments)

    assert database.get_state()["reservations"] == {}
