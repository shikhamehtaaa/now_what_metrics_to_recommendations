"""
Local web app for weekly brief and chat.
"""
import collections
import json
import os
import time

from flask import Flask, jsonify, request, send_from_directory

import recommender as rec

EVIDENCE_PATH = os.environ.get("EVIDENCE_PATH", "evidence.json")
MAX_QUESTION_CHARS = 300

# Public-site protection: every chat/refresh costs API quota, so cap it.
LIMITS = {"chat": (20, 3600), "refresh": (5, 3600)}  # (requests, per seconds) per visitor
GLOBAL_CHAT_PER_DAY = 300  # whole-site ceiling
_hits = collections.defaultdict(list)


def too_many(kind):
    # Behind a host's proxy, the LAST X-Forwarded-For entry is the address the proxy saw.
    ip = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip() or request.remote_addr
    limit, window = LIMITS[kind]
    now = time.time()
    keep = [t for t in _hits[(kind, ip)] if now - t < window]
    if len(keep) >= limit:
        _hits[(kind, ip)] = keep
        return True
    if kind == "chat":
        day = [t for t in _hits[("chat", "ALL")] if now - t < 86400]
        if len(day) >= GLOBAL_CHAT_PER_DAY:
            return True
        _hits[("chat", "ALL")] = day + [now]
    _hits[(kind, ip)] = keep + [now]
    return False


app = Flask(__name__, static_folder="static", static_url_path="/static")
_cache = {}  # the brief is cached so reloading the page doesn't re-call the model


def load_evidence():
    with open(EVIDENCE_PATH) as fh:
        return json.load(fh)


def friendly(error):
    """Never show raw API errors to the merchant; log them for the developer instead."""
    if error:
        print(f"[assistant fallback] {error}", flush=True)
        return "The AI assistant is unavailable right now, so this was written by code from your numbers."
    return None


def headline(ev):
    h = ev["headline"]
    return {
        "orders": {"prev": rec.n(h["orders_prev"]), "curr": rec.n(h["orders_curr"]),
                   "change": h["orders_curr"] - h["orders_prev"]},
        "revenue": {"prev": rec.usd(h["revenue_prev"]), "curr": rec.usd(h["revenue_curr"]),
                    "change": h["revenue_curr"] - h["revenue_prev"]},
        "conversion": {"prev": rec.rate(h["overall_conversion_prev"]), "curr": rec.rate(h["overall_conversion_curr"]),
                       "change": h["overall_conversion_curr"] - h["overall_conversion_prev"]},
    }


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/brief")
def brief():
    ev = load_evidence()
    if "brief" not in _cache or (request.args.get("refresh") and not too_many("refresh")):
        _cache["brief"] = rec.make_brief(ev)
    b = _cache["brief"]
    return jsonify({
        "weeks": ev["weeks"],
        "headline": headline(ev),
        "bullets": [x[2:] if x.startswith("- ") else x for x in b["bullets"]],
        "source": b["source"],
        "notice": friendly(b.get("error")),
        "data_notes": ev["data_notes"],
    })


@app.post("/api/chat")
def chat():
    if too_many("chat"):
        return jsonify({"error": "You've asked a lot of questions. Please try again later."}), 429
    data = request.get_json(silent=True) or {}
    question = str(data.get("question", "")).strip()
    if not question:
        return jsonify({"error": "Type a question first."}), 400
    if len(question) > MAX_QUESTION_CHARS:
        return jsonify({"error": f"Keep questions under {MAX_QUESTION_CHARS} characters."}), 400
    history = [(str(q), str(a)) for q, a in (data.get("history") or [])[-4:]]
    result = rec.answer(question, load_evidence(), history=history)
    return jsonify({"text": result["text"], "source": result["source"],
                    "notice": friendly(result.get("error"))})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)