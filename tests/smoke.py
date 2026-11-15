"""Local smoke tests for the forge: REST, GraphQL, MCP, git (via the git CLI).

Usage: python3 tests/smoke.py [--port N] [--token T]
Starts the forge on an ephemeral port (or the given one), runs the checks,
prints PASS/FAIL per check and exits non-zero on any failure.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forge.server import serve  # noqa: E402
from forge.store import Store  # noqa: E402

TOKEN = "test-token-123"
FAILURES = []
PASSES = [0]


def check(name, condition, detail=""):
    if condition:
        PASSES[0] += 1
        print(f"  PASS {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


def req(method, base, path, body=None, token=TOKEN, headers=None):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if headers:
        hdrs.update(headers)
    request = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), json.loads(raw) if raw else None
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw
        return err.code, dict(err.headers), parsed


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    store = Store(token=TOKEN)
    server = serve(store, port=args.port)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    print(f"forge on {base}")

    try:
        _rest(base)
        _graphql(base)
        _mcp(base)
        _git(base)
    finally:
        server.shutdown()
        server.server_close()

    print(f"\n{PASSES[0]} passed, {len(FAILURES)} failed")
    if FAILURES:
        print("failures:", ", ".join(FAILURES))
        return 1
    return 0


def _rest(base):
    print("== REST ==")
    status, headers, body = req("GET", base, "/healthz", token=None)
    check("healthz", status == 200 and body == {"ok": True}, str(status))
    status, headers, body = req("GET", base, "/api/v3/")
    check("root scopes", status == 200 and headers.get("X-OAuth-Scopes") == "repo, read:org",
          f"{status} {headers.get('X-OAuth-Scopes')}")
    check("request-id", bool(headers.get("X-GitHub-Request-Id")))
    status, _, body = req("GET", base, "/api/v3/meta")
    check("meta", status == 200 and body.get("installed_version"), str(body))
    status, _, body = req("GET", base, "/api/v3/user")
    check("user login", status == 200 and body.get("login") == "adaiversen", str(body))
    status, _, body = req("GET", base, "/api/v3/user", token=None)
    check("no token 401", status == 401, str(status))

    status, _, body = req("POST", base, "/api/v3/user/repos",
                          {"name": "smoke-repo", "private": True, "description": "d"})
    check("create repo 201", status == 201 and body["name"] == "smoke-repo", str(status))
    check("repo id", body.get("id") == 5000, str(body.get("id")))
    status, _, body = req("POST", base, "/api/v3/user/repos", {"name": "smoke-repo"})
    check("duplicate 422", status == 422 and "already exists" in body.get("message", ""),
          f"{status} {body}")
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo")
    check("get repo", status == 200 and body["default_branch"] == "main", str(status))
    status, _, body = req("GET", base, "/api/v3/repos/olgabauer/smoke-repo")
    check("foreign owner 404", status == 404, str(status))
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/nope")
    check("missing repo 404", status == 404, str(status))

    # contents: create, read, update, stale, directory, delete
    content = base64.b64encode(b"hello world\n").decode()
    status, _, body = req("PUT", base, "/api/v3/repos/adaiversen/smoke-repo/contents/docs/readme.md",
                          {"message": "add readme", "content": content})
    check("put contents 201", status == 201 and body["commit"]["sha"], f"{status} {body}")
    commit_sha = body["commit"]["sha"]
    blob_sha = body["content"]["sha"]
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo/contents/docs/readme.md")
    check("get contents", status == 200 and base64.b64decode(body["content"]) == b"hello world\n"
          and body["sha"] == blob_sha, f"{status} {body}")
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo/contents/docs")
    check("dir listing", status == 200 and isinstance(body, list)
          and [e["name"] for e in body] == ["readme.md"], f"{status} {body}")
    status, _, body = req("PUT", base, "/api/v3/repos/adaiversen/smoke-repo/contents/docs/readme.md",
                          {"message": "update", "content": base64.b64encode(b"v2\n").decode(),
                           "sha": blob_sha})
    check("put update 200", status == 200, f"{status} {body}")
    status, _, body = req("PUT", base, "/api/v3/repos/adaiversen/smoke-repo/contents/docs/readme.md",
                          {"message": "stale", "content": content, "sha": blob_sha})
    check("stale sha 409", status == 409, f"{status} {body}")
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo/commits")
    check("commits list", status == 200 and len(body) == 2
          and body[0]["commit"]["message"] == "update", f"{status} {body}")
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo/branches")
    check("branches", status == 200 and [b["name"] for b in body] == ["main"], str(body))

    # branches + commits on a branch
    status, _, body = req("POST", base, "/api/v3/repos/adaiversen/smoke-repo/git/refs",
                          {"ref": "refs/heads/feature", "sha": commit_sha})
    check("create branch", status == 201 and body["object"]["sha"] == commit_sha, f"{status} {body}")
    status, _, body = req("PUT", base, "/api/v3/repos/adaiversen/smoke-repo/contents/other.txt",
                          {"message": "on feature", "branch": "feature",
                           "content": base64.b64encode(b"feat\n").decode()})
    check("commit on branch", status == 201, f"{status} {body}")
    feature_sha = body["commit"]["sha"]
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo/commits?sha=feature")
    check("commits on branch", status == 200
          and any(c["sha"] == feature_sha for c in body), f"{status} {body}")
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo/commits")
    check("commits not on default", status == 200
          and not any(c["sha"] == feature_sha for c in body), f"{status} {body}")
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo/git/refs/heads/feature")
    check("get ref", status == 200 and body["object"]["sha"] == feature_sha, f"{status} {body}")

    # issues
    status, _, body = req("POST", base, "/api/v3/repos/adaiversen/smoke-repo/labels",
                          {"name": "bug", "color": "ededed"})
    check("create label", status == 201 and body["name"] == "bug", f"{status} {body}")
    status, _, body = req("POST", base, "/api/v3/repos/adaiversen/smoke-repo/issues",
                          {"title": "first", "body": "b1", "labels": ["bug"]})
    check("create issue", status == 201 and body["number"] == 1 and body["state"] == "open",
          f"{status} {body}")
    status, _, body = req("POST", base, "/api/v3/repos/adaiversen/smoke-repo/issues",
                          {"title": "bad", "body": "b", "labels": ["nope"]})
    check("bad label 422", status == 422, f"{status} {body}")
    status, _, body = req("POST", base, "/api/v3/repos/adaiversen/smoke-repo/issues",
                          {"title": "second", "body": "b2"})
    check("issue number 2", status == 201 and body["number"] == 2, f"{status} {body}")
    status, _, body = req("PATCH", base, "/api/v3/repos/adaiversen/smoke-repo/issues/2",
                          {"state": "closed"})
    check("close issue", status == 200 and body["state"] == "closed", f"{status} {body}")
    status, _, body = req("GET", base,
                          "/api/v3/repos/adaiversen/smoke-repo/issues?state=open&sort=created&direction=asc&per_page=100")
    check("list open asc", status == 200 and [i["number"] for i in body] == [1],
          f"{status} {body}")
    status, _, body = req("POST", base, "/api/v3/repos/adaiversen/smoke-repo/issues/1/comments",
                          {"body": "hello comment"})
    check("comment create", status == 201 and body["id"], f"{status} {body}")
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo/issues/1/comments")
    check("comments list", status == 200 and len(body) == 1
          and body[0]["body"] == "hello comment", f"{status} {body}")

    # pulls: create, files, merge, conflict
    status, _, body = req("POST", base, "/api/v3/repos/adaiversen/smoke-repo/pulls",
                          {"title": "pr1", "body": "pb", "head": "feature", "base": "main"})
    check("create pull", status == 201 and body["number"] == 1
          and body["head"]["sha"] == feature_sha, f"{status} {body}")
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo/pulls/1/files")
    check("pull files", status == 200 and [f["filename"] for f in body] == ["other.txt"]
          and body[0]["status"] == "added" and body[0]["additions"] >= 1, f"{status} {body}")
    status, _, body = req("PUT", base, "/api/v3/repos/adaiversen/smoke-repo/pulls/1/merge",
                          {"merge_method": "squash"})
    check("merge 200", status == 200 and body["merged"] is True, f"{status} {body}")
    merge_sha = body["sha"]
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo/pulls/1")
    check("merged view", status == 200 and body["merged"] is True
          and body["state"] == "closed" and body["merge_commit_sha"] == merge_sha,
          f"{status} {body}")
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo/contents/other.txt?ref=main")
    check("merge content on default", status == 200
          and base64.b64decode(body["content"]) == b"feat\n", f"{status} {body}")

    # conflict: two branches change the same file differently
    req("POST", base, "/api/v3/repos/adaiversen/smoke-repo/git/refs",
        {"ref": "refs/heads/conf-a", "sha": merge_sha})
    req("POST", base, "/api/v3/repos/adaiversen/smoke-repo/git/refs",
        {"ref": "refs/heads/conf-b", "sha": merge_sha})
    req("PUT", base, "/api/v3/repos/adaiversen/smoke-repo/contents/docs/readme.md",
        {"message": "a", "branch": "conf-a",
         "content": base64.b64encode(b"a\n").decode(),
         "sha": _blob_sha(base, "adaiversen", "smoke-repo", "docs/readme.md", "conf-a")})
    req("PUT", base, "/api/v3/repos/adaiversen/smoke-repo/contents/docs/readme.md",
        {"message": "b", "branch": "conf-b",
         "content": base64.b64encode(b"b\n").decode(),
         "sha": _blob_sha(base, "adaiversen", "smoke-repo", "docs/readme.md", "conf-b")})
    _, _, pr_a = req("POST", base, "/api/v3/repos/adaiversen/smoke-repo/pulls",
                     {"title": "pa", "head": "conf-a", "base": "main"})
    _, _, pr_b = req("POST", base, "/api/v3/repos/adaiversen/smoke-repo/pulls",
                     {"title": "pb", "head": "conf-b", "base": "main"})
    status, _, _ = req("PUT", base,
                       f"/api/v3/repos/adaiversen/smoke-repo/pulls/{pr_a['number']}/merge",
                       {"merge_method": "squash"})
    check("first merge ok", status == 200, str(status))
    status, _, body = req("PUT", base,
                          f"/api/v3/repos/adaiversen/smoke-repo/pulls/{pr_b['number']}/merge",
                          {"merge_method": "squash"})
    check("conflicted merge 405", status == 405, f"{status} {body}")

    # delete contents + repo
    sha = _blob_sha(base, "adaiversen", "smoke-repo", "docs/readme.md", "main")
    status, _, body = req("DELETE", base, "/api/v3/repos/adaiversen/smoke-repo/contents/docs/readme.md",
                          {"message": "rm", "sha": sha})
    check("delete contents", status == 200 and body["commit"]["sha"], f"{status} {body}")
    status, _, body = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo/contents/docs/readme.md")
    check("contents gone 404", status == 404, str(status))
    status, _, body = req("DELETE", base, "/api/v3/repos/adaiversen/smoke-repo")
    check("delete repo 204", status == 204, str(status))
    status, _, _ = req("GET", base, "/api/v3/repos/adaiversen/smoke-repo")
    check("repo gone 404", status == 404, str(status))

    # pagination: 30 repos at per_page=10
    for i in range(30):
        req("POST", base, "/api/v3/user/repos", {"name": f"page-{i:02d}", "private": True})
    seen = []
    nxt = f"{base}/api/v3/user/repos?per_page=10"
    pages = 0
    while nxt and pages < 20:
        status, headers, body = req("GET", base, nxt[len(base):])
        seen.extend(item["name"] for item in body)
        pages += 1
        link = headers.get("Link", "")
        nxt = None
        for part in link.split(","):
            if 'rel="next"' in part:
                nxt = part.split(";")[0].strip().strip("<>")
    check("pagination 30 repos", len(seen) == 30 and len(set(seen)) == 30 and pages == 3,
          f"seen={len(seen)} pages={pages}")


def _blob_sha(base, owner, repo, path, ref):
    status, _, body = req("GET", base, f"/api/v3/repos/{owner}/{repo}/contents/{path}?ref={ref}")
    return body["sha"]


def _graphql(base):
    print("== GraphQL ==")

    def gql(query, variables=None, token=TOKEN):
        return req("POST", base, "/api/graphql",
                   {"query": query, "variables": variables or {}}, token=token)

    status, _, body = gql("query ViewerLogin { viewer { login } }")
    check("viewer login", status == 200 and body["data"]["viewer"]["login"] == "adaiversen",
          str(body))
    status, _, body = gql("query ViewerLogin { viewer { login } }", token=None)
    check("graphql 401", status == 401, str(status))
    status, _, body = gql(
        'query RepositoryFields($owner: String!, $name: String!) {'
        ' repository(owner: $owner, name: $name) { id databaseId name'
        ' owner { login } defaultBranchRef { name } viewerPermission hasIssuesEnabled } }',
        {"owner": "adaiversen", "name": "page-00"})
    repo = body["data"]["repository"]
    check("repository fields", status == 200 and repo["name"] == "page-00"
          and repo["databaseId"] == 5001 and repo["owner"]["login"] == "adaiversen"
          and repo["viewerPermission"] == "ADMIN" and repo["hasIssuesEnabled"] is True,
          str(body))
    repo_id = repo["id"]
    check("global id opaque", repo_id != "5001" and len(repo_id) > 4, repo_id)
    status, _, body = gql(
        'query RepositoryFields($owner: String!, $name: String!) {'
        ' repository(owner: $owner, name: $name) { id } }',
        {"owner": "adaiversen", "name": "missing-repo"})
    check("unknown repo null", status == 200 and body["data"]["repository"] is None
          and "errors" not in body, str(body))
    status, _, body = gql(
        'query Issue_fields { Issue: __type(name: "Issue") {'
        ' fields(includeDeprecated: true) { name } } }')
    names = [f["name"] for f in body["data"]["Issue"]["fields"]]
    check("introspection issue", all(n in names for n in ("id", "number", "title", "state", "body")),
          str(names))
    status, _, body = gql(
        'query SearchType_enumValues { SearchType: __type(name: "SearchType") {'
        ' enumValues(includeDeprecated: true) { name } } }')
    names = [f["name"] for f in body["data"]["SearchType"]["enumValues"]]
    check("introspection searchtype", "ISSUE" in names, str(names))
    status, _, body = gql("query ViewerInvalidField { viewer { login noSuchFieldOnViewer } }")
    check("invalid field 200+errors", status == 200 and body.get("errors")
          and "noSuchFieldOnViewer" in body["errors"][0]["message"], str(body))

    # issues over REST then GraphQL
    req("POST", base, "/api/v3/repos/adaiversen/page-00/issues",
        {"title": "gql one", "body": "b"})
    req("POST", base, "/api/v3/repos/adaiversen/page-00/issues",
        {"title": "gql two", "body": "b"})
    req("POST", base, "/api/v3/repos/adaiversen/page-00/issues",
        {"title": "gql three", "body": "b"})
    status, _, body = gql(
        'query OpenIssues($owner: String!, $name: String!, $first: Int!) {'
        ' repository(owner: $owner, name: $name) {'
        ' issues(first: $first, states: [OPEN], orderBy: {field: CREATED_AT, direction: DESC}) {'
        ' nodes { number title state id } } } }',
        {"owner": "adaiversen", "name": "page-00", "first": 10})
    nodes = body["data"]["repository"]["issues"]["nodes"]
    check("open issues desc", status == 200 and [n["number"] for n in nodes] == [3, 2, 1],
          str(body))
    status, _, body = gql(
        'mutation IssueCreate($input: CreateIssueInput!) {'
        ' createIssue(input: $input) { issue { number id } } }',
        {"input": {"repositoryId": repo_id, "title": "created over gql", "body": "b"}})
    num = body["data"]["createIssue"]["issue"]["number"]
    check("createIssue", status == 200 and num == 4, str(body))
    issue_id = body["data"]["createIssue"]["issue"]["id"]
    status, _, body = req("GET", base, f"/api/v3/repos/adaiversen/page-00/issues/{num}")
    check("createIssue REST view", status == 200 and body["title"] == "created over gql",
          str(body))
    status, _, body = gql(
        'mutation IssueClose($input: CloseIssueInput!) {'
        ' closeIssue(input: $input) { issue { number id state } } }',
        {"input": {"issueId": issue_id}})
    check("closeIssue", status == 200
          and body["data"]["closeIssue"]["issue"]["state"] == "CLOSED", str(body))
    status, _, body = gql(
        'query NodeRepository($id: ID!) { node(id: $id) { id ... on Repository { name nameWithOwner } } }',
        {"id": repo_id})
    check("node repository", status == 200 and body["data"]["node"]["name"] == "page-00"
          and body["data"]["node"]["nameWithOwner"] == "adaiversen/page-00", str(body))
    # batched
    status, _, body = req("POST", base, "/api/graphql", [
        {"query": "query ViewerLogin { viewer { login } }"},
        {"query": 'query Issue_fields { Issue: __type(name: "Issue") { fields { name } } }'},
    ])
    check("batched", status == 200 and isinstance(body, list) and len(body) == 2
          and body[0]["data"]["viewer"]["login"] == "adaiversen", str(body))
    # pagination cursors: 7 issues in page-01
    for i in range(7):
        req("POST", base, "/api/v3/repos/adaiversen/page-01/issues",
            {"title": f"pg {i}", "body": "b"})
    q = ('query IssuesPage($owner: String!, $name: String!, $first: Int!, $after: String) {'
         ' repository(owner: $owner, name: $name) { issues(first: $first, after: $after) {'
         ' nodes { number } pageInfo { hasNextPage endCursor } } } }')
    status, _, body = gql(q, {"owner": "adaiversen", "name": "page-01", "first": 5, "after": None})
    page1 = body["data"]["repository"]["issues"]
    cursor = page1["pageInfo"]["endCursor"]
    check("page 1", status == 200 and len(page1["nodes"]) == 5
          and page1["pageInfo"]["hasNextPage"] is True and cursor, str(body))
    status, _, body = gql(q, {"owner": "adaiversen", "name": "page-01", "first": 5, "after": cursor})
    page2 = body["data"]["repository"]["issues"]
    check("page 2", status == 200 and len(page2["nodes"]) == 2
          and page2["pageInfo"]["hasNextPage"] is False, str(body))
    check("pages distinct", {n["number"] for n in page1["nodes"]} .isdisjoint(
        {n["number"] for n in page2["nodes"]}))


def _mcp(base):
    print("== MCP ==")

    def rpc(method, params=None, req_id=1, token=TOKEN, raw=None):
        body = raw if raw is not None else {"jsonrpc": "2.0", "id": req_id,
                                            "method": method, "params": params or {}}
        url = base + "/mcp"
        data = body if isinstance(body, (str, bytes)) else json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json"}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                raw_body = resp.read()
                return resp.status, json.loads(raw_body) if raw_body else None
        except urllib.error.HTTPError as err:
            raw_body = err.read()
            try:
                return err.code, json.loads(raw_body) if raw_body else None
            except ValueError:
                return err.code, raw_body

    status, body = rpc("server/discover", {})
    check("discover", status == 200 and "2026-07-28" in body["result"]["supportedVersions"]
          and body["result"]["serverInfo"]["name"] == "forge"
          and body["result"]["capabilities"].get("tools") is not None, str(body))
    status, body = rpc("tools/list", {})
    names = [t["name"] for t in body["result"]["tools"]]
    check("tools list", status == 200 and len(names) >= 6
          and all(n in names for n in ("create_repo", "get_repo", "create_issue",
                                       "list_issues", "create_pull_request",
                                       "merge_pull_request")), str(names))
    status, body2 = rpc("tools/list", {}, req_id=2)
    check("tools list idempotent", json.dumps(body["result"]["tools"])
          == json.dumps(body2["result"]["tools"]))
    status, body = rpc("tools/call", {"name": "create_repo",
                                      "arguments": {"name": "mcp-repo", "private": True}}, req_id=3)
    check("mcp create_repo", status == 200 and not body["result"].get("isError")
          and body["result"]["structuredContent"]["name"] == "mcp-repo", str(body))
    status, body = rpc("tools/call", {"name": "get_repo",
                                      "arguments": {"owner": "adaiversen", "repo": "mcp-repo"}},
                      req_id=4)
    check("mcp get_repo", status == 200
          and "mcp-repo" in body["result"]["content"][0]["text"], str(body))
    status, body = rpc("tools/call", {"name": "create_issue",
                                      "arguments": {"owner": "adaiversen", "repo": "mcp-repo",
                                                  "title": "mcp issue", "body": "b"}}, req_id=5)
    check("mcp create_issue", status == 200
          and body["result"]["structuredContent"]["number"] == 1, str(body))
    status, _, rest_body = req("GET", base, "/api/v3/repos/adaiversen/mcp-repo/issues?state=all")
    check("mcp issue REST view", status == 200 and len(rest_body) == 1
          and rest_body[0]["title"] == "mcp issue", str(rest_body))
    status, body = rpc("tools/call", {"name": "list_issues",
                                      "arguments": {"owner": "adaiversen", "repo": "mcp-repo",
                                                  "state": "open"}}, req_id=6)
    check("mcp list_issues", status == 200
          and "mcp issue" in body["result"]["content"][0]["text"], str(body))
    status, body = rpc("tools/call", {"name": "no_such_tool", "arguments": {}}, req_id=7)
    check("unknown tool -32602", status == 200 and body["error"]["code"] == -32602, str(body))
    status, body = rpc("tools/call", {"name": "create_issue", "arguments": {}}, req_id=8)
    check("missing args -32602", status == 200 and body["error"]["code"] == -32602, str(body))
    status, body = rpc("tools/list", {}, req_id=9,
                       raw=b'{"jsonrpc": "2.0", "id": 424242, "method": "tools/list"')
    check("parse error 400/-32700", status == 400 and body["error"]["code"] == -32700,
          f"{status} {body}")
    status, body = rpc("tools/call", {"name": "create_issue",
                                      "arguments": {"owner": "x", "repo": "y", "title": "t"}},
                      req_id=10, token=None)
    check("mcp auth 401", status == 401, str(status))
    status, body = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}}, req_id=11)
    check("legacy initialize", status == 200
          and body["result"]["serverInfo"]["name"] == "forge", str(body))


def _git(base):
    print("== git (real git CLI) ==")
    tmp = tempfile.mkdtemp(prefix="forge-smoke-")
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", HOME=tmp,
               GIT_AUTHOR_NAME="ada iversen", GIT_AUTHOR_EMAIL="adaiversen@example.invalid",
               GIT_COMMITTER_NAME="ada iversen",
               GIT_COMMITTER_EMAIL="adaiversen@example.invalid")
    port = base.split(":")[-1]

    def git(*args, cwd=None, expect=0):
        proc = subprocess.run(["git", *args], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=30)
        return proc

    # create the repo over REST first (git cannot invent repos)
    req("POST", base, "/api/v3/user/repos", {"name": "git-smoke", "private": True})
    url = f"http://127.0.0.1:{port}/adaiversen/git-smoke.git"
    proc = git("clone", url, "w1", cwd=tmp)
    check("clone empty", proc.returncode == 0, proc.stderr[-300:])
    w1 = os.path.join(tmp, "w1")
    with open(os.path.join(w1, "hello.txt"), "w") as fh:
        fh.write("hello\n")
    git("add", ".", cwd=w1)
    git("commit", "-m", "first", cwd=w1)
    proc = git("push", "origin", "main", cwd=w1)
    check("push first", proc.returncode == 0, proc.stderr[-300:])
    proc = git("clone", url, "w2", cwd=tmp)
    check("clone with content", proc.returncode == 0, proc.stderr[-300:])
    with open(os.path.join(tmp, "w2", "hello.txt")) as fh:
        check("cloned bytes", fh.read() == "hello\n")
    # second commit + incremental fetch
    with open(os.path.join(w1, "hello.txt"), "a") as fh:
        fh.write("world\n")
    git("add", ".", cwd=w1)
    git("commit", "-m", "second", cwd=w1)
    proc = git("push", "origin", "main", cwd=w1)
    check("push second", proc.returncode == 0, proc.stderr[-300:])
    proc = git("fetch", "origin", cwd=os.path.join(tmp, "w2"))
    check("fetch incremental", proc.returncode == 0, proc.stderr[-300:])
    proc = git("merge", "--ff-only", "origin/main", cwd=os.path.join(tmp, "w2"))
    check("ff merge", proc.returncode == 0, proc.stderr[-300:])
    with open(os.path.join(tmp, "w2", "hello.txt")) as fh:
        check("fetched bytes", fh.read() == "hello\nworld\n")
    # shallow clone
    proc = git("clone", "--depth", "1", url, "w3", cwd=tmp)
    check("shallow clone", proc.returncode == 0, proc.stderr[-300:])
    proc = git("rev-list", "--count", "HEAD", cwd=os.path.join(tmp, "w3"))
    check("shallow depth 1", proc.returncode == 0 and proc.stdout.strip() == "1",
          proc.stdout + proc.stderr[-200:])
    # non-fast-forward push must be refused
    proc = git("reset", "--hard", "HEAD~1", cwd=os.path.join(tmp, "w2"))
    with open(os.path.join(tmp, "w2", "hello.txt"), "w") as fh:
        fh.write("diverged\n")
    git("add", ".", cwd=os.path.join(tmp, "w2"))
    git("commit", "-m", "diverged", cwd=os.path.join(tmp, "w2"))
    proc = git("push", "origin", "main", cwd=os.path.join(tmp, "w2"))
    check("non-ff refused", proc.returncode != 0 and "non-fast-forward" in proc.stderr,
          proc.stderr[-300:])
    # tag push + ls-refs
    git("tag", "v1", cwd=w1)
    proc = git("push", "origin", "v1", cwd=w1)
    check("push tag", proc.returncode == 0, proc.stderr[-300:])
    proc = git("ls-remote", url)
    check("ls-remote", proc.returncode == 0 and "refs/heads/main" in proc.stdout
          and "refs/tags/v1" in proc.stdout, proc.stdout + proc.stderr[-200:])
    # malformed request does not kill the server
    proc = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                           "-X", "POST", "--data-binary", "zzzznot-a-pkt-line",
                           f"{url}/git-upload-pack"], capture_output=True, text=True, timeout=10)
    status, _, _ = req("GET", base, "/healthz", token=None)
    check("server survives malformed", status == 200, proc.stdout)


if __name__ == "__main__":
    sys.exit(main())
