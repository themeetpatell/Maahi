"""Vercel connector — projects and deployments.

Wraps the Vercel REST API. Lists projects and deployments and reads a single
deployment's status for the daily brief, and can re-deploy a previous
deployment (``publish`` — promotes a build, gated by the autonomy policy).

Auth is a bearer token. An optional ``VERCEL_TEAM_ID`` scopes every request to
a team via the ``teamId`` query parameter; without it, requests run against the
token owner's personal account.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import Capability, Connector, ConnectorResult

_API_BASE = "https://api.vercel.com"


class VercelConnector(Connector):
    """Connector for a Vercel account's projects and deployments."""

    key = "vercel"
    label = "Vercel"
    required_env = ("VERCEL_TOKEN",)
    blurb = (
        "Set VERCEL_TOKEN to a token created at "
        "https://vercel.com/account/tokens. Optionally set VERCEL_TEAM_ID to "
        "scope every request to a specific team."
    )

    # ---- helpers ----

    def _client(self) -> httpx.Client:
        """Build a Vercel REST API client with bearer auth."""
        return httpx.Client(
            base_url=_API_BASE,
            headers={
                "Authorization": f"Bearer {self.env('VERCEL_TOKEN')}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(20.0, connect=8.0),
        )

    def _params(self, **extra: Any) -> dict[str, Any]:
        """Merge caller params with the optional teamId scope.

        Drops any keys whose value is ``None``/empty so we never send blank
        query params, and appends ``teamId`` when VERCEL_TEAM_ID is set.
        """
        params: dict[str, Any] = {
            k: v for k, v in extra.items() if v is not None and v != ""
        }
        team_id = self.env("VERCEL_TEAM_ID")
        if team_id:
            params["teamId"] = team_id
        return params

    @staticmethod
    def _int(value: Any, default: int) -> int:
        """Coerce a param to int, falling back to ``default`` on garbage."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # ---- capabilities ----

    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability(
                "list_projects",
                "List projects in the account/team.",
                {"limit": "int: default 20"},
                "read",
            ),
            Capability(
                "list_deployments",
                "List recent deployments, optionally for one project.",
                {"limit": "int: default 20", "project_id": "str: optional"},
                "read",
            ),
            Capability(
                "deployment_status",
                "Get full status for a single deployment.",
                {"deployment_id": "str"},
                "read",
            ),
            Capability(
                "redeploy",
                "Re-deploy a previous deployment by reference. Makes live.",
                {"deployment_id": "str"},
                "publish",
            ),
        )

    # ---- operations ----

    def op_list_projects(self, limit: int = 20) -> ConnectorResult:
        """List projects, newest first (API default order)."""
        lim = max(1, min(self._int(limit, 20), 100))
        try:
            with self._client() as c:
                r = c.get("/v9/projects", params=self._params(limit=lim))
                r.raise_for_status()
                projects = r.json().get("projects", [])
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Vercel list_projects failed: {e}")
        if not isinstance(projects, list):
            projects = []
        return ConnectorResult.success(f"{len(projects)} projects", data=projects)

    def op_list_deployments(
        self, limit: int = 20, project_id: str = ""
    ) -> ConnectorResult:
        """List recent deployments, optionally filtered to one project."""
        lim = max(1, min(self._int(limit, 20), 100))
        pid = str(project_id or "").strip()
        try:
            with self._client() as c:
                r = c.get(
                    "/v6/deployments",
                    params=self._params(limit=lim, projectId=pid),
                )
                r.raise_for_status()
                deployments = r.json().get("deployments", [])
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Vercel list_deployments failed: {e}")
        if not isinstance(deployments, list):
            deployments = []
        slim = [
            {
                "uid": d.get("uid"),
                "name": d.get("name"),
                "state": d.get("state"),
                "readyState": d.get("readyState"),
                "url": d.get("url"),
                "created": d.get("created") or d.get("createdAt"),
            }
            for d in deployments
            if isinstance(d, dict)
        ]
        return ConnectorResult.success(f"{len(slim)} deployments", data=slim)

    def op_deployment_status(self, deployment_id: str = "") -> ConnectorResult:
        """Get the full record for a single deployment."""
        did = str(deployment_id or "").strip()
        if not did:
            return ConnectorResult.fail(
                "Vercel deployment_status: deployment_id required"
            )
        try:
            with self._client() as c:
                r = c.get(f"/v13/deployments/{did}", params=self._params())
                r.raise_for_status()
                deployment = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Vercel deployment_status failed: {e}")
        state = (
            deployment.get("readyState") or deployment.get("state")
            if isinstance(deployment, dict) else None
        )
        return ConnectorResult.success(
            f"Deployment {did}: {state or 'unknown'}", data=deployment
        )

    def op_redeploy(self, deployment_id: str = "") -> ConnectorResult:
        """Re-deploy a prior deployment (best effort, makes the build live).

        Reads the source deployment for its ``name`` then POSTs a new
        deployment referencing it via ``deploymentId`` — Vercel's documented
        "redeploy from an existing deployment" shape.
        """
        did = str(deployment_id or "").strip()
        if not did:
            return ConnectorResult.fail("Vercel redeploy: deployment_id required")
        try:
            with self._client() as c:
                src = c.get(f"/v13/deployments/{did}", params=self._params())
                src.raise_for_status()
                src_json = src.json()
                name = src_json.get("name") if isinstance(src_json, dict) else None
                if not name:
                    return ConnectorResult.fail(
                        f"Vercel redeploy: source deployment {did} has no name"
                    )
                r = c.post(
                    "/v13/deployments",
                    params=self._params(),
                    json={"name": name, "deploymentId": did},
                )
                r.raise_for_status()
                deployment = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Vercel redeploy failed: {e}")
        new_id = deployment.get("id") or deployment.get("uid") \
            if isinstance(deployment, dict) else None
        return ConnectorResult.success(
            f"Redeploy of {did} triggered ({name})"
            + (f" -> {new_id}" if new_id else ""),
            data=deployment,
        )

    # ---- brief hooks ----

    def health(self) -> ConnectorResult:
        """Cheap auth check via the current-user endpoint."""
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        try:
            with self._client() as c:
                r = c.get("/v2/user", params=self._params())
                r.raise_for_status()
                payload = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Vercel auth failed: {e}")
        user = payload.get("user") if isinstance(payload, dict) else None
        if not user:
            return ConnectorResult.fail("Vercel auth failed: no user in response")
        who = user.get("username") or user.get("email") or user.get("uid")
        return ConnectorResult.success(f"Vercel: connected as {who}")

    def pulse(self) -> ConnectorResult:
        """Deployment health snapshot for the morning brief.

        One API call: count the last 20 deployments by state so the brief can
        flag failures and in-flight builds at a glance.
        """
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        r = self.op_list_deployments(limit=20)
        if not r.ok:
            return r
        deployments = r.data or []
        ready = building = failed = 0
        for d in deployments:
            state = str(
                (d.get("readyState") or d.get("state") or "")
            ).upper()
            if state == "READY":
                ready += 1
            elif state in ("BUILDING", "QUEUED", "INITIALIZING"):
                building += 1
            elif state in ("ERROR", "CANCELED", "CANCELLED"):
                failed += 1
        summary = (
            f"{len(deployments)} deploys: {ready} ready, "
            f"{building} building, {failed} failed"
        )
        return ConnectorResult.success(
            summary,
            data={
                "deployments": len(deployments),
                "ready": ready,
                "building": building,
                "failed": failed,
            },
        )
