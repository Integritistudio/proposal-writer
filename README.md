# Upwork Proposal Agent

BD team tool: paste an Upwork job post in the browser and get a proposal that matches your winning proposals and portfolio. The agent reads two Word documents as its brain and logs every proposal so it stays up to date.

## Setup

1. **Python 3.10+** (e.g. from Homebrew). Use a virtual environment so pip doesn’t touch system Python:

   ```bash
   cd /path/to/my-ai-agent
   python3 -m venv .venv
   source .venv/bin/activate   # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Add your Word documents** in the project folder (same folder as `app.py`):
   - `Upwork Winning Proposals.docx` — your winning proposal examples (structure, tone, tech URLs).
   - `Portfolio.docx` — your portfolio items to reference in proposals.

   To use another folder or filenames, set:
   - `UPWORK_DOCS_DIR` — folder containing the docx files.
   - `UPWORK_WINNING_DOC` — winning proposals filename.
   - `UPWORK_PORTFOLIO_DOC` — portfolio filename.

3. **Set your OpenAI API key** (e.g. in `.env` or export):
   ```bash
   export OPENAI_API_KEY="sk-..."
   ```
   Optional: `UPWORK_OPENAI_MODEL` (default: `gpt-4o-mini`).

## Run

Activate the venv, then start the app:

```bash
source .venv/bin/activate
python app.py
```

Open **http://127.0.0.1:5001** in the browser (default port 5001 avoids macOS AirPlay on 5000). Paste the Upwork job post and click **Generate proposal**. Copy the result into Upwork.

For a different port (e.g. 8080):
```bash
PORT=8080 python app.py
```

## How it stays up to date

- **Documents:** Each run reads the latest `Upwork Winning Proposals.docx` and `Portfolio.docx`. Update those files and the next proposal uses the new content.
- **Log:** Every generated proposal is appended to `learning/upwork_proposals_log.jsonl` (timestamp, job post, proposal, detected tech stacks). The engine uses the last few entries as context so tone and structure stay consistent as the team uses the tool.

## Log format

Each line in `learning/upwork_proposals_log.jsonl` is a JSON object:
- `ts` — UTC time
- `job_post` — pasted job text
- `proposal` — generated proposal
- `tech_stacks` — detected stack → URL map
- `outcome` — optional (e.g. `"won"` / `"lost"`); you can edit the file to add this later for analysis.

---

## Deploying (including with Supabase)

**Supabase does not run long-lived apps like this Flask server.** It provides a database (Postgres), Auth, Storage, and Edge Functions — not a place to “deploy” the Flask app itself.

You have two practical options:

### Option 1: Deploy the Flask app on Render (recommended)

1. Push this repo to GitHub.
2. Go to [Render](https://render.com) → **New** → **Web Service**.
3. Connect the repo. Render will detect `render.yaml` or you can set:
   - **Build:** `pip install -r requirements.txt`
   - **Start:** `python app.py`
4. Add **Environment variables** in the dashboard:
   - `OPENAI_API_KEY` = your key (required)
   - `PORT` is set by Render automatically.
5. For the **Word documents**: either include them in the repo (in the project root) or upload them once and add them via a **Background Worker** / separate step. Easiest is to commit `Upwork Winning Proposals.docx` and `Portfolio.docx` to the repo (you can add them to a private repo).
6. Deploy. Your app will run at `https://<your-service>.onrender.com`. Use **Rate proposals** and **Generate proposal** as usual.

The app stores proposals and ratings in the `learning/` folder on the server. On Render, the filesystem is ephemeral unless you use a **persistent disk** (paid). So for a persistent log and ratings across deploys, use Option 2 (Supabase as storage/DB).

### Option 2: Flask on Render + Supabase for persistence

- **Run the app:** Same as Option 1 (deploy Flask on Render).
- **Use Supabase for:**
  - **Storage:** Upload `Upwork Winning Proposals.docx` and `Portfolio.docx` to a Supabase Storage bucket, then set `UPWORK_DOCS_DIR` to a path where your app downloads these (you’d add a small script or startup step to pull from Supabase Storage).
  - **Database:** Later you can replace the JSONL/JSON files with Supabase (Postgres) tables for proposals and ratings. That requires code changes (e.g. using `psycopg2` or Supabase client and writing/reading from tables instead of files).

So: **deploy the Flask app on Render (or Railway / Fly.io), and optionally use Supabase for storage/database** — not for “hosting” the Flask app.

### Docker

To run with Docker (e.g. on a VPS or any host that runs containers):

```bash
docker build -t upwork-agent .
docker run -p 5001:5001 -e OPENAI_API_KEY=sk-... -v $(pwd)/learning:/app/learning -v $(pwd)/Upwork\ Winning\ Proposals.docx:/app/Upwork\ Winning\ Proposals.docx -v $(pwd)/Portfolio.docx:/app/Portfolio.docx upwork-agent
```

Mount your docx files and the `learning` folder so data persists.
