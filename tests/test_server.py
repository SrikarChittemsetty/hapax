"""The MCP server: protocol surface, and durability across a server restart.

Most of these drive `HapaxServer.handle()` with plain dicts, which is the whole
reason transport is separated from dispatch. The last two spawn the real server
over real stdio and kill it, because "the task survives the server" is the claim
the project is built on and a dict-level test cannot make it.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time

import pytest

from hapax.memory import InMemoryTaskStore
from hapax.server import TASKS_EXTENSION, HapaxServer, Tool
from hapax.task import TaskState


def rpc(method: str, params: dict | None = None, req_id: int | str = 1) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


# What a task-capable client sends at initialize. Creation is server-directed
# in the finalized extension: this declaration is the one and only opt-in, and
# without it the server MUST answer tools/call inline.
TASKS_CAP = {"capabilities": {"extensions": {TASKS_EXTENSION: {}}}}


def negotiate(server) -> None:
    server.handle(rpc("initialize", TASKS_CAP, req_id="init"))


@pytest.fixture
def calls():
    return []


@pytest.fixture
def server(calls):
    def handler(arguments):
        calls.append(arguments)
        return {"echoed": arguments.get("value")}

    tool = Tool(
        name="echo",
        description="Echo the value back.",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        handler=handler,
    )
    # Inline so a test can assert on the outcome without racing a thread.
    return HapaxServer(InMemoryTaskStore(), tools=[tool], run_in_background=False)


# --- protocol surface ---------------------------------------------------------


def test_initialize_advertises_the_tasks_extension(server):
    result = server.handle(rpc("initialize"))["result"]
    assert result["protocolVersion"] == "2026-07-28"
    assert result["serverInfo"]["name"] == "hapax"
    # The finalized design keys everything on the extension id in the
    # capabilities object — no experimental namespace, no method list.
    assert TASKS_EXTENSION in result["capabilities"]["extensions"]


def test_tools_list(server):
    tools = server.handle(rpc("tools/list"))["result"]["tools"]
    assert [t["name"] for t in tools] == ["echo"]
    assert tools[0]["inputSchema"]["type"] == "object"


def test_a_client_that_never_negotiated_gets_its_result_inline(server, calls):
    # No initialize, or an initialize without the extension: either way the
    # server MUST NOT return a task envelope. Even an explicit legacy `task`
    # parameter cannot opt in — the capability declaration is the only door.
    result = server.handle(
        rpc("tools/call", {"name": "echo", "arguments": {"value": "hi"}, "task": {}})
    )["result"]
    assert calls == [{"value": "hi"}]
    assert json.loads(result["content"][0]["text"]) == {"echoed": "hi"}
    # No task was created for a non-negotiated call.
    assert server.store.list_tasks() == []


def test_a_negotiated_call_returns_a_task_envelope(server):
    negotiate(server)
    result = server.handle(
        rpc("tools/call", {"name": "echo", "arguments": {"value": "hi"}})
    )["result"]
    assert result["resultType"] == "task"
    assert result["taskId"].startswith("task_")
    assert result["createdAt"] and result["lastUpdatedAt"]


def test_the_result_comes_back_from_tasks_get(server):
    negotiate(server)
    created = server.handle(
        rpc("tools/call", {"name": "echo", "arguments": {"value": "hi"}})
    )["result"]
    got = server.handle(rpc("tasks/get", {"taskId": created["taskId"]}))["result"]
    assert got["status"] == "completed"
    assert got["resultType"] == "complete"
    assert json.loads(got["result"]["content"][0]["text"]) == {"echoed": "hi"}


def test_a_retried_call_with_the_same_key_does_not_run_the_tool_twice(server, calls):
    negotiate(server)
    request = rpc(
        "tools/call",
        {"name": "echo", "arguments": {"value": "hi"}, "task": {"idempotencyKey": "k1"}},
    )
    first = server.handle(request)["result"]
    second = server.handle(request)["result"]

    assert first["taskId"] == second["taskId"]
    # This is the guarantee, expressed at the wire level: one call, one effect.
    assert len(calls) == 1


def test_a_failing_tool_fails_its_task_not_the_server():
    def explode(_arguments):
        raise RuntimeError("tool blew up")

    tool = Tool("boom", "Always fails.", {"type": "object"}, explode)
    server = HapaxServer(InMemoryTaskStore(), tools=[tool], run_in_background=False)
    negotiate(server)

    created = server.handle(rpc("tools/call", {"name": "boom"}))["result"]
    got = server.handle(rpc("tasks/get", {"taskId": created["taskId"]}))["result"]
    assert got["status"] == "failed"
    assert "tool blew up" in got["error"]["message"]


def test_tasks_list_is_gone(server):
    # Removed in the finalized extension: a server cannot scope a listing to a
    # caller, so serving one is an information leak. It must be method-not-
    # found, exactly as if it had never existed.
    assert server.handle(rpc("tasks/list"))["error"]["code"] == -32601


def test_cancelling_a_finished_task_is_an_ack_not_an_error(server):
    negotiate(server)
    created = server.handle(
        rpc("tools/call", {"name": "echo", "arguments": {"value": "x"}})
    )["result"]

    # The work finished (inline) before the cancel arrived — the spec's
    # cooperative model says that is a legitimate outcome, not a bad request.
    # The ack is empty; the truth is in tasks/get.
    assert server.handle(rpc("tasks/cancel", {"taskId": created["taskId"]}))["result"] == {}
    got = server.handle(rpc("tasks/get", {"taskId": created["taskId"]}))["result"]
    assert got["status"] == "completed"


def test_tasks_update_acks_known_and_rejects_unknown(server):
    negotiate(server)
    created = server.handle(
        rpc("tools/call", {"name": "echo", "arguments": {"value": "x"}})
    )["result"]

    # Hapax never surfaces inputRequests, and the spec directs servers to
    # ignore responses to keys that are not outstanding — so a well-formed
    # update on a real task is an empty ack.
    ok = server.handle(rpc(
        "tasks/update",
        {"taskId": created["taskId"], "inputResponses": {"never-issued": {}}},
    ))
    assert ok["result"] == {}

    err = server.handle(rpc("tasks/update", {"taskId": "task_missing", "inputResponses": {}}))
    assert err["error"]["code"] == -32001


def test_cancelling_a_pending_task_works():
    server = HapaxServer(InMemoryTaskStore(), tools=[], run_in_background=False)
    task = server.store.create_task({"tool": "slow"})

    # SEP-2663 makes cancel an empty ack, not a task shape — the caller reads
    # the new state back with tasks/get.
    assert server.handle(rpc("tasks/cancel", {"taskId": task.id}))["result"] == {}
    got = server.handle(rpc("tasks/get", {"taskId": task.id}))["result"]
    assert got["status"] == "cancelled"


# --- error handling -----------------------------------------------------------


def test_unknown_method_and_unknown_tool_and_unknown_task(server):
    assert server.handle(rpc("nope/nope"))["error"]["code"] == -32601
    assert server.handle(rpc("tools/call", {"name": "ghost"}))["error"]["code"] == -32602
    assert server.handle(rpc("tasks/get", {"taskId": "task_missing"}))["error"]["code"] == -32001


def test_a_notification_gets_no_reply(server):
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_a_malformed_request_is_rejected_not_raised(server):
    assert server.handle({"id": 1, "method": "tools/list"})["error"]["code"] == -32600


# --- the part a dict cannot prove --------------------------------------------


def _send(proc, payload):
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def _spawn(conninfo):
    return subprocess.Popen(
        [sys.executable, "-m", "hapax.server", "--conninfo", conninfo],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_stdio_transport_round_trip(pg_store, pg_conninfo):
    proc = _spawn(pg_conninfo)
    try:
        init = _send(proc, rpc("initialize", req_id="a"))
        assert init["id"] == "a"
        assert init["result"]["serverInfo"]["name"] == "hapax"

        tools = _send(proc, rpc("tools/list", req_id="b"))["result"]["tools"]
        assert [t["name"] for t in tools] == ["charge"]
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_task_outlives_the_server_that_created_it(pg_store, pg_conninfo, ledger):
    """Start a task, kill the server, ask a *different* server for the result.

    This is the whole project at the protocol level. An in-memory task store
    passes every other test in this file and fails this one, because the task
    dies with the process that created it.
    """
    proc = _spawn(pg_conninfo)
    try:
        _send(proc, rpc("initialize", TASKS_CAP, req_id="a"))
        created = _send(proc, rpc(
            "tools/call",
            {"name": "charge",
             "arguments": {"customer": "restart_demo", "amount": 50},
             "task": {"idempotencyKey": "restart-1"}},
            req_id="b",
        ))["result"]
        task_id = created["taskId"]

        # Let the background work finish, then confirm this server can see it.
        deadline = time.time() + 20
        while time.time() < deadline:
            got = _send(proc, rpc("tasks/get", {"taskId": task_id}, req_id="c"))["result"]
            if got["status"] == "completed":
                break
            time.sleep(0.05)
        assert got["status"] == "completed"
    finally:
        # Not a graceful shutdown — the server is destroyed.
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)

    survivor = _spawn(pg_conninfo)
    try:
        _send(survivor, rpc("initialize", TASKS_CAP, req_id="a"))
        got = _send(survivor, rpc("tasks/get", {"taskId": task_id}, req_id="d"))["result"]
        assert got["status"] == "completed"
        assert json.loads(got["result"]["content"][0]["text"])["charged"] == 50
    finally:
        survivor.kill()
        survivor.wait(timeout=10)


def test_a_running_task_is_not_claimable_until_the_server_stops_renewing(pg_store, pg_conninfo):
    """A live server's work must not be handed to a dispatcher underneath it.

    The exactly-once guarantee would survive that — terminal states and the
    single-transaction commit see to it — but a second worker duplicating
    in-progress work is still waste, and the row would be saying "nobody is
    doing this" while somebody is.
    """
    from hapax.server import HapaxServer, Tool

    tool = Tool("slow", "Takes a moment.", {"type": "object"}, lambda a: {"ok": True})
    server = HapaxServer(pg_store, tools=[tool], run_in_background=False, lease_seconds=60)
    negotiate(server)

    created = server.handle(rpc("tools/call", {"name": "slow"}))["result"]

    # It ran inline and is terminal, so it is unclaimable for the strongest
    # reason; the lease is what covers the window before that.
    assert pg_store.get_task(created["taskId"]).state is TaskState.COMPLETED
    assert pg_store.claim_task(lease_seconds=1) is None

    # And a task mid-flight (still working, lease held) is equally off-limits.
    task = pg_store.create_task({"tool": "slow"}, idempotency_key="held")
    pg_store.heartbeat(task.id, lease_seconds=60)
    assert pg_store.claim_task(lease_seconds=1) is None
