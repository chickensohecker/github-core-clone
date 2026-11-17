"""Model Context Protocol endpoint at /mcp: JSON-RPC 2.0 over Streamable
HTTP, modern 2026-07-28 revision (server/discover, no handshake, no
session). Plain JSON answers; every request carries the same six tools.
"""

import json

from . import serial
from .store import ForgeError

JSON = "application/json"
SERVER_NAME = "github-core-clone"
SERVER_VERSION = "1.0.0"

SUPPORTED_VERSIONS = ["2026-07-28"]
KNOWN_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26",
                  "2024-11-05"]

TOOLS = [
    {
        "name": "create_repo",
        "description": "Create a repository owned by the authenticated user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Repository name"},
                "description": {"type": "string", "description": "Repository description"},
                "private": {"type": "boolean", "description": "Whether the repository is private"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_repo",
        "description": "Get a repository by owner and name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login"},
                "repo": {"type": "string", "description": "Repository name"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_issue",
        "description": "Create an issue in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "title": {"type": "string", "description": "Issue title"},
                "body": {"type": "string", "description": "Issue body"},
            },
            "required": ["owner", "repo", "title"],
        },
    },
    {
        "name": "list_issues",
        "description": "List issues in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "state": {"type": "string", "description": "Issue state: open, closed or all"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Open a pull request from a head branch into a base branch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "title": {"type": "string", "description": "Pull request title"},
                "body": {"type": "string", "description": "Pull request body"},
                "head": {"type": "string", "description": "Head branch name"},
                "base": {"type": "string", "description": "Base branch name"},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
    },
    {
        "name": "merge_pull_request",
        "description": "Merge a pull request with a squash merge.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "pull_number": {"type": "integer", "description": "Pull request number"},
            },
            "required": ["owner", "repo", "pull_number"],
        },
    },
]


def _result_text(value):
    return serial.dumps(value).decode("utf-8")


def _tool_ok(value):
    return {"content": [{"type": "text", "text": _result_text(value)}],
            "structuredContent": value}


def _tool_error(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _require(args, *names):
    for name in names:
        if args.get(name) is None:
            return "missing required argument: %s" % name
    return None


def _repo_or_error(store, args):
    repo = store.repo_for(args.get("owner"), args.get("repo"))
    if repo is None:
        return None, _tool_error("repository not found")
    return repo, None


def _tool_create_repo(store, args):
    missing = _require(args, "name")
    if missing:
        return _tool_error(missing)
    try:
        repo = store.create_repo(args["name"], args.get("private", False),
                                 args.get("description", ""))
    except ForgeError as exc:
        return _tool_error(exc.message)
    return _tool_ok(serial.repo_json(store, repo))


def _tool_get_repo(store, args):
    missing = _require(args, "owner", "repo")
    if missing:
        return _tool_error(missing)
    repo, error = _repo_or_error(store, args)
    if error is not None:
        return error
    return _tool_ok(serial.repo_json(store, repo))


def _tool_create_issue(store, args):
    missing = _require(args, "owner", "repo", "title")
    if missing:
        return _tool_error(missing)
    repo, error = _repo_or_error(store, args)
    if error is not None:
        return error
    try:
        issue = store.create_issue(repo, args["title"], args.get("body"))
    except ForgeError as exc:
        return _tool_error(exc.message)
    return _tool_ok(serial.issue_dict(store, repo, issue))


def _tool_list_issues(store, args):
    missing = _require(args, "owner", "repo")
    if missing:
        return _tool_error(missing)
    repo, error = _repo_or_error(store, args)
    if error is not None:
        return error
    state = args.get("state") or "open"
    issues = store.list_issues(repo, state, "asc")
    items = [serial.issue_dict(store, repo, issue) for issue in issues]
    return {"content": [{"type": "text", "text": _result_text(items)}],
            "structuredContent": {"issues": items}}


def _tool_create_pull(store, args):
    missing = _require(args, "owner", "repo", "title", "head", "base")
    if missing:
        return _tool_error(missing)
    repo, error = _repo_or_error(store, args)
    if error is not None:
        return error
    try:
        pull = store.create_pull(repo, args["title"], args["head"],
                                 args["base"], args.get("body"))
    except ForgeError as exc:
        return _tool_error(exc.message)
    return _tool_ok(serial.pull_dict(store, repo, pull))


def _tool_merge_pull(store, args):
    missing = _require(args, "owner", "repo", "pull_number")
    if missing:
        return _tool_error(missing)
    repo, error = _repo_or_error(store, args)
    if error is not None:
        return error
    number = args["pull_number"]
    if isinstance(number, str) and number.isdigit():
        number = int(number)
    pull = store.get_pull(repo, number)
    if pull is None:
        return _tool_error("pull request not found")
    try:
        sha, _message = store.merge_pull(repo, pull, "squash")
    except ForgeError as exc:
        return _tool_error(exc.message)
    return _tool_ok({"merged": True, "number": pull["number"], "sha": sha,
                     "state": "closed",
                     "merge_commit_sha": pull["merge_commit_sha"]})


_TOOL_HANDLERS = {
    "create_repo": _tool_create_repo,
    "get_repo": _tool_get_repo,
    "create_issue": _tool_create_issue,
    "list_issues": _tool_list_issues,
    "create_pull_request": _tool_create_pull,
    "merge_pull_request": _tool_merge_pull,
}


def _server_info():
    return {"name": SERVER_NAME, "version": SERVER_VERSION}


def _dispatch(store, request):
    """One JSON-RPC request dict -> (http_status, response_dict_or_None).
    None means a notification: answer with an empty 202."""
    if not isinstance(request, dict):
        return 200, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "Invalid Request"}}
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    def ok(result):
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": result}

    def err(code, message):
        return 200, {"jsonrpc": "2.0", "id": request_id,
                     "error": {"code": code, "message": message}}

    if not isinstance(method, str):
        return err(-32600, "Invalid Request")
    if method == "server/discover":
        return ok({
            "resultType": "discover",
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {"tools": {}},
            "serverInfo": _server_info(),
            "_meta": {"io.modelcontextprotocol/serverInfo": _server_info()},
        })
    if method == "initialize":
        offered = params.get("protocolVersion")
        if offered not in KNOWN_VERSIONS:
            return err(-32602, "unsupported protocol version: %s" % offered)
        return ok({"protocolVersion": offered,
                   "capabilities": {"tools": {}},
                   "serverInfo": _server_info()})
    if method == "notifications/initialized":
        return 202, None
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return err(-32602, "arguments must be an object")
        if not isinstance(name, str):
            return err(-32602, "a tool name is required")
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            return err(-32602, "unknown tool: %s" % name)
        try:
            result = handler(store, arguments)
        except ForgeError as exc:
            result = _tool_error(exc.message)
        return ok(result)
    return err(-32601, "Method not found: %s" % method)


def handle(store, body):
    """POST /mcp. Returns (status, content_type, body_bytes)."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, JSON, serial.dumps({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32700, "message": "Parse error"}})
    if isinstance(payload, list):
        responses = []
        for item in payload:
            _status, response = _dispatch(store, item)
            if response is not None:
                responses.append(response)
        return 200, JSON, serial.dumps(responses)
    status, response = _dispatch(store, payload)
    if response is None:
        return status, JSON, b""
    return status, JSON, serial.dumps(response)
