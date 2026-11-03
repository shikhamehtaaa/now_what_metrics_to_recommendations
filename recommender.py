"""
LLM layer: turns evidence.json into a short weekly brief and answers follow-up questions.
Provider: Google Gemini (google-genai SDK).

Guardrails (this is the part to show off in the write-up):
  - The model never sees raw data, only a list of "facts" the code builds from evidence.json.
  - Every number in the model's reply must also appear in those facts (checked in code).
  - If a reply fails the check twice (or the API errors), the app falls back to bullets
    written by code, so a made-up number can never reach the merchant.
  - Questions the data cannot answer (for example "which ad campaign caused this?")
    get an honest "I can't tell from this data", plus what data would be needed.

Setup:
  pip install google-genai
  export GEMINI_API_KEY=key
  (optional) export RECS_MODEL=gemini-3.8-flash
  (optional) export RECS_FALLBACK_MODELS=gemini-3.6-flash,gemini-3.5-flash-lite

Usage:
  python recommender.py evidence.json # weekly brief
  python recommender.py evidence.json "why did revenue fall?"  # ask a question
  python recommender.py evidence.json --no-llm # code-written bullets only
"""
import json
import os
import re
import sys
import time

MODEL = os.environ.get("RECS_MODEL", "gemini-3.8-flash")
MAX_TRIES = 2
API_RETRIES = 2            # extra attempts per model for temporary errors (503 overloaded, 429 rate limit)
RETRY_WAIT = 2             # seconds; doubles each retry (2, 4)
# If the main model stays overloaded, try these in order (comma-separated; set to "" to disable).
FALLBACK_MODELS = [m.strip() for m in
                   os.environ.get("RECS_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash-lite").split(",")
                   if m.strip()]
MAX_BULLET_WORDS = 32   # prompt asks for 22; slack so we don't reject on a word or two
MAX_CHAT_WORDS = 110    # prompt asks for 80

BRIEF_SYSTEM = """You write a short weekly brief for a small online store owner who is overwhelmed by dashboards.
Rules:
- Use ONLY the facts provided. Copy every number exactly as written in the facts. Do not round, convert, add, subtract, or invent any number. If a number is not in the facts, do not use it.
- Write at most 3 bullets, each starting with "- ". No headings, no numbering, no bold or other markdown, no other text.
- Each bullet: what changed, how much it matters in dollars, and ONE concrete next step the owner can check or do in their own store.
- Maximum 22 words per bullet. Short and scannable. Plain language: never write p-value, significant, or other statistics jargon.
- Do not claim what caused a change. Phrase next steps as things to check.
- For a finding about visitor numbers (sessions), the next step is about where visitors come from (ads, search, email, promotions), not product pages. For conversion findings, the next step is about product pages, prices, or the add-to-cart experience.
- If a finding says it was not tested or may be normal variation, say it may be normal variation.
- 'unknown' means no brand was recorded. Never present it as a brand.
- Leave out findings the facts describe as too small or unreliable to act on."""

CHAT_SYSTEM = """You answer questions from a small online store owner about their weekly store numbers.
Rules:
- Use ONLY the facts provided. Copy every number exactly as written. Do not round, convert, add, subtract, or invent any number.
- Answer in at most 80 words, in plain language, with no statistics jargon and no markdown formatting.
- If the facts cannot answer the question (for example, causes such as ad campaigns, traffic sources, or promotions), say you cannot tell from this data, and name the data that would be needed.
- Do not claim what caused a change. Offer things to check instead.
- 'unknown' means no brand was recorded. Never present it as a brand."""


# ----------------------------- formatting -----------------------------
def n(x):
    return f"{round(x):,}"


def usd(x, cents=False):
    return f"${x:,.2f}" if cents else f"${round(x):,}"


def pct(x):
    return f"{abs(x) * 100:.1f}%"


def rate(x):
    return f"{x * 100:.2f}%"


def money_signed(x):
    return f"{'+' if x >= 0 else '-'}{usd(abs(x))}"


# ------------------------------- facts --------------------------------
def confidence(f):
    if f["is_significant"] is None:
        return "not tested (no baseline), may be normal weekly variation"
    return "statistically reliable" if f["is_significant"] else "not statistically reliable"


def finding_line(f):
    verb = "rose" if f["relative_change"] > 0 else "fell"
    rc = pct(f["relative_change"])
    if f["id"] == "traffic":
        main = f"Sessions with a product view {verb} {rc} ({n(f['prev'])} to {n(f['curr'])})."
    elif f["id"] == "average_order_value":
        main = f"Average order value {verb} {rc} ({usd(f['prev'], True)} to {usd(f['curr'], True)})."
    else:
        main = (f"Conversion from {f['from']} to {f['to']} {verb} {rc} in relative terms "
                f"({rate(f['prev'])} to {rate(f['curr'])}).")
    return f"- {main} Estimated revenue impact: {money_signed(f['revenue_impact'])}. Confidence: {confidence(f)}."


def segment_lines(f):
    lines = []
    for dim, rows in f.get("segments", {}).items():
        for r in rows:
            if r["segment"] == "unknown":  # no brand recorded: not actionable
                continue
            label = f"{r['segment']} ({dim.replace('_', ' ')})"
            if "rate_prev" in r:
                lines.append(f"    - {label}: conversion {rate(r['rate_prev'])} to {rate(r['rate_curr'])}")
            else:
                ch = (r["sessions_curr"] - r["sessions_prev"]) / r["sessions_prev"]
                word = "up" if ch > 0 else "down"
                lines.append(f"    - {label}: sessions {n(r['sessions_prev'])} to {n(r['sessions_curr'])} ({word} {pct(ch)})")
    return lines


def build_facts(ev):
    h, w = ev["headline"], ev["weeks"]
    rev_change = h["revenue_curr"] - h["revenue_prev"]
    t, c = h["orders_change_from_traffic"], h["orders_change_from_conversion"]
    lines = ["WEEK SUMMARY",
             f"- Compared the weeks starting {w['previous']} and {w['current']}.",
             f"- Orders {'rose' if h['orders_change'] > 0 else 'fell'} from {n(h['orders_prev'])} to "
             f"{n(h['orders_curr'])} ({n(abs(h['orders_change']))} {'more' if h['orders_change'] > 0 else 'fewer'}).",
             f"- Revenue {'rose' if rev_change > 0 else 'fell'} from {usd(h['revenue_prev'])} to "
             f"{usd(h['revenue_curr'])} ({usd(abs(rev_change))} {'more' if rev_change > 0 else 'less'}).",
             f"- About {n(abs(t))} {'more' if t > 0 else 'fewer'} orders came from visitor volume. Changes in how "
             f"often visitors buy accounted for about {n(abs(c))} {'more' if c > 0 else 'fewer'} orders "
             f"({'a material' if h['conversion_effect_is_material'] else 'not a material'} effect)."]
    top_ids = {f["id"] for f in ev["top_findings"]}
    lines.append("TOP FINDINGS (large enough to act on)")
    if not ev["top_findings"]:
        lines.append("- Nothing was large enough to need action this week.")
    for f in ev["top_findings"]:
        lines.append(finding_line(f))
        lines.extend(segment_lines(f))
    lines.append("OTHER FINDINGS (too small or unreliable to act on)")
    for f in ev["findings"]:
        if f["id"] not in top_ids:
            lines.append(finding_line(f))
    lines.append("DATA LIMITS")
    lines.extend(f"- {note}" for note in ev["data_notes"])
    return "\n".join(lines)


# ---------------------------- number check ----------------------------
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers(text):
    # strip trailing commas ("1,718," at the end of a clause) before converting
    return {round(float(t.rstrip(",").replace(",", "")), 6) for t in NUM.findall(text)}


def unsupported(reply, allowed):
    return sorted(numbers(reply) - allowed)


# ------------------------- reply cleaning -----------------------------
BULLET = re.compile(r"^\s*[-*\u2022]\s+")  # Gemini often uses "* " or "•" instead of "- "


def clean(text):
    return text.replace("**", "").replace("__", "").strip()


def parse_bullets(reply):
    return ["- " + clean(BULLET.sub("", ln)) for ln in reply.splitlines() if BULLET.match(ln)]


def words(text):
    return len(text.split())


# ----------------------------- LLM call -------------------------------
class LLMError(RuntimeError):
    pass


_client = None


def call_llm(system, user):
    """Gemini call. Returns text. Raises LLMError on setup problems or API failure."""
    global _client
    try:
        from google import genai  # imported here so --no-llm works without the package
        from google.genai import types
    except ImportError as e:
        raise LLMError("Missing package. Run: pip install google-genai") from e
    if _client is None:
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            raise LLMError("No API key found. Set GEMINI_API_KEY in your environment.")
        _client = genai.Client()  # reads GEMINI_API_KEY / GOOGLE_API_KEY from the environment
    models = [MODEL] + FALLBACK_MODELS
    last = None
    for model in models:
        for attempt in range(API_RETRIES + 1):
            try:
                resp = _client.models.generate_content(
                    model=model,
                    contents=user,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=0.2,
                        # generous: on Gemini "thinking" models, hidden thinking tokens count toward this limit
                        max_output_tokens=4096,
                    ),
                )
                return resp.text or ""  # .text is None if the reply was blocked or empty
            except Exception as e:
                last = e
                temporary = any(c in str(e) for c in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"))
                if not temporary:
                    raise LLMError(f"Gemini call failed: {e}") from e  # 404, bad key, etc: retrying won't help
                if attempt < API_RETRIES:
                    time.sleep(RETRY_WAIT * 2 ** attempt)
    raise LLMError(f"Gemini is busy right now (tried {', '.join(models)}). Last error: {last}")


# ------------------------ code-written fallback -----------------------
def short_line(f):
    verb = "rose" if f["relative_change"] > 0 else "fell"
    rc = pct(f["relative_change"])
    if f["id"] == "traffic":
        main = f"Sessions with a product view {verb} {rc} ({n(f['prev'])} to {n(f['curr'])})"
    elif f["id"] == "average_order_value":
        main = f"Average order value {verb} {rc} ({usd(f['prev'], True)} to {usd(f['curr'], True)})"
    else:
        main = f"Conversion from {f['from']} to {f['to']} {verb} {rc} ({rate(f['prev'])} to {rate(f['curr'])})"
    maybe = " May be normal variation." if f["is_significant"] is not True else ""
    return f"{main}, {money_signed(f['revenue_impact'])}.{maybe}"


def template_bullets(ev):
    bullets = []
    for f in ev["top_findings"]:
        segs = [r["segment"] for rows in f.get("segments", {}).values() for r in rows
                if r["segment"] != "unknown"][:2]
        where = f" Biggest movers: {', '.join(segs)}." if segs else ""
        if f["id"] == "traffic":
            step = "Check where visitors come from: ads, search, email."
        elif f["id"] == "average_order_value":
            step = "Check for a recent price change or bundle."
        else:
            step = "Check product pages, prices and stock."
        bullets.append("- " + short_line(f) + where + " " + step)
    return bullets[:3] or ["- Nothing was large enough to need action this week."]


# ----------------------------- brief / chat ---------------------------
def make_brief(ev, llm=call_llm):
    facts = build_facts(ev)
    allowed = numbers(facts)
    feedback, error = "", None
    for _ in range(MAX_TRIES):
        try:
            reply = llm(BRIEF_SYSTEM, f"FACTS:\n{facts}\n{feedback}\nWrite the brief.")
        except LLMError as e:
            error = str(e)
            break  # retrying a config/network error won't help
        bullets = parse_bullets(reply)
        bad = unsupported("\n".join(bullets), allowed)
        long_ = [b for b in bullets if words(b) > MAX_BULLET_WORDS]
        if bullets and len(bullets) <= 3 and not bad and not long_:
            return {"source": "llm", "bullets": bullets}
        feedback = (f"\nYour previous reply was rejected (numbers not in the facts: {bad}; "
                    f"bullet count: {len(bullets)}; bullets over 22 words: {len(long_)}). "
                    f"Use only numbers exactly as written in the facts, at most 3 bullets, "
                    f"22 words each.\n")
    return {"source": "template", "bullets": template_bullets(ev), "error": error}


def answer(question, ev, history=None, llm=call_llm):
    facts = build_facts(ev)
    allowed = numbers(facts) | numbers(question)
    convo = "".join(f"Owner: {q}\nAssistant: {a}\n" for q, a in (history or [])[-4:])
    feedback, error = "", None
    for _ in range(MAX_TRIES):
        try:
            reply = llm(CHAT_SYSTEM, f"FACTS:\n{facts}\n\n{convo}Owner: {question}\n{feedback}")
        except LLMError as e:
            error = str(e)
            break
        text = clean(reply)
        bad = unsupported(text, allowed)
        if text and not bad and words(text) <= MAX_CHAT_WORDS:
            return {"source": "llm", "text": text}
        feedback = (f"\nYour previous reply was rejected (numbers not in the facts: {bad}; "
                    f"{words(text)} words). Use only numbers exactly as written in the facts "
                    f"and at most 80 words.\n")
    intro = ("The assistant is temporarily unavailable, so I can't answer that question right now. "
             "Here is what the data shows:\n" if error else
             "I couldn't verify an answer against your numbers, so here is what the data shows:\n")
    return {"source": "template", "error": error,
            "text": intro + "\n".join(template_bullets(ev))}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    with open(sys.argv[1]) as fh:
        evidence = json.load(fh)
    args = sys.argv[2:]
    if "--no-llm" in args:
        print("\n".join(template_bullets(evidence)))
        sys.exit()
    result = answer(" ".join(args), evidence) if args else make_brief(evidence)
    print(result.get("text") or "\n".join(result["bullets"]), f"\n[source: {result['source']}]")
    if result.get("error"):
        print(f"[fallback reason: {result['error']}]")