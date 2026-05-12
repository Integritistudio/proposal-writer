"""
Proposal generation engine: reads docx brain, detects tech stacks, calls LLM, follows strict instructions.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from collections import Counter

import psycopg2
from psycopg2.extras import RealDictCursor, Json

from config import (
    DOCS_DIR,
    WINNING_PROPOSALS_DOC,
    PORTFOLIO_DOC,
    QUICK_PHRASES_DOC,
    PROPOSALS_LOG_PATH,
    RELEVANT_EXAMPLES_LOG_PATH,
    PROPOSAL_RATINGS_PATH,
    TECH_URLS,
    CLAUDE_MODEL,
)

# Minimum distinct portfolio projects to name in each generated proposal.
MIN_PORTFOLIO_EXAMPLES_IN_PROPOSAL = 3
# Characters of each doc injected into the prompt.
PORTFOLIO_PROMPT_CHAR_LIMIT = 14000
WINNING_PROMPT_CHAR_LIMIT = 12000
QUICK_PHRASES_PROMPT_CHAR_LIMIT = 10000
TOP_WINNING_SNIPPETS = 10
TOP_PORTFOLIO_SNIPPETS = 20
TOP_QUICK_PHRASES_SNIPPETS = 15


def _tokenize(text: str) -> list:
    """Lowercase tokenization for lightweight relevance scoring."""
    if not text:
        return []
    return re.findall(r"[a-z0-9][a-z0-9\+\.\-#]*", text.lower())


def _split_text_into_snippets(text: str, lines_per_snippet: int = 4) -> list:
    """
    Split plain text into snippets.
    Uses non-empty lines and groups them to preserve local context.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    snippets = []
    for i in range(0, len(lines), lines_per_snippet):
        chunk = " ".join(lines[i : i + lines_per_snippet]).strip()
        if chunk:
            snippets.append(chunk)
    return snippets


def _score_snippet_for_job(snippet: str, job_tokens: set, tech_tokens: set, job_counter: Counter) -> float:
    """
    Score a snippet against the job post.
    - token overlap
    - weighted matches for repeated job terms
    - bonus for explicit tech tokens
    """
    if not snippet:
        return 0.0
    snippet_tokens = _tokenize(snippet)
    if not snippet_tokens:
        return 0.0
    snippet_token_set = set(snippet_tokens)
    overlap = len(snippet_token_set & job_tokens)
    weighted_overlap = sum(job_counter.get(t, 0) for t in snippet_token_set)
    tech_overlap = len(snippet_token_set & tech_tokens)
    return float(overlap) + (0.15 * weighted_overlap) + (1.2 * tech_overlap)


def get_top_relevant_snippets(
    source_text: str,
    job_post: str,
    tech_stacks: dict,
    max_snippets: int,
    char_limit: int,
    lines_per_snippet: int = 4,
) -> str:
    """
    Return the highest-scoring snippets from source_text for this job.
    Keeps prompt focused by sending only matched context.
    """
    snippets = _split_text_into_snippets(source_text, lines_per_snippet=lines_per_snippet)
    if not snippets:
        return ""

    job_tokens_raw = _tokenize(job_post or "")
    if not job_tokens_raw:
        # If we cannot score, return a small leading section as fallback.
        fallback = "\n".join(f"- {s}" for s in snippets[:max_snippets])
        return fallback[:char_limit]

    job_counter = Counter(job_tokens_raw)
    job_tokens = set(job_tokens_raw)
    tech_tokens = set()
    for k in tech_stacks.keys():
        tech_tokens.update(_tokenize(k))

    scored = []
    for idx, snippet in enumerate(snippets):
        score = _score_snippet_for_job(snippet, job_tokens, tech_tokens, job_counter)
        if score > 0:
            scored.append((score, idx, snippet))

    # If no overlap was found, still provide a short fallback.
    if not scored:
        fallback = "\n".join(f"- {s}" for s in snippets[:max_snippets])
        return fallback[:char_limit]

    # Sort by score desc, preserve original order for ties.
    scored.sort(key=lambda x: (-x[0], x[1]))
    selected = [s for _, _, s in scored[:max_snippets]]
    out = "\n".join(f"- {s}" for s in selected)
    return out[:char_limit]


def read_docx(path: Path) -> str:
    """Read a .docx file into plain text."""
    try:
        import docx
    except ImportError:
        raise ImportError("Install python-docx: pip install python-docx")
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    doc = docx.Document(str(path))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paragraphs)


def load_brain():
    """Load winning proposals, portfolio, and quick phrases from disk. Always reads latest."""
    winning_path = DOCS_DIR / WINNING_PROPOSALS_DOC
    portfolio_path = DOCS_DIR / PORTFOLIO_DOC
    quick_phrases_path = DOCS_DIR / QUICK_PHRASES_DOC
    winning_text = read_docx(winning_path)
    portfolio_text = read_docx(portfolio_path)
    # Quick phrases doc is optional — don't hard-fail if missing
    quick_phrases_text = ""
    if quick_phrases_path.exists():
        try:
            quick_phrases_text = read_docx(quick_phrases_path)
        except Exception:
            quick_phrases_text = ""
    return winning_text, portfolio_text, quick_phrases_text


def extract_urls_from_docs(text: str) -> list:
    """Extract URLs from winning/portfolio docs so we can pass them; model uses these when referencing a tech."""
    if not text:
        return []
    # Match http(s) URLs
    url_pattern = re.compile(r"https?://[^\s\]\)\"\']+", re.IGNORECASE)
    return list(dict.fromkeys(url_pattern.findall(text)))


def detect_tech_stacks(text: str) -> dict:
    """Return {normalized_stack: url} for tech stacks mentioned in text."""
    text_lower = text.lower()
    found = {}
    for key, url in TECH_URLS.items():
        if key in text_lower:
            norm = key.replace(" ", "").replace(".", "")
            found[norm] = url
    return found


def summarize_websites_and_tech() -> dict:
    """
    Summarize websites and tech stacks from the winning proposals and portfolio docs.

    Returns:
      {
        "websites": [list of unique URLs],
        "tech_stacks": [list of unique tech stack keys detected in the docs],
      }
    """
    try:
        winning_text, portfolio_text, quick_phrases_text = load_brain()
    except Exception:
        return {"websites": [], "tech_stacks": []}

    # Detect which tech stacks appear anywhere in the docs
    combined = (winning_text or "") + "\n" + (portfolio_text or "") + "\n" + (quick_phrases_text or "")
    text_lower = combined.lower()
    techs = []
    for key in TECH_URLS.keys():
        if key in text_lower:
            techs.append(key)
    # Dedupe while preserving order
    seen = set()
    tech_unique = []
    for t in techs:
        if t not in seen:
            seen.add(t)
            tech_unique.append(t)

    # Group portfolio projects by tech stack, with URLs and description snippets.
    # Treat each line that contains a URL as a separate project entry.
    projects_by_tech: dict[str, list[dict]] = {t: [] for t in tech_unique}
    all_urls: list[str] = []
    for raw_line in (portfolio_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line_urls = extract_urls_from_docs(line)
        if not line_urls:
            continue
        all_urls.extend(line_urls)
        line_lower = line.lower()
        for key in tech_unique:
            if key in line_lower:
                projects_by_tech.setdefault(key, []).append({
                    "description": line,
                    "urls": line_urls,
                })

    # Global unique websites list (for possible future use)
    websites_seen = []
    websites_set = set()
    for u in all_urls:
        if u not in websites_set:
            websites_set.add(u)
            websites_seen.append(u)

    return {
        "websites": websites_seen,
        "tech_stacks": tech_unique,
        "projects_by_tech": projects_by_tech,
    }


def get_relevant_examples_for_job(job_post: str, tech_stacks: dict, max_entries: int = 15) -> str:
    """Load saved relevant examples that match this job (by tech stack overlap); agent uses these for similar jobs."""
    if not RELEVANT_EXAMPLES_LOG_PATH.exists():
        return ""
    lines = []
    try:
        with open(RELEVANT_EXAMPLES_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return ""
    if not lines:
        return ""
    job_lower = job_post.lower()
    our_techs = set((k or "").lower() for k in tech_stacks)
    matched = []
    for line in reversed(lines[-50:]):  # last 50, then filter
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            ex_tech = set((k or "").lower() for k in (data.get("tech_stacks") or {}))
            if our_techs & ex_tech or not our_techs:  # overlap or no tech filter
                relevant = (data.get("relevant_example") or "").strip()
                if relevant:
                    matched.append(relevant)
        except Exception:
            continue
    # Dedupe and limit
    seen = set()
    unique = []
    for m in matched:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    if not unique:
        return ""
    return "For similar jobs, use these relevant examples (include the ones that fit this job): " + "; ".join(unique[:10])


def load_ratings() -> dict:
    """Load { ts: rating } from proposal_ratings.json."""
    if not PROPOSAL_RATINGS_PATH.exists():
        return {}
    try:
        with open(PROPOSAL_RATINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_rating(ts: str, rating: int) -> None:
    """Save or update rating for a proposal by its ts. Rating 1-10."""
    ensure_learning_dir()
    ratings = load_ratings()
    ratings[ts] = rating
    with open(PROPOSAL_RATINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(ratings, f, indent=2)


def get_proposals_for_rating(user_id=None) -> list:
    """
    Return list of proposals to rate: one per job (latest only if rewritten multiple times).
    Each item: { ts, job_snippet, proposal_snippet, proposal_full, rating }.
    """
    parsed = []

    # Prefer Postgres if configured.
    db_url = os.getenv("DATABASE_URL") or os.getenv("UPWORK_DATABASE_URL")
    if db_url:
        try:
            conn = _get_db_conn()
        except Exception:
            conn = None
        if conn is not None:
            try:
                with conn:
                    with conn.cursor() as cur:
                        if user_id:
                            cur.execute(
                                """
                                SELECT ts, job_post, proposal, tech_stacks, user_id
                                FROM proposals
                                WHERE user_id = %s
                                ORDER BY ts DESC;
                                """,
                                (user_id,),
                            )
                        else:
                            cur.execute(
                                """
                                SELECT ts, job_post, proposal, tech_stacks, user_id
                                FROM proposals
                                ORDER BY ts DESC;
                                """
                            )
                        rows = cur.fetchall() or []
                        for row in rows:
                            parsed.append({
                                "ts": row["ts"],
                                "job_post": row["job_post"],
                                "proposal": row["proposal"],
                                "tech_stacks": row.get("tech_stacks") or {},
                                "user_id": row.get("user_id") or "",
                            })
            finally:
                conn.close()

    # Fallback to JSONL file (local/dev)
    if not parsed:
        if not PROPOSALS_LOG_PATH.exists():
            return []
        lines = []
        try:
            with open(PROPOSALS_LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                parsed.append(data)
            except Exception:
                continue
    if not parsed:
        return []
    # Group by job_post (exact), keep latest (max ts) per job
    by_job = {}
    for p in parsed:
        if user_id and (p.get("user_id") or "") != user_id:
            continue
        job = (p.get("job_post") or "").strip()
        ts = p.get("ts") or ""
        if not ts:
            continue
        if job not in by_job or (ts > by_job[job]["ts"]):
            by_job[job] = {
                "ts": ts,
                "job_post": job,
                "proposal": p.get("proposal") or "",
                "tech_stacks": p.get("tech_stacks") or {},
            }
    ratings = load_ratings()
    out = []
    for job, row in by_job.items():
        ts = row["ts"]
        proposal = row["proposal"]
        out.append({
            "ts": ts,
            "job_snippet": (row["job_post"] or "")[:200] + ("..." if len(row["job_post"] or "") > 200 else ""),
            "proposal_snippet": (proposal or "")[:250] + ("..." if len(proposal or "") > 250 else ""),
            "proposal_full": proposal,
            "rating": ratings.get(ts),
        })
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out


def get_high_rated_proposals_context(min_rating: int = 6, max_entries: int = 5) -> str:
    """Proposals rated >= min_rating (6–7=viewed, 10=lead). Agent learns from these for future jobs."""
    ratings = load_ratings()
    if not ratings or not PROPOSALS_LOG_PATH.exists():
        return ""
    high_ts = [ts for ts, r in ratings.items() if r is not None and int(r) >= min_rating]
    if not high_ts:
        return ""
    high_ts_set = set(high_ts)
    lines = []
    try:
        with open(PROPOSALS_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return ""
    examples = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            ts = data.get("ts")
            if ts in high_ts_set:
                examples.append({
                    "rating": ratings.get(ts),
                    "proposal_snippet": (data.get("proposal") or "")[:500] + "...",
                })
        except Exception:
            continue
    examples = examples[:max_entries]
    if not examples:
        return ""
    parts = [f"High-rated proposal (rating {ex['rating']}): {ex['proposal_snippet']}" for ex in examples]
    return (
        "Proposals that got a lead or were viewed (secondary hints only—openings and structure must still match WINNING PROPOSALS; ignore generic 'opportunity' openings here): "
        + " | ".join(parts)
    )


def get_recent_proposals_context(max_entries: int = 5) -> str:
    """Read last N proposals from log to keep tone/structure consistent (self-update)."""
    if not PROPOSALS_LOG_PATH.exists():
        return ""
    lines = []
    try:
        with open(PROPOSALS_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return ""
    if not lines:
        return ""
    recent = lines[-max_entries:]
    examples = []
    for line in recent:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            examples.append({
                "job_snippet": (data.get("job_post") or "")[:300] + "...",
                "proposal_snippet": (data.get("proposal") or "")[:400] + "...",
            })
        except Exception:
            continue
    if not examples:
        return ""
    parts = []
    for i, ex in enumerate(examples, 1):
        parts.append(f"Recent example {i}:\nJob: {ex['job_snippet']}\nProposal: {ex['proposal_snippet']}")
    return "\n\n".join(parts)


def clean_proposal_format(proposal: str) -> str:
    """Remove markdown formatting, em dashes, and duplicate project name brackets."""
    if not proposal:
        return proposal
    # Remove **Name** [Name] -> Name (same name in brackets after bold)
    proposal = re.sub(r"\*\*([^*]+)\*\*\s*\[\1\]", r"\1", proposal)
    # Remove *Name* [Name] -> Name
    proposal = re.sub(r"\*([^*]+)\*\s*\[\1\]", r"\1", proposal)
    # Remove standalone **...** bold (leave text)
    proposal = re.sub(r"\*\*([^*]+)\*\*", r"\1", proposal)
    # Remove standalone *...* italics (leave text)
    proposal = re.sub(r"\*([^*]+)\*", r"\1", proposal)
    # Remove standalone [Name] when it duplicates the name before it (e.g. "Vino Site [Vino Site]" -> "Vino Site")
    proposal = re.sub(r"([A-Za-z0-9\s&]+)\s*\[\1\]", r"\1", proposal)
    # Replace em dashes (— U+2014 and – U+2013) with a plain comma+space or just strip them
    proposal = proposal.replace("—", ",").replace("–", ",")
    # Clean up any double commas or comma+space+comma artifacts
    proposal = re.sub(r",\s*,", ",", proposal)
    return proposal


def ensure_learning_dir():
    PROPOSALS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _get_db_conn():
    """
    Get a PostgreSQL connection using DATABASE_URL / UPWORK_DATABASE_URL.
    Used for persisting proposals so they survive deploys.
    """
    db_url = os.getenv("DATABASE_URL") or os.getenv("UPWORK_DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL or UPWORK_DATABASE_URL must be set for proposals DB.")
    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    return conn


def init_proposals_db() -> None:
    """
    Ensure the proposals table exists in Postgres.
    """
    db_url = os.getenv("DATABASE_URL") or os.getenv("UPWORK_DATABASE_URL")
    if not db_url:
        # Allow running locally without DB; JSONL logging will still work.
        return
    conn = _get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS proposals (
                        id SERIAL PRIMARY KEY,
                        ts TEXT NOT NULL,
                        user_id TEXT,
                        job_post TEXT NOT NULL,
                        proposal TEXT NOT NULL,
                        tech_stacks JSONB,
                        outcome TEXT
                    );
                    """
                )
    finally:
        conn.close()


def save_relevant_example(job_post: str, relevant_example: str, tech_stacks: dict):
    """Append one relevant-example record so the agent remembers it for similar jobs."""
    ensure_learning_dir()
    record = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "job_post_snippet": (job_post or "")[:500],
        "relevant_example": (relevant_example or "").strip(),
        "tech_stacks": tech_stacks,
    }
    with open(RELEVANT_EXAMPLES_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_proposal(job_post: str, proposal: str, tech_stacks: dict, outcome: str = None, user_id=None):
    """
    Append one proposal to the JSONL log (for local use) and also
    store it in Postgres (for persistence across deploys).
    Outcome can be set later (e.g. won/lost).
    """
    ts = datetime.utcnow().isoformat() + "Z"
    record = {
        "ts": ts,
        "job_post": job_post,
        "proposal": proposal,
        "tech_stacks": tech_stacks,
        "outcome": outcome,
        "user_id": user_id,
    }

    # JSONL log (local/dev usage)
    try:
        ensure_learning_dir()
        with open(PROPOSALS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # Don't fail proposal generation if JSONL logging fails.
        pass

    # Postgres log (Render / persistent)
    db_url = os.getenv("DATABASE_URL") or os.getenv("UPWORK_DATABASE_URL")
    if not db_url:
        return
    try:
        conn = _get_db_conn()
    except Exception:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO proposals (ts, user_id, job_post, proposal, tech_stacks, outcome)
                    VALUES (%s, %s, %s, %s, %s, %s);
                    """,
                    (ts, user_id, job_post, proposal, Json(tech_stacks or {}), outcome),
                )
    finally:
        conn.close()


def build_system_prompt(recent_context: str) -> str:
    return """You are an expert Upwork proposal writer for Haseeb, founder of Integriti and Integriti Studio — a full-service web development agency specialising in Shopify, Webflow, WordPress, Squarespace, and custom app development.

Every time a job post is given, you write a winning proposal in Haseeb's exact voice, tone, and structure — based on 30+ real winning proposals that have already landed clients. You never write generic proposals. Every proposal is targeted, specific, and tailored to that exact job.

---

WHO HASEEB IS:
- Certified Shopify Developer with 7+ years of hands-on experience
- Full-stack web developer — Shopify, Webflow, WordPress, Squarespace, Wix, Next.js
- Agency founder — Integriti (integriti.io) and Integriti Studio (integritistudio.com)
- Individual contractor who personally does the work (important for jobs that ask this)
- Based in Canada (EST timezone, available 7AM–6PM EST)
- Available for Zoom calls, Slack, ClickUp, Asana
- Offers long-term collaboration and post-launch support

---

OUTPUT FORMAT — CRITICAL:
- Output plain text only. No markdown formatting whatsoever — no asterisks, no pound signs, no dashes as bullets, no underscores, no bold, no italics, no headers.
- Write exactly as it would appear when pasted into the Upwork proposal text box.
- The only formatting allowed is a plain dash and URL on each line for portfolio examples.
- Do NOT add any preamble. Do not write "Here is your proposal:" or "Sure, here's a draft:" — start directly with Hi/Hello or the hidden keyword if one exists.
- NEVER use em dashes (—) or en dashes (–) anywhere in the proposal. Use a comma, period, or rewrite the sentence instead.

CONCISENESS — CRITICAL:
- Every sentence must earn its place. Cut anything that doesn't add new information.
- One idea per sentence. No run-on sentences joined by em dashes or semicolons.
- Each paragraph should be 2-4 sentences maximum. No paragraph walls.
- No over-explaining: say what you'll do, not why it is obvious you'll do it.
- No throat-clearing openers ("I wanted to reach out", "I came across your job post", "I believe I am a great fit").
- No padding closers ("I look forward to hearing from you", "Please feel free to reach out", "I am excited about this opportunity").
- If a sentence can be cut without losing meaning, cut it.

---

I vs WE — DEFAULT RULE:
- Default: use "I", "my", "me" throughout. Haseeb sends proposals from his individual account.
- Use "We", "our", "Integriti" ONLY when the job explicitly asks for an agency/team, or when the user's extra instructions say to mention the company.
- Never mix I and We in the same proposal.

---

THE 6-STAGE WINNING FORMULA — follow this structure every time:

STAGE 1 — HOOK (first line, never generic):
Choose based on job type:
- Use client's name if visible: "Hi [Name],"
- If they have a live site/store mentioned: reference it by name to show you looked
- Arabic/Middle-East client: open with "Salam,"
- Startup/founder job: validate the product idea in one sentence
- Vague or unclear job: surface the most common failure mode for that project type
- Hidden instruction in job post (e.g. "write APPLE at the top", "start with THURSDAY"): do it as the absolute first word, then continue

STAGE 2 — PROOF (immediately after hook):
- Lead with the single most relevant past project by name with its live URL
- Match proof to job type: Shopify job → Shopify store, Webflow job → Webflow site, WordPress job → WordPress site
- Never dump the full portfolio here — pick the 1-2 most relevant examples first

STAGE 3 — UNDERSTANDING (show you read the brief):
- Mirror the job structure back using the client's own language and terms
- Address the hardest or most technical requirement first
- For multi-part jobs, address each part in the same order as the brief

STAGE 4 — TECHNICAL PLAN:
- Specific enough to show expertise, simple enough for a non-technical client
- Use the exact tools and technologies the client mentioned
- When client asks for a platform recommendation: give a confident specific answer with clear reasoning — never just "it depends"
- Speak the industry language:
  - Shopify: "Liquid", "OS 2.0", "Sections Everywhere", "line item properties", "metafields", "JSON templates"
  - Webflow: "CMS collections", "Finsweet", "interactions", "dynamic content"
  - Fashion/luxury: "editorial typography", "generous whitespace", "lets the product breathe"
  - Startup/founder: "funnel", "conversion", "retention", "lean MVP"
  - B2B/agency: "long-term partner", "operational efficiency", "frictionless collaboration"

STAGE 5 — EXTRAS (differentiation):
- Proactively flag risks or constraints the client didn't mention (Shopify 100-variant limit, HIPAA, QR URL stability, etc.)
- For previous-developer-failed jobs: acknowledge the audit-first step explicitly and early
- For urgent/timed jobs: confirm deadline clearly and suggest a backup/rollback plan

STAGE 6 — CLOSE:
- Low pressure, never pushy
- Match the close to client type:
  - Simple/vague job → "Let's connect via chat"
  - Complex technical job → "Let's connect over a Zoom call"
  - Startup/founder job → "Let's discuss further details over a Zoom call"
  - Agency/B2B job → "Let's schedule a short call to explore how we can support your workflow"
- For vague or ambiguous scope: end with 1-2 smart clarifying questions directed at the client
- For jobs where tasks aren't listed: ask "Could you share the list of tasks?"
- For long-term potential: mirror it back: "I'm open to long-term collaboration"

---

PROPOSAL LENGTH — match to job complexity:
- Short / simple / vague job → 3-5 paragraphs
- Complex / detailed / multi-part job → 6-10 paragraphs
- Lean job post → lean proposal (mirror the client's energy)
- Always answer every explicit application question in the order asked

---

CRITICAL RULES:
1. Always check for hidden instructions first (keywords to include, format requirements) — follow them as the very first word
2. Never be generic — every proposal must reference something specific about that job or client
3. Answer every explicit question the job post asks, in the same order
4. Never use filler: "I am passionate about", "I would love the opportunity to", "Looking forward to hearing from you", "My expertise allows me to effectively", "I can definitely help with this"
5. For jobs with a stated budget: come in at or slightly below the lower end — never race to the bottom
6. For invite-only jobs: keep it tight — proof + approach + communication style is enough
7. For audit/fix/finish jobs: "I'll start with a thorough audit before touching anything"
8. For individual contractor jobs that explicitly say no agencies: state "I am an individual contractor and will personally handle 100% of the work"
9. Never repeat the same sentence or idea twice in a proposal
10. Use WINNING PROPOSALS from the reference documents as the primary style guide — match their tone, sentence rhythm, and phrasing exactly

---

SMART CLARIFYING QUESTIONS (use only 1-2, only when genuinely needed):
- "Do you have existing branding/design assets, or will you need design direction?"
- "Will this need real-time inventory tracking, or are you managing stock manually?"
- "Could you share the list of tasks? This will help me provide accurate quotes and timelines."
- "Do you envision this as a step-by-step visual configurator, or a guided experience that submits a structured quote request?"
- "Will vendors handle their own shipping, or will it be managed centrally?"
- "Should the variant/image updates apply globally or only on select collections?"

---

PORTFOLIO REFERENCE (use this to match examples to jobs):

SHOPIFY STORES:
- coloritto.co — Ella theme, custom mega menu, wallpaper calculator (Liquid + JS), bypassed 100-variant limit
- meroliving.com — Ella theme, WordPress to Shopify migration, fixed multi-currency sync
- hertrove.com — Lorenza theme, redesigned, currency converter fix, AR/EN multilingual
- thinlizzy.com.au — Shopify Plus, Expanse, GemPages, Recharge subscriptions, luxury skincare
- vinosite.com — Shopify Plus, Dawn, auto-add reusable bag to cart, advanced cart logic
- rypgolf.com — Ella, Shopify 2.0, PageFly to Liquid migration, Klaviyo integration
- nettpharmacy.com — Ella, healthcare, 1000+ products, complete build
- nuu-muu.com — metafield-based variant colors, product color redirect, complex variants
- oushkfuel.com — fully custom Shopify 2.0 single-product store from Figma, bold branding
- urthlabs.com — Ella, GemPages, Recharge subscriptions API
- masterchocolat.com — build-your-own chocolate box, Liquid, product bundling
- clearwellness360.com — Recharge, custom JS for delivery duration selection
- mercian hockey — Wix to Shopify migration, complete redesign, Ella
- try.drinknello.com — Webflow to Shopify migration, Wonder theme
- shop.skinandhaircenter.com — WordPress + Shopify linked for ecom
- zenduradental.com — Real-Time Pricing Custom App, Node.js, React, Polaris, SAP integration
- prettydynasty.com — DSers automated dropshipping
- dinamelwani.com — luxury fashion, strong imagery, minimalist
- melvinjoyeria.com — pre-owned luxury watches, Abbott's Edge inventory sync
- snackexpedition.com — GemPages, subscription snack box
- skirack.com — Aurora, tailored product pages, optimized user flow

SHOPIFY CUSTOM APPS:
- Shopify-Klaviyo Connector — custom event tracking, storefront JS + API
- Loyalty Program App — customer status tracking, cron jobs, metafields, Klaviyo sync
- Real-Time Pricing App — ERP-based dynamic pricing (zenduradental.com)
- Inventory Sync App — real-time stock from external systems
- Shipping Rate Calculator App — dynamic shipping by pickup location
- Tech stack: Node.js backend, React.js + Polaris frontend

WEBFLOW SITES:
- molyneauxhome.webflow.io — Airtable integration, Finsweet CMS Bridge, CMS filtering
- check-my-ride.webflow.io — real-time map MVP, Firebase + Supabase + Mapbox
- anneclaireexperience.com / acecharleston.com — fitness platform, Webflow CMS
- weareplai.com — template customisation, lead gen form, Stripe checkout
- integriti.io — agency site, CMS, custom animations
- integritistudio.com — portfolio, Next.js headless
- durabuiltwindows.com — renovation page

WORDPRESS SITES:
- skinandhaircenter.com — Figma to Elementor, booking forms, SEO
- ccsrcalgary.com — chiropractic, Elementor Pro, appointment booking, payments
- windigosigns.com — B2B wholesale signage, Elementor, JotForm, dealer CTAs
- weacttctac.org — custom post types, ACF metafields, events system
- yourlondonchauffeur.co.uk — WooCommerce, custom booking
- coloradomediation.org — legal/mediation, service pages
- vrvisiongroup.com — GTM, GA4, form click tracking
- clearheartcounselling.com, thrivenowphysio.com, fraserlifephysio.ca — healthcare sites
- ramzunalanguages.org — language learning platform

SQUARESPACE:
- analogtattoo.com — complete site, ecom, custom shipping (flat, international, USPS)
- foodshot.com.au — food/restaurant brand
- kiremico.com — artist portfolio, minimalist

NEXT.JS:
- mxsocal.com — landing page + bike rental MVP (Node.js)
- proteksolutions.ca — business site
- hirundo.tech — complete website

---

JOB TYPE PLAYBOOK (apply the matching playbook for each job):

Shopify theme build/customisation → lead with most relevant store, mention specific theme, address OS 2.0, Sections Everywhere, mobile-first, Lighthouse scores
Shopify custom feature/calculator/builder → open with pain point, reference coloritto wallpaper calculator + masterchocolat build-your-own box, mention 100-variant limit solution proactively, explain line item properties
Shopify app development → reference zenduradental immediately, list other custom apps, confirm Node.js + React + Polaris + App Bridge stack
Shopify subscriptions/Recharge → reference clearwellness360 + thinlizzy, for fashion subscriptions speak drop culture and exclusivity
Webflow build → reference check-my-ride for real-time/complex, molyneauxhome for Airtable/CMS, integriti.io for design-focused
Webflow + Zapier/Airtable/Stripe → validate product idea, walk through automation stack component by component, bid mid-range, offer pre-kickoff review, confirm QA Looms + handover doc
WordPress build → match site to job type (healthcare → physio/chiro, legal → coloradomediation, B2B → windigosigns, fitness → vitalitywithnadira)
Bilingual/RTL/Arabic → open "Salam,", reference hertrove.com, mention Shopify Markets, confirm RTL via dir="rtl"
Luxury/editorial/fashion → speak "editorial typography", "generous whitespace", "lets the product breathe", lead with design thinking, reference hertrove + dinamelwani + thinlizzy
Multi-vendor marketplace → recommend WooCommerce + Dokan Pro over Shopify, explain why, give milestone timeline
Agency/long-term partner → use "We are Integriti", reference integriti.io + integritistudio.com, confirm Slack/ClickUp/Asana, organise portfolio by platform, end with "long-term digital partner"
Individual contractor (explicitly no agencies) → confirm "I am an individual contractor and will personally handle 100% of the work", use I not we, give hourly rate $25-30/hr
Audit/fix/finish existing work → acknowledge audit-first step early, address reassuringly if previous developer failed, offer to review what's reusable before billing
Timed/urgent launch → confirm exact deadline in first lines, reference timed launches (BFCM on thinlizzy, holiday on vinosite), suggest rollback plan
Simple/vague job → open with pain point, expand scope briefly, ask 1-2 smart clarifying questions, keep short"""


def build_user_prompt(
    job_post: str,
    user_instructions: str,
    winning_text: str,
    portfolio_text: str,
    quick_phrases_text: str,
    tech_stacks: dict,
    recent_context: str,
    urls_from_docs: list,
    relevant_examples_context: str,
    relevant_example_override: str = None,
    high_rated_context: str = None,
) -> str:
    urls_block = "\n".join(urls_from_docs[:40]) if urls_from_docs else ""

    user = f"""WINNING PROPOSALS — study these carefully. Match their tone, sentence rhythm, how they open, how they close, and how they reference past work:

\"\"\"
{winning_text[:WINNING_PROMPT_CHAR_LIMIT]}
\"\"\"

QUICK PHRASES & PORTFOLIO SNIPPETS — pre-written lines organised by tech stack and situation. Use the ones that fit this job directly. They are already proven and human-sounding:

\"\"\"
{quick_phrases_text[:QUICK_PHRASES_PROMPT_CHAR_LIMIT]}
\"\"\"

FULL PORTFOLIO — all past projects with URLs. Use ONLY the projects that are genuinely relevant to this job's specific challenge:

\"\"\"
{portfolio_text[:PORTFOLIO_PROMPT_CHAR_LIMIT]}
\"\"\"

"""
    if urls_block:
        user += f"""Project site URLs (use these after examples — only real project URLs, never platform URLs like shopify.com):
{urls_block}

"""
    if relevant_examples_context:
        user += f"""Previously saved relevant examples for similar jobs (use if they fit):
{relevant_examples_context}

"""
    if high_rated_context:
        user += f"""High-rated past proposals (structure/voice hints only):
{high_rated_context}

"""
    if recent_context:
        user += f"""Recent proposals (tone reference only — WINNING PROPOSALS wins for structure):
{recent_context}

"""
    if relevant_example_override:
        user += f"""REQUIRED EXAMPLES — must appear in the proposal: {relevant_example_override}
Include additional relevant projects from the portfolio as needed. Each gets its live URL.

"""
    user += f"""JOB POST:
\"\"\"
{job_post}
\"\"\"
"""
    if user_instructions:
        user += f"""
EXTRA INSTRUCTIONS FOR THIS PROPOSAL:
\"\"\"
{user_instructions}
\"\"\"
"""

    if relevant_example_override:
        user += """
TASK: Write one Upwork proposal following the 6-stage formula (Hook, Proof, Understanding, Technical Plan, Extras, Close). Include every required example above plus any other genuinely relevant portfolio items. Each gets its live URL on its own line. Plain text only — no markdown, no bold, no dividers. Output ONLY the proposal text."""
    else:
        user += """
TASK: Write one Upwork proposal for the job post above.

Before writing, silently work through:
1. Scan for hidden instructions (keywords to write first, format requirements) — if found, use as absolute first word/line
2. Identify the job type and apply the matching playbook from your instructions
3. Find the 1-3 portfolio examples most relevant to this specific challenge (same problem type, not just same platform)
4. Check if client name or their website is mentioned — use it
5. I or We? Default is I (individual) — only use We if the job explicitly asks for a team/agency
6. Match length to complexity: simple/vague = 3-5 paragraphs, complex/detailed = 6-10 paragraphs

Write the proposal following the 6-stage formula:
Stage 1 — Hook: client name, their site, hidden keyword, validate idea, or surface pain point — never generic
Stage 2 — Proof: most relevant past project with live URL, immediately
Stage 3 — Understanding: mirror brief using their language, hardest requirement first
Stage 4 — Technical plan: specific to their tools, use correct industry terminology
Stage 5 — Extras: flag risks or constraints they didn't ask about
Stage 6 — Close: match to job type (chat / Zoom / agency call / clarifying questions)

Plain text only. No markdown. No em dashes. No filler phrases. No "I see a/an ... opportunity". Keep every paragraph to 2-4 sentences. Cut any sentence that repeats an idea already stated.

After the proposal, on a new line write exactly:
Note - Relevant example for this job: X
(X = the 1-2 best-fit portfolio items for future similar jobs)

Output the proposal first, then that Note line. Nothing else."""

    return user


def generate_proposal(job_post: str, user_instructions: str = "", relevant_example_override: str = None, user_id=None) -> dict:
    """
    Load brain from docx, detect tech stacks, call LLM, log proposal.
    If relevant_example_override is set, save it for future jobs and rewrite the proposal using it (no Note line in output).
    Returns { "proposal": str, "tech_stacks": dict, "relevant_example": str, "error": str or None }.
    """
    try:
        winning_text, portfolio_text, quick_phrases_text = load_brain()
    except FileNotFoundError as e:
        return {"proposal": "", "tech_stacks": {}, "relevant_example": "", "error": str(e)}
    except Exception as e:
        return {"proposal": "", "tech_stacks": {}, "relevant_example": "", "error": str(e)}

    tech_stacks = detect_tech_stacks(job_post)
    matched_winning_text = get_top_relevant_snippets(
        source_text=winning_text,
        job_post=job_post,
        tech_stacks=tech_stacks,
        max_snippets=TOP_WINNING_SNIPPETS,
        char_limit=WINNING_PROMPT_CHAR_LIMIT,
        lines_per_snippet=4,
    )
    matched_portfolio_text = get_top_relevant_snippets(
        source_text=portfolio_text,
        job_post=job_post,
        tech_stacks=tech_stacks,
        max_snippets=TOP_PORTFOLIO_SNIPPETS,
        char_limit=PORTFOLIO_PROMPT_CHAR_LIMIT,
        lines_per_snippet=3,
    )
    matched_quick_phrases_text = get_top_relevant_snippets(
        source_text=quick_phrases_text,
        job_post=job_post,
        tech_stacks=tech_stacks,
        max_snippets=TOP_QUICK_PHRASES_SNIPPETS,
        char_limit=QUICK_PHRASES_PROMPT_CHAR_LIMIT,
        lines_per_snippet=3,
    )
    recent_context = get_recent_proposals_context()
    relevant_examples_context = get_relevant_examples_for_job(job_post, tech_stacks)
    high_rated_context = get_high_rated_proposals_context()
    urls_from_docs = extract_urls_from_docs(winning_text + "\n" + portfolio_text + "\n" + quick_phrases_text)

    if relevant_example_override:
        save_relevant_example(job_post, relevant_example_override.strip(), tech_stacks)

    system = build_system_prompt(recent_context)
    user = build_user_prompt(
        job_post,
        (user_instructions or "").strip(),
        matched_winning_text or winning_text[:WINNING_PROMPT_CHAR_LIMIT],
        matched_portfolio_text or portfolio_text[:PORTFOLIO_PROMPT_CHAR_LIMIT],
        matched_quick_phrases_text or quick_phrases_text[:QUICK_PHRASES_PROMPT_CHAR_LIMIT],
        tech_stacks,
        recent_context,
        urls_from_docs,
        relevant_examples_context,
        relevant_example_override,
        high_rated_context,
    )

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "proposal": "",
            "tech_stacks": tech_stacks,
            "relevant_example": "",
            "error": "ANTHROPIC_API_KEY not set. Set it in the environment or .env file.",
        }

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": system,
                    # Cache the system prompt — it never changes between calls.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": user,
                        }
                    ],
                }
            ],
        )
        raw = (resp.content[0].text or "").strip()
        if relevant_example_override:
            proposal = raw
            relevant_example = relevant_example_override.strip()
        else:
            # Parse "Note - Relevant example for this job: X" from the end
            relevant_example = ""
            note_marker = "Note - Relevant example for this job:"
            if note_marker in raw:
                idx = raw.find(note_marker)
                proposal = raw[:idx].strip()
                tail = raw[idx:].strip()
                if tail.startswith(note_marker):
                    relevant_example = tail[len(note_marker):].strip()
            else:
                proposal = raw
            if relevant_example:
                save_relevant_example(job_post, relevant_example, tech_stacks)
        proposal = clean_proposal_format(proposal)
    except Exception as e:
        return {"proposal": "", "tech_stacks": tech_stacks, "relevant_example": "", "error": str(e)}

    log_proposal(job_post, proposal, tech_stacks, user_id=user_id)
    return {"proposal": proposal, "tech_stacks": tech_stacks, "relevant_example": relevant_example, "error": None}
