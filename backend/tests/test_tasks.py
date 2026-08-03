"""Task management: creation from deadlines, assignment and workload."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core import clock
from app.models.base import TaskPriority, TaskStatus
from app.models.client import Client
from app.models.compliance import ComplianceItem
from app.models.firm import Firm
from app.models.task import Task
from app.services import tasks as task_service
from app.worker import tasks as worker_tasks
from tests.conftest import first_item_of_type, make_client_payload


@pytest.fixture
def junior(client, auth_headers) -> dict:
    response = client.post(
        "/api/v1/auth/practitioners",
        json={
            "full_name": "Junior Jain",
            "email": "junior@sharma-ca.in",
            "password": "another-long-password",
            "role": "junior",
        },
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_task(client, headers, **overrides):
    payload = {"title": "Reconcile the GST ledger"}
    payload.update(overrides)
    return client.post("/api/v1/tasks", json=payload, headers=headers)


class TestPriorityDerivation:
    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (-1, TaskPriority.URGENT),
            (0, TaskPriority.HIGH),
            (3, TaskPriority.HIGH),
            (4, TaskPriority.NORMAL),
            (10, TaskPriority.NORMAL),
            (11, TaskPriority.LOW),
        ],
    )
    def test_urgency_follows_the_clock(self, days, expected):
        assert task_service.derive_priority(days) == expected


class TestTaskCrud:
    def test_creates_a_standalone_task(self, client, auth_headers):
        response = create_task(client, auth_headers)
        assert response.status_code == 201, response.text

        task = response.json()
        assert task["title"] == "Reconcile the GST ledger"
        assert task["status"] == "todo"
        assert task["priority"] == "normal"
        assert task["completed_at"] is None

    def test_assigns_to_a_team_member(self, client, auth_headers, junior):
        task = create_task(client, auth_headers, assignee_id=junior["id"]).json()
        assert task["assignee_id"] == junior["id"]
        assert task["assignee_name"] == "Junior Jain"

    def test_an_assignee_from_another_firm_is_rejected(self, client, auth_headers):
        response = create_task(client, auth_headers, assignee_id=str(uuid.uuid4()))
        assert response.status_code == 400
        assert "does not belong to this firm" in response.json()["detail"]

    def test_links_to_a_client_and_filing(self, client, auth_headers, client_id):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        task = create_task(
            client, auth_headers, client_id=client_id, compliance_item_id=item["id"]
        ).json()
        assert task["client_name"] == "Nimbus Textiles Pvt Ltd"
        assert task["compliance_type_name"].startswith("GSTR-3B")
        assert task["period_label"] == item["period_label"]

    def test_marking_done_stamps_the_completion_time(self, client, auth_headers):
        task = create_task(client, auth_headers).json()
        response = client.patch(
            f"/api/v1/tasks/{task['id']}", json={"status": "done"}, headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json()["completed_at"] is not None

    def test_reopening_clears_the_completion_time(self, client, auth_headers):
        task = create_task(client, auth_headers).json()
        client.patch(
            f"/api/v1/tasks/{task['id']}", json={"status": "done"}, headers=auth_headers
        )
        response = client.patch(
            f"/api/v1/tasks/{task['id']}",
            json={"status": "in_progress"},
            headers=auth_headers,
        )
        assert response.json()["completed_at"] is None

    def test_an_overdue_open_task_is_flagged(self, client, auth_headers):
        yesterday = (clock.today() - timedelta(days=1)).isoformat()
        task = create_task(client, auth_headers, due_date=yesterday).json()
        assert task["is_overdue"] is True
        assert task["days_remaining"] == -1

    def test_a_completed_task_is_not_overdue(self, client, auth_headers):
        yesterday = (clock.today() - timedelta(days=1)).isoformat()
        task = create_task(client, auth_headers, due_date=yesterday).json()
        response = client.patch(
            f"/api/v1/tasks/{task['id']}", json={"status": "done"}, headers=auth_headers
        )
        assert response.json()["is_overdue"] is False

    def test_only_a_manager_can_delete(self, client, auth_headers, junior):
        task = create_task(client, auth_headers).json()
        junior_token = client.post(
            "/api/v1/auth/login",
            json={"email": "junior@sharma-ca.in", "password": "another-long-password"},
        ).json()["access_token"]

        denied = client.delete(
            f"/api/v1/tasks/{task['id']}",
            headers={"Authorization": f"Bearer {junior_token}"},
        )
        assert denied.status_code == 403
        assert client.delete(
            f"/api/v1/tasks/{task['id']}", headers=auth_headers
        ).status_code == 204

    def test_another_firms_task_is_a_404(self, client, auth_headers):
        response = client.get(f"/api/v1/tasks/{uuid.uuid4()}", headers=auth_headers)
        assert response.status_code == 404

    def test_tasks_require_authentication(self, client):
        assert client.get("/api/v1/tasks").status_code == 401


class TestTaskFilters:
    def test_filters_by_status_priority_and_assignee(self, client, auth_headers, junior):
        create_task(client, auth_headers, title="Open one")
        create_task(client, auth_headers, title="Urgent one", priority="urgent")
        done = create_task(client, auth_headers, title="Finished one").json()
        client.patch(
            f"/api/v1/tasks/{done['id']}", json={"status": "done"}, headers=auth_headers
        )
        create_task(client, auth_headers, title="Junior's", assignee_id=junior["id"])

        assert client.get("/api/v1/tasks", headers=auth_headers).json()["total"] == 4

        open_only = client.get(
            "/api/v1/tasks", params={"open_only": True}, headers=auth_headers
        ).json()
        assert open_only["total"] == 3

        urgent = client.get(
            "/api/v1/tasks", params={"priority": "urgent"}, headers=auth_headers
        ).json()
        assert [t["title"] for t in urgent["items"]] == ["Urgent one"]

        theirs = client.get(
            "/api/v1/tasks",
            params={"assignee_id": junior["id"]},
            headers=auth_headers,
        ).json()
        assert [t["title"] for t in theirs["items"]] == ["Junior's"]

    def test_mine_returns_only_my_tasks(self, client, auth_headers, junior, registered_firm):
        create_task(
            client, auth_headers, title="Mine", assignee_id=registered_firm["practitioner"]["id"]
        )
        create_task(client, auth_headers, title="Theirs", assignee_id=junior["id"])

        mine = client.get(
            "/api/v1/tasks", params={"mine": True}, headers=auth_headers
        ).json()
        assert [t["title"] for t in mine["items"]] == ["Mine"]

    def test_overdue_only_excludes_closed_work(self, client, auth_headers):
        yesterday = (clock.today() - timedelta(days=1)).isoformat()
        create_task(client, auth_headers, title="Late", due_date=yesterday)
        closed = create_task(
            client, auth_headers, title="Late but done", due_date=yesterday
        ).json()
        client.patch(
            f"/api/v1/tasks/{closed['id']}", json={"status": "done"}, headers=auth_headers
        )

        overdue = client.get(
            "/api/v1/tasks", params={"overdue_only": True}, headers=auth_headers
        ).json()
        assert [t["title"] for t in overdue["items"]] == ["Late"]

    def test_searches_title_and_description(self, client, auth_headers):
        create_task(client, auth_headers, title="File TDS return")
        create_task(
            client, auth_headers, title="Something else", description="chase TDS challan"
        )
        create_task(client, auth_headers, title="Unrelated")

        found = client.get(
            "/api/v1/tasks", params={"search": "tds"}, headers=auth_headers
        ).json()
        assert found["total"] == 2

    def test_undated_tasks_sort_after_dated_ones(self, client, auth_headers):
        create_task(client, auth_headers, title="No date")
        create_task(
            client,
            auth_headers,
            title="Dated",
            due_date=(clock.today() + timedelta(days=5)).isoformat(),
        )
        items = client.get("/api/v1/tasks", headers=auth_headers).json()["items"]
        assert [t["title"] for t in items] == ["Dated", "No date"]


class TestGeneration:
    def test_creates_tasks_from_upcoming_deadlines(self, client, auth_headers, client_id):
        response = client.post(
            "/api/v1/tasks/generate", json={"horizon_days": 30}, headers=auth_headers
        )
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["created"] > 0
        assert all(task["compliance_item_id"] is not None for task in body["tasks"])
        assert all(task["client_name"] for task in body["tasks"])

    def test_generation_is_idempotent(self, client, auth_headers, client_id):
        first = client.post(
            "/api/v1/tasks/generate", json={"horizon_days": 30}, headers=auth_headers
        ).json()
        second = client.post(
            "/api/v1/tasks/generate", json={"horizon_days": 30}, headers=auth_headers
        ).json()
        assert first["created"] > 0
        assert second["created"] == 0

    def test_a_cancelled_task_does_not_come_back(self, client, auth_headers, client_id):
        generated = client.post(
            "/api/v1/tasks/generate", json={"horizon_days": 30}, headers=auth_headers
        ).json()["tasks"]
        client.patch(
            f"/api/v1/tasks/{generated[0]['id']}",
            json={"status": "cancelled"},
            headers=auth_headers,
        )
        again = client.post(
            "/api/v1/tasks/generate", json={"horizon_days": 30}, headers=auth_headers
        ).json()
        assert again["created"] == 0

    def test_a_wider_horizon_creates_more(self, client, auth_headers, client_id):
        narrow = client.post(
            "/api/v1/tasks/generate", json={"horizon_days": 7}, headers=auth_headers
        ).json()["created"]
        wider = client.post(
            "/api/v1/tasks/generate", json={"horizon_days": 90}, headers=auth_headers
        ).json()["created"]
        assert wider > 0
        assert narrow >= 0

    def test_generated_tasks_inherit_the_client_assignee(
        self, client, auth_headers, junior, db
    ):
        from tests.conftest import make_client_payload

        client.post(
            "/api/v1/clients",
            json=make_client_payload(assigned_practitioner_id=junior["id"]),
            headers=auth_headers,
        )
        tasks = client.post(
            "/api/v1/tasks/generate", json={"horizon_days": 60}, headers=auth_headers
        ).json()["tasks"]
        assert tasks
        assert all(task["assignee_id"] == junior["id"] for task in tasks)

    def test_filed_items_do_not_produce_tasks(self, client, auth_headers, client_id):
        calendar = client.get(
            "/api/v1/compliance/calendar",
            params={"from_date": "2020-01-01", "to_date": "2035-12-31", "limit": 1000},
            headers=auth_headers,
        ).json()
        client.post(
            "/api/v1/compliance/items/bulk-status",
            json={
                "item_ids": [item["id"] for item in calendar["items"]],
                "status": "filed",
            },
            headers=auth_headers,
        )
        response = client.post(
            "/api/v1/tasks/generate", json={"horizon_days": 90}, headers=auth_headers
        )
        assert response.json()["created"] == 0

    def test_a_junior_cannot_generate(self, client, auth_headers, junior):
        junior_token = client.post(
            "/api/v1/auth/login",
            json={"email": "junior@sharma-ca.in", "password": "another-long-password"},
        ).json()["access_token"]
        response = client.post(
            "/api/v1/tasks/generate",
            json={"horizon_days": 30},
            headers={"Authorization": f"Bearer {junior_token}"},
        )
        assert response.status_code == 403


class TestBulkUpdate:
    def test_reassigns_many_tasks_at_once(self, client, auth_headers, junior):
        ids = [create_task(client, auth_headers, title=f"T{i}").json()["id"] for i in range(3)]
        response = client.post(
            "/api/v1/tasks/bulk",
            json={"task_ids": ids, "assignee_id": junior["id"], "status": "in_progress"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json() == {"updated": 3, "skipped": 0}

        items = client.get("/api/v1/tasks", headers=auth_headers).json()["items"]
        assert all(task["assignee_id"] == junior["id"] for task in items)
        assert all(task["status"] == "in_progress" for task in items)

    def test_unknown_ids_are_counted_as_skipped(self, client, auth_headers):
        real = create_task(client, auth_headers).json()["id"]
        response = client.post(
            "/api/v1/tasks/bulk",
            json={"task_ids": [real, str(uuid.uuid4())], "status": "done"},
            headers=auth_headers,
        )
        assert response.json() == {"updated": 1, "skipped": 1}

    def test_an_empty_update_is_rejected(self, client, auth_headers):
        real = create_task(client, auth_headers).json()["id"]
        response = client.post(
            "/api/v1/tasks/bulk", json={"task_ids": [real]}, headers=auth_headers
        )
        assert response.status_code == 422


class TestWorkload:
    def test_reports_load_per_practitioner(self, client, auth_headers, junior, registered_firm):
        owner_id = registered_firm["practitioner"]["id"]
        create_task(client, auth_headers, title="Owner's", assignee_id=owner_id)
        create_task(
            client,
            auth_headers,
            title="Junior late",
            assignee_id=junior["id"],
            due_date=(clock.today() - timedelta(days=2)).isoformat(),
        )
        create_task(
            client,
            auth_headers,
            title="Junior soon",
            assignee_id=junior["id"],
            due_date=(clock.today() + timedelta(days=3)).isoformat(),
        )

        response = client.get("/api/v1/tasks/workload", headers=auth_headers)
        assert response.status_code == 200

        rows = {row["practitioner_name"]: row for row in response.json()["rows"]}
        assert rows["Anita Sharma"]["open_tasks"] == 1
        assert rows["Junior Jain"]["open_tasks"] == 2
        assert rows["Junior Jain"]["overdue"] == 1
        assert rows["Junior Jain"]["due_this_week"] == 1
        assert response.json()["total_open"] == 3

    def test_unassigned_work_gets_its_own_row(self, client, auth_headers):
        create_task(client, auth_headers, title="Nobody's")
        body = client.get("/api/v1/tasks/workload", headers=auth_headers).json()
        assert body["unassigned_open"] == 1
        assert any(row["practitioner_name"] == "Unassigned" for row in body["rows"])

    def test_no_unassigned_row_when_everything_is_assigned(
        self, client, auth_headers, registered_firm
    ):
        create_task(
            client,
            auth_headers,
            assignee_id=registered_firm["practitioner"]["id"],
        )
        body = client.get("/api/v1/tasks/workload", headers=auth_headers).json()
        assert body["unassigned_open"] == 0
        assert not any(row["practitioner_name"] == "Unassigned" for row in body["rows"])

    def test_completed_work_counts_separately(self, client, auth_headers, registered_firm):
        owner_id = registered_firm["practitioner"]["id"]
        task = create_task(client, auth_headers, assignee_id=owner_id).json()
        client.patch(
            f"/api/v1/tasks/{task['id']}", json={"status": "done"}, headers=auth_headers
        )

        rows = {
            row["practitioner_name"]: row
            for row in client.get("/api/v1/tasks/workload", headers=auth_headers).json()["rows"]
        }
        assert rows["Anita Sharma"]["open_tasks"] == 0
        assert rows["Anita Sharma"]["completed_this_month"] == 1
        assert rows["Anita Sharma"]["by_status"][TaskStatus.DONE.value] == 1

    def test_estimated_effort_is_summed(self, client, auth_headers, registered_firm):
        owner_id = registered_firm["practitioner"]["id"]
        create_task(client, auth_headers, assignee_id=owner_id, estimated_minutes=45)
        create_task(client, auth_headers, assignee_id=owner_id, estimated_minutes=30)

        rows = {
            row["practitioner_name"]: row
            for row in client.get("/api/v1/tasks/workload", headers=auth_headers).json()["rows"]
        }
        assert rows["Anita Sharma"]["estimated_minutes"] == 75


class TestWorkAimedAtAnAccountThatIsSwitchedOff:
    """A deactivated member is still in the firm, and still refused at sign-in.

    Their row stays for the history hanging off it, so every check that asked
    only "are they in this firm?" went on accepting them. Work put on their
    name is work nobody can open — it appears under no active member's queue
    and it is not unassigned either, so a statutory deadline ends up on a name
    nobody is watching. Neither half of that may stand: no new work may be
    aimed at them, and what they were already holding has to come back.
    """

    @staticmethod
    def _deactivate(client, auth_headers, practitioner_id) -> None:
        response = client.patch(
            f"/api/v1/auth/practitioners/{practitioner_id}",
            json={"is_active": False},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

    @staticmethod
    def _workload(client, auth_headers) -> dict:
        return client.get("/api/v1/tasks/workload", headers=auth_headers).json()

    def test_a_task_cannot_be_assigned_to_a_deactivated_member(
        self, client, auth_headers, junior
    ):
        self._deactivate(client, auth_headers, junior["id"])

        response = create_task(client, auth_headers, assignee_id=junior["id"])

        assert response.status_code == 400
        assert "deactivated" in response.json()["detail"]

    def test_the_refusal_names_them(self, client, auth_headers, junior):
        """A manager reassigning a queue needs to know which name was refused."""
        self._deactivate(client, auth_headers, junior["id"])

        response = create_task(client, auth_headers, assignee_id=junior["id"])

        assert "Junior Jain" in response.json()["detail"]

    def test_a_bulk_reassignment_cannot_aim_at_one_either(
        self, client, auth_headers, junior
    ):
        """The bulk path moves whole queues at once, so it is the likelier way in."""
        task = create_task(client, auth_headers).json()
        self._deactivate(client, auth_headers, junior["id"])

        response = client.post(
            "/api/v1/tasks/bulk",
            json={"task_ids": [task["id"]], "assignee_id": junior["id"]},
            headers=auth_headers,
        )

        assert response.status_code == 400
        assert client.get(f"/api/v1/tasks/{task['id']}", headers=auth_headers).json()[
            "assignee_id"
        ] is None

    def test_an_existing_task_cannot_be_handed_to_one(self, client, auth_headers, junior):
        """Reassigning one task at a time is the everyday path, not just bulk."""
        task = create_task(client, auth_headers).json()
        self._deactivate(client, auth_headers, junior["id"])

        response = client.patch(
            f"/api/v1/tasks/{task['id']}",
            json={"assignee_id": junior["id"]},
            headers=auth_headers,
        )

        assert response.status_code == 400
        assert "deactivated" in response.json()["detail"]

    def test_an_existing_client_cannot_be_moved_into_their_care(
        self, client, auth_headers, client_id, junior
    ):
        self._deactivate(client, auth_headers, junior["id"])

        response = client.patch(
            f"/api/v1/clients/{client_id}",
            json={"assigned_practitioner_id": junior["id"]},
            headers=auth_headers,
        )

        assert response.status_code == 400
        assert "deactivated" in response.json()["detail"]

    def test_a_client_cannot_be_put_in_their_care(self, client, auth_headers, junior):
        """Every filing generated for that client would inherit the name."""
        self._deactivate(client, auth_headers, junior["id"])

        response = client.post(
            "/api/v1/clients",
            json=make_client_payload(
                pan="AAECS9876P", gstin="27AAECS9876P1Z8",
                assigned_practitioner_id=junior["id"],
            ),
            headers=auth_headers,
        )

        assert response.status_code == 400
        assert "deactivated" in response.json()["detail"]

    def test_a_filing_cannot_be_put_in_their_care(
        self, client, auth_headers, client_id, junior
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        self._deactivate(client, auth_headers, junior["id"])

        response = client.patch(
            f"/api/v1/compliance/items/{item['id']}",
            json={"assigned_practitioner_id": junior["id"]},
            headers=auth_headers,
        )

        assert response.status_code == 400
        assert "deactivated" in response.json()["detail"]

    def test_switching_someone_off_hands_their_open_work_back(
        self, client, auth_headers, junior
    ):
        task = create_task(
            client, auth_headers, assignee_id=junior["id"], title="File GSTR-3B"
        ).json()

        self._deactivate(client, auth_headers, junior["id"])

        after = client.get(f"/api/v1/tasks/{task['id']}", headers=auth_headers).json()
        assert after["assignee_id"] is None
        assert after["assignee_name"] is None

    def test_the_work_is_then_findable_where_a_manager_looks_for_it(
        self, client, auth_headers, junior
    ):
        """Unassigned is the queue a manager works through, not a black hole."""
        create_task(client, auth_headers, assignee_id=junior["id"])

        self._deactivate(client, auth_headers, junior["id"])

        assert self._workload(client, auth_headers)["unassigned_open"] == 1

    def test_finished_work_keeps_the_name_of_whoever_did_it(
        self, client, auth_headers, junior
    ):
        """Those rows are the record of who did what — rewriting them loses it."""
        task = create_task(client, auth_headers, assignee_id=junior["id"]).json()
        client.patch(
            f"/api/v1/tasks/{task['id']}",
            json={"status": TaskStatus.DONE.value},
            headers=auth_headers,
        )

        self._deactivate(client, auth_headers, junior["id"])

        after = client.get(f"/api/v1/tasks/{task['id']}", headers=auth_headers).json()
        assert after["assignee_id"] == junior["id"]
        assert after["assignee_name"] == "Junior Jain"

    def test_nobody_elses_queue_is_touched(self, client, auth_headers, junior, registered_firm):
        """Only the departing member's work moves."""
        owner_id = registered_firm["practitioner"]["id"]
        theirs = create_task(client, auth_headers, assignee_id=owner_id).json()
        create_task(client, auth_headers, assignee_id=junior["id"])

        self._deactivate(client, auth_headers, junior["id"])

        assert client.get(f"/api/v1/tasks/{theirs['id']}", headers=auth_headers).json()[
            "assignee_id"
        ] == owner_id

    def test_the_audit_trail_says_what_moved(self, client, auth_headers, junior):
        """A queue emptying overnight needs a recorded reason."""
        create_task(client, auth_headers, assignee_id=junior["id"])
        create_task(client, auth_headers, assignee_id=junior["id"])

        self._deactivate(client, auth_headers, junior["id"])

        entries = client.get(
            "/api/v1/audit",
            params={"action": "practitioner.update"},
            headers=auth_headers,
        ).json()["items"]
        assert "2 open task(s) returned to unassigned" in entries[0]["summary"]

    def test_an_ordinary_edit_moves_no_work(self, client, auth_headers, junior):
        """Only the switch-off transition releases; a name change must not."""
        task = create_task(client, auth_headers, assignee_id=junior["id"]).json()

        client.patch(
            f"/api/v1/auth/practitioners/{junior['id']}",
            json={"full_name": "Junior Jain-Mehta"},
            headers=auth_headers,
        )

        assert client.get(f"/api/v1/tasks/{task['id']}", headers=auth_headers).json()[
            "assignee_id"
        ] == junior["id"]

    def test_generated_tasks_do_not_inherit_a_switched_off_owner(
        self, client, auth_headers, client_id, junior
    ):
        """The filing keeps the old name; the task it produces must not.

        The nightly sweep is unattended, so this is the path that would quietly
        build a queue for someone who cannot sign in.
        """
        client.patch(
            f"/api/v1/clients/{client_id}",
            json={"assigned_practitioner_id": junior["id"]},
            headers=auth_headers,
        )
        self._deactivate(client, auth_headers, junior["id"])

        created = client.post(
            "/api/v1/tasks/generate", json={"horizon_days": 60}, headers=auth_headers
        ).json()

        assert created["created"] > 0
        assert all(task["assignee_id"] is None for task in created["tasks"])

    def test_generation_falls_through_to_the_client_owner_instead(
        self, client, auth_headers, client_id, junior, registered_firm
    ):
        """A departure costs the filing its owner, not the fallback behind it.

        The filing names the leaver and the client names someone active. Going
        straight to unassigned would throw away an owner the firm had already
        chosen, and hand a manager sorting work they did not need to do.
        """
        owner_id = registered_firm["practitioner"]["id"]
        client.patch(
            f"/api/v1/clients/{client_id}",
            json={"assigned_practitioner_id": owner_id},
            headers=auth_headers,
        )
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        client.patch(
            f"/api/v1/compliance/items/{item['id']}",
            json={"assigned_practitioner_id": junior["id"]},
            headers=auth_headers,
        )
        self._deactivate(client, auth_headers, junior["id"])

        created = client.post(
            "/api/v1/tasks/generate", json={"horizon_days": 60}, headers=auth_headers
        ).json()

        for_item = [t for t in created["tasks"] if t["compliance_item_id"] == item["id"]]
        assert for_item, "the filing under test produced no task"
        assert for_item[0]["assignee_id"] == owner_id


class TestWorkForAFilingNobodyOwesAnyMore:
    """A task is the job of discharging one filing, and outlived it.

    Off-boarding a client closes every open filing they had; so does dropping
    a registration the client no longer holds. Neither touched the tasks. They
    stayed on a practitioner's queue, counted in the workload view, and counted
    down to a deadline nobody owes — eight of them for a single client, right
    beside the real work.
    """

    HORIZON = {"horizon_days": 60}

    def _generate(self, client, headers, horizon_days: int | None = None) -> int:
        response = client.post(
            "/api/v1/tasks/generate",
            json={"horizon_days": horizon_days or self.HORIZON["horizon_days"]},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return response.json()["created"]

    def _tasks_for(self, client, headers, client_id: str) -> list[dict]:
        return client.get(
            "/api/v1/tasks",
            params={"client_id": client_id, "limit": 200},
            headers=headers,
        ).json()["items"]

    def _open_tasks_for(self, client, headers, client_id: str) -> list[dict]:
        return [
            task
            for task in self._tasks_for(client, headers, client_id)
            if task["status"] in {s.value for s in task_service.OPEN_TASK_STATUSES}
        ]

    def test_off_boarding_a_client_closes_the_work_raised_for_them(
        self, client, auth_headers, client_id
    ):
        assert self._generate(client, auth_headers) > 0
        assert self._open_tasks_for(client, auth_headers, client_id)

        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)

        assert not self._open_tasks_for(client, auth_headers, client_id)

    def test_the_workload_view_stops_counting_it(self, client, auth_headers, client_id):
        """Which is where a manager decides who is drowning and who is free."""
        self._generate(client, auth_headers)
        before = client.get("/api/v1/tasks/workload", headers=auth_headers).json()
        assert before["total_open"] > 0

        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)

        after = client.get("/api/v1/tasks/workload", headers=auth_headers).json()
        assert after["total_open"] == 0

    def test_taking_the_client_back_on_brings_the_work_back(
        self, client, auth_headers, client_id
    ):
        """Generation skips a filing that already carries a task, whatever its
        status, so without this the reopened calendar would come back with
        nothing raised against it and no sweep would ever notice."""
        self._generate(client, auth_headers)
        before = {
            task["id"] for task in self._open_tasks_for(client, auth_headers, client_id)
        }

        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)
        client.patch(
            f"/api/v1/clients/{client_id}", json={"is_active": True}, headers=auth_headers
        )

        after = {
            task["id"] for task in self._open_tasks_for(client, auth_headers, client_id)
        }
        assert after == before

    def test_work_already_under_way_comes_back_as_it_was(
        self, client, auth_headers, client_id
    ):
        self._generate(client, auth_headers)
        task = self._open_tasks_for(client, auth_headers, client_id)[0]
        client.patch(
            f"/api/v1/tasks/{task['id']}",
            json={"status": "in_progress"},
            headers=auth_headers,
        )

        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)
        assert (
            client.get(f"/api/v1/tasks/{task['id']}", headers=auth_headers).json()["status"]
            == "cancelled"
        )

        client.patch(
            f"/api/v1/clients/{client_id}", json={"is_active": True}, headers=auth_headers
        )
        assert (
            client.get(f"/api/v1/tasks/{task['id']}", headers=auth_headers).json()["status"]
            == "in_progress"
        )

    def test_a_task_a_manager_cancelled_stays_cancelled(
        self, client, auth_headers, client_id
    ):
        """Their decision about the work is not this mechanism's to undo."""
        self._generate(client, auth_headers)
        task = self._open_tasks_for(client, auth_headers, client_id)[0]
        client.patch(
            f"/api/v1/tasks/{task['id']}",
            json={"status": "cancelled"},
            headers=auth_headers,
        )

        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)
        client.patch(
            f"/api/v1/clients/{client_id}", json={"is_active": True}, headers=auth_headers
        )

        assert (
            client.get(f"/api/v1/tasks/{task['id']}", headers=auth_headers).json()["status"]
            == "cancelled"
        )

    def test_finished_work_keeps_its_record(self, client, auth_headers, client_id):
        """A filing done before the client left was still done."""
        self._generate(client, auth_headers)
        task = self._open_tasks_for(client, auth_headers, client_id)[0]
        client.patch(
            f"/api/v1/tasks/{task['id']}", json={"status": "done"}, headers=auth_headers
        )

        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)

        after = client.get(f"/api/v1/tasks/{task['id']}", headers=auth_headers).json()
        assert after["status"] == "done"
        assert after["completed_at"] is not None

    def test_dropping_a_registration_closes_only_its_own_work(
        self, client, auth_headers, client_id
    ):
        """The client is still on the books, so this stale work would sit
        among real work rather than under a name someone might think to check."""
        # Far enough out to reach a GST period that has not begun: only
        # those can be withdrawn, since a registration surrendered mid-month
        # still owes that month's return.
        self._generate(client, auth_headers, horizon_days=180)
        gst_items = {
            item["id"]: item
            for item in client.get(
                "/api/v1/compliance/calendar",
                params={"limit": 1000, "to_date": (clock.today() + timedelta(days=400)).isoformat()},
                headers=auth_headers,
            ).json()["items"]
            if item["compliance_type_code"].startswith("GSTR")
        }
        ahead = {
            task["id"]
            for task in self._open_tasks_for(client, auth_headers, client_id)
            if (item := gst_items.get(task["compliance_item_id"]))
            and item["period_start"] > clock.today().isoformat()
        }
        assert ahead, "no GST work for a period still to begin — nothing under test"

        client.patch(
            f"/api/v1/clients/{client_id}",
            json={"gst_registered": False},
            headers=auth_headers,
        )

        open_now = self._open_tasks_for(client, auth_headers, client_id)
        assert not ({task["id"] for task in open_now} & ahead)
        # Income tax is untouched: that registration did not change.
        assert open_now, "dropping GST must not clear the client's whole queue"

    def test_registering_again_brings_that_work_back(
        self, client, auth_headers, client_id
    ):
        self._generate(client, auth_headers, horizon_days=180)
        before = {
            task["id"] for task in self._open_tasks_for(client, auth_headers, client_id)
        }

        client.patch(
            f"/api/v1/clients/{client_id}",
            json={"gst_registered": False},
            headers=auth_headers,
        )
        client.patch(
            f"/api/v1/clients/{client_id}",
            json={"gst_registered": True},
            headers=auth_headers,
        )

        after = {
            task["id"] for task in self._open_tasks_for(client, auth_headers, client_id)
        }
        assert after == before

    def test_a_standalone_task_is_never_touched(self, client, auth_headers, client_id):
        """It names no filing, so no filing closing decides anything about it."""
        standalone = create_task(
            client, auth_headers, client_id=client_id, title="Chase the bank for a NOC"
        ).json()

        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)

        assert (
            client.get(f"/api/v1/tasks/{standalone['id']}", headers=auth_headers).json()[
                "status"
            ]
            == "todo"
        )

    def test_another_firm_client_keeps_its_queue(self, client, auth_headers, client_id):
        """The withdrawal is scoped by the filings it was handed, nothing wider."""
        other_id = client.post(
            "/api/v1/clients",
            json=make_client_payload(name="Second Client", pan="BBBPC1234D"),
            headers=auth_headers,
        ).json()["client"]["id"]
        self._generate(client, auth_headers)
        before = len(self._open_tasks_for(client, auth_headers, other_id))
        assert before > 0

        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)

        assert len(self._open_tasks_for(client, auth_headers, other_id)) == before


class TestOneRunAtATime:
    """Task generation is a read-decide-write, and it was not ordered.

    Read which filings already carry a task, decide that these do not, insert
    one each. Idempotent against a second run, that is — not against a
    concurrent one.

    Compliance generation survives the same race because the database refuses
    the duplicate: ``uq_compliance_item_period`` covers (client, type, period).
    A task has no such constraint, and cannot — a practitioner may legitimately
    raise more than one against a filing by hand — so the ordering has to be
    taken in the service.

    *Generate from filings* on the tasks screen is one runner, reachable by any
    manager at any moment, including twice from one double-clicked button; the
    02:00 beat sweeping every firm in a single transaction is the other, and it
    is inside that transaction for as long as the whole sweep takes. What came
    out was the same filing twice on somebody's queue, counted twice in the
    workload view, counting down twice to one deadline — and one of the pair
    surviving every withdrawal and reinstatement that assumes there is one.
    """

    def _doubled_filings(self, db, firm_uuid: uuid.UUID) -> list[str]:
        return [
            str(item_id)
            for item_id, count in db.execute(
                select(Task.compliance_item_id, func.count(Task.id))
                .where(Task.firm_id == firm_uuid, Task.compliance_item_id.is_not(None))
                .group_by(Task.compliance_item_id)
            ).all()
            if count > 1
        ]

    def test_the_firm_is_held_before_its_queue_is_read(
        self, db, firm_id, client_id, monkeypatch
    ):
        """Ordering is the whole fix, so ordering is what is asserted: a lock
        taken after the decision orders the writes and nothing else, which is
        exactly the state this replaced."""
        order: list[str] = []
        monkeypatch.setattr(
            task_service.firms, "lock_firm", lambda session, fid: order.append("lock")
        )
        reading = db.scalars
        monkeypatch.setattr(
            db, "scalars", lambda *a, **kw: (order.append("read"), reading(*a, **kw))[1]
        )

        task_service.create_tasks_for_due_items(db, uuid.UUID(firm_id), horizon_days=60)

        assert order[:2] == ["lock", "read"]

    def test_work_raised_while_we_waited_is_not_raised_again(
        self, db, firm_id, client_id, monkeypatch
    ):
        """The interleaving itself, in the order it happens.

        The competing run is staged on the lock: it commits from a second
        connection at the moment this one takes the firm's row, which is the
        instant a real loser resumes at. Everything after that is the ordinary
        code path deciding what is left to raise.
        """
        db.rollback()  # SQLite will not let another connection write past a held read

        from app.database import SessionLocal

        fired: list[uuid.UUID] = []

        def winner_commits_first(session, fid):
            if fired:
                return
            fired.append(fid)
            other = SessionLocal()
            try:
                task_service.create_tasks_for_due_items(
                    other, uuid.UUID(firm_id), horizon_days=60
                )
                other.commit()
            finally:
                other.close()

        monkeypatch.setattr(task_service.firms, "lock_firm", winner_commits_first)

        task_service.create_tasks_for_due_items(db, uuid.UUID(firm_id), horizon_days=60)
        db.commit()

        assert fired, "the competing run never happened"
        doubled = self._doubled_filings(db, uuid.UUID(firm_id))
        assert not doubled, f"filings raised twice: {doubled}"

    def test_the_workload_view_is_not_told_the_work_twice(
        self, db, firm_id, client_id, monkeypatch
    ):
        """Where the duplicate does its damage: this is what a manager reads to
        decide who is drowning and who is free."""
        db.rollback()
        from app.database import SessionLocal

        fired: list[uuid.UUID] = []

        def winner_commits_first(session, fid):
            if fired:
                return
            fired.append(fid)
            other = SessionLocal()
            try:
                task_service.create_tasks_for_due_items(
                    other, uuid.UUID(firm_id), horizon_days=60
                )
                other.commit()
            finally:
                other.close()

        monkeypatch.setattr(task_service.firms, "lock_firm", winner_commits_first)
        task_service.create_tasks_for_due_items(db, uuid.UUID(firm_id), horizon_days=60)
        db.commit()

        monkeypatch.undo()
        open_tasks = sum(row.open_tasks for row in task_service.workload(db, uuid.UUID(firm_id)))
        filings = db.scalar(
            select(func.count(func.distinct(Task.compliance_item_id))).where(
                Task.firm_id == uuid.UUID(firm_id), Task.compliance_item_id.is_not(None)
            )
        )
        assert open_tasks == filings

    def test_a_double_clicked_button_raises_one_set_of_work(
        self, client, auth_headers, client_id, db, firm_id
    ):
        """The ordinary path, unmocked: pressing it twice is still one queue."""
        first = client.post(
            "/api/v1/tasks/generate", json={"horizon_days": 60}, headers=auth_headers
        ).json()["created"]
        second = client.post(
            "/api/v1/tasks/generate", json={"horizon_days": 60}, headers=auth_headers
        ).json()["created"]

        assert first > 0
        assert second == 0
        assert not self._doubled_filings(db, uuid.UUID(firm_id))

    def test_the_beat_sweep_holds_the_firms_in_a_fixed_order(
        self, db, client_id, monkeypatch
    ):
        """Two runs walking an unordered list can each hold what the other
        wants next, and the sweep's transaction spans every firm at once."""
        held: list[uuid.UUID] = []
        monkeypatch.setattr(
            task_service.firms, "lock_firm", lambda session, fid: held.append(fid)
        )

        worker_tasks.generate_tasks_task()

        assert held == sorted(held), "an undefined lock order lets two sweeps cross"
        assert set(held) == set(
            db.scalars(select(Firm.id).where(Firm.is_active.is_(True))).all()
        )


class TestATaskThatNamesTwoClientsAtOnce:
    """A task names a client and, optionally, the filing it discharges.

    Both were checked against the firm; neither was checked against the other.
    A compliance item is addressable by id alone, so a stale id from the wrong
    screen put one client's return onto another client's task — the everyday
    version of the mistake, and the one a reminder is already refused for.

    It does not stay cosmetic. The board renders the client name from
    ``client_id`` and the period from the filing, so the row reads as one
    client's work while being another's, and whoever picks it up files against
    the wrong client. Withdrawal follows the *filing*, so off-boarding the
    named client leaves the task standing while surrendering the other
    client's registration cancels it out from under them — in both cases for
    reasons nothing on the task explains.
    """

    @pytest.fixture
    def other_client_id(self, client, auth_headers) -> str:
        response = client.post(
            "/api/v1/clients",
            json=make_client_payload(
                name="Ravi Traders", pan="AAFCR7788K", gstin="27AAFCR7788K1Z9"
            ),
            headers=auth_headers,
        )
        assert response.status_code == 201, response.text
        return response.json()["client"]["id"]

    def _their_filing(self, client, auth_headers, owner_id: str) -> dict:
        body = client.get(
            "/api/v1/compliance/calendar",
            params={"client_id": owner_id, "limit": 1000},
            headers=auth_headers,
        ).json()
        assert body["items"], "that client has no filings"
        return body["items"][0]

    def _a_future_gst_filing(self, client, auth_headers, owner_id: str) -> dict:
        """One of their GST returns for a period that has not begun — the only
        kind a surrender closes, since a registration dropped mid-month still
        owes that month's return."""
        today = clock.today().isoformat()
        body = client.get(
            "/api/v1/compliance/calendar",
            params={"client_id": owner_id, "limit": 1000},
            headers=auth_headers,
        ).json()
        item = next(
            (
                candidate
                for candidate in body["items"]
                if candidate["compliance_type_code"].startswith("GSTR")
                and candidate["period_start"] > today
            ),
            None,
        )
        assert item is not None, "that client has no GST filing for a future period"
        return item

    def test_a_filing_belonging_to_another_client_is_refused(
        self, client, auth_headers, client_id, other_client_id
    ):
        theirs = self._their_filing(client, auth_headers, other_client_id)

        response = create_task(
            client,
            auth_headers,
            client_id=client_id,
            compliance_item_id=theirs["id"],
        )

        assert response.status_code == 400, response.text
        assert "different client" in response.json()["detail"]

    def test_nothing_is_created_when_the_pair_is_refused(
        self, client, auth_headers, client_id, other_client_id, db, firm_id
    ):
        theirs = self._their_filing(client, auth_headers, other_client_id)
        before = db.scalar(
            select(func.count(Task.id)).where(Task.firm_id == uuid.UUID(firm_id))
        )

        create_task(
            client, auth_headers, client_id=client_id, compliance_item_id=theirs["id"]
        )

        assert (
            db.scalar(select(func.count(Task.id)).where(Task.firm_id == uuid.UUID(firm_id)))
            == before
        )

    def test_the_filing_supplies_the_client_when_none_is_named(
        self, client, auth_headers, client_id
    ):
        """Generation always takes the client from the filing, so a task
        created by hand against one does the same rather than landing
        clientless on the board."""
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")

        response = create_task(
            client, auth_headers, compliance_item_id=item["id"]
        )

        assert response.status_code == 201, response.text
        assert response.json()["client_id"] == client_id
        assert response.json()["client_name"] == "Nimbus Textiles Pvt Ltd"

    def test_the_matching_pair_is_still_accepted(self, client, auth_headers, client_id):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")

        response = create_task(
            client, auth_headers, client_id=client_id, compliance_item_id=item["id"]
        )

        assert response.status_code == 201, response.text
        assert response.json()["compliance_item_id"] == item["id"]
        assert response.json()["period_label"] == item["period_label"]

    def test_withdrawal_no_longer_reaches_past_the_named_client(
        self, client, auth_headers, client_id, other_client_id, db, firm_id
    ):
        """What the mismatch cost, from the other end: closing one client's
        obligations cancelled work booked against a different client.

        The task said Nimbus; the filing behind it was Ravi's. Ravi surrenders
        GST, their filings close, and the withdrawal follows the filing — so a
        task on Nimbus's board vanished for a reason nothing on it explains.
        With the pair refused there is no such task to reach.
        """
        theirs = self._a_future_gst_filing(client, auth_headers, other_client_id)
        create_task(
            client,
            auth_headers,
            client_id=client_id,
            compliance_item_id=theirs["id"],
            title="Reconcile the GST ledger for Nimbus",
        )

        client.patch(
            f"/api/v1/clients/{other_client_id}",
            json={"gst_registered": False},
            headers=auth_headers,
        )

        db.expire_all()
        cancelled = db.scalars(
            select(Task).where(
                Task.firm_id == uuid.UUID(firm_id),
                Task.client_id == uuid.UUID(client_id),
                Task.status == TaskStatus.CANCELLED,
            )
        ).all()
        assert not cancelled, "a surrender reached past the client the task named"


class TestWhatADepartingMemberIsStillNamedOn:
    """Three things name a practitioner; only the tasks were handed back.

    ``assert_assignable`` refuses to put a client, a filing or a task on
    someone switched off, and says why: work on a name nobody can sign in as
    shows up on no active member's queue and in no unassigned pile, so the
    deadline sits where nobody is watching it. Deactivation itself left exactly
    that state on two of the three.

    The clients are the half that does not stay still. Generation stamps a new
    compliance item with ``client.assigned_practitioner_id``, so every filing
    materialised for that client afterwards — monthly, by the unattended
    nightly top-up — was raised onto the departed member afresh. The problem
    did not merely persist; it regenerated.
    """

    @staticmethod
    def _deactivate(client, auth_headers, practitioner_id) -> None:
        response = client.patch(
            f"/api/v1/auth/practitioners/{practitioner_id}",
            json={"is_active": False},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

    @staticmethod
    def _own_the_client(client, auth_headers, client_id, practitioner_id) -> None:
        response = client.patch(
            f"/api/v1/clients/{client_id}",
            json={"assigned_practitioner_id": practitioner_id},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

    @staticmethod
    def _own_the_filing(client, auth_headers, item_id, practitioner_id) -> None:
        response = client.patch(
            f"/api/v1/compliance/items/{item_id}",
            json={"assigned_practitioner_id": practitioner_id},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

    @staticmethod
    def _items_of(client, auth_headers, **params) -> list[dict]:
        body = client.get(
            "/api/v1/compliance/calendar",
            params={"from_date": "2020-01-01", "to_date": "2035-12-31", "limit": 1000, **params},
            headers=auth_headers,
        ).json()
        return body["items"]

    # -------------------------------------------------- what comes back --

    def test_a_client_they_owned_comes_back_unassigned(
        self, client, auth_headers, client_id, junior
    ):
        self._own_the_client(client, auth_headers, client_id, junior["id"])

        self._deactivate(client, auth_headers, junior["id"])

        after = client.get(f"/api/v1/clients/{client_id}", headers=auth_headers).json()
        assert after["assigned_practitioner_id"] is None
        assert after["assigned_practitioner_name"] is None

    def test_their_open_filings_come_back_unassigned(
        self, client, auth_headers, client_id, junior
    ):
        """The calendar filters by assignee and has no unassigned bucket, so a
        filing left under a dead account is one nothing surfaces."""
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        self._own_the_filing(client, auth_headers, item["id"], junior["id"])
        assert self._items_of(client, auth_headers, assigned_to=junior["id"])

        self._deactivate(client, auth_headers, junior["id"])

        assert self._items_of(client, auth_headers, assigned_to=junior["id"]) == []

    def test_a_return_they_lodged_keeps_their_name(
        self, client, auth_headers, client_id, junior
    ):
        """A filed return records who filed it — rewriting that loses it."""
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        self._own_the_filing(client, auth_headers, item["id"], junior["id"])
        client.patch(
            f"/api/v1/compliance/items/{item['id']}",
            json={"status": "filed"},
            headers=auth_headers,
        )

        self._deactivate(client, auth_headers, junior["id"])

        after = client.get(
            f"/api/v1/compliance/items/{item['id']}", headers=auth_headers
        ).json()
        assert after["assigned_practitioner_id"] == junior["id"]

    def test_nobody_elses_work_moves(
        self, client, auth_headers, client_id, junior, registered_firm
    ):
        owner_id = registered_firm["practitioner"]["id"]
        self._own_the_client(client, auth_headers, client_id, owner_id)
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        self._own_the_filing(client, auth_headers, item["id"], owner_id)

        self._deactivate(client, auth_headers, junior["id"])

        assert client.get(f"/api/v1/clients/{client_id}", headers=auth_headers).json()[
            "assigned_practitioner_id"
        ] == owner_id
        assert client.get(
            f"/api/v1/compliance/items/{item['id']}", headers=auth_headers
        ).json()["assigned_practitioner_id"] == owner_id

    def test_an_ordinary_edit_moves_nothing(
        self, client, auth_headers, client_id, junior
    ):
        """Only the switch-off transition releases; a name change must not."""
        self._own_the_client(client, auth_headers, client_id, junior["id"])

        client.patch(
            f"/api/v1/auth/practitioners/{junior['id']}",
            json={"full_name": "Junior Jain-Mehta"},
            headers=auth_headers,
        )

        assert client.get(f"/api/v1/clients/{client_id}", headers=auth_headers).json()[
            "assigned_practitioner_id"
        ] == junior["id"]

    def test_the_audit_trail_says_what_moved(
        self, client, auth_headers, client_id, junior
    ):
        """A client losing its owner overnight needs a recorded reason."""
        self._own_the_client(client, auth_headers, client_id, junior["id"])

        self._deactivate(client, auth_headers, junior["id"])

        entries = client.get(
            "/api/v1/audit",
            params={"action": "practitioner.update"},
            headers=auth_headers,
        ).json()["items"]
        assert (
            "1 client(s) and 0 open filing(s) returned to unassigned"
            in entries[0]["summary"]
        )

    # ------------------------------------------- and stops coming back --

    def test_a_top_up_stops_raising_filings_onto_them(
        self, client, auth_headers, client_id, junior, db
    ):
        """The half that regenerated. Generation reads the client's owner, so
        every month's new filings were stamped with the departed member again —
        by the nightly sweep, unattended, for as long as the client was on the
        books.

        The client is pointed back at them directly, which is the state any
        deployment already carrying this is in.
        """
        self._deactivate(client, auth_headers, junior["id"])
        record = db.get(Client, uuid.UUID(client_id))
        record.assigned_practitioner_id = uuid.UUID(junior["id"])
        db.commit()

        response = client.post(
            f"/api/v1/clients/{client_id}/compliance-items",
            json={
                "window_start": (clock.today() + timedelta(days=400)).isoformat(),
                "window_end": (clock.today() + timedelta(days=700)).isoformat(),
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["created"] > 0

        assert self._items_of(client, auth_headers, assigned_to=junior["id"]) == []

    def test_the_nightly_sweep_does_not_either(
        self, client, auth_headers, client_id, junior, db
    ):
        self._deactivate(client, auth_headers, junior["id"])
        record = db.get(Client, uuid.UUID(client_id))
        record.assigned_practitioner_id = uuid.UUID(junior["id"])
        db.commit()
        # Push the horizon out so the sweep has something left to materialise.
        db.query(ComplianceItem).filter(
            ComplianceItem.client_id == uuid.UUID(client_id)
        ).delete()
        db.commit()

        worker_tasks.generate_compliance_items_task()

        assert self._items_of(client, auth_headers, assigned_to=junior["id"]) == []
        assert self._items_of(client, auth_headers), "the sweep created nothing at all"

    def test_an_active_owner_is_still_inherited(
        self, client, auth_headers, client_id, junior
    ):
        """The guard drops a dead name, not every name."""
        self._own_the_client(client, auth_headers, client_id, junior["id"])

        response = client.post(
            f"/api/v1/clients/{client_id}/compliance-items",
            json={
                "window_start": (clock.today() + timedelta(days=400)).isoformat(),
                "window_end": (clock.today() + timedelta(days=700)).isoformat(),
            },
            headers=auth_headers,
        )
        assert response.json()["created"] > 0

        assert self._items_of(client, auth_headers, assigned_to=junior["id"])


class TestASelectionThatNamesOneTaskTwice:
    """``skipped`` was measured against the raw id list, so a task named twice
    counted as one the board could not find.

    A selection is built by clicking rows, and the board re-reads between
    clicks, so a repeat is ordinary. Counting it as a skip reports work that
    was done as work that was not — see the compliance-calendar counterpart,
    which had the same arithmetic.
    """

    def test_a_repeated_id_is_not_reported_as_skipped(self, client, auth_headers):
        task_id = create_task(client, auth_headers).json()["id"]

        response = client.post(
            "/api/v1/tasks/bulk",
            json={"task_ids": [task_id] * 3, "status": "in_progress"},
            headers=auth_headers,
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"updated": 1, "skipped": 0}

    def test_the_repeated_task_is_still_updated(self, client, auth_headers):
        task_id = create_task(client, auth_headers).json()["id"]

        client.post(
            "/api/v1/tasks/bulk",
            json={"task_ids": [task_id, task_id], "priority": "urgent"},
            headers=auth_headers,
        )

        body = client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers).json()
        assert body["priority"] == "urgent"

    def test_an_unknown_id_is_still_counted_once_beside_a_repeat(
        self, client, auth_headers
    ):
        task_id = create_task(client, auth_headers).json()["id"]
        stranger = str(uuid.uuid4())

        response = client.post(
            "/api/v1/tasks/bulk",
            json={"task_ids": [task_id, task_id, stranger, stranger], "status": "done"},
            headers=auth_headers,
        )

        assert response.json() == {"updated": 1, "skipped": 1}
