"""Stand-in OpenAI-compatible LLM for offline testing. FAKE_LLM_BAD_RATE makes some replies garbage.
Run: uvicorn tests.fake_llm:app --port 9000"""
import json
import os
import random
import re

from fastapi import FastAPI, Request

app = FastAPI()
BAD_RATE = float(os.environ.get("FAKE_LLM_BAD_RATE", "0.2"))


@app.get("/v1/models")
def models():
    return {"data": [{"id": "fake"}]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    text = (await req.json())["messages"][-1]["content"]
    if "ALWAYS_BAD" in text or random.random() < BAD_RATE:
        content = "Sure! Here is the answer: category = billing-ish"
    else:
        m = re.search(r"ORD-?(\d{5})", text)
        content = "<think>hmm</think>" + json.dumps({
            "category": "Refund" if "refund" in text.lower() else "Other",
            "order_id": f"ORD-{m.group(1)}" if m else None,
            "product": None, "urgency": "Medium", "requested_action": "help"})
    return {"choices": [{"message": {"content": content}}]}
