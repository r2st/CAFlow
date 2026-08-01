"""Task management: creation from deadlines, assignment and workload."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.models.base import TaskPriority, TaskStatus
from app.services import tasks as task_service
from tests.conftest import first_item_of_type


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
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        task = create_task(client, auth_headers, due_date=yesterday).json()
        assert task["is_overdue"] is True
        assert task["days_remaining"] == -1

    def test_a_completed_task_is_not_overdue(self, client, auth_headers):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
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
        yesterday = (date.today() - timedelta(days=1)).isoformat()
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
            due_date=(date.today() + timedelta(days=5)).isoformat(),
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
            due_date=(date.today() - timedelta(days=2)).isoformat(),
        )
        create_task(
            client,
            auth_headers,
            title="Junior soon",
            assignee_id=junior["id"],
            due_date=(date.today() + timedelta(days=3)).isoformat(),
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
