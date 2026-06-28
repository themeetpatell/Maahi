"""Supabase connector — projects, health, edge functions, and SQL.

Wraps the Supabase Management API (https://api.supabase.com). Lists the
projects on the account, checks per-project service health, enumerates edge
functions, and runs arbitrary SQL against a project's database. The SQL
endpoint can mutate data, so ``run_query`` is tagged ``write`` for the
autonomy policy.

Auth is a Management API personal access token in the ``Authorization``
bearer header.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import Capability, Connector, ConnectorResult

_API_BASE = "https://api.supabase.com"


class SupabaseConnector(Connector):
    """Connector for a Supabase account's projects via the Management API."""

    key = "supabase"
    label = "Supabase"
    required_env = ("SUPABASE_ACCESS_TOKEN",)
    blurb = (
        "Set SUPABASE_ACCESS_TOKEN to a Supabase Management API personal "
        "access token. Create one at "
        "https://supabase.com/dashboard/account/tokens."
    )

    # ---- helpers ----

    def _client(self) -> httpx.Client:
        """Build a Supabase Management API client with bearer auth."""
        return httpx.Client(
            base_url=_API_BASE,
            headers={
                "Authorization": f"Bearer {self.env('SUPABASE_ACCESS_TOKEN')}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(20.0, connect=8.0),
        )

    # ---- capabilities ----

    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability(
                "list_projects",
                "List all Supabase projects on the account.",
                {},
                "read",
            ),
            Capability(
                "project_health",
                "Summarize the health of a project's services.",
                {"project_ref": "str"},
                "read",
            ),
            Capability(
                "list_functions",
                "List edge functions deployed to a project.",
                {"project_ref": "str"},
                "read",
            ),
            Capability(
                "run_query",
                "Run SQL against a project's database. Can mutate data.",
                {"project_ref": "str", "query": "str: SQL"},
                "write",
            ),
        )

    # ---- operations ----

    def op_list_projects(self) -> ConnectorResult:
        """List every project on the account with id/name/region/status."""
        try:
            with self._client() as c:
                r = c.get("/v1/projects")
                r.raise_for_status()
                projects = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Supabase list_projects failed: {e}")
        if not isinstance(projects, list):
            projects = []
        return ConnectorResult.success(f"{len(projects)} projects", data=projects)

    def op_project_health(self, project_ref: str = "") -> ConnectorResult:
        """Summarize healthy/unhealthy services for a project.

        Tries the per-project health endpoint; if it is unavailable (404),
        falls back to the project list and reports the project's status.
        """
        ref = str(project_ref).strip()
        if not ref:
            return ConnectorResult.fail("Supabase project_health: project_ref required")
        try:
            with self._client() as c:
                r = c.get(
                    f"/v1/projects/{ref}/health",
                    params={
                        "services": "auth,db,pooler,realtime,rest,storage",
                    },
                )
                if r.status_code == 404:
                    return self._health_fallback(ref)
                r.raise_for_status()
                services = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Supabase project_health failed: {e}")
        if not isinstance(services, list):
            services = []
        healthy = [s for s in services if str(s.get("healthy")).lower() == "true" or s.get("healthy") is True]
        unhealthy = [s for s in services if s not in healthy]
        bad_names = [str(s.get("name", "?")) for s in unhealthy]
        if unhealthy:
            summary = (
                f"{ref}: {len(healthy)}/{len(services)} services healthy "
                f"(unhealthy: {', '.join(bad_names)})"
            )
        else:
            summary = f"{ref}: all {len(services)} services healthy"
        return ConnectorResult.success(
            summary,
            data={
                "project_ref": ref,
                "services": services,
                "healthy": len(healthy),
                "unhealthy": len(unhealthy),
            },
        )

    def _health_fallback(self, ref: str) -> ConnectorResult:
        """Report a project's status from the project list when /health 404s."""
        listed = self.op_list_projects()
        if not listed.ok:
            return listed
        projects = listed.data or []
        match = next(
            (p for p in projects if str(p.get("id")) == ref or str(p.get("ref")) == ref),
            None,
        )
        if match is None:
            return ConnectorResult.fail(
                f"Supabase project_health: project {ref} not found"
            )
        status = str(match.get("status", "UNKNOWN"))
        return ConnectorResult.success(
            f"{ref}: status {status}",
            data={"project_ref": ref, "status": status, "project": match},
        )

    def op_list_functions(self, project_ref: str = "") -> ConnectorResult:
        """List edge functions deployed to a project."""
        ref = str(project_ref).strip()
        if not ref:
            return ConnectorResult.fail("Supabase list_functions: project_ref required")
        try:
            with self._client() as c:
                r = c.get(f"/v1/projects/{ref}/functions")
                r.raise_for_status()
                functions = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Supabase list_functions failed: {e}")
        if not isinstance(functions, list):
            functions = []
        return ConnectorResult.success(
            f"{len(functions)} edge functions", data=functions
        )

    def op_run_query(self, project_ref: str = "", query: str = "") -> ConnectorResult:
        """Run SQL against a project's database and return the rows."""
        ref = str(project_ref).strip()
        if not ref:
            return ConnectorResult.fail("Supabase run_query: project_ref required")
        sql = str(query).strip()
        if not sql:
            return ConnectorResult.fail("Supabase run_query: query required")
        try:
            with self._client() as c:
                r = c.post(
                    f"/v1/projects/{ref}/database/query",
                    json={"query": sql},
                )
                r.raise_for_status()
                rows = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Supabase run_query failed: {e}")
        count = len(rows) if isinstance(rows, list) else 1
        return ConnectorResult.success(f"{count} rows", data=rows)

    # ---- brief hooks ----

    def health(self) -> ConnectorResult:
        """Cheap auth/reachability check via the projects list."""
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        try:
            with self._client() as c:
                c.get("/v1/projects").raise_for_status()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Supabase auth failed: {e}")
        return ConnectorResult.success("Supabase: connected")

    def pulse(self) -> ConnectorResult:
        """Project count + names/status for the morning brief."""
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        r = self.op_list_projects()
        if not r.ok:
            return r
        projects = r.data or []
        bits = [
            f"{p.get('name', p.get('id', '?'))} ({p.get('status', '?')})"
            for p in projects[:5]
        ]
        shown = ", ".join(bits) + ("…" if len(projects) > 5 else "")
        return ConnectorResult.success(
            f"{len(projects)} Supabase projects: {shown}" if projects
            else "0 Supabase projects",
            data={"projects": len(projects)},
        )
