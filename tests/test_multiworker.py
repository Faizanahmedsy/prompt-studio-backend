"""Collaboration across uvicorn workers.

With one process the in-memory hub is already correct, so every bug in the
Redis fan-out and in the shared presence hash is invisible. These tests run
against four workers behind one port, which is what a real deployment looks
like — two people on the same diagram routinely land on different processes.
"""

import asyncio
import json
from typing import Any

import httpx
import pytest
import websockets

from tests.conftest import API, register, sample_doc, unwrap

PEERS = 6  # over four workers, at least two processes are certainly involved


async def wait_for(socket: Any, kind: str, timeout: float = 10.0) -> dict[str, Any]:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"never received {kind!r}")
        frame: dict[str, Any] = json.loads(await asyncio.wait_for(socket.recv(), remaining))
        if frame.get("type") == kind:
            return frame


@pytest.fixture
async def crowded_project(client: httpx.AsyncClient, live_cluster: str) -> Any:
    """A project with six members, each holding an open socket on the cluster."""
    owner = await register(client, full_name="Owner")
    project = unwrap(
        await client.post(
            f"{API}/projects", headers=owner.headers, json={"name": "Crowd", "doc": sample_doc(0)}
        )
    )
    tokens = []
    for index in range(PEERS):
        peer = await register(client, full_name=f"Peer {index}")
        await client.post(
            f"{API}/projects/{project['id']}/members",
            headers=owner.headers,
            json={"email": peer.email, "role": "EDITOR"},
        )
        tokens.append(peer.access_token)

    sockets = []
    for token in tokens:
        socket = await websockets.connect(
            f"ws://{live_cluster}{API}/ws/projects/{project['id']}?token={token}"
        )
        await wait_for(socket, "hello")
        sockets.append(socket)
    # Let the joins settle into Redis before anything asserts on the roster.
    await asyncio.sleep(1.0)

    try:
        yield owner, project, sockets
    finally:
        for socket in sockets:
            await socket.close()


async def test_an_edit_crosses_every_worker(crowded_project: Any) -> None:
    owner, project, sockets = crowded_project
    doc = {"screens": [{"id": "s1", "key": "crossed", "title": "Crossed workers"}]}
    await sockets[0].send(json.dumps({"type": "doc.update", "doc": doc}))

    delivered = await asyncio.gather(
        *[wait_for(socket, "doc.updated") for socket in sockets[1:]], return_exceptions=True
    )
    landed = [
        frame
        for frame in delivered
        if isinstance(frame, dict) and frame["doc"]["screens"][0]["key"] == "crossed"
    ]
    assert len(landed) == PEERS - 1, (
        f"only {len(landed)}/{PEERS - 1} sockets saw the edit — the pub/sub fan-out is dropping it"
    )

    acknowledged = await wait_for(sockets[0], "doc.ack")
    assert acknowledged["ack"] is True


async def test_presence_agrees_whichever_worker_answers(
    client: httpx.AsyncClient, live_cluster: str, crowded_project: Any
) -> None:
    owner, project, sockets = crowded_project

    # Asked repeatedly: the port is shared, so consecutive requests land on
    # different workers. Any process answering with only its own rooms shows up
    # here as a different count.
    async with httpx.AsyncClient(base_url=f"http://{live_cluster}", timeout=30) as remote:
        counts = set()
        for _ in range(PEERS):
            reply = await remote.get(
                f"{API}/projects/{project['id']}/presence", headers=owner.headers
            )
            counts.add(len(unwrap(reply)))
    assert counts == {PEERS}, f"workers disagree about who is in the room: {sorted(counts)}"

    # And a process holding no sockets at all still knows.
    from_outside = unwrap(
        await client.get(f"{API}/projects/{project['id']}/presence", headers=owner.headers)
    )
    assert len(from_outside) == PEERS


async def test_leaving_clears_the_shared_roster(
    client: httpx.AsyncClient, crowded_project: Any
) -> None:
    owner, project, sockets = crowded_project

    for socket in sockets[:3]:
        await socket.close()
    await asyncio.sleep(1.5)

    remaining = unwrap(
        await client.get(f"{API}/projects/{project['id']}/presence", headers=owner.headers)
    )
    assert len(remaining) == PEERS - 3, "closed sockets are still counted as present"
