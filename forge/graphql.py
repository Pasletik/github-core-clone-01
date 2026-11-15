"""GraphQL v4 (/api/graphql): the documents gh and github-mcp-server send.

A small hand-written parser for the GraphQL query language subset the
suite uses (operations, variables, aliases, inline input objects and
enums, inline fragments, `__type` introspection, batched operation
arrays) plus an executor over the Store. Unknown fields are a 200 with
an `errors` array naming the field; an unknown repository is
`data.repository = null` with no errors; no token is HTTP 401.
"""

from __future__ import annotations

import base64
import binascii
import json
import re

from .server import Response, json_error, json_response
from .store import StoreError

# ---------------------------------------------------------------- lexer ----

_TOKEN_RE = re.compile(r"""
    (?P<ws>[ \t\r\n,]+)
  | (?P<ellipsis>\.\.\.)
  | (?P<punct>[!$\(\)\:\=\@\[\]\{\}\|&])
  | (?P<string>"(?:\\.|[^"\\])*")
  | (?P<number>-?(?:0|[1-9][0-9]*)(\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)
  | (?P<name>[_A-Za-z][_0-9A-Za-z]*)
""", re.VERBOSE)


class GraphQLError(Exception):
    pass


def _lex(text: str):
    tokens = []
    i = 0
    while i < len(text):
        if text[i] == "#":
            j = text.find("\n", i)
            i = len(text) if j < 0 else j + 1
            continue
        m = _TOKEN_RE.match(text, i)
        if m is None:
            raise GraphQLError(f"unexpected character {text[i]!r}")
        i = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        tokens.append((kind, m.group()))
    tokens.append(("eof", ""))
    return tokens


# ---------------------------------------------------------------- parser ---

class Parser:
    def __init__(self, text: str):
        self.tokens = _lex(text)
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, value: str):
        kind, text = self.next()
        if text != value:
            raise GraphQLError(f"expected {value!r}, got {text!r}")
        return text

    def expect_name(self) -> str:
        kind, text = self.next()
        if kind != "name":
            raise GraphQLError(f"expected a name, got {text!r}")
        return text

    def parse_document(self):
        operations = []
        fragments = {}
        while self.peek()[0] != "eof":
            kind, text = self.peek()
            if text == "fragment":
                self.next()
                name = self.expect_name()
                self.expect("on")
                type_cond = self.expect_name()
                fragments[name] = {
                    "type": type_cond,
                    "selections": self.parse_selection_set(),
                }
            else:
                operations.append(self.parse_operation())
        return {"operations": operations, "fragments": fragments}

    def parse_operation(self):
        kind, text = self.peek()
        if text == "{":
            return {"kind": "query", "name": None, "variables": {},
                    "selections": self.parse_selection_set()}
        op_kind = self.next()[1]  # query | mutation | subscription
        name = None
        if self.peek()[0] == "name":
            name = self.next()[1]
        variables = {}
        if self.peek()[1] == "(":
            variables = self.parse_variable_definitions()
        selections = self.parse_selection_set()
        return {"kind": op_kind, "name": name, "variables": variables,
                "selections": selections}

    def parse_variable_definitions(self):
        self.expect("(")
        variables = {}
        while self.peek()[1] != ")":
            self.expect("$")
            name = self.expect_name()
            self.expect(":")
            variables[name] = self.parse_type()
            if self.peek()[1] == "=":
                self.next()
                variables[name + "=default"] = self.parse_value()
        self.expect(")")
        return variables

    def parse_type(self):
        kind, text = self.peek()
        if text == "[":
            self.next()
            inner = self.parse_type()
            self.expect("]")
            result = {"list": inner}
        else:
            result = {"named": self.expect_name()}
        if self.peek()[1] == "!":
            self.next()
            result = {"non_null": result}
        return result

    def parse_selection_set(self):
        self.expect("{")
        selections = []
        while self.peek()[1] != "}":
            selections.append(self.parse_selection())
        self.expect("}")
        return selections

    def parse_selection(self):
        kind, text = self.peek()
        if text == "...":
            self.next()
            if self.peek()[1] == "name" and self.peek()[1] != "on":
                name = self.next()[1]
                return {"kind": "spread", "name": name}
            type_cond = None
            if self.peek()[1] == "on":
                self.next()
                type_cond = self.expect_name()
            return {"kind": "inline", "type": type_cond,
                    "selections": self.parse_selection_set()}
        name = self.expect_name()
        alias = None
        if self.peek()[1] == ":":
            self.next()
            alias = name
            name = self.expect_name()
        args = {}
        if self.peek()[1] == "(":
            args = self.parse_arguments()
        selections = None
        if self.peek()[1] == "{":
            selections = self.parse_selection_set()
        return {"kind": "field", "name": name, "alias": alias or name,
                "args": args, "selections": selections}

    def parse_arguments(self):
        self.expect("(")
        args = {}
        while self.peek()[1] != ")":
            name = self.expect_name()
            self.expect(":")
            args[name] = self.parse_value()
        self.expect(")")
        return args

    def parse_value(self):
        kind, text = self.next()
        if text == "$":
            return {"var": self.expect_name()}
        if text == "[":
            values = []
            while self.peek()[1] != "]":
                values.append(self.parse_value())
            self.expect("]")
            return {"list": values}
        if text == "{":
            obj = {}
            while self.peek()[1] != "}":
                name = self.expect_name()
                self.expect(":")
                obj[name] = self.parse_value()
            self.expect("}")
            return {"object": obj}
        if kind == "string":
            return {"string": json.loads(text)}
        if kind == "number":
            return {"int": int(text)} if re.fullmatch(r"-?\d+", text) else {"float": float(text)}
        if kind == "name":
            if text == "true":
                return {"bool": True}
            if text == "false":
                return {"bool": False}
            if text == "null":
                return {"null": True}
            return {"enum": text}
        raise GraphQLError(f"unexpected value {text!r}")


def _resolve_value(node, variables):
    if "var" in node:
        return variables.get(node["var"])
    if "list" in node:
        return [_resolve_value(v, variables) for v in node["list"]]
    if "object" in node:
        return {k: _resolve_value(v, variables) for k, v in node["object"].items()}
    if "string" in node:
        return node["string"]
    if "int" in node:
        return node["int"]
    if "float" in node:
        return node["float"]
    if "bool" in node:
        return node["bool"]
    if "null" in node:
        return None
    if "enum" in node:
        return node["enum"]
    return None


# ---------------------------------------------------------------- schema ---

def _fields(*names):
    return list(names)


SCHEMA = {
    "Query": {"kind": "OBJECT", "fields": _fields("viewer", "repository", "node", "__type", "__schema")},
    "Mutation": {"kind": "OBJECT", "fields": _fields("createIssue", "closeIssue", "createPullRequest", "mergePullRequest", "addComment")},
    "User": {"kind": "OBJECT", "fields": _fields("login", "id", "name", "email")},
    "Repository": {"kind": "OBJECT", "fields": _fields(
        "id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef",
        "viewerPermission", "hasIssuesEnabled", "issue", "issues",
        "pullRequests", "pullRequest", "description", "private", "url", "createdAt", "updatedAt")},
    "Ref": {"kind": "OBJECT", "fields": _fields("name")},
    "Issue": {"kind": "OBJECT", "fields": _fields(
        "id", "number", "title", "state", "body", "createdAt", "updatedAt", "url")},
    "IssueConnection": {"kind": "OBJECT", "fields": _fields("nodes", "edges", "pageInfo", "totalCount")},
    "IssueEdge": {"kind": "OBJECT", "fields": _fields("node", "cursor")},
    "PageInfo": {"kind": "OBJECT", "fields": _fields("hasNextPage", "hasPreviousPage", "endCursor", "startCursor")},
    "PullRequest": {"kind": "OBJECT", "fields": _fields(
        "id", "number", "title", "body", "state", "merged", "mergedAt",
        "headRefName", "baseRefName", "mergeable", "files", "createdAt", "updatedAt")},
    "PullRequestConnection": {"kind": "OBJECT", "fields": _fields("nodes", "edges", "pageInfo", "totalCount")},
    "PullRequestChangedFile": {"kind": "OBJECT", "fields": _fields("path", "filename")},
    "PullRequestChangedFileConnection": {"kind": "OBJECT", "fields": _fields("nodes", "edges", "pageInfo")},
    "IssueComment": {"kind": "OBJECT", "fields": _fields("id", "body")},
    "IssueCommentEdge": {"kind": "OBJECT", "fields": _fields("node", "cursor")},
    "CreateIssuePayload": {"kind": "OBJECT", "fields": _fields("issue")},
    "CloseIssuePayload": {"kind": "OBJECT", "fields": _fields("issue")},
    "CreatePullRequestPayload": {"kind": "OBJECT", "fields": _fields("pullRequest")},
    "MergePullRequestPayload": {"kind": "OBJECT", "fields": _fields("pullRequest")},
    "AddCommentPayload": {"kind": "OBJECT", "fields": _fields("commentEdge")},
    "SearchType": {"kind": "ENUM", "values": ["ISSUE", "PULL_REQUEST", "REPOSITORY", "USER", "DISCUSSION", "COMMIT"]},
    "IssueState": {"kind": "ENUM", "values": ["OPEN", "CLOSED"]},
    "PullRequestState": {"kind": "ENUM", "values": ["OPEN", "CLOSED", "MERGED"]},
    "MergeableState": {"kind": "ENUM", "values": ["MERGEABLE", "CONFLICTING", "UNKNOWN"]},
    "PullRequestMergeMethod": {"kind": "ENUM", "values": ["MERGE", "SQUASH", "REBASE"]},
    "RepositoryPermission": {"kind": "ENUM", "values": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"]},
    "IssueOrderField": {"kind": "ENUM", "values": ["CREATED_AT", "UPDATED_AT", "COMMENTS"]},
    "OrderDirection": {"kind": "ENUM", "values": ["ASC", "DESC"]},
    "__Type": {"kind": "OBJECT", "fields": _fields("name", "kind", "fields", "enumValues")},
    "__Field": {"kind": "OBJECT", "fields": _fields("name")},
    "__EnumValue": {"kind": "OBJECT", "fields": _fields("name")},
}

# field -> (return type, resolver name). Resolver names are methods on Executor.
FIELD_TYPES = {
    ("Query", "viewer"): "User",
    ("Query", "repository"): "Repository",
    ("Query", "node"): "Node",
    ("Query", "__type"): "__Type",
    ("Repository", "owner"): "User",
    ("Repository", "defaultBranchRef"): "Ref",
    ("Repository", "issue"): "Issue",
    ("Repository", "issues"): "IssueConnection",
    ("Repository", "pullRequests"): "PullRequestConnection",
    ("Repository", "pullRequest"): "PullRequest",
    ("IssueConnection", "nodes"): "[Issue]",
    ("IssueConnection", "edges"): "[IssueEdge]",
    ("IssueConnection", "pageInfo"): "PageInfo",
    ("IssueEdge", "node"): "Issue",
    ("PullRequestConnection", "nodes"): "[PullRequest]",
    ("PullRequestConnection", "pageInfo"): "PageInfo",
    ("PullRequest", "files"): "PullRequestChangedFileConnection",
    ("PullRequestChangedFileConnection", "nodes"): "[PullRequestChangedFile]",
    ("Mutation", "createIssue"): "CreateIssuePayload",
    ("Mutation", "closeIssue"): "CloseIssuePayload",
    ("Mutation", "createPullRequest"): "CreatePullRequestPayload",
    ("Mutation", "mergePullRequest"): "MergePullRequestPayload",
    ("Mutation", "addComment"): "AddCommentPayload",
    ("CreateIssuePayload", "issue"): "Issue",
    ("CloseIssuePayload", "issue"): "Issue",
    ("CreatePullRequestPayload", "pullRequest"): "PullRequest",
    ("MergePullRequestPayload", "pullRequest"): "PullRequest",
    ("AddCommentPayload", "commentEdge"): "IssueCommentEdge",
    ("IssueCommentEdge", "node"): "IssueComment",
    ("__Type", "fields"): "[__Field]",
    ("__Type", "enumValues"): "[__EnumValue]",
}


# ---------------------------------------------------------------- executor -

def _decode_global_id(gid: str):
    try:
        text = base64.b64decode(gid.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    parts = text.split(":")
    if len(parts) < 2:
        return None
    return parts


def _cursor(number: int) -> str:
    return base64.b64encode(f"cursor:{number}".encode()).decode("ascii")


def _cursor_number(cursor):
    if not isinstance(cursor, str):
        return None
    try:
        text = base64.b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if text.startswith("cursor:"):
        try:
            return int(text[len("cursor:"):])
        except ValueError:
            return None
    return None


class Executor:
    def __init__(self, store):
        self.store = store
        self.errors = []

    # -- value builders -------------------------------------------------------
    def user_value(self, login, user_id=None):
        return {"__typename": "User", "login": login,
                "id": str(user_id) if user_id is not None else login,
                "name": login, "email": f"{login}@example.invalid"}

    def repo_value(self, repo):
        default_ref = repo.refs.get(f"refs/heads/{repo.default_branch}")
        return {
            "__typename": "Repository",
            "id": repo.global_id(),
            "databaseId": repo.id,
            "name": repo.name,
            "nameWithOwner": repo.full_name,
            "owner": self.user_value(repo.owner),
            "defaultBranchRef": ({"__typename": "Ref", "name": repo.default_branch}
                                 if default_ref is not None else None),
            "viewerPermission": "ADMIN",
            "hasIssuesEnabled": True,
            "description": repo.description,
            "private": repo.private,
            "url": f"https://example.invalid/{repo.full_name}",
            "createdAt": repo.created_at,
            "updatedAt": repo.updated_at,
            "_repo": repo,
        }

    def issue_value(self, repo, issue):
        return {
            "__typename": "Issue",
            "id": self.store.issue_global_id(repo, issue),
            "number": issue["number"],
            "title": issue["title"],
            "state": issue["state"].upper(),
            "body": issue["body"],
            "createdAt": issue["created_at"],
            "updatedAt": issue["updated_at"],
            "url": f"https://example.invalid/{repo.full_name}/issues/{issue['number']}",
            "_repo": repo,
            "_issue": issue,
        }

    def pull_value(self, repo, pull):
        state = "MERGED" if pull["merged"] else pull["state"].upper()
        return {
            "__typename": "PullRequest",
            "id": self.store.pull_global_id(repo, pull),
            "number": pull["number"],
            "title": pull["title"],
            "body": pull["body"],
            "state": state,
            "merged": pull["merged"],
            "mergedAt": pull["merged_at"],
            "headRefName": pull["head"]["ref"],
            "baseRefName": pull["base"]["ref"],
            "createdAt": pull["created_at"],
            "updatedAt": pull["updated_at"],
            "_repo": repo,
            "_pull": pull,
        }

    # -- execution --------------------------------------------------------------
    def execute(self, document, operation_name, variables):
        self.errors = []
        operations = document["operations"]
        operation = None
        if operation_name:
            for op in operations:
                if op["name"] == operation_name:
                    operation = op
                    break
            if operation is None:
                self.errors.append({"message": f'unknown operation {operation_name!r}'})
                return None
        elif len(operations) == 1:
            operation = operations[0]
        else:
            self.errors.append({"message": "multiple operations need an operationName"})
            return None
        variables = dict(variables or {})
        for key, value in list(operation["variables"].items()):
            if key.endswith("=default") and key[:-8] not in variables:
                variables[key[:-8]] = _resolve_value(value, variables)
        if operation["kind"] == "mutation":
            root_type, root_value = "Mutation", {"__typename": "Mutation"}
        else:
            root_type, root_value = "Query", {"__typename": "Query"}
        data = self._execute_selections(root_type, root_value, operation["selections"],
                                        document["fragments"], variables)
        return data

    def _execute_selections(self, type_name, value, selections, fragments, variables):
        if value is None:
            return None
        # The runtime type wins: an interface such as Node resolves to the
        # concrete type the value carries (Repository, Issue, ...).
        type_name = value.get("__typename") or type_name
        out = {}
        for sel in selections:
            if sel["kind"] == "spread":
                frag = fragments.get(sel["name"])
                if frag is None:
                    self.errors.append({"message": f"unknown fragment {sel['name']!r}"})
                    continue
                if frag["type"] and frag["type"] != value.get("__typename"):
                    continue
                out.update(self._execute_selections(type_name, value, frag["selections"],
                                                    fragments, variables))
                continue
            if sel["kind"] == "inline":
                if sel["type"] and sel["type"] != value.get("__typename"):
                    continue
                out.update(self._execute_selections(type_name, value, sel["selections"],
                                                    fragments, variables))
                continue
            # field
            name = sel["name"]
            alias = sel["alias"]
            if name == "__typename":
                out[alias] = value.get("__typename")
                continue
            type_def = SCHEMA.get(type_name)
            if type_def is None or name not in type_def.get("fields", []):
                self.errors.append({"message": f'Cannot query field "{name}" on type "{type_name}".'})
                continue
            args = {k: _resolve_value(v, variables) for k, v in sel["args"].items()}
            try:
                resolved = self._resolve_field(type_name, value, name, args)
            except StoreError as err:
                self.errors.append({"message": err.message})
                resolved = None
            if resolved is None:
                out[alias] = None
                continue
            if sel["selections"] is None:
                out[alias] = resolved
                continue
            child_type = FIELD_TYPES.get((type_name, name))
            if child_type is None:
                out[alias] = resolved
                continue
            if child_type.startswith("["):
                item_type = child_type[1:-1]
                out[alias] = [self._execute_selections(item_type, item, sel["selections"],
                                                        fragments, variables)
                              for item in resolved]
            else:
                out[alias] = self._execute_selections(child_type, resolved, sel["selections"],
                                                      fragments, variables)
        return out

    # -- field resolvers --------------------------------------------------------
    def _resolve_field(self, type_name, value, name, args):
        store = self.store
        if type_name == "Query":
            if name == "viewer":
                v = store.viewer
                return self.user_value(v["login"], v["id"])
            if name == "repository":
                repo = store.get_repo(args.get("owner") or "", args.get("name") or "")
                if repo is None or repo.owner != store.viewer["login"]:
                    return None
                return self.repo_value(repo)
            if name == "node":
                return self._resolve_node(args.get("id"))
            if name == "__type":
                return self._introspect(args.get("name"))
            return None
        if type_name == "Repository":
            repo = value["_repo"]
            if name == "issue":
                issue = store.get_issue(repo, args.get("number"))
                return self.issue_value(repo, issue) if issue else None
            if name == "issues":
                return self._issues_connection(repo, args)
            if name == "pullRequests":
                return self._pulls_connection(repo, args)
            if name == "pullRequest":
                pull = store.get_pull(repo, args.get("number"))
                return self.pull_value(repo, pull) if pull else None
            return value.get(name)
        if type_name == "IssueConnection":
            return value.get(name)
        if type_name == "PullRequestConnection":
            return value.get(name)
        if type_name == "PullRequest":
            repo = value["_repo"]
            pull = value["_pull"]
            if name == "mergeable":
                return store.pull_mergeable(repo, pull)
            if name == "files":
                return self._files_connection(repo, pull, args)
            return value.get(name)
        if type_name == "PullRequestChangedFileConnection":
            return value.get(name)
        if type_name == "__Type":
            if name == "fields":
                include = args.get("includeDeprecated", False)
                type_name_arg = value.get("_type_name")
                fields = SCHEMA.get(type_name_arg, {}).get("fields", [])
                return [{"__typename": "__Field", "name": f} for f in fields]
            if name == "enumValues":
                type_name_arg = value.get("_type_name")
                values = SCHEMA.get(type_name_arg, {}).get("values", [])
                return [{"__typename": "__EnumValue", "name": v} for v in values]
            return value.get(name)
        if type_name == "Mutation":
            return self._mutation(name, args)
        return value.get(name)

    def _resolve_node(self, gid):
        parts = _decode_global_id(gid or "")
        if not parts:
            return None
        kind = parts[0]
        if kind == "Repository" and len(parts) == 2 and parts[1].isdigit():
            for repo in self.store.repos.values():
                if repo.id == int(parts[1]):
                    return self.repo_value(repo)
            return None
        if kind == "Issue" and len(parts) == 3:
            for repo in self.store.repos.values():
                if repo.id == int(parts[1]):
                    issue = self.store.get_issue(repo, int(parts[2]))
                    return self.issue_value(repo, issue) if issue else None
            return None
        if kind == "PullRequest" and len(parts) == 3:
            for repo in self.store.repos.values():
                if repo.id == int(parts[1]):
                    pull = self.store.get_pull(repo, int(parts[2]))
                    return self.pull_value(repo, pull) if pull else None
            return None
        return None

    def _introspect(self, name):
        if not isinstance(name, str) or name not in SCHEMA:
            return None
        definition = SCHEMA[name]
        return {"__typename": "__Type", "name": name, "kind": definition["kind"],
                "_type_name": name}

    def _issues_connection(self, repo, args):
        issues = list(repo.issues)
        states = args.get("states")
        if states:
            wanted = {s.lower() for s in states}
            issues = [i for i in issues if i["state"] in wanted]
        order_by = args.get("orderBy") or {}
        field = (order_by.get("field") or "CREATED_AT").upper()
        direction = (order_by.get("direction") or "DESC").upper()
        if field in ("CREATED_AT", "UPDATED_AT"):
            issues.sort(key=lambda i: i["number"], reverse=(direction == "DESC"))
        first = args.get("first")
        after = _cursor_number(args.get("after"))
        if after is not None:
            # skip up to and including the cursor, in the current order
            if direction == "DESC":
                issues = [i for i in issues if i["number"] < after]
            else:
                issues = [i for i in issues if i["number"] > after]
        total = len(issues)
        if isinstance(first, int):
            page = issues[:first]
        else:
            page = issues
        has_next = len(page) < total
        end_cursor = _cursor(page[-1]["number"]) if page else None
        return {
            "__typename": "IssueConnection",
            "nodes": [self.issue_value(repo, i) for i in page],
            "pageInfo": {"__typename": "PageInfo", "hasNextPage": has_next,
                         "hasPreviousPage": False, "endCursor": end_cursor,
                         "startCursor": _cursor(page[0]["number"]) if page else None},
            "totalCount": total,
        }

    def _pulls_connection(self, repo, args):
        pulls = [p for p in repo.pulls if p["state"] == "open"]
        states = args.get("states")
        if states:
            wanted = set()
            for s in states:
                wanted.add("MERGED" if s == "MERGED" else s)
            pulls = [p for p in pulls
                     if (("MERGED" if p["merged"] else p["state"].upper()) in wanted
                         or (p["state"].upper() in wanted))]
        first = args.get("first")
        if isinstance(first, int):
            pulls = pulls[:first]
        return {
            "__typename": "PullRequestConnection",
            "nodes": [self.pull_value(repo, p) for p in pulls],
            "pageInfo": {"__typename": "PageInfo", "hasNextPage": False,
                         "hasPreviousPage": False, "endCursor": None,
                         "startCursor": None},
            "totalCount": len(pulls),
        }

    def _files_connection(self, repo, pull, args):
        nodes = []
        for entry in self.store.diff_pull_files(repo, pull):
            nodes.append({"__typename": "PullRequestChangedFile",
                          "path": entry["filename"],
                          "filename": entry["filename"]})
        first = args.get("first")
        if isinstance(first, int):
            nodes = nodes[:first]
        return {
            "__typename": "PullRequestChangedFileConnection",
            "nodes": nodes,
            "pageInfo": {"__typename": "PageInfo", "hasNextPage": False,
                         "hasPreviousPage": False, "endCursor": None,
                         "startCursor": None},
        }

    # -- mutations ------------------------------------------------------------
    def _mutation(self, name, args):
        store = self.store
        payload = args.get("input") or {}
        if name == "createIssue":
            parts = _decode_global_id(payload.get("repositoryId") or "")
            repo = self._repo_by_global_id(parts)
            if repo is None:
                raise StoreError(422, "repository not found")
            issue = store.create_issue(repo, payload.get("title") or "",
                                       payload.get("body") or "", ())
            return {"__typename": "CreateIssuePayload",
                    "issue": self.issue_value(repo, issue)}
        if name == "closeIssue":
            parts = _decode_global_id(payload.get("issueId") or "")
            if not parts or parts[0] != "Issue":
                raise StoreError(422, "issue not found")
            repo = self._repo_by_id(int(parts[1])) if parts[1].isdigit() else None
            issue = store.get_issue(repo, int(parts[2])) if repo else None
            if issue is None:
                raise StoreError(422, "issue not found")
            issue["state"] = "closed"
            issue["updated_at"] = store.now_iso()
            return {"__typename": "CloseIssuePayload",
                    "issue": self.issue_value(repo, issue)}
        if name == "createPullRequest":
            parts = _decode_global_id(payload.get("repositoryId") or "")
            repo = self._repo_by_global_id(parts)
            if repo is None:
                raise StoreError(422, "repository not found")
            pull = store.create_pull(repo, payload.get("title") or "",
                                     payload.get("body") or "",
                                     payload.get("headRefName") or "",
                                     payload.get("baseRefName") or "")
            return {"__typename": "CreatePullRequestPayload",
                    "pullRequest": self.pull_value(repo, pull)}
        if name == "mergePullRequest":
            parts = _decode_global_id(payload.get("pullRequestId") or "")
            if not parts or parts[0] != "PullRequest":
                raise StoreError(422, "pull request not found")
            repo = self._repo_by_id(int(parts[1])) if parts[1].isdigit() else None
            pull = store.get_pull(repo, int(parts[2])) if repo else None
            if pull is None:
                raise StoreError(422, "pull request not found")
            method = (payload.get("mergeMethod") or "SQUASH").lower()
            store.merge_pull(repo, pull, method)
            return {"__typename": "MergePullRequestPayload",
                    "pullRequest": self.pull_value(repo, pull)}
        if name == "addComment":
            parts = _decode_global_id(payload.get("subjectId") or "")
            if not parts or parts[0] != "Issue":
                raise StoreError(422, "issue not found")
            repo = self._repo_by_id(int(parts[1])) if parts[1].isdigit() else None
            issue = store.get_issue(repo, int(parts[2])) if repo else None
            if issue is None:
                raise StoreError(422, "issue not found")
            comment = store.add_comment(repo, issue, payload.get("body") or "")
            return {"__typename": "AddCommentPayload",
                    "commentEdge": {"__typename": "IssueCommentEdge",
                                    "node": {"__typename": "IssueComment",
                                             "id": store.comment_global_id(repo, issue, comment),
                                             "body": comment["body"]},
                                    "cursor": _cursor(comment["id"])}}
        raise StoreError(422, f"unknown mutation {name}")

    def _repo_by_global_id(self, parts):
        if not parts or parts[0] != "Repository" or len(parts) != 2:
            return None
        return self._repo_by_id(int(parts[1])) if parts[1].isdigit() else None

    def _repo_by_id(self, repo_id):
        for repo in self.store.repos.values():
            if repo.id == repo_id:
                return repo
        return None


# ---------------------------------------------------------------- dispatch --

def dispatch(store, req) -> Response:
    if req.method != "POST":
        return json_error(404, "Not Found")
    if not store.authenticate(req.bearer()):
        return json_error(401, "Requires authentication")
    try:
        payload = req.json()
    except ValueError:
        return json_error(400, "request body is not valid JSON")
    executor = Executor(store)
    if isinstance(payload, list):
        results = [_run_one(executor, entry) for entry in payload]
        return json_response(200, results)
    if isinstance(payload, dict):
        return json_response(200, _run_one(executor, payload))
    return json_error(400, "request body must be a JSON object or array")


def _run_one(executor, entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("query"), str):
        return {"data": None,
                "errors": [{"message": "request body must carry a query string"}]}
    try:
        document = Parser(entry["query"]).parse_document()
    except GraphQLError as err:
        return {"data": None, "errors": [{"message": str(err)}]}
    data = executor.execute(document, entry.get("operationName"),
                            entry.get("variables") or {})
    if executor.errors:
        return {"data": data, "errors": executor.errors}
    return {"data": data}
