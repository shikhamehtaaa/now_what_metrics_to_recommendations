"""Offline tests: fake LLMs stand in for Gemini. Run: python test_recommender.py"""
import json
import recommender as r

ev = json.load(open("evidence.json"))
facts = r.build_facts(ev)
GOOD = ("- Sessions with a product view fell 6.4% (209,653 to 196,277), about -$18,338. May be normal variation; check your traffic sources.\n"
        "- Average order value rose 3.4% ($59.77 to $61.81), +$9,236. Check whether a recent price change or bundle explains it.\n"
        "- Conversion from view to cart fell 2.4% (17.11% to 16.70%), -$6,529. Check product page changes in price Q2 items.")

def seq(*replies):
    it = iter(replies)
    def f(system, user):
        v = next(it)
        if isinstance(v, Exception): raise v
        return v
    return f

def test_facts_have_numbers():
    for s in ["209,653", "196,277", "$18,338", "6.4%", "$59.77", "17.11%"]:
        assert s in facts, s

def test_good_reply():
    out = r.make_brief(ev, llm=seq(GOOD)); assert out["source"] == "llm" and len(out["bullets"]) == 3, out

def test_gemini_star_bullets_and_bold():
    reply = "Here is your brief:\n" + GOOD.replace("- ", "* ").replace("Sessions", "**Sessions**")
    out = r.make_brief(ev, llm=seq(reply))
    assert out["source"] == "llm" and "**" not in "".join(out["bullets"]) and out["bullets"][0].startswith("- "), out

def test_invented_number_retries_then_ok():
    bad = GOOD.replace("$18,338", "$18,400")
    out = r.make_brief(ev, llm=seq(bad, GOOD)); assert out["source"] == "llm"

def test_invented_number_twice_falls_back():
    bad = GOOD.replace("$18,338", "$18,400")
    out = r.make_brief(ev, llm=seq(bad, bad)); assert out["source"] == "template" and out["bullets"]

def test_too_many_bullets_falls_back():
    out = r.make_brief(ev, llm=seq(GOOD + "\n- extra", GOOD + "\n- extra")); assert out["source"] == "template"

def test_too_long_bullet_falls_back():
    long = "- " + "word " * 60
    out = r.make_brief(ev, llm=seq(long, long)); assert out["source"] == "template"

def test_api_error_falls_back_with_reason():
    out = r.make_brief(ev, llm=seq(r.LLMError("quota"))); assert out["source"] == "template" and out["error"] == "quota"

def test_chat_ok_and_hallucination():
    ok = "Orders fell from 4,809 to 4,530, mostly because fewer people visited. I can't tell which ad or promotion is behind that from this data."
    assert r.answer("why did orders fall?", ev, llm=seq(ok))["source"] == "llm"
    bad = ok.replace("4,530", "4,500")
    out = r.answer("why did orders fall?", ev, llm=seq(bad, bad)); assert out["source"] == "template"

def test_chat_allows_numbers_from_question():
    out = r.answer("what if I raise prices 10%?", ev, llm=seq("I can't tell what a 10% price rise would do from this data."))
    assert out["source"] == "llm", out

def test_template_never_empty_and_nothing_material():
    assert r.template_bullets(ev)
    ev2 = dict(ev, top_findings=[]); assert "Nothing" in r.template_bullets(ev2)[0]
    assert r.build_facts(ev2)

def test_aa_missing_package_or_key_is_clean_error():
    out = r.make_brief(ev)  # real call_llm, no network/package/key here
    assert out["source"] == "template" and out["error"], out

def test_chat_api_error_message_is_honest():
    out = r.answer("why?", ev, llm=seq(r.LLMError("busy")))
    assert "temporarily unavailable" in out["text"] and out["error"] == "busy", out

def test_retries_on_503_then_succeeds():
    import types, sys
    calls = {"n": 0}
    class Models:
        def generate_content(self, **kw):
            calls["n"] += 1
            if calls["n"] < 3: raise Exception("503 UNAVAILABLE")
            return types.SimpleNamespace(text="ok")
    fake_genai = types.ModuleType("google.genai"); fake_genai.Client = lambda: None
    fake_types = types.ModuleType("google.genai.types"); fake_types.GenerateContentConfig = lambda **k: k
    fake_genai.types = fake_types
    google = types.ModuleType("google"); google.genai = fake_genai
    sys.modules.update({"google": google, "google.genai": fake_genai, "google.genai.types": fake_types})
    r._client = types.SimpleNamespace(models=Models()); r.RETRY_WAIT = 0
    assert r.call_llm("s", "u") == "ok" and calls["n"] == 3
    # model 1 stays overloaded, model 2 works: should switch models
    used = []
    def two(**kw):
        used.append(kw["model"])
        if kw["model"] == r.MODEL: raise Exception("503 UNAVAILABLE")
        return types.SimpleNamespace(text="from fallback")
    r._client.models.generate_content = two
    assert r.call_llm("s", "u") == "from fallback" and used[-1] == r.FALLBACK_MODELS[0] and used.count(r.MODEL) == r.API_RETRIES + 1, used
    calls["n"] = -99  # always fails: should give up with a clear error
    def always(**kw): raise Exception("503 UNAVAILABLE")
    r._client.models.generate_content = always
    try: r.call_llm("s", "u"); assert False
    except r.LLMError as e: assert "busy" in str(e)
    r._client.models.generate_content = lambda **kw: (_ for _ in ()).throw(Exception("404 NOT_FOUND"))
    try: r.call_llm("s", "u"); assert False
    except r.LLMError as e: assert "404" in str(e) and "busy" not in str(e)
    r._client = None

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok  ", name)
