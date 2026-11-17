import json, sys
for line in open(sys.argv[1]):
    try: e = json.loads(line)
    except Exception: continue
    t = e.get("type")
    if t == "system" and e.get("subtype") == "init":
        print("INIT tools:", [x for x in e.get("tools", [])], "mcp:", e.get("mcp_servers"))
    if t == "assistant":
        for c in e["message"]["content"]:
            if c["type"] == "tool_use": print(">> CALL", c["name"], json.dumps(c["input"], ensure_ascii=False)[:600])
            elif c["type"] == "text" and c["text"].strip(): print(".. SAYS", c["text"].strip()[:400])
    if t == "user":
        for c in e["message"]["content"]:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                cont = c.get("content"); s = json.dumps(cont, ensure_ascii=False) if not isinstance(cont, str) else cont
                print("<< RESULT", s[:700])
    if t == "result":
        print("RESULT", e.get("subtype"), "turns", e.get("num_turns"), "cost", e.get("total_cost_usd"), "ms", e.get("duration_ms"))
