"""
OpenAI-compatible HTTP API wrapper around OpenManus Manus agent.
Runs on 0.0.0.0:8000 via uvicorn.
"""
import uuid
import time
import json
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="OpenManus API")

# Patch the Manus system prompt at import time so the agent knows about the
# shared noVNC browser environment the user can see at vnc.designflow.app
try:
    import app.prompt.manus as manus_prompt
    manus_prompt.SYSTEM_PROMPT = (
        "You are OpenManus, an all-capable AI assistant, aimed at solving any task "
        "presented by the user. You have various tools at your disposal that you can "
        "call upon to efficiently complete complex requests.\n"
        "The initial directory is: {directory}\n\n"
        "IMPORTANT — Browser & noVNC setup:\n"
        "You have full control of a real Chromium browser running in a shared noVNC "
        "desktop. The user can see and interact with the same browser at "
        "https://vnc.designflow.app.\n\n"
        "RULES YOU MUST FOLLOW:\n"
        "1. Whenever you first use the browser on a task, immediately tell the user: "
        "'I have opened the browser. You can watch and interact with it at https://vnc.designflow.app'\n"
        "2. When you need the user to log in, navigate to the login page first, then "
        "say: 'Please log in at https://vnc.designflow.app — I will wait and continue "
        "once you are logged in.'\n"
        "3. Never claim you cannot open a browser or access a website. Always use "
        "your browser tools proactively.\n"
        "4. Never claim you cannot see the screen — you have browser tools to extract "
        "content and take screenshots.\n"
        "5. When a page is a JavaScript single-page application (SPA) or content is not "
        "visible in raw HTML, take a screenshot and use browser_evaluate to run JavaScript "
        "to extract the rendered content. Never give up after one HTML extraction attempt.\n"
        "6. When downloading files or documents, use the browser to navigate and click "
        "download buttons directly. Check screenshots to confirm what is actually rendered."
    )
    logger.info("Manus system prompt patched with noVNC browser info")
except Exception as e:
    logger.warning(f"Could not patch Manus system prompt: {e}")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    import os, httpx
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://openrouter.ai/api/v1/models/user",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.warning(f"Failed to fetch OpenRouter models: {e}")
    return {
        "object": "list",
        "data": [
            {
                "id": "manus",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "openmanus",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
        messages = body.get("messages", [])
        stream = body.get("stream", False)
        requested_model = body.get("model")
        requested_temperature = body.get("temperature")
        requested_max_tokens = body.get("max_tokens")

        prompt = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"),
            "",
        )
        if not prompt:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "No user message found", "type": "invalid_request_error"}},
            )

        logger.info(
            f"Running Manus agent | model={requested_model} "
            f"stream={stream} temperature={requested_temperature} "
            f"max_tokens={requested_max_tokens} | prompt={prompt[:100]}..."
        )

        from app.agent.manus import Manus
        from app.llm import LLM
        from app.schema import Message

        llm = LLM()
        if requested_model and requested_model not in ("manus", "openmanus"):
            llm.model = requested_model
        if requested_temperature is not None:
            llm.temperature = float(requested_temperature)
        if requested_max_tokens is not None:
            llm.max_tokens = int(requested_max_tokens)

        prior_messages = []
        for msg in messages[:-1]:
            role = msg.get("role", "user")
            content = msg.get("content") or ""
            if role == "user":
                prior_messages.append(Message.user_message(content))
            elif role == "assistant":
                prior_messages.append(Message.assistant_message(content))
            elif role == "system":
                prior_messages.append(Message.system_message(content))

        agent = await Manus.create()
        agent.max_steps = 100
        if prior_messages:
            agent.messages = prior_messages

        try:
            result = await agent.run(prompt)
        finally:
            await agent.cleanup()

        if hasattr(result, "content"):
            answer = result.content
        elif isinstance(result, str):
            answer = result
        else:
            answer = str(result)

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model_id = requested_model or "manus"

        try:
            prompt_tokens = llm.count_tokens(" ".join(m.get("content", "") or "" for m in messages))
            completion_tokens = llm.count_tokens(answer or "")
        except Exception:
            prompt_tokens = len(prompt.split())
            completion_tokens = len(answer.split()) if answer else 0

        if stream:
            async def event_stream():
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"

                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {"content": answer}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"

                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    except Exception as e:
        logger.error(f"Error in chat_completions: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(e), "type": "server_error"}},
        )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
