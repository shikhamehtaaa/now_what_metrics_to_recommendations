"""App tests with a fake LLM. Run: python3 test_app.py"""
import json
import app as a
import recommender as r

GOOD = ("- Sessions with a product view fell 6.4% (209,653 to 196,277), about -$18,338. Check where visitors come from.\n"
        "- Average order value rose 3.4% ($59.77 to $61.81), +$9,236. May be normal variation.\n"
        "- Conversion from view to cart fell 2.4% (17.11% to 16.70%), -$6,529. Check product pages.")
c = a.app.test_client()

def fake_ok(system, user):
    return GOOD if "brief" in user.lower().split("facts:")[-1][-40:] else "I can't tell which ad caused it from this data. You'd need traffic-source data."
r.call_llm = fake_ok
for fn in ("make_brief", "answer"):  # default arg binds at definition time, so patch it
    getattr(r, fn).__defaults__ = (r.call_llm,) if fn == "make_brief" else (None, r.call_llm)

def test_page_and_brief():
    assert b"Now What?" in c.get("/").data
    d = c.get("/api/brief").get_json()
    assert d["source"] == "llm" and len(d["bullets"]) == 3 and not d["bullets"][0].startswith("-"), d
    assert d["headline"]["orders"]["curr"] == "4,530" and d["notice"] is None

def test_chat_ok_and_validation():
    d = c.post("/api/chat", json={"question": "which ad campaign caused this?"}).get_json()
    assert d["source"] == "llm" and "can't tell" in d["text"], d
    assert c.post("/api/chat", json={"question": "  "}).status_code == 400
    assert c.post("/api/chat", json={"question": "x" * 301}).status_code == 400
    assert c.post("/api/chat", data="not json").status_code == 400

def test_api_down_is_friendly_and_hides_raw_error():
    def down(system, user): raise r.LLMError("503 UNAVAILABLE secret details")
    r.make_brief.__defaults__ = (down,); r.answer.__defaults__ = (None, down)
    a._cache.clear()
    d = c.get("/api/brief").get_json()
    assert d["source"] == "template" and "unavailable" in d["notice"] and "secret" not in json.dumps(d), d
    d = c.post("/api/chat", json={"question": "why?"}).get_json()
    assert d["source"] == "template" and "secret" not in json.dumps(d), d

def test_rate_limit_chat():
    r.answer.__defaults__ = (None, lambda s, u: "I can't tell from this data.")
    a._hits.clear()
    codes = [c.post("/api/chat", json={"question": "hi"}).status_code for _ in range(22)]
    assert codes[:20] == [200] * 20 and codes[20] == 429, codes

if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"): f(); print("ok  ", k)
