"""Discovery: the question tree, the decisions on it, and the generated files.

The run is a gate — a generator waits until every question has an answer — so
the tests that matter are the counting ones: what "answered" means when an item
has been answered twice, and when `done` is allowed to flip.
"""

import hashlib

import httpx

from tests.conftest import API, register, sample_doc, unwrap

# One of each family, and deliberately uneven: R1 is a root, Q6 depends on it
# and needs a human, I-108 has no proposed answer to accept, M-016 has neither.
ITEMS = [
    {
        "key": "R1",
        "family": "RULE",
        "kind": "policy",
        "title": "Deletes are soft everywhere",
        "body": "Every table keeps `is_deleted`.",
        "modules": ["core"],
        "options": [
            {"key": "soft", "label": "Soft delete", "consequence": "restore is a button"},
            {"key": "hard", "label": "Hard delete", "consequence": "gone is gone"},
        ],
        "proposed": "Soft delete everywhere",
        "proposed_key": "soft",
        "position": 0,
    },
    {
        "key": "Q6",
        "family": "QUESTION",
        "kind": "permissions",
        "title": "Who may void an invoice?",
        "modules": ["billing", "core"],
        "needs_user": True,
        "depends_on": ["R1"],
        "proposed": "Owners only",
        "proposed_key": "owner",
        "position": 1,
    },
    {
        "key": "I-108",
        "family": "ISSUE",
        "kind": "data-integrity",
        "severity": "high",
        "title": "Invoice numbers have no unique index",
        "modules": ["billing"],
        "position": 2,
    },
    {
        "key": "M-016",
        "family": "M",
        "kind": "module",
        "title": "Refunds are never mentioned",
        "modules": ["billing"],
        "needs_user": True,
        "position": 3,
    },
]


async def create_project(client: httpx.AsyncClient, actor) -> str:  # type: ignore[no-untyped-def]
    response = await client.post(
        f"{API}/projects", headers=actor.headers, json={"name": "CRM", "doc": sample_doc(1)}
    )
    assert response.status_code == 201, response.text
    return str(unwrap(response)["id"])


async def create_run(client: httpx.AsyncClient, actor, project_id: str) -> dict:  # type: ignore[no-untyped-def]
    response = await client.post(
        f"{API}/projects/{project_id}/discovery/runs",
        headers=actor.headers,
        json={"label": "CRM trial", "source": "weave-discover", "items": ITEMS},
    )
    assert response.status_code == 201, response.text
    return dict(unwrap(response))


async def item_map(client: httpx.AsyncClient, actor, project_id: str, run_id: str) -> dict:  # type: ignore[no-untyped-def]
    """key -> item, as the studio sees it."""
    listed = unwrap(
        await client.get(
            f"{API}/projects/{project_id}/discovery/runs/{run_id}/items", headers=actor.headers
        )
    )
    return {item["key"]: item for item in listed}


async def invite(client: httpx.AsyncClient, owner, project_id: str, role: str):  # type: ignore[no-untyped-def]
    person = await register(client)
    response = await client.post(
        f"{API}/projects/{project_id}/members",
        headers=owner.headers,
        json={"email": person.email, "role": role},
    )
    assert response.status_code == 201, response.text
    return person


async def test_a_run_arrives_whole_and_reports_its_progress(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project_id = await create_project(client, owner)
    run = await create_run(client, owner, project_id)

    assert run["label"] == "CRM trial"
    assert run["done"] is False
    assert run["progress"] == {
        "total": 4,
        "answered": 0,
        "needs_user": 2,
        "needs_user_answered": 0,
        # A decision about auth is a decision about every module behind it, so
        # an item counts towards each of its modules.
        "modules": {
            "core": {"total": 2, "answered": 0},
            "billing": {"total": 3, "answered": 0},
        },
    }

    items = await item_map(client, owner, project_id, run["id"])
    assert set(items) == {"R1", "Q6", "I-108", "M-016"}
    assert items["Q6"]["depends_on"] == ["R1"]
    assert items["I-108"]["severity"] == "high"
    assert items["R1"]["options"][0]["consequence"] == "restore is a button"
    assert all(item["answer"] is None for item in items.values())

    listed = unwrap(
        await client.get(f"{API}/projects/{project_id}/discovery/runs", headers=owner.headers)
    )
    assert [one["id"] for one in listed] == [run["id"]]


async def test_answering_twice_keeps_the_latest_and_still_counts_once(
    client: httpx.AsyncClient,
) -> None:
    """Answers are append-only. Two decisions on one question is a change of
    mind, not two answered questions."""
    owner = await register(client)
    project_id = await create_project(client, owner)
    run = await create_run(client, owner, project_id)
    items = await item_map(client, owner, project_id, run["id"])
    answer_url = (
        f"{API}/projects/{project_id}/discovery/runs/{run['id']}/items/{items['R1']['id']}/answer"
    )

    first = await client.post(
        answer_url, headers=owner.headers, json={"decision": "Hard delete", "choice_key": "hard"}
    )
    assert first.status_code == 201, first.text
    second = unwrap(
        await client.post(
            answer_url,
            headers=owner.headers,
            json={"decision": "Soft delete", "choice_key": "soft", "note": "restore is a button"},
        )
    )
    assert second["answer"]["decision"] == "Soft delete"

    items = await item_map(client, owner, project_id, run["id"])
    assert items["R1"]["answer"]["decision"] == "Soft delete"
    assert items["R1"]["answer"]["note"] == "restore is a button"

    detail = unwrap(
        await client.get(
            f"{API}/projects/{project_id}/discovery/runs/{run['id']}", headers=owner.headers
        )
    )
    assert detail["progress"]["answered"] == 1
    assert detail["progress"]["modules"]["core"] == {"total": 2, "answered": 1}
    assert detail["done"] is False

    exported = unwrap(
        await client.get(
            f"{API}/projects/{project_id}/discovery/runs/{run['id']}/answers",
            headers=owner.headers,
        )
    )
    assert exported == [
        {
            "key": "R1",
            "family": "RULE",
            "decision": "Soft delete",
            "choice_key": "soft",
            "note": "restore is a button",
        }
    ]


async def test_bulk_accept_takes_the_proposals_and_names_what_it_skipped(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    project_id = await create_project(client, owner)
    run = await create_run(client, owner, project_id)
    items = await item_map(client, owner, project_id, run["id"])

    result = unwrap(
        await client.post(
            f"{API}/projects/{project_id}/discovery/runs/{run['id']}/answers/bulk",
            headers=owner.headers,
            json={"item_ids": [items["R1"]["id"], items["Q6"]["id"], items["I-108"]["id"]]},
        )
    )
    # I-108 has no proposed answer, so there is nothing to accept on its behalf.
    assert result == {"accepted": 2, "skipped": [items["I-108"]["id"]]}

    items = await item_map(client, owner, project_id, run["id"])
    assert items["R1"]["answer"]["decision"] == "Soft delete everywhere"
    assert items["Q6"]["answer"]["choice_key"] == "owner"
    assert items["I-108"]["answer"] is None


async def test_done_flips_only_when_the_last_question_is_answered(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    project_id = await create_project(client, owner)
    run = await create_run(client, owner, project_id)
    items = await item_map(client, owner, project_id, run["id"])
    base = f"{API}/projects/{project_id}/discovery/runs/{run['id']}"

    for key in ("R1", "Q6", "I-108", "M-016"):
        response = await client.post(
            f"{base}/items/{items[key]['id']}/answer",
            headers=owner.headers,
            json={"decision": f"decided {key}"},
        )
        assert response.status_code == 201, response.text
        detail = unwrap(await client.get(base, headers=owner.headers))
        assert detail["done"] is (key == "M-016"), key

    assert detail["progress"] == {
        "total": 4,
        "answered": 4,
        "needs_user": 2,
        "needs_user_answered": 2,
        "modules": {
            "core": {"total": 2, "answered": 2},
            "billing": {"total": 3, "answered": 3},
        },
    }


async def test_items_can_be_narrowed_to_what_the_studio_is_showing(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    project_id = await create_project(client, owner)
    run = await create_run(client, owner, project_id)
    items = await item_map(client, owner, project_id, run["id"])
    base = f"{API}/projects/{project_id}/discovery/runs/{run['id']}/items"

    await client.post(
        f"{API}/projects/{project_id}/discovery/runs/{run['id']}/items/{items['R1']['id']}/answer",
        headers=owner.headers,
        json={"decision": "Soft delete"},
    )

    async def keys(**query: object) -> list[str]:
        listed = unwrap(await client.get(base, headers=owner.headers, params=query))
        return [item["key"] for item in listed]

    assert await keys(module="core") == ["R1", "Q6"]
    assert await keys(family="ISSUE") == ["I-108"]
    assert await keys(needs_user=True) == ["Q6", "M-016"]
    assert await keys(unanswered=True) == ["Q6", "I-108", "M-016"]
    assert await keys(module="billing", unanswered=True) == ["Q6", "I-108", "M-016"]


async def test_an_artifact_is_one_row_per_name_and_carries_its_sha(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    project_id = await create_project(client, owner)
    # A path, not a name: the files live under `weave/` and are addressed the
    # way the generator writes them.
    url = f"{API}/projects/{project_id}/discovery/artifacts/discovery/kb.md"

    first = unwrap(
        await client.put(url, headers=owner.headers, json={"kind": "md", "body": "# KB"})
    )
    assert first["sha"] == hashlib.sha256(b"# KB").hexdigest()

    second = unwrap(
        await client.put(url, headers=owner.headers, json={"kind": "md", "body": "# KB, revised"})
    )
    assert second["id"] == first["id"], "pushing again must replace the row, not add one"
    assert second["sha"] == hashlib.sha256(b"# KB, revised").hexdigest()
    assert second["sha"] != first["sha"]

    listed = unwrap(
        await client.get(f"{API}/projects/{project_id}/discovery/artifacts", headers=owner.headers)
    )
    assert [one["name"] for one in listed] == ["discovery/kb.md"]
    assert "body" not in listed[0], "the listing must not carry four megabytes per file"

    read = unwrap(await client.get(url, headers=owner.headers))
    assert read["body"] == "# KB, revised"

    missing = await client.get(
        f"{API}/projects/{project_id}/discovery/artifacts/nope.md", headers=owner.headers
    )
    assert missing.status_code == 404


async def test_an_oversized_artifact_is_refused(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project_id = await create_project(client, owner)
    response = await client.put(
        f"{API}/projects/{project_id}/discovery/artifacts/huge.md",
        headers=owner.headers,
        json={"kind": "md", "body": "x" * (4 * 1024 * 1024 + 1)},
    )
    assert response.status_code == 422
    assert "4MB" in response.json()["message"]


async def test_a_run_from_another_project_is_not_reachable(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    mine = await create_project(client, owner)
    theirs = await create_project(client, owner)
    run = await create_run(client, owner, theirs)

    response = await client.get(
        f"{API}/projects/{mine}/discovery/runs/{run['id']}", headers=owner.headers
    )
    assert response.status_code == 404


async def test_every_discovery_route_is_closed_to_an_outsider(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    outsider = await register(client)
    project_id = await create_project(client, owner)
    run = await create_run(client, owner, project_id)
    items = await item_map(client, owner, project_id, run["id"])
    base = f"{API}/projects/{project_id}/discovery"
    headers = outsider.headers

    attempts = [
        client.get(f"{base}/runs", headers=headers),
        client.post(f"{base}/runs", headers=headers, json={"label": "x", "items": []}),
        client.get(f"{base}/runs/{run['id']}", headers=headers),
        client.get(f"{base}/runs/{run['id']}/items", headers=headers),
        client.post(
            f"{base}/runs/{run['id']}/items/{items['R1']['id']}/answer",
            headers=headers,
            json={"decision": "mine now"},
        ),
        client.post(
            f"{base}/runs/{run['id']}/answers/bulk",
            headers=headers,
            json={"item_ids": [items["R1"]["id"]]},
        ),
        client.get(f"{base}/runs/{run['id']}/answers", headers=headers),
        client.get(f"{base}/artifacts", headers=headers),
        client.get(f"{base}/artifacts/discovery/kb.md", headers=headers),
        client.put(f"{base}/artifacts/discovery/kb.md", headers=headers, json={"body": "mine"}),
    ]
    for attempt in attempts:
        response = await attempt
        # 404, not 403 — a stranger must not learn that the project exists.
        assert response.status_code == 404, response.request.url


async def test_a_viewer_reads_the_tree_but_cannot_decide_anything(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    project_id = await create_project(client, owner)
    run = await create_run(client, owner, project_id)
    items = await item_map(client, owner, project_id, run["id"])
    viewer = await invite(client, owner, project_id, "VIEWER")
    base = f"{API}/projects/{project_id}/discovery"

    assert (await client.get(f"{base}/runs", headers=viewer.headers)).status_code == 200
    assert (
        await client.get(f"{base}/runs/{run['id']}/items", headers=viewer.headers)
    ).status_code == 200

    writes = [
        client.post(f"{base}/runs", headers=viewer.headers, json={"label": "x", "items": []}),
        client.post(
            f"{base}/runs/{run['id']}/items/{items['R1']['id']}/answer",
            headers=viewer.headers,
            json={"decision": "mine"},
        ),
        client.post(
            f"{base}/runs/{run['id']}/answers/bulk",
            headers=viewer.headers,
            json={"item_ids": [items["R1"]["id"]]},
        ),
        client.put(f"{base}/artifacts/kb.md", headers=viewer.headers, json={"body": "mine"}),
    ]
    for attempt in writes:
        response = await attempt
        assert response.status_code == 403, response.request.url


async def test_an_editor_may_answer(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project_id = await create_project(client, owner)
    run = await create_run(client, owner, project_id)
    items = await item_map(client, owner, project_id, run["id"])
    editor = await invite(client, owner, project_id, "EDITOR")

    response = await client.post(
        f"{API}/projects/{project_id}/discovery/runs/{run['id']}/items/{items['Q6']['id']}/answer",
        headers=editor.headers,
        json={"decision": "Owners only", "choice_key": "owner"},
    )
    assert response.status_code == 201, response.text
    assert unwrap(response)["answer"]["decision"] == "Owners only"
