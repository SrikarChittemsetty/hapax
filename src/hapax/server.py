"""An MCP server that hands its long-running work to the durable store.

`protocol.py` maps store operations to SEP-2663 result shapes, but nothing served
them, which left the obvious question unanswered: can an agent runtime actually
talk to this? This is the answer — a JSON-RPC 2.0 server over stdio that speaks
`initialize`, `tools/list`, `tools/call`, `tasks/get`, `tasks/update` and
`tasks/cancel` (the surface of the finalized `io.modelcontextprotocol/tasks`
extension; `tasks/list` was removed from the spec and is not served), with every
task living in Postgres rather than in a dictionary that dies with the process.

Task creation is server-directed, as the final design requires: the client
declares the tasks extension in its `initialize` capabilities, and from then on
the *server* decides per-request whether a `tools/call` returns its result
inline or as a task envelope. A client that never declared the capability never
sees an envelope — the spec's MUST NOT — so the old per-request `task` opt-in
parameter is gone along with the `tools/list` warmup it required. The `task`
parameter is still *read* if a client sends one, for exactly one field:
`idempotencyKey`, which Hapax honours as the dedup key for retried calls.

    python -m hapax.server --conninfo "host=127.0.0.1 dbname=hapax"

The interesting part is what happens when a tool call is *task-augmented*. An
ordinary `tools/call` blocks until the tool returns. A task-augmented one returns
a task id immediately and runs the work in the background, so the agent can poll
`tasks/get` — and because the task lives in Postgres, "the agent polls later" and
"the server was restarted in between" are the same case. That is the property the
whole project exists for, and `tests/test_server.py` kills the server between the
call and the poll to prove it.

Transport is separated from dispatch on purpose: `HapaxServer.handle()` takes a
request dict and returns a response dict, so the entire protocol surface is
testable without opening a pipe. `serve_stdio()` is the thin loop around it.

Honest scope: this implements the methods and result shapes, not the whole of
MCP. There is no capability negotiation beyond advertising the extension, no
progress notifications, and no input-required round trip — the same boundaries
protocol.py already states. The official Python SDK does not implement Tasks yet
(python-sdk #2806 / #3005); when it does, the intent is to conform to its
`TaskStore` interface rather than keep a parallel one.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import traceback
from typing import Any, Callable, Iterable, TextIO

from .errors import InvalidTransition, TaskNotFound
from .protocol import TasksProtocol
from .store import TaskStore
from .task import TaskState

PROTOCOL_VERSION = "2026-07-28"
SERVER_NAME = "hapax"
SERVER_VERSION = "0.1.0"

# The extension identifier SEP-2663 assigns; capability negotiation and the
# task-augmentation decision both key on it.
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"

# JSON-RPC 2.0 error codes. -32000..-32099 is the reserved implementation range.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001


class Tool:
    """A tool the server exposes.

    `handler` receives the call arguments and returns a JSON-serialisable result.
    It may take as long as it likes: when invoked as a task, it runs off the
    request path entirely.
    """

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[[dict[str, Any]], Any],
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class HapaxServer:
    """Protocol dispatch. No sockets, no pipes — just dicts in and dicts out."""

    def __init__(
        self,
        store: TaskStore,
        tools: Iterable[Tool] = (),
        *,
        run_in_background: bool = True,
        lease_seconds: float = 300,
    ) -> None:
        self.store = store
        self.protocol = TasksProtocol(store)
        self.tools = {t.name: t for t in tools}
        # Long, because this lease says "a live server is running this tool",
        # and tool calls are allowed to be slow. A dispatcher only steals the
        # task if the whole server has been gone for this long.
        self.lease_seconds = lease_seconds
        # Tests run the work inline so they can assert on the outcome without
        # waiting on a thread; the real server runs it off the request path.
        self.run_in_background = run_in_background
        self._threads: list[threading.Thread] = []
        # Whether the connected client declared the tasks extension at
        # initialize. Until it does, tools/call MUST run inline — returning a
        # task envelope to a client that never negotiated the extension is an
        # invalid response by the spec's own words.
        self._client_supports_tasks = False

    # --- the one public entry point -------------------------------------------

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch one JSON-RPC request. Returns None for notifications."""
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return self._error(req_id, INVALID_REQUEST, "not a JSON-RPC 2.0 request")

        # A request without an id is a notification: act on it, answer nothing.
        is_notification = "id" not in request

        try:
            handler = {
                "initialize": self._initialize,
                "tools/list": self._tools_list,
                "tools/call": self._tools_call,
                "tasks/get": self._tasks_get,
                "tasks/update": self._tasks_update,
                "tasks/cancel": self._tasks_cancel,
                # No tasks/list: removed in the finalized extension — a server
                # cannot define which caller a listing should be scoped to, so
                # serving one is an information leak wearing a feature's name.
            }.get(method)

            if handler is None:
                if is_notification or method.startswith("notifications/"):
                    return None
                return self._error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")

            result = handler(params)
        except TaskNotFound as e:
            return None if is_notification else self._error(req_id, TASK_NOT_FOUND, str(e))
        except InvalidTransition as e:
            return None if is_notification else self._error(req_id, INVALID_PARAMS, str(e))
        except (KeyError, TypeError, ValueError) as e:
            return None if is_notification else self._error(req_id, INVALID_PARAMS, str(e))
        except Exception as e:  # noqa: BLE001 — a server must not die on one bad call
            return None if is_notification else self._error(
                req_id, INTERNAL_ERROR, f"{type(e).__name__}: {e}"
            )

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    # --- methods --------------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        # The single handshake point the redesign consolidated everything into:
        # the client declares the extension here, and that declaration alone
        # decides whether this connection ever sees a task envelope. No
        # per-request opt-in, no tool-level flags, no tools/list warmup.
        client_caps = params.get("capabilities") or {}
        extensions = client_caps.get("extensions") or {}
        self._client_supports_tasks = TASKS_EXTENSION in extensions

        return {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {
                "tools": {"listChanged": False},
                "extensions": {TASKS_EXTENSION: {}},
            },
        }

    def _tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": [t.describe() for t in self.tools.values()]}

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params["name"]
        arguments = params.get("arguments") or {}
        tool = self.tools.get(name)
        if tool is None:
            raise ValueError(f"unknown tool: {name}")

        # Server-directed creation. The server is the sole decider, and this
        # server's policy is simple: a client that negotiated the extension
        # gets the durable path, because surviving a crash between call and
        # result is the entire reason to point an agent at Hapax. A client
        # that did not negotiate it MUST get its result inline — returning an
        # envelope it never agreed to parse is an invalid response.
        if not self._client_supports_tasks:
            return _content(tool.handler(arguments))

        # `task` is no longer how a client requests a task — but if one is
        # present, its idempotencyKey is honoured as the dedup key. That field
        # is Hapax's whole value proposition, and a retried call carrying the
        # same key gets the original task back rather than a second effect.
        task_params = params.get("task") or {}
        ttl_ms = task_params.get("ttlMs")
        envelope = self.protocol.create_augmented(
            name,
            arguments,
            idempotency_key=task_params.get("idempotencyKey"),
            ttl_seconds=int(ttl_ms / 1000) if ttl_ms else None,
        )
        task_id = envelope["taskId"]

        # Only start work for a task that is actually still pending. A retried
        # request returning an already-finished task must not re-run anything.
        if self.store.get_task(task_id).state is TaskState.WORKING:
            self._start(tool, arguments, task_id)
        return envelope

    def _tasks_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.protocol.get(params["taskId"])

    def _tasks_update(self, params: dict[str, Any]) -> dict[str, Any]:
        """`tasks/update`: acknowledge input responses.

        Hapax never sets `input_required` (that loop is out of scope, and says
        so), so there is never an outstanding inputRequest key — and the spec
        directs a server to *ignore* responses to keys that are not
        outstanding, not to error on them. What must still be real is the
        existence check: an unknown taskId is an error, and get_task raising
        TaskNotFound is what produces it.
        """
        self.store.get_task(params["taskId"])
        return {}

    def _tasks_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.protocol.cancel(params["taskId"])

    # --- running the work -----------------------------------------------------

    def _start(self, tool: Tool, arguments: dict[str, Any], task_id: str) -> None:
        # Take a lease before starting. Without one the task looks unclaimed for
        # as long as the tool runs, so a dispatcher sharing this database would
        # be entitled to pick it up and run it alongside us. That would not
        # double the side effect — terminal states and the single-transaction
        # commit see to that — but it would waste a worker doing work already in
        # progress, and "nobody is doing this" would be a false statement about
        # the row. If the server dies, nothing renews the lease and the task
        # becomes claimable on its own, which is exactly the intent.
        self.store.heartbeat(task_id, lease_seconds=self.lease_seconds)

        def run() -> None:
            try:
                result = tool.handler(arguments)
                self.store.update_task(
                    task_id, TaskState.COMPLETED, result=_content(result)
                )
            except Exception as e:  # noqa: BLE001 — a failing tool fails its task, not the server
                try:
                    self.store.update_task(
                        task_id,
                        TaskState.FAILED,
                        error={"message": f"{type(e).__name__}: {e}"},
                    )
                except InvalidTransition:
                    # Cancelled while running. The terminal state already set by
                    # the cancel wins; nothing to do.
                    pass

        if not self.run_in_background:
            run()
            return
        thread = threading.Thread(target=run, daemon=True, name=f"hapax-{task_id[:12]}")
        self._threads.append(thread)
        thread.start()

    def wait_for_idle(self, timeout: float = 30.0) -> None:
        """Join the work threads. For tests and for a clean shutdown."""
        for thread in list(self._threads):
            thread.join(timeout=timeout)
        self._threads = [t for t in self._threads if t.is_alive()]

    # --- helpers --------------------------------------------------------------

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _content(value: Any) -> dict[str, Any]:
    """Wrap a handler's return value in MCP tool-result content."""
    if isinstance(value, dict) and "content" in value:
        return value
    return {"content": [{"type": "text", "text": json.dumps(value, default=str)}]}


# --- transport ----------------------------------------------------------------


def serve_stdio(server: HapaxServer, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Newline-delimited JSON-RPC over stdio, which is how MCP hosts speak."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            _write(stdout, {"jsonrpc": "2.0", "id": None,
                            "error": {"code": PARSE_ERROR, "message": str(e)}})
            continue

        try:
            response = server.handle(request)
        except Exception:  # noqa: BLE001 — never let one request kill the loop
            traceback.print_exc(file=sys.stderr)
            response = HapaxServer._error(request.get("id"), INTERNAL_ERROR, "server error")

        if response is not None:
            _write(stdout, response)


def _write(stdout: TextIO, payload: dict[str, Any]) -> None:
    stdout.write(json.dumps(payload) + "\n")
    stdout.flush()


# --- the demo tool ------------------------------------------------------------


def charge_tool(conninfo: str) -> Tool:
    """A deliberately slow, side-effecting tool: the one worth doing exactly once."""

    def handler(arguments: dict[str, Any]) -> Any:
        import time

        import psycopg

        from .worker import ensure_ledger, LEDGER_DDL  # noqa: F401

        amount = int(arguments.get("amount", 50))
        customer = str(arguments.get("customer", "cust_1"))
        seconds = float(arguments.get("seconds", 0.0))
        if seconds:
            time.sleep(seconds)

        with psycopg.connect(conninfo) as conn:
            with conn.cursor() as cur:
                cur.execute(LEDGER_DDL)
                cur.execute(
                    "INSERT INTO ledger (task_id, amount) VALUES (%s, %s)"
                    " ON CONFLICT (task_id) DO NOTHING",
                    [f"mcp-{customer}", amount],
                )
            conn.commit()
        return {"charged": amount, "customer": customer}

    return Tool(
        name="charge",
        description="Charge a customer. Slow, and must happen exactly once.",
        input_schema={
            "type": "object",
            "properties": {
                "customer": {"type": "string"},
                "amount": {"type": "integer"},
                "seconds": {"type": "number", "description": "simulated work time"},
            },
            "required": ["customer"],
        },
        handler=handler,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Hapax MCP server (stdio).")
    ap.add_argument("--conninfo", required=True)
    args = ap.parse_args()

    from .postgres import PostgresTaskStore

    store = PostgresTaskStore(args.conninfo)
    serve_stdio(HapaxServer(store, tools=[charge_tool(args.conninfo)]))


if __name__ == "__main__":
    main()
