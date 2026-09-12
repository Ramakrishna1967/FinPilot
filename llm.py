"""Claude narration layer.

Hard constraint: detection thresholds are deterministic Python (agents.py).
The LLM ONLY rephrases pre-computed numbers into plain-English finding text.
It never decides whether something is anomalous and never invents numbers.

If ANTHROPIC_API_KEY is absent/unreachable, falls back to deterministic
template text so the demo always works offline.
"""
import os

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

SYSTEM_PROMPTS = {
    "expense": (
        "You are the Expense Agent narrator for FinPilot Live, a finance monitoring team. "
        "You receive a PRE-COMPUTED anomaly (category, baseline spend, current spend, % change, top vendors). "
        "Write ONE plain-English sentence (max 30 words) stating the numbers exactly as given. "
        "Suggest a likely operational cause from the vendor names, phrased as a hypothesis ('possibly ...'). "
        "If the payload says review_flag is true, include the exact phrase 'flagged for review'. "
        "NEVER use the words fraud, fraudulent, scam, embezzlement, theft. "
        "Never invent numbers, dates, or vendors not in the payload."
    ),
    "cashflow": (
        "You are the Cash Flow Agent narrator for FinPilot Live. "
        "You receive PRE-COMPUTED cash numbers (current, projected 30d/60d, runway threshold). "
        "Write ONE plain-English sentence (max 30 words) stating current and projected cash exactly. "
        "If below threshold, say runway is below target and needs attention. Otherwise state it calmly. "
        "Never invent numbers."
    ),
    "ar": (
        "You are the Accounts Receivable narrator for FinPilot Live. "
        "You receive a PRE-COMPUTED overdue invoice (customer, amount, days late). "
        "Write ONE plain-English sentence (max 30 words) stating customer, amount and days late exactly. "
        "Recommend a polite follow-up action. Include the exact phrase 'flagged for review' when review_flag is true. "
        "NEVER use the words fraud, fraudulent, scam, theft, or accuse the customer of anything."
    ),
    "manager": (
        "You are the Finance Manager for FinPilot Live, synthesising specialist-agent findings. "
        "You receive structured findings with exact numbers. Write 2-3 plain-English sentences connecting "
        "them causally where the numbers support it (e.g. profit down mainly because of X and Y). "
        "Use ONLY the numbers provided — never invent or round beyond what is given. "
        "Separate observed flags from confirmed facts: say 'flagged for review', never claim fraud. "
        "NEVER use the words fraud, fraudulent, scam, embezzlement, theft."
    ),
    "chat": (
        "You are the FinPilot finance Q&A assistant answering questions about one company. "
        "You receive the question plus retrieved company finance records. Answer in 2-4 plain-English "
        "sentences using ONLY numbers present in the records — never invent, estimate, or round beyond "
        "what is given. Cite the figures you use. If the records do not contain the answer, say so plainly. "
        "Describe large or unusual items as 'flagged for review', never as confirmed problems. "
        "NEVER use the words fraud, fraudulent, scam, embezzlement, theft."
    ),
}

_client = None

def _get_client():
    global _client
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    if _client is None:
        try:
            from anthropic import Anthropic
            _client = Anthropic(api_key=api_key)
        except Exception:
            return None
    return _client


def _groq_complete(system: str, user: str, max_tokens: int) -> str | None:
    """Groq via its OpenAI-compatible endpoint (stdlib only). None on any failure."""
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        return None
    import json
    import urllib.request
    body = json.dumps({
        "model": os.getenv("GROQ_MODEL", GROQ_MODEL),
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode()
    try:
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=body, headers={"Content-Type": "application/json",
                                "Authorization": f"Bearer {api_key}",
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                                              "Chrome/126.0 Safari/537.36"})
        resp = urllib.request.urlopen(req, timeout=20)
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def narrate(role: str, payload: str, fallback: str, max_tokens: int = 150) -> str:
    """Groq first, Claude second, deterministic template fallback (offline-safe)."""
    text = _groq_complete(SYSTEM_PROMPTS.get(role, SYSTEM_PROMPTS["manager"]),
                          payload, max_tokens)
    if text is None:
        client = _get_client()
        if client is not None:
            try:
                msg = client.messages.create(
                    model=MODEL,
                    max_tokens=max_tokens,
                    system=SYSTEM_PROMPTS.get(role, SYSTEM_PROMPTS["manager"]),
                    messages=[{"role": "user", "content": payload}],
                )
                text = "".join(
                    b.text for b in msg.content if getattr(b, "type", "") == "text"
                ).strip() or None
            except Exception:
                text = None
    if not text:
        return fallback
    # Safety net: strip any accusatory language the model might add
    for bad in ["fraud", "fraudulent", "scam", "embezzlement", "theft", "stolen"]:
        if bad in text.lower():
            return fallback
    return text
