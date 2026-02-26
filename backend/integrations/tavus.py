"""
Tavus Integration — AI Conversational Video Avatar
Step 6: EHR knowledge base → Tavus persona → conversation URL

Tavus creates a lifelike video avatar that patients can talk to.
The avatar is seeded with the patient's specific EHR data via the system prompt.

Docs: https://docs.tavus.io/api-reference
"""

import os
from typing import Any, Dict, Optional

import httpx


class TavusClient:
    BASE_URL = "https://tavusapi.com/v2"

    def __init__(self) -> None:
        self.api_key    = os.getenv("TAVUS_API_KEY")
        self.replica_id = os.getenv("TAVUS_REPLICA_ID")   # Your licensed avatar replica

    async def create_conversation(
        self,
        patient_id: str,
        knowledge_base: Dict[str, Any],
    ) -> Dict[str, Optional[str]]:
        """
        1. Creates a Tavus Persona seeded with patient EHR data.
        2. Creates a Conversation tied to that Persona.

        Returns:
          {
            "persona_id":       str,
            "conversation_id":  str,
            "conversation_url": str,  # Embed in patient dashboard iframe
          }
        """
        if not self.api_key or not self.replica_id:
            print("[Tavus] TAVUS_API_KEY or TAVUS_REPLICA_ID not set — skipping avatar.")
            return {"persona_id": None, "conversation_id": None, "conversation_url": None}

        from prompts.avatar import build_avatar_system_prompt

        system_prompt = build_avatar_system_prompt(knowledge_base.get("ehr_summary", {}))

        async with httpx.AsyncClient(timeout=45.0) as client:
            # ── Step A: Create persona ──────────────────────
            persona_resp = await client.post(
                f"{self.BASE_URL}/personas",
                headers=self._headers(),
                json={
                    "persona_name": f"CareGuide_{patient_id}",
                    "system_prompt": system_prompt,
                    "replica_id":   self.replica_id,
                    # Tavus context window — attach voice script + battlecard text
                    "context": (
                        knowledge_base.get("voice_script", "")[:4000]
                    ),
                },
            )
            persona_resp.raise_for_status()
            persona_id = persona_resp.json()["persona_id"]

            # ── Step B: Create conversation ─────────────────
            conv_resp = await client.post(
                f"{self.BASE_URL}/conversations",
                headers=self._headers(),
                json={
                    "replica_id":        self.replica_id,
                    "persona_id":        persona_id,
                    "conversation_name": f"care_guide_{patient_id}",
                    "properties": {
                        "max_call_duration":      3600,   # 1 hour max
                        "participant_left_timeout": 60,
                        "enable_recording":        False,  # HIPAA: do not record
                    },
                },
            )
            conv_resp.raise_for_status()
            conv_data = conv_resp.json()

        return {
            "persona_id":       persona_id,
            "conversation_id":  conv_data.get("conversation_id"),
            "conversation_url": conv_data.get("conversation_url"),
        }

    # ── Private ──────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "x-tavus-api-key": self.api_key,
            "Content-Type":    "application/json",
        }
