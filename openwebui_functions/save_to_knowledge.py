"""
OpenWebUI Action Function: Save to Knowledge
============================================
Install via OpenWebUI Admin → Functions → + (New Function).
Paste the entire contents of this file.

When a user clicks the "Save to Knowledge" button that appears on AI messages,
this action:
  1. Takes the current conversation history.
  2. Calls the chat API asking the AI to write a structured knowledge piece
     summarizing what it just did / explained.
  3. Saves the result as a file in the "OpenManus Knowledge" collection
     (created automatically on first use).

Valves (Admin → Functions → gear icon):
  - knowledge_collection_name: Name of the Knowledge collection (default: "OpenManus Knowledge")
  - base_url: Internal OpenWebUI URL for API calls (default: "http://localhost:8080")
  - max_context_messages: How many recent messages to include (default: 10)
"""

import time
import httpx
from pydantic import BaseModel, Field


class Action:

    # ---------------------------------------------------------------------- #
    # Valves                                                                  #
    # ---------------------------------------------------------------------- #

    class Valves(BaseModel):
        knowledge_collection_name: str = Field(
            default="OpenManus Knowledge",
            description="Name of the Knowledge collection to save pieces into. Created automatically if it doesn't exist.",
        )
        base_url: str = Field(
            default="http://localhost:8080",
            description="Internal base URL of the OpenWebUI instance (used for API calls).",
        )
        max_context_messages: int = Field(
            default=10,
            description="Number of recent messages to include when asking the AI to write the knowledge piece.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ---------------------------------------------------------------------- #
    # Private helpers                                                         #
    # ---------------------------------------------------------------------- #

    def _auth_headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _get_or_create_knowledge(self, base_url: str, headers: dict, name: str) -> str | None:
        """Return the ID of the named knowledge collection, creating it if needed."""
        try:
            r = httpx.get(f"{base_url}/api/v1/knowledge/", headers=headers, timeout=10)
            r.raise_for_status()
            for col in r.json():
                if col.get("name") == name:
                    return col["id"]
            # Not found — create it
            r2 = httpx.post(
                f"{base_url}/api/v1/knowledge/create",
                headers=headers,
                json={
                    "name": name,
                    "description": "Automatically saved knowledge pieces from OpenManus chats.",
                },
                timeout=10,
            )
            r2.raise_for_status()
            return r2.json()["id"]
        except Exception as e:
            return None

    def _save_text_to_knowledge(
        self,
        base_url: str,
        headers: dict,
        knowledge_id: str,
        filename: str,
        content: str,
    ) -> bool:
        """Upload text as a file and attach it to a knowledge collection."""
        try:
            # Step 1 — upload file
            upload_headers = {k: v for k, v in headers.items() if k != "Content-Type"}
            r = httpx.post(
                f"{base_url}/api/v1/files/",
                headers=upload_headers,
                files={"file": (filename, content.encode("utf-8"), "text/plain")},
                timeout=30,
            )
            r.raise_for_status()
            file_id = r.json()["id"]

            # Step 2 — update content so RAG can index it
            r2 = httpx.post(
                f"{base_url}/api/v1/files/{file_id}/data/content/update",
                headers=headers,
                json={"content": content},
                timeout=30,
            )
            r2.raise_for_status()

            # Step 3 — attach to knowledge collection
            r3 = httpx.post(
                f"{base_url}/api/v1/knowledge/{knowledge_id}/file/add",
                headers=headers,
                json={"file_id": file_id},
                timeout=30,
            )
            r3.raise_for_status()
            return True
        except Exception:
            return False

    def _generate_knowledge_text(
        self,
        base_url: str,
        headers: dict,
        model: str,
        conversation: list[dict],
    ) -> str | None:
        """Ask the AI to write a knowledge piece based on the conversation."""
        system_message = {
            "role": "system",
            "content": (
                "You are a knowledge base curator. Your task is to read the provided "
                "conversation and write a concise, well-structured knowledge piece that "
                "captures the key information, steps, decisions, or insights. "
                "Use this format:\n\n"
                "# [Descriptive Title]\n\n"
                "**Summary:** One or two sentences describing what this covers.\n\n"
                "## Key Points\n"
                "- ...\n\n"
                "## Details\n"
                "(Include any important steps, commands, code snippets, or decisions.)\n\n"
                "Be specific and factual. Do not include conversational filler. "
                "The output will be saved directly to a knowledge base."
            ),
        }
        messages = [system_message] + conversation + [
            {
                "role": "user",
                "content": (
                    "Based on the conversation above, write a knowledge piece following "
                    "the format I described. Output only the knowledge piece — no preamble."
                ),
            }
        ]
        try:
            r = httpx.post(
                f"{base_url}/api/chat/completions",
                headers=headers,
                json={"model": model, "messages": messages, "stream": False},
                timeout=120,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception:
            return None

    # ---------------------------------------------------------------------- #
    # Action entry point                                                      #
    # ---------------------------------------------------------------------- #

    async def action(
        self,
        body: dict,
        __user__: dict | None = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> None:
        """
        Triggered when the user clicks the "Save to Knowledge" button on an AI message.
        body["messages"] holds the full conversation; body["model"] is the active model.
        """

        async def emit(description: str, done: bool = False):
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": description, "done": done}}
                )

        # ------------------------------------------------------------------ #
        # Extract context                                                     #
        # ------------------------------------------------------------------ #
        user_token = (__user__ or {}).get("token") or (__user__ or {}).get("id", "")
        model = body.get("model", "")
        all_messages = body.get("messages", [])

        # Use only the most recent N messages for context
        n = self.valves.max_context_messages
        recent_messages = all_messages[-n:] if len(all_messages) > n else all_messages

        # Trim to only role/content fields (remove any extra OpenWebUI metadata)
        clean_messages = [
            {"role": m["role"], "content": m.get("content") or ""}
            for m in recent_messages
            if m.get("role") in ("user", "assistant")
        ]

        if not clean_messages:
            await emit("No conversation found to summarize.", done=True)
            return

        base_url = self.valves.base_url
        headers = self._auth_headers(user_token)

        # ------------------------------------------------------------------ #
        # Step 1: Generate knowledge text via chat API                        #
        # ------------------------------------------------------------------ #
        await emit("Writing knowledge piece…")
        knowledge_text = self._generate_knowledge_text(base_url, headers, model, clean_messages)

        if not knowledge_text or not knowledge_text.strip():
            await emit("AI returned an empty response. Nothing saved.", done=True)
            return

        # ------------------------------------------------------------------ #
        # Step 2: Find / create knowledge collection                          #
        # ------------------------------------------------------------------ #
        await emit("Saving to Knowledge base…")
        collection_name = self.valves.knowledge_collection_name
        knowledge_id = self._get_or_create_knowledge(base_url, headers, collection_name)

        if not knowledge_id:
            await emit(
                f"Could not access knowledge collection '{collection_name}'. "
                "Check base_url valve and that Knowledge is enabled.",
                done=True,
            )
            return

        # ------------------------------------------------------------------ #
        # Step 3: Upload                                                       #
        # ------------------------------------------------------------------ #
        first_line = knowledge_text.strip().splitlines()[0].lstrip("#").strip()[:60]
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in first_line).strip()
        filename = f"{time.strftime('%Y%m%d_%H%M%S')}_{safe_name or 'knowledge'}.txt"

        ok = self._save_text_to_knowledge(base_url, headers, knowledge_id, filename, knowledge_text)

        if ok:
            await emit(f"Saved to '{collection_name}' → '{filename}'", done=True)
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "notification",
                        "data": {
                            "type": "success",
                            "content": f"Knowledge piece saved to '{collection_name}'",
                        },
                    }
                )
        else:
            await emit(
                "Upload failed. Check that the Knowledge API is reachable and the token is valid.",
                done=True,
            )
