"""Validate results.json against output_schema.json and the input messages. Stdlib only.
Usage: python check_results.py results.json ../final_round/for_candidates/practice_messages.json"""
import json
import re
import sys

results = json.load(open(sys.argv[1], encoding="utf-8"))
inputs = json.load(open(sys.argv[2], encoding="utf-8"))
ENUMS = {"status": {"completed", "failed", "rejected"},
         "category": {"Billing", "Technical issue", "Account access", "Refund", "Other", None},
         "urgency": {"Low", "Medium", "High", None}}
REQUIRED = ["message_id", "status", "category", "order_id", "product", "urgency", "requested_action",
            "attempts", "failure_reason"]
errors = []
ids = [r.get("message_id") for r in results]
if len(ids) != len(set(ids)):
    errors.append("duplicate message_id entries")
if set(ids) != {m["message_id"] for m in inputs}:
    errors.append(f"ids differ from input: missing {sorted({m['message_id'] for m in inputs} - set(ids))}, "
                  f"extra {sorted(set(ids) - {m['message_id'] for m in inputs})}")
for r in results:
    mid = r.get("message_id")
    errors += [f"{mid}: missing {k}" for k in REQUIRED if k not in r]
    errors += [f"{mid}: bad {k} {r.get(k)!r}" for k, ok in ENUMS.items() if r.get(k) not in ok]
    if r.get("order_id") is not None and not re.fullmatch(r"ORD-\d{5}", r["order_id"]):
        errors.append(f"{mid}: bad order_id {r['order_id']!r}")
    if not isinstance(r.get("attempts"), int) or r["attempts"] < 0:
        errors.append(f"{mid}: bad attempts")
    for k in ("product", "requested_action", "failure_reason"):
        if r.get(k) is not None and not isinstance(r[k], str):
            errors.append(f"{mid}: {k} not a string")
    if r.get("status") == "completed" and (r.get("category") is None or r.get("urgency") is None):
        errors.append(f"{mid}: completed without category/urgency")
print("\n".join(errors) or f"OK: {len(results)} entries, one per input message_id, all match output_schema.json")
sys.exit(1 if errors else 0)
