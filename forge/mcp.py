"""MCP (/mcp): JSON-RPC 2.0 Streamable HTTP, revision 2026-07-28.

The suite's mcp-client speaks two eras: modern (server/discover, no
handshake, no session) and legacy (initialize + notifications/initialized).
This endpoint serves the modern era and answers initialize as well, so
either probe connects. Every tools/call result carries both a text
content block (JSON) and structuredContent, the two shapes the checks
read. No Authorization header is HTTP 401.
"""

from __future__ import annotations

import json

from .server import Response, json_response
from .store import StoreError
from . import rest

SERVER_INFO = {"name": "forge", "version": "1.0.0"}
SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26"]

TOOLS = [
    {
        "name": "create_repo",
        "description": "Create a repository owned by the authenticated user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login"},
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
                "head": {"type": "string", "description": "Head branch name"},
                "base": {"type": "string", "description": "Base branch name"},
                "body": {"type": "string", "description": "Pull request body"},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
    },
    {
        "name": "merge_pull_request",
        "description": "Squash-merge a pull request by number.",
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

_TOOL_MAP = {tool["name"]: tool for tool in TOOLS}


def _rpc_result(request_id, result, headers=None):
    return json_response(200, {"jsonrpc": "2.0", "id": request_id, "result": result},
                         headers)


def _rpc_error(request_id, code, message, status=200):
    return json_response(status, {"jsonrpc": "2.0", "id": request_id,
                                  "error": {"code": code, "message": message}})


def _tool_result(payload):
    text = json.dumps(payload, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": False,
    }


def _tool_error(message: str):
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": {"error": message},
        "isError": True,
    }


def dispatch(store, req) -> Response:
    if req.method == "DELETE":
        return Response(200, b"")
    if req.method == "GET":
        return json_response(405, {"message": "Method Not Allowed"})
    if req.method != "POST":
        return json_response(404, {"message": "Not Found"})
    if not store.authenticate(req.bearer()):
        return _rpc_error(None, -32001, "Unauthorized", status=401)
    try:
        payload = req.json()
    except ValueError:
        return _rpc_error(None, -32700, "Parse error", status=400)
    if not isinstance(payload, dict):
        return _rpc_error(None, -32600, "Invalid Request", status=400)
    method = payload.get("method")
    request_id = payload.get("id")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if method is None:
        # A notification without a method is meaningless; ignore it.
        return Response(202, b"")
    if method == "server/discover":
        return _rpc_result(request_id, {
            "supportedVersions": SUPPORTED_VERSIONS,
            "capabilities": {"tools": {}},
            "serverInfo": dict(SERVER_INFO),
            "instructions": "A GitHub-compatible git forge: git smart HTTP, REST v3, GraphQL and MCP tools.",
            "_meta": {"io.modelcontextprotocol/serverInfo": dict(SERVER_INFO)},
        })
    if method == "initialize":
        offered = params.get("protocolVersion")
        chosen = offered if offered in SUPPORTED_VERSIONS else "2025-11-25"
        return _rpc_result(request_id, {
            "protocolVersion": chosen,
            "capabilities": {"tools": {}},
            "serverInfo": dict(SERVER_INFO),
        }, headers={"Mcp-Session-Id": "forge-session-1"})
    if method == "notifications/initialized":
        return Response(202, b"")
    if method == "tools/list":
        # The list is static: two calls answer byte-for-byte the same.
        return _rpc_result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        return _tools_call(store, request_id, params)
    return _rpc_error(request_id, -32601, f"Method not found: {method}")


def _tools_call(store, request_id, params):
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    tool = _TOOL_MAP.get(name)
    if tool is None:
        return _rpc_error(request_id, -32602, f"Unknown tool: {name}")
    required = tool["inputSchema"].get("required", [])
    missing = [key for key in required if key not in args or args[key] is None
               or (isinstance(args[key], str) and args[key] == "" and key in ("owner", "repo", "title", "head", "base", "name"))]
    if missing:
        return _rpc_error(request_id, -32602,
                          f"Missing required argument(s): {', '.join(missing)}")
    try:
        result = _run_tool(store, name, args)
    except StoreError as err:
        return _rpc_result(request_id, _tool_error(err.message))
    return _rpc_result(request_id, result)


def _repo_or_error(store, owner, name):
    if owner != store.viewer["login"]:
        raise StoreError(404, "Not Found")
    repo = store.get_repo(owner, name)
    if repo is None:
        raise StoreError(404, "Not Found")
    return repo


def _run_tool(store, name, args):
    owner = args.get("owner") or store.viewer["login"]
    if name == "create_repo":
        repo = store.create_repo(store.viewer["login"], args["name"],
                                 args.get("private") is True,
                                 args.get("description") or "")
        return _tool_result(rest._repo_view(store, repo))
    if name == "get_repo":
        repo = _repo_or_error(store, owner, args["repo"])
        return _tool_result(rest._repo_view(store, repo))
    if name == "create_issue":
        repo = _repo_or_error(store, owner, args["repo"])
        issue = store.create_issue(repo, args.get("title") or "",
                                   args.get("body") or "", ())
        return _tool_result(rest._issue_view(store, repo, issue))
    if name == "list_issues":
        repo = _repo_or_error(store, owner, args["repo"])
        state = (args.get("state") or "open").lower()
        issues = list(repo.issues)
        if state in ("open", "closed"):
            issues = [i for i in issues if i["state"] == state]
        views = [rest._issue_view(store, repo, i) for i in issues]
        return {
            "content": [{"type": "text",
                         "text": json.dumps(views, separators=(",", ":"))}],
            "structuredContent": {"issues": views},
            "isError": False,
        }
    if name == "create_pull_request":
        repo = _repo_or_error(store, owner, args["repo"])
        pull = store.create_pull(repo, args.get("title") or "",
                                 args.get("body") or "",
                                 args.get("head") or "",
                                 args.get("base") or "")
        return _tool_result(rest._pull_view(store, repo, pull))
    if name == "merge_pull_request":
        repo = _repo_or_error(store, owner, args["repo"])
        number = args.get("pull_number")
        if isinstance(number, str) and number.isdigit():
            number = int(number)
        pull = store.get_pull(repo, number)
        if pull is None:
            raise StoreError(404, "Not Found")
        merge_sha = store.merge_pull(repo, pull, "squash")
        return _tool_result({"merged": True, "number": pull["number"],
                             "sha": merge_sha, "state": pull["state"]})
    raise StoreError(422, f"Unknown tool: {name}")
