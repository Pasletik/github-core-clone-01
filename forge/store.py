"""The forge's in-memory store: repositories, git objects, issues, PRs.

One Store holds everything a trial creates. There is no persistence and
no wall clock: timestamps come from MAKEFUN_TRIAL_NOW (or the fixture
epoch) plus a monotonic one-second tick, ids are sequential counters,
and GraphQL global ids are base64 of a type-prefixed key — so two runs
against the same requests leave the same state.

Contents commits are real git commits in the repository's object store:
a blob for the file, a rebuilt tree, a commit whose parent is the
branch tip. Clones, GET /commits, branches and pull-request diffs all
read the same history.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
from datetime import datetime, timezone

from . import gitobjs
from . import scenario

ZERO_OID = gitobjs.ZERO_OID


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_trial_now(raw: str | None) -> int:
    if not raw:
        return scenario.SCENARIO_EPOCH_SEC
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return scenario.SCENARIO_EPOCH_SEC


class StoreError(Exception):
    """An application error with an HTTP status and a message body."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Repo:
    __slots__ = (
        "id", "owner", "name", "private", "description", "default_branch",
        "objects", "refs", "issues", "pulls", "labels", "next_issue_number",
        "next_pull_number", "created_at", "updated_at", "verified",
    )

    def __init__(self, repo_id: int, owner: str, name: str, private: bool,
                 description: str, default_branch: str, created_at: str):
        self.id = repo_id
        self.owner = owner
        self.name = name
        self.private = private
        self.description = description
        self.default_branch = default_branch
        self.objects = {}  # oid -> (type, data)
        self.refs = {}     # "refs/heads/main" -> oid (insertion ordered)
        self.issues = []   # list of dicts, in creation order
        self.pulls = []    # list of dicts, in creation order
        self.labels = {}   # name -> {"name", "color"}
        self.next_issue_number = 1
        self.next_pull_number = 1
        self.created_at = created_at
        self.updated_at = created_at
        # Oids whose full closure is known present in objects (see
        # gitproto._verify_push). Objects are never removed, so once an
        # oid is verified it stays verified for the repo's life.
        self.verified = set()

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def lookup(self, oid: str):
        return self.objects.get(oid)

    def put_object(self, obj_type: str, data: bytes) -> str:
        oid = gitobjs.hash_object(obj_type, data)
        self.objects[oid] = (obj_type, data)
        return oid

    def ref(self, name: str):
        return self.refs.get(name)

    def global_id(self) -> str:
        return _b64(f"Repository:{self.id}")


class Store:
    def __init__(self, token: str = "", trial_now: str | None = None):
        self.token = token
        self.viewer = scenario.VIEWER
        self.repos = {}  # (owner, name) -> Repo
        self._next_repo_id = 5000
        self._next_object_id = 1  # numeric ids for REST issue/PR/comment objects
        self._clock = _parse_trial_now(trial_now)
        self._request_seq = 0

    # -- deterministic clock and ids -----------------------------------------
    def now_iso(self) -> str:
        self._clock += 1
        return _iso(self._clock)

    def now_epoch(self) -> int:
        return self._clock

    def request_id(self) -> str:
        self._request_seq += 1
        return "req-%08x" % self._request_seq

    def next_numeric_id(self) -> int:
        value = self._next_object_id
        self._next_object_id += 1
        return value

    # -- auth ------------------------------------------------------------------
    def authenticate(self, auth_header: str | None) -> bool:
        """True when the request carries the trial token.

        With no token configured (local mode) any non-empty bearer token
        is accepted as the viewer.
        """
        if not auth_header:
            return False
        parts = auth_header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        presented = parts[1].strip()
        if not presented:
            return False
        if not self.token:
            return True
        return presented == self.token

    # -- repositories ----------------------------------------------------------
    def create_repo(self, owner: str, name: str, private: bool, description: str) -> Repo:
        key = (owner, name)
        if key in self.repos:
            raise StoreError(422, f"Repository {owner}/{name} already exists")
        repo_id = self._next_repo_id
        self._next_repo_id += 1
        repo = Repo(repo_id, owner, name, private, description or "",
                    "main", self.now_iso())
        self.repos[key] = repo
        return repo

    def get_repo(self, owner: str, name: str):
        return self.repos.get((owner, name))

    def delete_repo(self, owner: str, name: str) -> bool:
        return self.repos.pop((owner, name), None) is not None

    def viewer_repos(self):
        return [r for r in self.repos.values() if r.owner == self.viewer["login"]]

    # -- git: trees, commits, contents -----------------------------------------
    def _tree_entries(self, repo: Repo, tree_oid: str | None):
        if not tree_oid:
            return []
        found = repo.lookup(tree_oid)
        if found is None or found[0] != "tree":
            return []
        return gitobjs.parse_tree(found[1])

    def flatten_tree(self, repo: Repo, tree_oid: str | None):
        """{path: (mode, oid)} for every blob in the tree, recursively."""
        out = {}
        stack = [(tree_oid, "")]
        while stack:
            oid, prefix = stack.pop()
            if not oid:
                continue
            for mode, name, child in self._tree_entries(repo, oid):
                path = f"{prefix}{name}" if not prefix else f"{prefix}/{name}"
                if mode == "40000":
                    stack.append((child, path))
                else:
                    out[path] = (mode, child)
        return out

    def _update_tree(self, repo: Repo, tree_oid: str | None, parts, blob_oid: str | None) -> str:
        """Return a new tree oid with parts[0]/... set to blob_oid (None deletes)."""
        entries = {name: (mode, oid) for mode, name, oid in self._tree_entries(repo, tree_oid)}
        head, *rest = parts
        if not rest:
            if blob_oid is None:
                entries.pop(head, None)
            else:
                entries[head] = ("100644", blob_oid)
        else:
            sub_oid = entries.get(head, (None, None))[1]
            if blob_oid is None and sub_oid is None:
                return tree_oid or ""
            new_sub = self._update_tree(repo, sub_oid, rest, blob_oid)
            if new_sub:
                entries[head] = ("40000", new_sub)
            else:
                entries.pop(head, None)
        if not entries:
            return ""
        return repo.put_object("tree", gitobjs.build_tree(
            [(mode, name, oid) for name, (mode, oid) in entries.items()]))

    def _commit_identity(self) -> str:
        epoch = self.now_epoch()
        return f"{self.viewer['name']} <{self.viewer['email']}> {epoch} +0000"

    def commit_file(self, repo: Repo, branch: str, path: str, content: bytes,
                    message: str):
        """Commit content at path on branch; create the branch from the
        default tip when it does not exist yet. Returns (commit_sha, blob_sha)."""
        blob_sha = repo.put_object("blob", content)
        ref = f"refs/heads/{branch}"
        tip = repo.ref(ref)
        if tip is None:
            default_tip = repo.ref(f"refs/heads/{repo.default_branch}")
            base_tree = None
            parents = []
            if default_tip is not None:
                parsed = gitobjs.parse_commit(repo.lookup(default_tip)[1])
                base_tree = parsed["tree"]
                parents = [default_tip]
            elif branch != repo.default_branch:
                # brand-new branch on an unborn repo: root commit
                base_tree = None
                parents = []
        else:
            parsed = gitobjs.parse_commit(repo.lookup(tip)[1])
            base_tree = parsed["tree"]
            parents = [tip]
        new_tree = self._update_tree(repo, base_tree, path.split("/"), blob_sha)
        if not new_tree:
            raise StoreError(422, "cannot commit an empty tree")
        identity = self._commit_identity()
        commit = gitobjs.build_commit(new_tree, parents, identity, identity, message)
        commit_sha = repo.put_object("commit", commit)
        repo.refs[ref] = commit_sha
        repo.updated_at = self.now_iso()
        return commit_sha, blob_sha

    def delete_file(self, repo: Repo, branch: str, path: str, message: str) -> str:
        ref = f"refs/heads/{branch}"
        tip = repo.ref(ref)
        if tip is None:
            raise StoreError(404, f"branch {branch} not found")
        parsed = gitobjs.parse_commit(repo.lookup(tip)[1])
        flat = self.flatten_tree(repo, parsed["tree"])
        if path not in flat:
            raise StoreError(404, f"path {path} not found")
        new_tree = self._update_tree(repo, parsed["tree"], path.split("/"), None)
        identity = self._commit_identity()
        commit = gitobjs.build_commit(new_tree or self._empty_tree(repo), [tip],
                                      identity, identity, message)
        commit_sha = repo.put_object("commit", commit)
        repo.refs[ref] = commit_sha
        repo.updated_at = self.now_iso()
        return commit_sha

    def _empty_tree(self, repo: Repo) -> str:
        return repo.put_object("tree", b"")

    def resolve_ref(self, repo: Repo, ref: str):
        """A branch name, a full ref, or a commit sha -> commit oid or None."""
        if ref in repo.refs:
            return repo.refs[ref]
        short = f"refs/heads/{ref}"
        if short in repo.refs:
            return repo.refs[short]
        if ref == repo.default_branch:
            return repo.refs.get(f"refs/heads/{repo.default_branch}")
        if len(ref) == 40 and repo.lookup(ref) is not None:
            return ref
        return None

    def get_content(self, repo: Repo, ref: str, path: str):
        """(kind, payload): kind 'file' -> (blob_oid, bytes); 'dir' -> entries
        list of (mode, name, oid); None when the path does not exist."""
        tip = self.resolve_ref(repo, ref)
        if tip is None:
            return None
        parsed = gitobjs.parse_commit(repo.lookup(tip)[1])
        tree_oid = parsed["tree"]
        parts = [p for p in path.split("/") if p != ""]
        for i, part in enumerate(parts):
            entries = self._tree_entries(repo, tree_oid)
            match = next((e for e in entries if e[1] == part), None)
            if match is None:
                return None
            mode, _name, oid = match
            if i == len(parts) - 1:
                if mode == "40000":
                    sub = self._tree_entries(repo, oid)
                    return ("dir", [(m, n, o) for m, n, o in sub])
                found = repo.lookup(oid)
                return ("file", (oid, found[1] if found else b""))
            if mode != "40000":
                return None
            tree_oid = oid
        # path is "" (the root): list the top tree
        return ("dir", [(m, n, o) for m, n, o in self._tree_entries(repo, tree_oid)])

    def list_commits(self, repo: Repo, ref: str):
        """Commit oids reachable from ref, tip first (DFS over parents)."""
        tip = self.resolve_ref(repo, ref)
        if tip is None:
            return []
        out = []
        seen = set()
        stack = [tip]
        while stack:
            oid = stack.pop()
            if oid in seen or oid == ZERO_OID:
                continue
            found = repo.lookup(oid)
            if found is None or found[0] != "commit":
                continue
            seen.add(oid)
            out.append(oid)
            parsed = gitobjs.parse_commit(found[1])
            stack.extend(reversed(parsed["parents"]))
        return out

    # -- diff and merge ----------------------------------------------------------
    def diff_trees(self, repo: Repo, base_tree: str | None, head_tree: str | None):
        """[{filename, status, additions, deletions, changes}] base -> head."""
        base = self.flatten_tree(repo, base_tree)
        head = self.flatten_tree(repo, head_tree)
        files = []
        for path in sorted(set(base) | set(head)):
            in_base = path in base
            in_head = path in head
            if in_base and in_head and base[path][1] == head[path][1]:
                continue
            if not in_base:
                status = "added"
                old_lines, new_lines = [], self._blob_lines(repo, head[path][1])
            elif not in_head:
                status = "removed"
                old_lines, new_lines = self._blob_lines(repo, base[path][1]), []
            else:
                status = "modified"
                old_lines = self._blob_lines(repo, base[path][1])
                new_lines = self._blob_lines(repo, head[path][1])
            additions = deletions = 0
            for line in difflib.unified_diff(old_lines, new_lines, lineterm=""):
                if line.startswith("+") and not line.startswith("+++"):
                    additions += 1
                elif line.startswith("-") and not line.startswith("---"):
                    deletions += 1
            files.append({
                "filename": path, "status": status,
                "additions": additions, "deletions": deletions,
                "changes": additions + deletions,
            })
        return files

    def _blob_lines(self, repo: Repo, oid: str):
        found = repo.lookup(oid)
        if found is None:
            return []
        return found[1].decode("utf-8", "replace").splitlines()

    def three_way_merge(self, repo: Repo, base_commit: str, head_commit: str,
                        tip_commit: str):
        """Merge head into tip with base_commit as merge base.

        Returns (merged_tree_oid, conflict: bool). File-level merge: a path
        changed on both sides with different bytes is a conflict.
        """
        def tree_of(commit):
            return gitobjs.parse_commit(repo.lookup(commit)[1])["tree"]
        base = self.flatten_tree(repo, tree_of(base_commit))
        head = self.flatten_tree(repo, tree_of(head_commit))
        tip = self.flatten_tree(repo, tree_of(tip_commit))
        merged = {}
        conflict = False
        for path in sorted(set(base) | set(head) | set(tip)):
            b, h, t = base.get(path), head.get(path), tip.get(path)
            if h == b:
                if t is not None:
                    merged[path] = t
            elif t == b:
                if h is not None:
                    merged[path] = h
            elif h == t:
                if h is not None:
                    merged[path] = h
            else:
                conflict = True
        if conflict:
            return None, True
        tree_oid = None
        for path in sorted(merged):
            mode, oid = merged[path]
            tree_oid = self._update_tree(repo, tree_oid, path.split("/"), oid)
        return tree_oid or self._empty_tree(repo), False

    def merge_base(self, repo: Repo, a: str, b: str):
        """The first common ancestor of two commits (BFS from both sides)."""
        def ancestors(start):
            seen = set()
            stack = [start]
            while stack:
                oid = stack.pop()
                if oid in seen or oid == ZERO_OID:
                    continue
                found = repo.lookup(oid)
                if found is None or found[0] != "commit":
                    continue
                seen.add(oid)
                stack.extend(gitobjs.parse_commit(found[1])["parents"])
            return seen
        a_set = ancestors(a)
        stack = [b]
        seen_b = set()
        while stack:
            oid = stack.pop()
            if oid in seen_b or oid == ZERO_OID:
                continue
            if oid in a_set:
                return oid
            found = repo.lookup(oid)
            if found is None or found[0] != "commit":
                continue
            seen_b.add(oid)
            stack.extend(gitobjs.parse_commit(found[1])["parents"])
        return None

    # -- labels, issues, pull requests ------------------------------------------
    def create_label(self, repo: Repo, name: str, color: str):
        if name in repo.labels:
            return repo.labels[name]
        label = {"name": name, "color": color or "ededed"}
        repo.labels[name] = label
        return label

    def create_issue(self, repo: Repo, title: str, body: str, labels=()):
        for name in labels:
            if name not in repo.labels:
                raise StoreError(422, f"label {name} does not exist")
        number = repo.next_issue_number
        repo.next_issue_number += 1
        now = self.now_iso()
        issue = {
            "id": self.next_numeric_id(),
            "number": number,
            "title": title or "",
            "body": body or "",
            "state": "open",
            "labels": [{"name": n, "color": repo.labels[n]["color"]} for n in labels],
            "comments": [],
            "created_at": now,
            "updated_at": now,
        }
        repo.issues.append(issue)
        return issue

    def get_issue(self, repo: Repo, number: int):
        for issue in repo.issues:
            if issue["number"] == number:
                return issue
        return None

    def issue_global_id(self, repo: Repo, issue) -> str:
        return _b64(f"Issue:{repo.id}:{issue['number']}")

    def add_comment(self, repo: Repo, issue, body: str):
        comment = {
            "id": self.next_numeric_id(),
            "body": body or "",
            "created_at": self.now_iso(),
            "updated_at": self.now_iso(),
        }
        issue["comments"].append(comment)
        return comment

    def comment_global_id(self, repo: Repo, issue, comment) -> str:
        return _b64(f"IssueComment:{repo.id}:{issue['number']}:{comment['id']}")

    def create_pull(self, repo: Repo, title: str, body: str, head: str, base: str):
        head_tip = self.resolve_ref(repo, head)
        base_tip = self.resolve_ref(repo, base)
        if head_tip is None:
            raise StoreError(422, f"head branch {head} not found")
        if base_tip is None:
            raise StoreError(422, f"base branch {base} not found")
        number = repo.next_pull_number
        repo.next_pull_number += 1
        now = self.now_iso()
        pull = {
            "id": self.next_numeric_id(),
            "number": number,
            "title": title or "",
            "body": body or "",
            "state": "open",
            "merged": False,
            "merged_at": None,
            "merge_commit_sha": None,
            "head": {"ref": head, "sha": head_tip},
            "base": {"ref": base, "sha": base_tip},
            "created_at": now,
            "updated_at": now,
        }
        repo.pulls.append(pull)
        return pull

    def get_pull(self, repo: Repo, number: int):
        for pull in repo.pulls:
            if pull["number"] == number:
                return pull
        return None

    def pull_global_id(self, repo: Repo, pull) -> str:
        return _b64(f"PullRequest:{repo.id}:{pull['number']}")

    def diff_pull_files(self, repo: Repo, pull):
        """Files changed by a PR: merge-base tree -> head tree.

        Diffing against the base branch's *current* tip would leak unrelated
        commits that landed on the base after the branch point."""
        head_tip = self.resolve_ref(repo, pull["head"]["ref"])
        base_tip = self.resolve_ref(repo, pull["base"]["ref"])
        if head_tip is None or base_tip is None:
            return []
        base = self.merge_base(repo, head_tip, base_tip)
        if base is None:
            return []
        head_tree = gitobjs.parse_commit(repo.lookup(head_tip)[1])["tree"]
        base_tree = gitobjs.parse_commit(repo.lookup(base)[1])["tree"]
        return self.diff_trees(repo, base_tree, head_tree)

    def pull_mergeable(self, repo: Repo, pull) -> str:
        if pull["state"] != "open":
            return "UNKNOWN"
        head_tip = self.resolve_ref(repo, pull["head"]["ref"])
        base_tip = self.resolve_ref(repo, pull["base"]["ref"])
        if head_tip is None or base_tip is None:
            return "UNKNOWN"
        base = self.merge_base(repo, head_tip, base_tip)
        if base is None:
            return "UNKNOWN"
        _tree, conflict = self.three_way_merge(repo, base, head_tip, base_tip)
        return "CONFLICTING" if conflict else "MERGEABLE"

    def merge_pull(self, repo: Repo, pull, method: str = "squash"):
        """Squash-merge an open PR. Returns the merge commit sha.
        Raises StoreError(405) on conflict, (422) when already closed."""
        if pull["state"] != "open":
            raise StoreError(422, "pull request is not open")
        head_tip = self.resolve_ref(repo, pull["head"]["ref"])
        base_tip = self.resolve_ref(repo, pull["base"]["ref"])
        if head_tip is None or base_tip is None:
            raise StoreError(422, "head or base branch not found")
        base = self.merge_base(repo, head_tip, base_tip)
        if base is None:
            raise StoreError(422, "no merge base")
        tree, conflict = self.three_way_merge(repo, base, head_tip, base_tip)
        if conflict:
            raise StoreError(405, "merge conflict")
        identity = self._commit_identity()
        message = f"Merge pull request #{pull['number']} from {pull['head']['ref']}"
        commit = gitobjs.build_commit(tree, [base_tip], identity, identity, message)
        merge_sha = repo.put_object("commit", commit)
        repo.refs[f"refs/heads/{pull['base']['ref']}"] = merge_sha
        pull["state"] = "closed"
        pull["merged"] = True
        pull["merged_at"] = self.now_iso()
        pull["merge_commit_sha"] = merge_sha
        repo.updated_at = self.now_iso()
        return merge_sha
