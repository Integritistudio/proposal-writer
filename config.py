"""Configuration for Upwork Proposal Agent."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Word documents (place these in project root or set env OVERRIDE_DOCS_DIR)
DOCS_DIR = Path(os.getenv("UPWORK_DOCS_DIR", str(BASE_DIR)))
WINNING_PROPOSALS_DOC = os.getenv("UPWORK_WINNING_DOC", "Upwork Winning Proposals.docx")
PORTFOLIO_DOC = os.getenv("UPWORK_PORTFOLIO_DOC", "Portfolio.docx")
QUICK_PHRASES_DOC = os.getenv("UPWORK_QUICK_PHRASES_DOC", "Quick Proposal Phrases 2.docx")

# Learning log: every generated proposal is appended here
LEARNING_DIR = BASE_DIR / "learning"
PROPOSALS_LOG_PATH = LEARNING_DIR / "upwork_proposals_log.jsonl"
# Saved "relevant example" per job type — agent uses these for similar future jobs
RELEVANT_EXAMPLES_LOG_PATH = LEARNING_DIR / "relevant_examples.jsonl"
# Proposal ratings (ts -> rating) for learning from outcomes: 10=lead, 6-7=viewed, 3-4=not viewed
PROPOSAL_RATINGS_PATH = LEARNING_DIR / "proposal_ratings.json"

# Tech stack URL library — add any tool mentioned in job posts; URLs here or in winning/portfolio docs are used
TECH_URLS = {
    "shopify": "https://www.shopify.com",
    "webflow": "https://webflow.com",
    "squarespace": "https://www.squarespace.com",
    "square space": "https://www.squarespace.com",
    "gempages": "https://gempages.com",
    "gem pages": "https://gempages.com",
    "gem pages app": "https://gempages.com",
    "nodejs": "https://nodejs.org",
    "node.js": "https://nodejs.org",
    "node js": "https://nodejs.org",
    "react": "https://react.dev",
    "next.js": "https://nextjs.org",
    "nextjs": "https://nextjs.org",
    "wordpress": "https://wordpress.org",
    "woocommerce": "https://woocommerce.com",
    "python": "https://www.python.org",
    "django": "https://www.djangoproject.com",
    "vue": "https://vuejs.org",
    "angular": "https://angular.io",
    "figma": "https://www.figma.com",
    "api": "https://www.w3.org/apis/",
}

# LLM: set ANTHROPIC_API_KEY in the environment or .env file
CLAUDE_MODEL = os.getenv("UPWORK_CLAUDE_MODEL", "claude-opus-4-7")
