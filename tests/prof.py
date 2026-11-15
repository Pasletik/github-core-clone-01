"""Profile the hot paths: v2 fetch (depth 1, 200-commit repo) and REST GET issue.

Builds a 200-commit repo in-process (like the bench clone setup), then
times + cProfiles the fetch handler and a REST issue read.
"""

from __future__ import annotations

import cProfile
import io
import os
import pstats
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forge import gitobjs, gitproto, rest, store as store_mod


def build_repo(st, n_commits=200):
    repo = st.create_repo("adaiversen", "bench-clone", True, "")
    # 200 commits, each changing one 1 KiB file, like the bench setup.
    for i in range(n_commits):
        content = (f"commit {i}\n".encode() + b"x" * 1000)
        st.commit_file(repo, "main", "file.txt", content, f"commit {i}")
    return repo


def main():
    st = store_mod.Store(token="t")
    repo = build_repo(st)
    tip = st.resolve_ref(repo, "main")
    print("commits:", len(st.list_commits(repo, "main")), "objects:", len(repo.objects))

    # Simulate a v2 depth-1 fetch request (what a shallow clone sends).
    lines = [
        "command=fetch",
        "agent=git/2.43.0",
        "object-format=sha1",
        None,  # delim
        "thin-pack",
        "no-progress",
        "ofs-delta",
        f"want {tip}",
        "deepen 1",
        "done",
    ]

    # Warm up, then time 50 sequential fetches.
    gitproto._fetch_v2(repo, lines)
    start = time.perf_counter()
    for _ in range(50):
        gitproto._fetch_v2(repo, lines)
    elapsed = time.perf_counter() - start
    print(f"fetch depth-1 x50: {elapsed*1000:.1f} ms ({elapsed*20:.1f} ms each)")

    pr = cProfile.Profile()
    pr.enable()
    for _ in range(50):
        gitproto._fetch_v2(repo, lines)
    pr.disable()
    out = io.StringIO()
    pstats.Stats(pr, stream=out).sort_stats("cumulative").print_stats(18)
    print(out.getvalue())

    # REST read: create an issue, then time GET /issues/{n}.
    issue = st.create_issue(repo, "bench issue", "body", [])
    req = store_mod  # noqa
    from forge.server import Request

    def get_issue():
        r = Request("GET", f"/api/v3/repos/adaiversen/bench-clone/issues/{issue['number']}",
                    ["api", "v3", "repos", "adaiversen", "bench-clone", "issues",
                     str(issue["number"])],
                    {}, {"authorization": "Bearer t"}, b"")
        return rest.dispatch(st, r)

    get_issue()
    start = time.perf_counter()
    for _ in range(2000):
        get_issue()
    elapsed = time.perf_counter() - start
    print(f"GET issue x2000: {elapsed*1000:.1f} ms ({elapsed*0.5:.3f} ms each)")

    pr = cProfile.Profile()
    pr.enable()
    for _ in range(2000):
        get_issue()
    pr.disable()
    out = io.StringIO()
    pstats.Stats(pr, stream=out).sort_stats("cumulative").print_stats(15)
    print(out.getvalue())


if __name__ == "__main__":
    main()
