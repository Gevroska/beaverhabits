"""Exercise the dependency boundary behind GHSA-46f2-xhpw-8vh3."""

import asyncio
from uuid import uuid4

import pytest
from engineio.async_socket import AsyncSocket
from nicegui import Client, app, core, ui
from nicegui.nicegui import _on_connect, _on_disconnect, _on_handshake
from starlette.requests import Request


@pytest.mark.parametrize(
    "query_mode", ["missing", "empty", "other_client", "implicit", "replayed"]
)
async def test_socket_disconnect_releases_client(user, query_mode):
    """Use real Socket.IO rooms, including payload-only and misleading query IDs."""
    request = Request(
        {"type": "http", "path": "/", "headers": [], "query_string": b"", "session": {}}
    )
    client = Client(
        ui.page("/disconnect-regression", reconnect_timeout=0.05), request=request
    )
    tab_id, document_id = str(uuid4()), str(uuid4())
    transport_ids = []
    socket_ids = []

    async def connect():
        transport_id = str(uuid4())
        transport_ids.append(transport_id)
        core.sio.eio.sockets[transport_id] = AsyncSocket(core.sio.eio, transport_id)
        sid = await core.sio.manager.connect(transport_id, "/")
        socket_ids.append(sid)
        payload = {"client_id": client.id, "tab_id": tab_id, "document_id": document_id}
        query = {
            "missing": "",
            "replayed": "",
            "empty": "client_id=",
            "other_client": f"client_id={uuid4()}",
            "implicit": f"implicit_handshake=true&client_id={client.id}&tab_id={tab_id}&document_id={document_id}",
        }[query_mode]
        environ = {
            "QUERY_STRING": query,
            "asgi.scope": {"query_string": query.encode()},
        }
        core.sio.environ[transport_id] = environ
        await _on_connect(sid, environ)
        if query_mode != "implicit":
            accepted = await _on_handshake(sid, payload)
            if query_mode == "other_client":
                assert not accepted
                assert not client.has_socket_connection
                assert client.id not in core.sio.rooms(sid)
                Client.prune_instances(client_age_threshold=0)
                assert client.id not in Client.instances
                return None
            assert accepted
        if query_mode == "replayed":
            # A repeated frame must not register a second connection for this sid.
            await _on_handshake(sid, payload)
        assert client.id in core.sio.rooms(sid)
        assert client.has_socket_connection
        return sid

    try:
        first_sid = await connect()
        if first_sid is None:
            return
        _on_disconnect(first_sid)
        await core.sio.manager.disconnect(first_sid, "/")
        assert not client.has_socket_connection

        # A normal reconnect must cancel pending deletion and preserve tab data.
        app.storage._tabs[tab_id]["sentinel"] = "preserved"
        second_sid = await connect()
        await asyncio.sleep(0.1)
        assert client.id in Client.instances
        assert app.storage._tabs[tab_id]["sentinel"] == "preserved"

        _on_disconnect(second_sid)
        await core.sio.manager.disconnect(second_sid, "/")
        assert not client.has_socket_connection
        async with asyncio.timeout(2):
            while client.id in Client.instances:
                await asyncio.sleep(0.01)
            while any(
                task.get_name() == f"outbox loop {client.id}"
                for task in asyncio.all_tasks()
            ):
                await asyncio.sleep(0.01)
        assert client.outbox._should_stop
    finally:
        if client.id in Client.instances:
            client.delete()
        for sid in socket_ids:
            await core.sio.manager.disconnect(sid, "/")
        for transport_id in transport_ids:
            core.sio.environ.pop(transport_id, None)
            core.sio.eio.sockets.pop(transport_id, None)
