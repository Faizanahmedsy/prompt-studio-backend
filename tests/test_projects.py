"""Projects: creation, the document, concurrency, versions, comments."""

import httpx

from tests.conftest import API, register, sample_doc, unwrap


async def create(client: httpx.AsyncClient, actor, **overrides) -> dict:  # type: ignore[no-untyped-def]
    body = {"name": "Ops Console", "description": "internal tool", "doc": sample_doc(2, 3)}
    body.update(overrides)
    response = await client.post(f"{API}/projects", headers=actor.headers, json=body)
    assert response.status_code == 201, response.text
    return unwrap(response)


async def test_create_makes_the_author_the_owner(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    project = await create(client, actor)
    assert project["my_role"] == "OWNER"
    assert project["owner"]["email"] == actor.email
    assert project["member_count"] == 1
    assert project["members"][0]["email"] == actor.email
    assert project["members"][0]["status"] == "ACTIVE"


async def test_counts_are_derived_from_the_document(client: httpx.AsyncClient) -> None:
    """The list page shows "12 screens" without loading twelve documents, so
    the numbers on the row have to be kept in step with the JSON."""
    actor = await register(client)
    project = await create(client, actor, doc=sample_doc(5, 7))
    assert project["screen_count"] == 5
    assert project["module_count"] == 7

    saved = unwrap(
        await client.put(
            f"{API}/projects/{project['id']}/document",
            headers=actor.headers,
            json={"doc": sample_doc(2, 1), "base_version": project["doc_version"]},
        )
    )
    assert saved["screen_count"] == 2
    assert saved["module_count"] == 1


async def test_the_whole_document_survives_a_round_trip(client: httpx.AsyncClient) -> None:
    """The server stores the editor's document verbatim. Anything it silently
    reshaped would come back as data loss in the canvas."""
    actor = await register(client)
    doc = sample_doc(3, 2)
    doc["theme"] = {"primaryColor": "#ff0000", "density": "compact"}
    doc["surfaces"] = {"mobile": {"stack": {"framework": "expo-router"}}}
    doc["requirements"] = "line one\nline two\twith a tab\nand a “quote”"
    project = await create(client, actor, doc=doc)
    fetched = unwrap(await client.get(f"{API}/projects/{project['id']}", headers=actor.headers))
    assert fetched["doc"] == doc


async def test_a_document_that_is_not_an_object_is_refused(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    response = await client.post(
        f"{API}/projects", headers=actor.headers, json={"name": "x", "doc": [1, 2, 3]}
    )
    assert response.status_code == 422


async def test_a_document_with_a_non_list_screens_is_refused(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    response = await client.post(
        f"{API}/projects", headers=actor.headers, json={"name": "x", "doc": {"screens": "nope"}}
    )
    assert response.status_code == 422


async def test_stale_saves_are_refused_with_the_current_version(
    client: httpx.AsyncClient,
) -> None:
    actor = await register(client)
    project = await create(client, actor)
    stale = project["doc_version"]

    unwrap(
        await client.put(
            f"{API}/projects/{project['id']}/document",
            headers=actor.headers,
            json={"doc": sample_doc(4), "base_version": stale},
        )
    )
    conflicted = await client.put(
        f"{API}/projects/{project['id']}/document",
        headers=actor.headers,
        json={"doc": sample_doc(1), "base_version": stale},
    )
    assert conflicted.status_code == 409
    detail = conflicted.json()["errors"][0]
    assert detail["current_version"] == stale + 1
    assert detail["sent_version"] == stale

    # The refused save really did not land.
    current = unwrap(await client.get(f"{API}/projects/{project['id']}", headers=actor.headers))
    assert len(current["doc"]["screens"]) == 4


async def test_omitting_the_base_version_forces_the_write(client: httpx.AsyncClient) -> None:
    """The socket path has already reconciled, so it says so explicitly rather
    than being made to guess a version it would only be told to ignore."""
    actor = await register(client)
    project = await create(client, actor)
    await client.put(
        f"{API}/projects/{project['id']}/document",
        headers=actor.headers,
        json={"doc": sample_doc(9), "base_version": project["doc_version"]},
    )
    forced = await client.put(
        f"{API}/projects/{project['id']}/document",
        headers=actor.headers,
        json={"doc": sample_doc(1)},
    )
    assert forced.status_code == 200


async def test_rename_and_archive(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    project = await create(client, actor)
    renamed = unwrap(
        await client.patch(
            f"{API}/projects/{project['id']}",
            headers=actor.headers,
            json={"name": "Renamed", "is_archived": True},
        )
    )
    assert renamed["name"] == "Renamed"
    assert renamed["is_archived"] is True

    listed = unwrap(await client.get(f"{API}/projects", headers=actor.headers))
    assert listed["total"] == 0, "archived projects are out of the default list"
    archived = unwrap(await client.get(f"{API}/projects?status=archived", headers=actor.headers))
    assert archived["total"] == 1


async def test_delete_is_recoverable(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    project = await create(client, actor)

    assert (
        await client.delete(f"{API}/projects/{project['id']}", headers=actor.headers)
    ).status_code == 200
    assert (
        await client.get(f"{API}/projects/{project['id']}", headers=actor.headers)
    ).status_code == 404
    assert unwrap(await client.get(f"{API}/projects", headers=actor.headers))["total"] == 0

    restored = await client.post(f"{API}/projects/{project['id']}/restore", headers=actor.headers)
    assert restored.status_code == 200
    assert unwrap(await client.get(f"{API}/projects", headers=actor.headers))["total"] == 1


async def test_only_an_owner_can_restore(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    other = await register(client)
    project = await create(client, owner)
    await client.delete(f"{API}/projects/{project['id']}", headers=owner.headers)

    response = await client.post(f"{API}/projects/{project['id']}/restore", headers=other.headers)
    # 404, like every other project route — telling a non-member "forbidden"
    # would confirm the project exists.
    assert response.status_code == 404


async def test_search_and_sort(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    await create(client, actor, name="Alpha Console", doc=sample_doc(1))
    await create(client, actor, name="Beta Dashboard", doc=sample_doc(9))

    found = unwrap(await client.get(f"{API}/projects?search=beta", headers=actor.headers))
    assert found["total"] == 1 and found["items"][0]["name"] == "Beta Dashboard"

    by_screens = unwrap(await client.get(f"{API}/projects?sort=screens", headers=actor.headers))
    assert by_screens["items"][0]["name"] == "Beta Dashboard"

    by_name = unwrap(await client.get(f"{API}/projects?sort=name", headers=actor.headers))
    assert [item["name"] for item in by_name["items"]] == ["Alpha Console", "Beta Dashboard"]


async def test_pagination_reports_itself_correctly(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    for index in range(7):
        await create(client, actor, name=f"Project {index}")

    first = unwrap(await client.get(f"{API}/projects?page=1&size=3", headers=actor.headers))
    assert (first["total"], first["pages"], first["has_next"], first["has_prev"]) == (
        7,
        3,
        True,
        False,
    )
    assert len(first["items"]) == 3

    last = unwrap(await client.get(f"{API}/projects?page=3&size=3", headers=actor.headers))
    assert (last["has_next"], last["has_prev"], len(last["items"])) == (False, True, 1)


async def test_versions_snapshot_restore_and_prune(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    project = await create(client, actor, doc=sample_doc(1))
    project_id = project["id"]

    named = unwrap(
        await client.post(
            f"{API}/projects/{project_id}/versions",
            headers=actor.headers,
            json={"label": "before the demo"},
        )
    )
    assert named["is_auto"] is False

    await client.put(
        f"{API}/projects/{project_id}/document",
        headers=actor.headers,
        json={"doc": sample_doc(6)},
    )
    assert (
        len(
            unwrap(await client.get(f"{API}/projects/{project_id}", headers=actor.headers))["doc"][
                "screens"
            ]
        )
        == 6
    )

    restored = unwrap(
        await client.post(
            f"{API}/projects/{project_id}/versions/{named['id']}/restore", headers=actor.headers
        )
    )
    assert restored["screen_count"] == 1
    body = unwrap(await client.get(f"{API}/projects/{project_id}", headers=actor.headers))
    assert len(body["doc"]["screens"]) == 1


async def test_a_version_from_another_project_is_not_reachable(
    client: httpx.AsyncClient,
) -> None:
    actor = await register(client)
    mine = await create(client, actor, name="Mine")
    theirs = await create(client, actor, name="Theirs")
    version = unwrap(
        await client.post(
            f"{API}/projects/{theirs['id']}/versions", headers=actor.headers, json={"label": "x"}
        )
    )
    response = await client.get(
        f"{API}/projects/{mine['id']}/versions/{version['id']}", headers=actor.headers
    )
    assert response.status_code == 404


async def test_activity_feed_records_what_happened(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    project = await create(client, actor)
    await client.patch(
        f"{API}/projects/{project['id']}", headers=actor.headers, json={"name": "Renamed"}
    )
    feed = unwrap(
        await client.get(f"{API}/projects/{project['id']}/activity", headers=actor.headers)
    )
    kinds = {item["type"] for item in feed["items"]}
    assert "PROJECT_CREATED" in kinds
    assert "PROJECT_RENAMED" in kinds
    assert all(item["actor_email"] == actor.email for item in feed["items"])


async def test_comments(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    project = await create(client, actor)
    comment = unwrap(
        await client.post(
            f"{API}/projects/{project['id']}/comments",
            headers=actor.headers,
            json={
                "body": "this screen needs a filter",
                "target_kind": "screen",
                "target_key": "screen_0",
            },
        )
    )
    assert comment["target_key"] == "screen_0"

    listed = unwrap(
        await client.get(f"{API}/projects/{project['id']}/comments", headers=actor.headers)
    )
    assert len(listed) == 1

    unwrap(
        await client.patch(
            f"{API}/projects/{project['id']}/comments/{comment['id']}",
            headers=actor.headers,
            json={"resolved": True},
        )
    )
    open_only = unwrap(
        await client.get(f"{API}/projects/{project['id']}/comments", headers=actor.headers)
    )
    assert open_only == []
    with_resolved = unwrap(
        await client.get(
            f"{API}/projects/{project['id']}/comments?include_resolved=true", headers=actor.headers
        )
    )
    assert len(with_resolved) == 1

    await client.delete(
        f"{API}/projects/{project['id']}/comments/{comment['id']}", headers=actor.headers
    )
    assert (
        unwrap(
            await client.get(
                f"{API}/projects/{project['id']}/comments?include_resolved=true",
                headers=actor.headers,
            )
        )
        == []
    )


async def test_only_the_author_may_reword_a_comment(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    guest = await register(client)
    project = await create(client, owner)
    await client.post(
        f"{API}/projects/{project['id']}/members",
        headers=owner.headers,
        json={"email": guest.email, "role": "EDITOR"},
    )
    comment = unwrap(
        await client.post(
            f"{API}/projects/{project['id']}/comments",
            headers=owner.headers,
            json={"body": "mine"},
        )
    )
    edited = await client.patch(
        f"{API}/projects/{project['id']}/comments/{comment['id']}",
        headers=guest.headers,
        json={"body": "not yours"},
    )
    assert edited.status_code == 409
    # ...but anyone on the project may resolve the thread.
    resolved = await client.patch(
        f"{API}/projects/{project['id']}/comments/{comment['id']}",
        headers=guest.headers,
        json={"resolved": True},
    )
    assert resolved.status_code == 200


async def test_an_absurdly_large_document_is_refused(client: httpx.AsyncClient) -> None:
    """Without a ceiling, one request can push the server into swap. The limit
    is roughly twenty times the largest honest diagram, so nothing real hits it.
    """
    actor = await register(client)
    doc = sample_doc(1)
    doc["requirements"] = "x" * (9 * 1024 * 1024)
    response = await client.post(
        f"{API}/projects", headers=actor.headers, json={"name": "Huge", "doc": doc}
    )
    assert response.status_code == 422
    assert "limit" in response.json()["errors"][0]["message"]


async def test_a_large_but_realistic_document_is_accepted(client: httpx.AsyncClient) -> None:
    """The cap must be far enough above a real diagram that nobody meets it."""
    actor = await register(client)
    response = await client.post(
        f"{API}/projects",
        headers=actor.headers,
        json={"name": "Big but real", "doc": sample_doc(400, 1200)},
    )
    assert response.status_code == 201
    assert unwrap(response)["screen_count"] == 400


async def test_an_unregistered_owner_cannot_be_the_last_one(
    client: httpx.AsyncClient,
) -> None:
    """Otherwise: hand OWNER to a typo'd address, leave, and the project is
    reachable by no account at all — no admin route can undo it."""
    owner = await register(client)
    project = await create(client, owner)
    invited = unwrap(
        await client.post(
            f"{API}/projects/{project['id']}/members",
            headers=owner.headers,
            json={"email": "typo@exmaple.com", "role": "OWNER"},
        )
    )
    assert invited["status"] == "INVITED"

    stranded = await client.post(f"{API}/projects/{project['id']}/leave", headers=owner.headers)
    assert stranded.status_code == 409
    assert unwrap(await client.get(f"{API}/projects", headers=owner.headers))["total"] == 1


async def test_absurd_paging_and_versions_are_refused_not_crashed(
    client: httpx.AsyncClient,
) -> None:
    """All three overflow a database integer, which any authenticated caller
    could otherwise use to turn a list endpoint into a 500 at will."""
    actor = await register(client)
    assert (
        await client.get(f"{API}/projects?page=99999999999999999999", headers=actor.headers)
    ).status_code == 422
    assert (
        await client.post(
            f"{API}/projects",
            headers=actor.headers,
            json={"name": "x", "schema_version": 99999999999999999999},
        )
    ).status_code == 422


async def test_inviting_a_thousand_addresses_at_creation_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """One unthrottled request otherwise sent a thousand invitation emails to
    arbitrary third parties. The bulk-invite route already capped this at 50."""
    actor = await register(client)
    response = await client.post(
        f"{API}/projects",
        headers=actor.headers,
        json={
            "name": "Spam",
            "member_emails": [f"victim{index}@example.com" for index in range(200)],
        },
    )
    assert response.status_code == 422


async def test_a_snapshot_names_who_wrote_it_not_who_replaced_it(
    client: httpx.AsyncClient,
) -> None:
    """Version history answers "who changed this", so it has to name the author.

    The snapshot holds the document *before* a write, and it was stamped with
    the person doing the write — so the history said Bob authored Alice's work.
    """
    alice = await register(client)
    project = await create(client, alice)
    bob = await register(client)
    await client.post(
        f"{API}/projects/{project['id']}/members",
        headers=alice.headers,
        json={"email": bob.email, "role": "EDITOR"},
    )

    detail = unwrap(await client.get(f"{API}/projects/{project['id']}", headers=alice.headers))
    await client.put(
        f"{API}/projects/{project['id']}/document",
        headers=alice.headers,
        json={"doc": sample_doc(3), "base_version": detail["doc_version"]},
    )
    after_alice = unwrap(await client.get(f"{API}/projects/{project['id']}", headers=alice.headers))
    await client.put(
        f"{API}/projects/{project['id']}/document",
        headers=bob.headers,
        json={"doc": sample_doc(4), "base_version": after_alice["doc_version"]},
    )

    versions = unwrap(
        await client.get(f"{API}/projects/{project['id']}/versions", headers=alice.headers)
    )
    # The newest snapshot holds what Alice wrote, so it is hers.
    newest = versions["items"][0]
    assert newest["created_by"] == alice.id


async def test_the_history_says_who_and_the_feed_collapses_a_run_of_saves(
    client: httpx.AsyncClient,
) -> None:
    """Version history and the activity feed together answer "who changed what".

    Neither did before: snapshots carried a bare UUID the client cannot resolve,
    and the live path recorded no activity at all — so for most projects the
    entire record of a day's work was a counter with no names on it.
    """
    alice = await register(client)
    project = await create(client, alice)
    detail = unwrap(await client.get(f"{API}/projects/{project['id']}", headers=alice.headers))

    version = detail["doc_version"]
    for count in range(3):
        saved = unwrap(
            await client.put(
                f"{API}/projects/{project['id']}/document",
                headers=alice.headers,
                json={"doc": sample_doc(count + 3), "base_version": version},
            )
        )
        version = saved["doc_version"]

    versions = unwrap(
        await client.get(f"{API}/projects/{project['id']}/versions", headers=alice.headers)
    )
    assert versions["items"], "a save with no label still snapshots"
    assert versions["items"][0]["created_by_name"] == alice.user["full_name"]
    assert versions["items"][0]["created_by_email"] == alice.email

    feed = unwrap(
        await client.get(f"{API}/projects/{project['id']}/activity", headers=alice.headers)
    )
    edits = [row for row in feed["items"] if row["type"] == "PROJECT_UPDATED"]
    # One entry for the run, not one per save — and it says how many.
    assert len(edits) == 1
    assert edits[0]["actor_email"] == alice.email
    assert edits[0]["meta"]["edits"] == 3


async def test_two_people_editing_get_an_entry_each(client: httpx.AsyncClient) -> None:
    alice = await register(client)
    project = await create(client, alice)
    bob = await register(client)
    await client.post(
        f"{API}/projects/{project['id']}/members",
        headers=alice.headers,
        json={"email": bob.email, "role": "EDITOR"},
    )

    detail = unwrap(await client.get(f"{API}/projects/{project['id']}", headers=alice.headers))
    version = detail["doc_version"]
    for actor in (alice, bob, alice):
        saved = unwrap(
            await client.put(
                f"{API}/projects/{project['id']}/document",
                headers=actor.headers,
                json={"doc": sample_doc(5), "base_version": version},
            )
        )
        version = saved["doc_version"]

    feed = unwrap(
        await client.get(f"{API}/projects/{project['id']}/activity", headers=alice.headers)
    )
    edits = [row for row in feed["items"] if row["type"] == "PROJECT_UPDATED"]
    # Alice's two saves are one session; Bob's is his own.
    assert sorted(row["actor_email"] for row in edits) == sorted([alice.email, bob.email])
