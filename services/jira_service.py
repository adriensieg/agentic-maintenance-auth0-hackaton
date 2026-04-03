"""
services/jira_service.py
─────────────────────────
Jira ticket operations (create, update, transition, comment).

Token chain:
  1. Auth0 Management token (client-credentials, M2M app).
  2. Read Atlassian refresh token from Auth0 user identity (Token Vault).
  3. Exchange with Atlassian for a fresh access token.
  4. Use token against Jira Cloud REST API v3.

The Atlassian refresh token is rotated on every use (Auth0 vault stores
the updated token).  The access token is discarded after the API call.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from auth.token_vault import token_vault
from config.settings import get_settings
from models import JiraTicket, TicketStatus

logger = logging.getLogger("washfix.services.jira")


class JiraService:

    def __init__(self) -> None:
        self._settings = get_settings()

    # ── Token acquisition ─────────────────────────────────────────────────

    async def _get_jira_token(self) -> str:
        """
        Three-step chain:
          1. Auth0 Management API token.
          2. Atlassian refresh_token from Auth0 vault (stored on user identity).
          3. Exchange with Atlassian for a fresh access_token.
        Returns the Jira access token.
        """
        s = self._settings
        logger.info("Jira token chain: starting.")

        async with httpx.AsyncClient(timeout=15) as client:

            # Step 1 — Management token
            logger.info("Jira [1/3] Fetching Auth0 management token.")
            r = await client.post(s.auth0_token_url, json={
                "grant_type":    "client_credentials",
                "client_id":     s.auth0_mgmt_client_id,
                "client_secret": s.auth0_mgmt_client_secret,
                "audience":      f"https://{s.auth0_domain}/api/v2/",
            })
            r.raise_for_status()
            mgmt_token = r.json()["access_token"]
            logger.info("Jira [1/3] Management token obtained.")

            # Step 2 — Atlassian refresh token from vault
            from urllib.parse import quote
            encoded = quote(s.jira_auth0_user_id, safe="")
            logger.info(f"Jira [2/3] Fetching vault user {s.jira_auth0_user_id}.")
            r = await client.get(
                f"https://{s.auth0_domain}/api/v2/users/{encoded}",
                headers={"Authorization": f"Bearer {mgmt_token}"},
            )
            r.raise_for_status()
            user = r.json()
            identities = user.get("identities", [])
            jira_id = next(
                (i for i in identities
                 if "atlassian" in i.get("connection", "").lower()
                 or "jira" in i.get("connection", "").lower()),
                identities[0] if identities else None,
            )
            if not jira_id or not jira_id.get("refresh_token"):
                raise RuntimeError(
                    "No Jira/Atlassian refresh token in Auth0 vault. "
                    "Re-authenticate via Auth0 → Social → Jira connection."
                )
            refresh_token = jira_id["refresh_token"]
            logger.info("Jira [2/3] Refresh token found.")

            # Step 3 — Exchange with Atlassian
            logger.info("Jira [3/3] Exchanging refresh token with Atlassian.")
            r = await client.post(
                "https://auth.atlassian.com/oauth/token",
                json={
                    "grant_type":    "refresh_token",
                    "client_id":     s.jira_client_id,
                    "client_secret": s.jira_client_secret,
                    "refresh_token": refresh_token,
                },
            )
            r.raise_for_status()
            access_token: str = r.json()["access_token"]
            logger.info("Jira [3/3] Access token obtained.")

        return access_token

    # ── CRUD ──────────────────────────────────────────────────────────────

    async def list_tickets(self, max_results: int = 50) -> list[JiraTicket]:
        s = self._settings
        token = await self._get_jira_token()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{s.jira_api_base}/search/jql",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={
                    "jql":        f'project = "{s.jira_project_key}" ORDER BY created DESC',
                    "maxResults": max_results,
                    "fields":     "summary,status,issuetype,assignee",
                },
            )
            r.raise_for_status()

        issues = r.json().get("issues", [])
        tickets = []
        for i in issues:
            f = i["fields"]
            tickets.append(JiraTicket(
                key      = i["key"],
                url      = f"{s.jira_browse_base}/{i['key']}",
                summary  = f["summary"],
                status   = TicketStatus(f["status"]["name"]) if f["status"]["name"] in TicketStatus._value2member_map_ else TicketStatus.OPEN,
                assignee = f["assignee"]["displayName"] if f.get("assignee") else None,
            ))
        logger.info(f"Jira list_tickets: {len(tickets)} tickets returned.")
        return tickets

    async def get_ticket(self, key: str) -> JiraTicket:
        s = self._settings
        token = await self._get_jira_token()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{s.jira_api_base}/issue/{key.upper()}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            r.raise_for_status()

        d = r.json()
        f = d["fields"]
        return JiraTicket(
            key         = d["key"],
            url         = f"{s.jira_browse_base}/{d['key']}",
            summary     = f["summary"],
            status      = TicketStatus(f["status"]["name"]) if f["status"]["name"] in TicketStatus._value2member_map_ else TicketStatus.OPEN,
            assignee    = f["assignee"]["displayName"] if f.get("assignee") else None,
            description = str(f.get("description") or ""),
        )

    async def create_ticket(
        self,
        summary: str,
        description: str = "",
        issue_type: str = "Task",
        priority: str = "High",
        labels: Optional[list[str]] = None,
        custom_fields: Optional[dict[str, Any]] = None,
    ) -> JiraTicket:
        """
        Create a new Jira ticket with rich description.
        Returns the created JiraTicket.
        """
        s = self._settings
        token = await self._get_jira_token()

        fields: dict[str, Any] = {
            "project":   {"key": s.jira_project_key},
            "summary":   summary,
            "issuetype": {"name": issue_type},
            "priority":  {"name": priority},
            "description": {
                "type":    "doc",
                "version": 1,
                "content": [
                    {
                        "type":    "paragraph",
                        "content": [{"type": "text", "text": description}],
                    }
                ],
            },
        }
        if labels:
            fields["labels"] = labels
        if custom_fields:
            fields.update(custom_fields)

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{s.jira_api_base}/issue",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept":        "application/json",
                    "Content-Type":  "application/json",
                },
                json={"fields": fields},
            )
            r.raise_for_status()

        key = r.json()["key"]
        url = f"{s.jira_browse_base}/{key}"
        logger.info(f"Jira ticket created: {key} — {summary}")
        return JiraTicket(key=key, url=url, summary=summary)

    async def add_comment(self, key: str, comment: str) -> None:
        """Append an audit comment to an existing ticket."""
        s = self._settings
        token = await self._get_jira_token()
        payload = {
            "body": {
                "type":    "doc",
                "version": 1,
                "content": [
                    {
                        "type":    "paragraph",
                        "content": [{"type": "text", "text": comment}],
                    }
                ],
            }
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{s.jira_api_base}/issue/{key}/comment",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept":        "application/json",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
        logger.info(f"Jira comment added to {key}.")

    async def transition_ticket(
        self,
        key: str,
        status_name: str,
    ) -> None:
        """Move a ticket to a new status (e.g. 'In Progress', 'Done')."""
        s = self._settings
        token = await self._get_jira_token()

        # First, list available transitions
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{s.jira_api_base}/issue/{key}/transitions",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            r.raise_for_status()

        transitions = r.json().get("transitions", [])
        target = next(
            (t for t in transitions if t["name"].lower() == status_name.lower()),
            None,
        )
        if not target:
            available = [t["name"] for t in transitions]
            logger.warning(f"Transition '{status_name}' not found. Available: {available}")
            return

        token2 = await self._get_jira_token()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{s.jira_api_base}/issue/{key}/transitions",
                headers={
                    "Authorization": f"Bearer {token2}",
                    "Accept":        "application/json",
                    "Content-Type":  "application/json",
                },
                json={"transition": {"id": target["id"]}},
            )
            r.raise_for_status()
        logger.info(f"Jira ticket {key} transitioned to '{status_name}'.")

    async def create_repair_ticket(
        self,
        session_id: str,
        user_name: str,
        unit: str,
        address: str,
        appliance_model: str,
        fault_code: str,
        part_number: str,
        technician_name: str,
        arrival_window: str,
    ) -> JiraTicket:
        """
        High-level convenience: create a fully detailed repair service ticket.
        Includes all audit-relevant fields.
        """
        summary = f"[REPAIR] {appliance_model} — {fault_code} — {unit}"
        description = (
            f"Repair Service Request\n"
            f"{'='*40}\n"
            f"Session ID   : {session_id}\n"
            f"Resident     : {user_name}\n"
            f"Unit         : {unit}\n"
            f"Address      : {address}\n"
            f"Appliance    : {appliance_model}\n"
            f"Fault Code   : {fault_code}\n"
            f"Part Required: {part_number}\n"
            f"Technician   : {technician_name}\n"
            f"Arrival      : {arrival_window}\n"
            f"{'='*40}\n"
            f"Created by WashFix AI Agent (automated).\n"
        )
        ticket = await self.create_ticket(
            summary     = summary,
            description = description,
            issue_type  = "Task",
            priority    = "High",
            labels      = ["appliance-repair", "automated", fault_code.lower()],
        )
        logger.info(f"Repair ticket created: {ticket.key}")
        return ticket


# Singleton
jira_service = JiraService()
