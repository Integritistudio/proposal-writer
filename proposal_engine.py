"""
Proposal generation engine: reads docx brain, detects tech stacks, calls LLM, follows strict instructions.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

from config import (
    DOCS_DIR,
    WINNING_PROPOSALS_DOC,
    PORTFOLIO_DOC,
    PROPOSALS_LOG_PATH,
    RELEVANT_EXAMPLES_LOG_PATH,
    PROPOSAL_RATINGS_PATH,
    TECH_URLS,
    OPENAI_MODEL,
)


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
    """Load winning proposals and portfolio from disk. Always reads latest (keeps up to date)."""
    winning_path = DOCS_DIR / WINNING_PROPOSALS_DOC
    portfolio_path = DOCS_DIR / PORTFOLIO_DOC
    winning_text = read_docx(winning_path)
    portfolio_text = read_docx(portfolio_path)
    return winning_text, portfolio_text


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


def get_proposals_for_rating() -> list:
    """
    Return list of proposals to rate: one per job (latest only if rewritten multiple times).
    Each item: { ts, job_snippet, proposal_snippet, proposal_full, rating }.
    """
    if not PROPOSALS_LOG_PATH.exists():
        return []
    lines = []
    try:
        with open(PROPOSALS_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    parsed = []
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
    return "Proposals that got a lead or were viewed (learn from their style): " + " | ".join(parts)


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
    """Remove markdown-style project names like **Vino Site** [Vino Site] or *Ryp Golf* [Ryp Golf]; keep plain name."""
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
    return proposal


def ensure_learning_dir():
    PROPOSALS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


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


def log_proposal(job_post: str, proposal: str, tech_stacks: dict, outcome: str = None):
    """Append one proposal to the log. Outcome can be set later (e.g. won/lost)."""
    ensure_learning_dir()
    record = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "job_post": job_post,
        "proposal": proposal,
        "tech_stacks": tech_stacks,
        "outcome": outcome,
    }
    with open(PROPOSALS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_system_prompt(recent_context: str) -> str:
    return """You are an expert Upwork proposal writer acting as the BD team's voice. Your core learning comes from the winning proposals document and the portfolio document provided below.

STRICT RULES:
- Use the winning proposals as the ONLY pattern for structure, tone, and how to present the solution. Copy that style.
- Do NOT use AI jargon. Do NOT mention being an AI or language model.
- Do NOT use icons in headings. Do NOT use divider lines (no --- or ===).
- Do NOT use markdown or brackets for project/company names. Never write like **Vino Site** [Vino Site] or *Ryp Golf* [Ryp Golf]. Reference portfolio items in plain text only (e.g. "Vino Site, Ryp Golf" or "such as Vino Site and Ryp Golf").
- Add 1–3 relevant portfolio items that match the job. Name them in plain text only—no bold, no asterisks, no square brackets.
- After each relevant example you mention, add that example's website URL (the live store/site URL from the portfolio or winning proposals, e.g. coloritto.co, urthlabs.com). Do NOT add tech stack product URLs (e.g. shopify.com, gempages.com) in the proposal—only the client/store or project website URLs (e.g. coloritto.co) after the example.
- Write like a human: clear, confident, customized. No fluff.
- Follow the same structure as in the winning proposals (opening, understanding, approach, examples, closing)."""


def build_user_prompt(
    job_post: str,
    winning_text: str,
    portfolio_text: str,
    tech_stacks: dict,
    recent_context: str,
    urls_from_docs: list,
    relevant_examples_context: str,
    relevant_example_override: str = None,
    high_rated_context: str = None,
) -> str:
    tech_lines = [f"- {k}: {v}" for k, v in tech_stacks.items()]
    tech_block = "\n".join(tech_lines) if tech_lines else "None detected; omit URLs if not relevant."
    urls_block = "\n".join(urls_from_docs[:40]) if urls_from_docs else ""

    user = f"""
WINNING PROPOSALS (your primary pattern — match this structure and tone):

\"\"\"
{winning_text[:12000]}
\"\"\"

PORTFOLIO (use relevant items in the proposal; reference by plain name only, e.g. Vino Site, not **Vino Site** [Vino Site]. After each example, add that project's website URL from the portfolio, e.g. coloritto.co, urthlabs.com):

\"\"\"
{portfolio_text[:8000]}
\"\"\"

"""
    if urls_block:
        user += f"""
Website URLs from the winning proposals and portfolio (use these after the relevant examples in the proposal—e.g. "Vino Site (coloritto.co)" or "see urthlabs.com". Do NOT use tech product URLs like shopify.com or gempages.com in the proposal):
{urls_block}

"""
    if recent_context:
        user += f"""
RECENT PROPOSALS (stay consistent with these):
{recent_context}

"""
    if relevant_examples_context:
        user += f"""
{relevant_examples_context}

"""
    if high_rated_context:
        user += f"""
{high_rated_context}

"""
    if relevant_example_override:
        user += f"""
You MUST use these relevant examples in this proposal (the user specified them): {relevant_example_override}
After each example, add that project's website URL from the portfolio/winning proposals (e.g. coloritto.co, urthlabs.com). Do NOT add tech product URLs like shopify.com or gempages.com.

"""
    user += f"""
NEW JOB POST:

\"\"\"
{job_post}
\"\"\"

"""
    if relevant_example_override:
        user += """TASK: Write one complete Upwork proposal. Use the winning proposals' structure and tone. Use the relevant examples specified above. After each example, add its website URL (e.g. coloritto.co, urthlabs.com) from the portfolio—not tech stack URLs. No icons in headings, no divider lines, no AI jargon. Output ONLY the proposal text."""
    else:
        user += """TASK:
1. Write one complete Upwork proposal. Use the winning proposals' structure and tone. Reference 1–3 relevant portfolio items by plain name only (no ** or [ ] or *). After each example you mention, add that project's website URL from the portfolio or winning proposals (e.g. coloritto.co, urthlabs.com)—do NOT add tech product URLs like shopify.com or gempages.com. No icons in headings, no divider lines, no AI jargon.
2. After the proposal, on a new line, write exactly this line (replace X with the one or two portfolio items that are the best fit for this job): Note - Relevant example for this job: X
Output the proposal first, then that Note line. The agent will remember the relevant example for similar jobs."""

    return user


def generate_proposal(job_post: str, relevant_example_override: str = None) -> dict:
    """
    Load brain from docx, detect tech stacks, call LLM, log proposal.
    If relevant_example_override is set, save it for future jobs and rewrite the proposal using it (no Note line in output).
    Returns { "proposal": str, "tech_stacks": dict, "relevant_example": str, "error": str or None }.
    """
    try:
        winning_text, portfolio_text = load_brain()
    except FileNotFoundError as e:
        return {"proposal": "", "tech_stacks": {}, "relevant_example": "", "error": str(e)}
    except Exception as e:
        return {"proposal": "", "tech_stacks": {}, "relevant_example": "", "error": str(e)}

    tech_stacks = detect_tech_stacks(job_post)
    recent_context = get_recent_proposals_context()
    relevant_examples_context = get_relevant_examples_for_job(job_post, tech_stacks)
    high_rated_context = get_high_rated_proposals_context()
    urls_from_docs = extract_urls_from_docs(winning_text + "\n" + portfolio_text)

    if relevant_example_override:
        save_relevant_example(job_post, relevant_example_override.strip(), tech_stacks)

    system = build_system_prompt(recent_context)
    user = build_user_prompt(
        job_post, winning_text, portfolio_text, tech_stacks, recent_context, urls_from_docs,
        relevant_examples_context, relevant_example_override, high_rated_context,
    )

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {
            "proposal": "",
            "tech_stacks": tech_stacks,
            "relevant_example": "",
            "error": "OPENAI_API_KEY not set. Set it in the environment or .env file.",
        }

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.7,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
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

    log_proposal(job_post, proposal, tech_stacks)
    return {"proposal": proposal, "tech_stacks": tech_stacks, "relevant_example": relevant_example, "error": None}
