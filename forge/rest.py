"""REST v3 (/api/v3): repos, branches, commits, contents, labels, issues,
pull requests, git refs.

The contract is the one checks-rest.ts states: JSON everywhere, a
non-empty X-GitHub-Request-Id on every reply, 404 JSON for unknown
routes, 400 for a malformed JSON body, 401 without a token, and a
repository under any owner but the token's login is 404 (never 403).
"""

from __future__ import annotations

import base64
import binascii

from .server import Response, json_error, json_response
from .store import StoreError
from . import gitobjs


def dispatch(store, req) -> Response:
    try:
        return _dispatch(store, req)
    except StoreError as err:
        return json_error(err.status, err.message,
                          {"X-GitHub-Request-Id": store.request_id()})
    except ValueError:
        return json_error(400, "malformed JSON body",
                          {"X-GitHub-Request-Id": store.request_id()})


def _dispatch(store, req) -> Response:
    segments = req.segments  # ["api", "v3", ...]
    path = req.path
    method = req.method
    rid = {"X-GitHub-Request-Id": store.request_id()}

    # API root and meta: no auth.
    if method == "GET" and path in ("/api/v3", "/api/v3/"):
        return json_response(200, {
            "current_user_url": "/api/v3/user",
            "rate_limit_url": "/api/v3/rate_limit",
        }, {**rid, "X-OAuth-Scopes": "repo, read:org"})
    if method == "GET" and path == "/api/v3/meta":
        return json_response(200, {"installed_version": "1.0.0"}, rid)

    # Everything else under /api/v3 needs the trial token.
    if not store.authenticate(req.bearer()):
        return json_error(401, "Requires authentication", rid)

    body = _body(req)
    rest = segments[2:]

    if rest == ["user"] and method == "GET":
        return json_response(200, _user_view(store), rid)
    if rest == ["user", "repos"]:
        if method == "POST":
            return _create_repo(store, req, body, rid)
        if method == "GET":
            return _list_user_repos(store, req, rid)
    if len(rest) >= 2 and rest[0] == "repos":
        return _repo_routes(store, req, rest[1:], body, rid)
    return json_error(404, "Not Found", rid)


def _body(req):
    if req.method in ("POST", "PUT", "PATCH") or (req.method == "DELETE" and req.body):
        if not req.body:
            return {}
        parsed = req.json()
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            raise ValueError("body is not a JSON object")
        return parsed
    return {}


# -- views --------------------------------------------------------------------

def _user_view(store):
    v = store.viewer
    return {
        "login": v["login"], "id": v["id"], "name": v["name"],
        "email": v["email"], "type": "User",
        "html_url": f"https://example.invalid/{v['login']}",
        "url": "/api/v3/user",
    }


def _repo_view(store, repo):
    v = store.viewer
    return {
        "id": repo.id, "name": repo.name, "full_name": repo.full_name,
        "private": repo.private, "description": repo.description,
        "default_branch": repo.default_branch,
        "owner": {"login": repo.owner, "id": v["id"], "type": "User"},
        "has_issues": True, "has_projects": True, "has_wiki": False,
        "created_at": repo.created_at, "updated_at": repo.updated_at,
        "pushed_at": repo.updated_at,
        "url": f"/api/v3/repos/{repo.owner}/{repo.name}",
        "html_url": f"https://example.invalid/{repo.owner}/{repo.name}",
        "clone_url": f"https://example.invalid/{repo.owner}/{repo.name}.git",
    }


def _issue_view(store, repo, issue):
    return {
        "id": issue["id"], "number": issue["number"],
        "title": issue["title"], "body": issue["body"],
        "state": issue["state"], "labels": issue["labels"],
        "user": {"login": store.viewer["login"]},
        "comments": len(issue["comments"]),
        "created_at": issue["created_at"], "updated_at": issue["updated_at"],
        "url": f"/api/v3/repos/{repo.owner}/{repo.name}/issues/{issue['number']}",
        "html_url": f"https://example.invalid/{repo.owner}/{repo.name}/issues/{issue['number']}",
    }


def _pull_view(store, repo, pull):
    view = {
        "id": pull["id"], "number": pull["number"],
        "title": pull["title"], "body": pull["body"],
        "state": pull["state"], "merged": pull["merged"],
        "merged_at": pull["merged_at"],
        "merge_commit_sha": pull["merge_commit_sha"],
        "head": {"ref": pull["head"]["ref"], "sha": pull["head"]["sha"],
                 "label": f"{repo.owner}:{pull['head']['ref']}"},
        "base": {"ref": pull["base"]["ref"], "sha": pull["base"]["sha"],
                 "label": f"{repo.owner}:{pull['base']['ref']}"},
        "user": {"login": store.viewer["login"]},
        "created_at": pull["created_at"], "updated_at": pull["updated_at"],
        "url": f"/api/v3/repos/{repo.owner}/{repo.name}/pulls/{pull['number']}",
    }
    return view


# -- repos ----------------------------------------------------------------------

def _create_repo(store, req, body, rid):
    name = body.get("name")
    if not isinstance(name, str) or not name:
        return json_error(422, "name is required", rid)
    private = body.get("private", False) is True
    description = body.get("description") or ""
    try:
        repo = store.create_repo(store.viewer["login"], name, private, description)
    except StoreError as err:
        return json_error(err.status, err.message, rid)
    return json_response(201, _repo_view(store, repo), rid)


def _list_user_repos(store, req, rid):
    repos = store.viewer_repos()
    items, headers = _paginate(store, req, [_repo_view(store, r) for r in repos])
    return json_response(200, items, {**rid, **headers})


def _paginate(store, req, items):
    try:
        per_page = int(req.query.get("per_page", "30"))
    except ValueError:
        per_page = 30
    per_page = max(1, min(per_page, 100))
    try:
        page = int(req.query.get("page", "1"))
    except ValueError:
        page = 1
    page = max(1, page)
    total = len(items)
    start = (page - 1) * per_page
    sliced = items[start:start + per_page]
    host = req.header("host", "127.0.0.1")
    base = f"http://{host}{req.path}"
    links = []
    last_page = max(1, (total + per_page - 1) // per_page)
    if page < last_page:
        links.append(f'<{base}?per_page={per_page}&page={page + 1}>; rel="next"')
        links.append(f'<{base}?per_page={per_page}&page={last_page}>; rel="last"')
    if page > 1:
        links.append(f'<{base}?per_page={per_page}&page={page - 1}>; rel="prev"')
        links.append(f'<{base}?per_page={per_page}&page=1>; rel="first"')
    headers = {}
    if links:
        headers["Link"] = ", ".join(links)
    return sliced, headers


def _repo_routes(store, req, rest, body, rid):
    """rest = [owner, name, ...tail] under /api/v3/repos/."""
    owner, name = rest[0], rest[1]
    tail = rest[2:]
    method = req.method
    if owner != store.viewer["login"]:
        return json_error(404, "Not Found", rid)
    repo = store.get_repo(owner, name)
    if repo is None:
        return json_error(404, "Not Found", rid)

    if not tail:
        if method == "GET":
            return json_response(200, _repo_view(store, repo), rid)
        if method == "PATCH":
            if "description" in body:
                repo.description = body.get("description") or ""
            if "private" in body:
                repo.private = body.get("private") is True
            repo.updated_at = store.now_iso()
            return json_response(200, _repo_view(store, repo), rid)
        if method == "DELETE":
            store.delete_repo(owner, name)
            return Response(204, b"", rid)
        return json_error(404, "Not Found", rid)

    if tail == ["branches"]:
        if method == "GET":
            branches = [{"name": n[len("refs/heads/"):],
                         "commit": {"sha": sha}, "protected": False}
                        for n, sha in repo.refs.items()
                        if n.startswith("refs/heads/")]
            return json_response(200, branches, rid)
    if len(tail) == 2 and tail[0] == "branches" and method == "GET":
        ref = f"refs/heads/{tail[1]}"
        sha = repo.refs.get(ref)
        if sha is None:
            return json_error(404, "Not Found", rid)
        return json_response(200, {"name": tail[1], "commit": {"sha": sha}}, rid)
    if tail == ["commits"] and method == "GET":
        ref = req.query.get("sha", repo.default_branch)
        commits = store.list_commits(repo, ref)
        out = []
        for sha in commits:
            found = repo.lookup(sha)
            parsed = gitobjs.parse_commit(found[1])
            out.append({
                "sha": sha,
                "commit": {
                    "message": parsed["message"],
                    "author": _identity_view(parsed["author"]),
                    "committer": _identity_view(parsed["committer"]),
                },
            })
        return json_response(200, out, rid)
    if tail == ["git", "refs"] and method == "POST":
        return _create_ref(store, repo, body, rid)
    if len(tail) >= 3 and tail[0] == "git" and tail[1] == "refs" and method == "GET":
        return _get_ref(store, repo, "/".join(tail[2:]), rid)
    if tail[0] == "contents":
        return _contents(store, req, repo, tail[1:], body, rid)
    if tail == ["labels"]:
        if method == "POST":
            label = store.create_label(repo, body.get("name") or "",
                                       body.get("color") or "ededed")
            return json_response(201, label, rid)
        if method == "GET":
            return json_response(200, list(repo.labels.values()), rid)
    if tail == ["issues"]:
        if method == "POST":
            return _create_issue(store, repo, body, rid)
        if method == "GET":
            return _list_issues(store, req, repo, rid)
    if len(tail) == 2 and tail[0] == "issues" and tail[1].isdigit():
        issue = store.get_issue(repo, int(tail[1]))
        if issue is None:
            return json_error(404, "Not Found", rid)
        if method == "GET":
            return json_response(200, _issue_view(store, repo, issue), rid)
        if method == "PATCH":
            if "state" in body and body["state"] in ("open", "closed"):
                issue["state"] = body["state"]
            if "title" in body:
                issue["title"] = body["title"]
            if "body" in body:
                issue["body"] = body["body"]
            issue["updated_at"] = store.now_iso()
            return json_response(200, _issue_view(store, repo, issue), rid)
    if len(tail) == 3 and tail[0] == "issues" and tail[1].isdigit() and tail[2] == "comments":
        issue = store.get_issue(repo, int(tail[1]))
        if issue is None:
            return json_error(404, "Not Found", rid)
        if method == "POST":
            comment = store.add_comment(repo, issue, body.get("body") or "")
            return json_response(201, {"id": comment["id"], "body": comment["body"],
                                       "created_at": comment["created_at"]}, rid)
        if method == "GET":
            return json_response(200, [{"id": c["id"], "body": c["body"],
                                        "created_at": c["created_at"]}
                                       for c in issue["comments"]], rid)
    if tail == ["pulls"]:
        if method == "POST":
            return _create_pull(store, repo, body, rid)
        if method == "GET":
            return _list_pulls(store, req, repo, rid)
    if len(tail) == 2 and tail[0] == "pulls" and tail[1].isdigit():
        pull = store.get_pull(repo, int(tail[1]))
        if pull is None:
            return json_error(404, "Not Found", rid)
        if method == "GET":
            return json_response(200, _pull_view(store, repo, pull), rid)
    if len(tail) == 3 and tail[0] == "pulls" and tail[1].isdigit() and tail[2] == "files":
        pull = store.get_pull(repo, int(tail[1]))
        if pull is None:
            return json_error(404, "Not Found", rid)
        if method == "GET":
            return json_response(200, _pull_files(store, repo, pull), rid)
    if len(tail) == 3 and tail[0] == "pulls" and tail[1].isdigit() and tail[2] == "merge":
        pull = store.get_pull(repo, int(tail[1]))
        if pull is None:
            return json_error(404, "Not Found", rid)
        if method == "PUT":
            return _merge_pull(store, repo, pull, body, rid)
    return json_error(404, "Not Found", rid)


def _identity_view(identity: str):
    # "Name <email> epoch +0000" -> {name, email, date}
    name, _, rest = identity.partition(" <")
    email, _, tail = rest.partition(">")
    epoch = tail.strip().split(" ")[0] if tail.strip() else "0"
    try:
        from datetime import datetime, timezone
        date = datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        date = ""
    return {"name": name, "email": email, "date": date}


def _create_ref(store, repo, body, rid):
    ref = body.get("ref") or ""
    sha = body.get("sha") or ""
    if not ref.startswith("refs/") or not sha:
        return json_error(422, "ref and sha are required", rid)
    if ref in repo.refs:
        return json_error(422, f"ref {ref} already exists", rid)
    if repo.lookup(sha) is None:
        return json_error(422, f"object {sha} not found", rid)
    repo.refs[ref] = sha
    return json_response(201, {"ref": ref, "object": {"sha": sha, "type": "commit"}}, rid)


def _get_ref(store, repo, ref, rid):
    full = ref if ref.startswith("refs/") else f"refs/{ref}"
    sha = repo.refs.get(full)
    if sha is None:
        return json_error(404, "Not Found", rid)
    return json_response(200, {"ref": full, "object": {"sha": sha, "type": "commit"}}, rid)


# -- contents -------------------------------------------------------------------

def _contents(store, req, repo, parts, body, rid):
    path = "/".join(parts)
    method = req.method
    if method == "GET":
        ref = req.query.get("ref", repo.default_branch)
        found = store.get_content(repo, ref, path)
        if found is None:
            return json_error(404, "Not Found", rid)
        kind, payload = found
        if kind == "dir":
            entries = []
            for mode, name, oid in payload:
                entry = {"type": "dir" if mode == "40000" else "file",
                         "name": name, "path": f"{path}/{name}" if path else name,
                         "sha": oid}
                if mode != "40000":
                    blob = repo.lookup(oid)
                    entry["size"] = len(blob[1]) if blob else 0
                entries.append(entry)
            return json_response(200, entries, rid)
        oid, data = payload
        return json_response(200, {
            "type": "file", "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
            "sha": oid, "size": len(data),
            "name": parts[-1], "path": path,
        }, rid)
    if method == "PUT":
        return _put_contents(store, req, repo, path, body, rid)
    if method == "DELETE":
        return _delete_contents(store, repo, path, body, rid)
    return json_error(404, "Not Found", rid)


def _put_contents(store, req, repo, path, body, rid):
    try:
        content = base64.b64decode(body.get("content") or "", validate=True)
    except (binascii.Error, ValueError):
        return json_error(400, "content is not valid base64", rid)
    message = body.get("message") or f"Update {path}"
    branch = body.get("branch") or repo.default_branch
    wanted_sha = body.get("sha")
    tip = store.resolve_ref(repo, branch)
    exists = False
    current_sha = None
    if tip is not None:
        found = store.get_content(repo, branch, path)
        if found is not None and found[0] == "file":
            exists = True
            current_sha = found[1][0]
    if exists:
        if wanted_sha is not None and wanted_sha.lower() != current_sha.lower():
            return json_error(409, "sha does not match the current blob", rid)
        if wanted_sha is None:
            return json_error(422, "sha is required to update a file", rid)
        status = 200
    else:
        status = 201
    commit_sha, blob_sha = store.commit_file(repo, branch, path, content, message)
    return json_response(status, {
        "content": {"name": path.split("/")[-1], "path": path, "sha": blob_sha,
                    "size": len(content), "type": "file", "encoding": "base64"},
        "commit": {"sha": commit_sha},
    }, rid)


def _delete_contents(store, repo, path, body, rid):
    branch = body.get("branch") or repo.default_branch
    wanted_sha = body.get("sha")
    found = store.get_content(repo, branch, path)
    if found is None or found[0] != "file":
        return json_error(404, "Not Found", rid)
    if wanted_sha is not None and wanted_sha.lower() != found[1][0].lower():
        return json_error(422, "sha does not match the current blob", rid)
    message = body.get("message") or f"Delete {path}"
    commit_sha = store.delete_file(repo, branch, path, message)
    return json_response(200, {"commit": {"sha": commit_sha}}, rid)


# -- issues and pulls -------------------------------------------------------------

def _create_issue(store, repo, body, rid):
    labels = body.get("labels") or []
    if isinstance(labels, str):
        labels = [labels]
    try:
        issue = store.create_issue(repo, body.get("title") or "",
                                   body.get("body") or "", labels)
    except StoreError as err:
        return json_error(err.status, err.message, rid)
    return json_response(201, _issue_view(store, repo, issue), rid)


def _list_issues(store, req, repo, rid):
    state = req.query.get("state", "open")
    sort = req.query.get("sort", "created")
    direction = req.query.get("direction", "desc")
    issues = list(repo.issues)
    if state in ("open", "closed"):
        issues = [i for i in issues if i["state"] == state]
    if sort == "created":
        issues.sort(key=lambda i: i["number"], reverse=(direction == "desc"))
    items, headers = _paginate(store, req, [_issue_view(store, repo, i) for i in issues])
    return json_response(200, items, {**rid, **headers})


def _create_pull(store, repo, body, rid):
    head = body.get("head") or ""
    base = body.get("base") or repo.default_branch
    if isinstance(head, dict):
        head = head.get("ref") or ""
    if isinstance(base, dict):
        base = base.get("ref") or ""
    # An open PR from the same head already exists: 422 (the GraphQL
    # fixture tolerates it and re-reads the list).
    for pull in repo.pulls:
        if pull["head"]["ref"] == head and pull["state"] == "open":
            return json_error(422, "a pull request from this head already exists", rid)
    try:
        pull = store.create_pull(repo, body.get("title") or "",
                                 body.get("body") or "", head, base)
    except StoreError as err:
        return json_error(err.status, err.message, rid)
    return json_response(201, _pull_view(store, repo, pull), rid)


def _list_pulls(store, req, repo, rid):
    state = req.query.get("state", "open")
    pulls = list(repo.pulls)
    if state == "open":
        pulls = [p for p in pulls if p["state"] == "open"]
    elif state == "closed":
        pulls = [p for p in pulls if p["state"] == "closed"]
    items, headers = _paginate(store, req, [_pull_view(store, repo, p) for p in pulls])
    return json_response(200, items, {**rid, **headers})


def _pull_files(store, repo, pull):
    return store.diff_pull_files(repo, pull)


def _merge_pull(store, repo, pull, body, rid):
    method = (body.get("merge_method") or "squash").lower()
    if method not in ("squash", "merge", "rebase"):
        method = "squash"
    try:
        merge_sha = store.merge_pull(repo, pull, method)
    except StoreError as err:
        return json_error(err.status, err.message, rid)
    return json_response(200, {"sha": merge_sha, "merged": True,
                               "message": f"Pull request #{pull['number']} merged"}, rid)
