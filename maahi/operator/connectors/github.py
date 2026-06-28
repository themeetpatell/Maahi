"""GitHub connector — repos, pull requests, issues, and CI checks.

Wraps the GitHub REST API (v2022-11-28). Reads repos / PRs / issues and CI
check status for the daily brief, opens issues and posts comments (reversible
internal ``write``), and can merge a pull request (``publish`` — makes the
change live on the default branch, gated by the autonomy policy).

Auth is a bearer token (a fine-grained or classic PAT). An optional
``GITHUB_OWNER`` provides a default org/user for convenience; it is not
required for any operation that already takes an explicit ``owner/name`` repo.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import Capability, Connector, ConnectorResult

_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"


class GitHubConnector(Connector):
    """Connector for a GitHub account's repos, PRs, issues, and checks."""

    key = "github"
    label = "GitHub"
    required_env = ("GITHUB_TOKEN",)
    blurb = (
        "Set GITHUB_TOKEN to a fine-grained personal access token created at "
        "https://github.com/settings/tokens with repository + pull-request "
        "scopes. Optionally set GITHUB_OWNER to a default org/user."
    )

    # ---- helpers ----

    def _client(self) -> httpx.Client:
        """Build a GitHub REST API client with bearer auth + API version."""
        return httpx.Client(
            base_url=_API_BASE,
            headers={
                "Authorization": f"Bearer {self.env('GITHUB_TOKEN')}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION,
            },
            timeout=httpx.Timeout(20.0, connect=8.0),
        )

    @staticmethod
    def _int(value: Any, default: int) -> int:
        """Coerce a param to int, falling back to ``default`` on garbage."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _repo(self, repo: str) -> str:
        """Normalize a repo ref, filling in GITHUB_OWNER for a bare name."""
        ref = str(repo or "").strip().strip("/")
        if ref and "/" not in ref:
            owner = self.env("GITHUB_OWNER")
            if owner:
                ref = f"{owner}/{ref}"
        return ref

    # ---- capabilities ----

    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability(
                "list_repos",
                "List repositories you can access.",
                {"limit": "int: default 20", "sort": "str: default updated"},
                "read",
            ),
            Capability(
                "list_prs",
                "List pull requests in a repo.",
                {
                    "repo": "str: owner/name",
                    "state": "str: default open",
                    "limit": "int: default 20",
                },
                "read",
            ),
            Capability(
                "list_issues",
                "List issues in a repo (pull requests excluded).",
                {
                    "repo": "str: owner/name",
                    "state": "str: default open",
                    "limit": "int: default 20",
                },
                "read",
            ),
            Capability(
                "pr_checks",
                "Summarize CI check-run pass/fail for a pull request.",
                {"repo": "str: owner/name", "pr_number": "int"},
                "read",
            ),
            Capability(
                "create_issue",
                "Open a new issue in a repo. Reversible (can be closed).",
                {
                    "repo": "str: owner/name",
                    "title": "str",
                    "body": "str: optional",
                },
                "write",
            ),
            Capability(
                "comment_issue",
                "Add a comment to an issue or pull request. Reversible.",
                {"repo": "str: owner/name", "number": "int", "body": "str"},
                "write",
            ),
            Capability(
                "merge_pr",
                "Merge a pull request into its base branch. Makes it live.",
                {
                    "repo": "str: owner/name",
                    "pr_number": "int",
                    "method": "str: merge|squash|rebase default squash",
                },
                "publish",
            ),
        )

    # ---- operations ----

    def op_list_repos(
        self, limit: int = 20, sort: str = "updated"
    ) -> ConnectorResult:
        """List repos accessible to the token, newest activity first."""
        per_page = max(1, min(self._int(limit, 20), 100))
        sort_by = str(sort or "updated").strip() or "updated"
        try:
            with self._client() as c:
                r = c.get(
                    "/user/repos",
                    params={"sort": sort_by, "per_page": per_page},
                )
                r.raise_for_status()
                repos = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"GitHub list_repos failed: {e}")
        if not isinstance(repos, list):
            repos = []
        slim = [
            {
                "full_name": repo.get("full_name"),
                "private": repo.get("private"),
                "updated_at": repo.get("updated_at"),
                "open_issues_count": repo.get("open_issues_count"),
            }
            for repo in repos
            if isinstance(repo, dict)
        ]
        return ConnectorResult.success(f"{len(slim)} repos", data=slim)

    def op_list_prs(
        self, repo: str = "", state: str = "open", limit: int = 20
    ) -> ConnectorResult:
        """List pull requests in a repo, filtered by state."""
        ref = self._repo(repo)
        if not ref:
            return ConnectorResult.fail("GitHub list_prs: repo (owner/name) required")
        per_page = max(1, min(self._int(limit, 20), 100))
        st = str(state or "open").strip() or "open"
        try:
            with self._client() as c:
                r = c.get(
                    f"/repos/{ref}/pulls",
                    params={"state": st, "per_page": per_page},
                )
                r.raise_for_status()
                prs = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"GitHub list_prs failed: {e}")
        if not isinstance(prs, list):
            prs = []
        return ConnectorResult.success(
            f"{len(prs)} {st} PRs in {ref}", data=prs
        )

    def op_list_issues(
        self, repo: str = "", state: str = "open", limit: int = 20
    ) -> ConnectorResult:
        """List issues in a repo, excluding pull requests."""
        ref = self._repo(repo)
        if not ref:
            return ConnectorResult.fail("GitHub list_issues: repo (owner/name) required")
        per_page = max(1, min(self._int(limit, 20), 100))
        st = str(state or "open").strip() or "open"
        try:
            with self._client() as c:
                r = c.get(
                    f"/repos/{ref}/issues",
                    params={"state": st, "per_page": per_page},
                )
                r.raise_for_status()
                raw = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"GitHub list_issues failed: {e}")
        if not isinstance(raw, list):
            raw = []
        # The issues endpoint also returns PRs; drop anything carrying a
        # "pull_request" key so this is issues-only.
        issues = [
            it for it in raw
            if isinstance(it, dict) and "pull_request" not in it
        ]
        return ConnectorResult.success(
            f"{len(issues)} {st} issues in {ref}", data=issues
        )

    def op_pr_checks(self, repo: str = "", pr_number: int = 0) -> ConnectorResult:
        """Summarize CI check-runs for a PR's head commit (pass/fail counts)."""
        ref = self._repo(repo)
        if not ref:
            return ConnectorResult.fail("GitHub pr_checks: repo (owner/name) required")
        num = self._int(pr_number, 0)
        if num <= 0:
            return ConnectorResult.fail("GitHub pr_checks: pr_number required")
        try:
            with self._client() as c:
                pr = c.get(f"/repos/{ref}/pulls/{num}")
                pr.raise_for_status()
                head_sha = (pr.json().get("head") or {}).get("sha")
                if not head_sha:
                    return ConnectorResult.fail(
                        f"GitHub pr_checks: no head sha for PR #{num} in {ref}"
                    )
                cr = c.get(f"/repos/{ref}/commits/{head_sha}/check-runs")
                cr.raise_for_status()
                check_runs = cr.json().get("check_runs", [])
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"GitHub pr_checks failed: {e}")
        if not isinstance(check_runs, list):
            check_runs = []
        passed = failed = pending = 0
        for run in check_runs:
            if not isinstance(run, dict):
                continue
            status = str(run.get("status") or "").lower()
            conclusion = str(run.get("conclusion") or "").lower()
            if status != "completed":
                pending += 1
            elif conclusion in ("success", "neutral", "skipped"):
                passed += 1
            else:  # failure, cancelled, timed_out, action_required, stale…
                failed += 1
        total = len(check_runs)
        summary = (
            f"PR #{num} checks: {passed}/{total} passed, "
            f"{failed} failed, {pending} pending"
        )
        return ConnectorResult.success(
            summary,
            data={
                "sha": head_sha,
                "total": total,
                "passed": passed,
                "failed": failed,
                "pending": pending,
            },
        )

    def op_create_issue(
        self, repo: str = "", title: str = "", body: str = ""
    ) -> ConnectorResult:
        """Open a new issue. Reversible — it can always be closed."""
        ref = self._repo(repo)
        if not ref:
            return ConnectorResult.fail("GitHub create_issue: repo (owner/name) required")
        ttl = str(title or "").strip()
        if not ttl:
            return ConnectorResult.fail("GitHub create_issue: title required")
        payload: dict[str, Any] = {"title": ttl}
        body_text = str(body or "").strip()
        if body_text:
            payload["body"] = body_text
        try:
            with self._client() as c:
                r = c.post(f"/repos/{ref}/issues", json=payload)
                r.raise_for_status()
                issue = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"GitHub create_issue failed: {e}")
        number = issue.get("number") if isinstance(issue, dict) else None
        return ConnectorResult.success(
            f"Issue #{number} created in {ref}", data=issue
        )

    def op_comment_issue(
        self, repo: str = "", number: int = 0, body: str = ""
    ) -> ConnectorResult:
        """Add a comment to an issue or PR. Reversible."""
        ref = self._repo(repo)
        if not ref:
            return ConnectorResult.fail("GitHub comment_issue: repo (owner/name) required")
        num = self._int(number, 0)
        if num <= 0:
            return ConnectorResult.fail("GitHub comment_issue: number required")
        text = str(body or "").strip()
        if not text:
            return ConnectorResult.fail("GitHub comment_issue: body required")
        try:
            with self._client() as c:
                r = c.post(
                    f"/repos/{ref}/issues/{num}/comments", json={"body": text}
                )
                r.raise_for_status()
                comment = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"GitHub comment_issue failed: {e}")
        return ConnectorResult.success(
            f"Comment added to {ref}#{num}", data=comment
        )

    def op_merge_pr(
        self, repo: str = "", pr_number: int = 0, method: str = "squash"
    ) -> ConnectorResult:
        """Merge a pull request into its base branch (makes the change live)."""
        ref = self._repo(repo)
        if not ref:
            return ConnectorResult.fail("GitHub merge_pr: repo (owner/name) required")
        num = self._int(pr_number, 0)
        if num <= 0:
            return ConnectorResult.fail("GitHub merge_pr: pr_number required")
        m = str(method or "squash").strip().lower()
        if m not in ("merge", "squash", "rebase"):
            m = "squash"
        try:
            with self._client() as c:
                r = c.put(
                    f"/repos/{ref}/pulls/{num}/merge",
                    json={"merge_method": m},
                )
                r.raise_for_status()
                result = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"GitHub merge_pr failed: {e}")
        merged = bool(result.get("merged")) if isinstance(result, dict) else False
        return ConnectorResult.success(
            f"PR #{num} merged ({m}) in {ref}" if merged
            else f"PR #{num} merge requested in {ref}",
            data=result,
        )

    # ---- brief hooks ----

    def health(self) -> ConnectorResult:
        """Cheap auth check via the authenticated-user endpoint."""
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        try:
            with self._client() as c:
                r = c.get("/user")
                r.raise_for_status()
                login = r.json().get("login")
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"GitHub auth failed: {e}")
        if not login:
            return ConnectorResult.fail("GitHub auth failed: no login in response")
        return ConnectorResult.success(f"GitHub: connected as {login}")

    def pulse(self) -> ConnectorResult:
        """Repo count + most-recently-updated repo for the morning brief.

        Kept to a single API call: the repo list (sorted by recent activity)
        already carries each repo's ``open_issues_count``, so we can total
        open issues without per-repo fan-out.
        """
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        r = self.op_list_repos(limit=10, sort="updated")
        if not r.ok:
            return r
        repos = r.data or []
        most_recent = repos[0].get("full_name") if repos else None
        open_issues = sum(
            int(repo.get("open_issues_count") or 0) for repo in repos
        )
        if repos:
            summary = (
                f"{len(repos)} repos, most recent: {most_recent} "
                f"({open_issues} open issues across them)"
            )
        else:
            summary = "0 repos"
        return ConnectorResult.success(
            summary,
            data={
                "repos": len(repos),
                "most_recent": most_recent,
                "open_issues": open_issues,
            },
        )
