# JobNavigator

**Self-hosted job-hunt automation.**

Scrape career pages and aggregators, score jobs against your résumés with an LLM, tailor résumés and cover letters, auto-fill applications from your persona, get Telegram alerts, track every application.

<p align="center">
  <img src="docs/demo.gif" alt="JobNavigator 2.0 — feed, scoring, tailoring, applications" width="100%">
</p>

<p align="center">
  <a href="docs/board.png"><img src="docs/board.png" alt="Green Paper theme" width="24%"></a>
  <a href="docs/slate.png"><img src="docs/slate.png" alt="Stone theme" width="24%"></a>
  <a href="docs/v1like.png"><img src="docs/v1like.png" alt="V1 Style theme" width="24%"></a>
  <a href="docs/win98.png"><img src="docs/win98.png" alt="Windows 98 theme" width="24%"></a>
  <br>
  <sub>Five themes, each in light and dark: Paper (above), Green Paper, Stone, V1 Style, Windows 98.</sub>
</p>

## How It Works

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                               JOB DISCOVERY                                 │
│                                                                             │
│   Career Pages          │  Aggregators            │  Chrome Extension       │
│                         │                         │                         │
│   Any site via          │  JobSpy: LinkedIn,      │  Passive LinkedIn       │
│   Playwright            │  Indeed, ZipRecruiter,  │  capture while          │
│                         │  Google Jobs            │  browsing               │
│   11 ATS endpoints:     │                         │                         │
│   Workday, Greenhouse   │  LinkedIn Personal      │  Auto-fill based on     │
│   Lever, Ashby,         │  collections            │  your persona input     │
│   Oracle, Phenom,       │                         │  and question bank      │
│   TalentBrew, Rippling  │  Jobright.ai            │                         │
│   SmartRecruiters,      │  Levels.fyi             │  Save any job from      │
│   + custom              │  freehire.me            │  any page               │
│                         │                         │                         │
└─────────────────────────┴───────────┬─────────────┴─────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                PROCESSING                                   │
│                                                                             │
│   Dedup ────── URL-hash dedup, tracking params stripped                     │
│   Filters ──── Title / company include & exclude, body exclusion phrases    │
│   H-1B ─────── Company LCA data from MyVisaJobs (cached)                    │
│   Salary ───── Extracted from posting, H-1B data, description               │
│                                                                             │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                 JOB FEED                                    │
│                                                                             │
│   Review ───── Dynamic filters, sorting, detail panel                       │
│   Decide ───── Save promising jobs, skip the rest, score with AI            │
│                                                                             │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              AI RESUME SCORING                              │
│                                                                             │
│   Providers ── Claude API/CLI, Codex CLI, OpenAI, OpenRouter, Ollama        │
│   Depths ───── Light (scores only) or Full (report + keyword analysis)      │
│   Multi ────── Score against multiple resumes, compare fit per role         │
│                                                                             │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           RESUME + COVER LETTER                             │
│                                                                             │
│   Templates ── 8 resume + 8 cover-letter, auto-discovered (add your own!)   │
│   AI Tailor ── Rewrites resume bullets/keywords from the scoring report     │
│   AI Letter ── Job-specific cover letters from resume + JD, voice presets   │
│   Export ───── Live-preview PDF via Playwright with export                  │
│                                                                             │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                   TRACK                                     │
│                                                                             │
│   Autofill ─── ATS forms + AI answers to free-text questions (Persona)      │
│   Apps ─────── Stages, interviews, notes, prep handover, funnel and Sankey  │
│   Tracer ───── Unique links per resume/letter, tracks who opened them       │
│   Gmail ────── Auto-detects responses, updates application status           │
│   Telegram ─── Job alerts, daily digest, scrape health notifications        │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Features

| Feature | Description |
|---------|-------------|
| **Discovery** | Career pages (Playwright + 11 ATS handlers), JobSpy (LinkedIn, Indeed, ZipRecruiter, Google), LinkedIn collections, Levels.fyi, Jobright.ai, freehire.me, the Chrome extension |
| **Scoring** | Claude, OpenAI, OpenRouter, Ollama, Claude Code or Codex CLI; live model search, a model per feature; Light (score) or Full (report, keyword coverage, requirement mapping) against every base résumé; prompt caching on Anthropic |
| **Résumés** | Structured base résumés, tailoring per job with a review step, 8 PDF templates (drop in your own), tracked links that record opens |
| **Cover letters** | Generated from the paired résumé and persona; voice and length presets, 8 templates, PDF |
| **Dedup** | URL identity hash (tracking params stripped) plus company + title hash across sources |
| **Jobs** | Filters, sorting, keyboard shortcuts (`?`), full report, collapsible analysis pane, bulk actions with undo, posting preview |
| **Applications** | Stages, interviews, notes, status history (funnel and Sankey in Stats), a prep handover for any AI chat |
| **Persona** | Contact, work authorization, compensation, preferences, résumé content, Q&A bank; import from a résumé or PDF |
| **Extension** | Passive LinkedIn capture, save any job from any page, ATS form autofill and AI answers to free-text questions from your Persona |
| **Gmail** | Polls replies, classifies them, updates the application |
| **Telegram** | New-job alerts, daily digest, scrape health, inline actions |
| **H-1B** | Company filing lookups, description exclusion phrases |
| **Scheduling** | Intervals and crons for scraping, email, backups, cleanup, auto-reject; every cron field explains itself and offers presets |
| **Themes** | Paper, Green Paper, Stone, V1 Style, Windows 98 × light / dark / system |

> The posting preview is an `iframe`; sites that refuse framing show blank unless the extension (which strips the frame-blocking headers) is installed. "Open" always works, and applied jobs keep a cached snapshot.

## Evidence-based application copilot (local-first)

JobNavigator keeps a fact database of your career, and a model is never the source of truth:

```
your résumés → extracted claims → reconciled Career Evidence → role-family base
             → job requirements → evidence mapping → résumé plan → safe rewrites
             → claim audit → LaTeX/PDF → autofill
```

| Step | Where | What it guarantees |
|------|-------|--------------------|
| **Résumé Library** | `/profile` | Upload every historical résumé you have, several at once. Each document is kept as a source, and every canonical fact shows which of them support it. You never retype what a résumé already says. |
| **Career Evidence** | `/profile` | The reconciled facts: employment, internships, projects, research, skills linked to where you used them, education, certifications, achievements, links. A second résumé describing the same job adds its extra bullets to one canonical role instead of forking a duplicate; a field two résumés state differently becomes a **conflict you resolve** — no model picks, and a stored value is never overwritten. Imported facts arrive **unverified** and are ignored until you confirm them. Add anything true your résumés left out. |
| **Role résumés** | `/resumes` | One base per role family — Software Engineering, Product / Technical PM, Data / ML, plus any you configure. A family base is a *selection* over the same verified evidence, not a separate truth: it decides what leads, never what may be claimed. A job is classified into a family (with a reason you can override) and its tailored résumé derives from that base. Classification is no part of Candidate Fit. |
| **Role Match** | a job's **Workspace** (`/jobs/:id`) | Requirements are extracted with schema-constrained output at temperature 0 and marked MATCHED / PARTIAL / MISSING / UNKNOWN with the facts that support each one. The 0–100 score is computed deterministically, every component is shown, and the weights live in Settings › Copilot. A claim without a verified citation earns nothing, and work authorization comes only from your own answers. It measures evidence coverage; it is not any employer's ATS score. |
| **Gaps** | Workspace | Safe to add · safe to rephrase · cannot claim · needs clarification. New context is saved as a verified profile fact first, then the match reruns. A missing requirement is never written onto a résumé. |
| **Résumé** | Workspace | Drafted only from verified facts, through a fixed LaTeX template — by default Jake Gutierrez's MIT-licensed [Jake's Resume](https://github.com/jakegut/resume), vendored in `backend/resume/templates/jakes` with its license and provenance. Employers, titles, schools, degrees, project names and dates are copied from facts, never written by the model. Every bullet carries `source_fact_ids`. Machine checks (uncited claims; numbers or tools absent from the sources) plus a second model audit mark each claim SUPPORTED / AMBIGUOUS / UNSUPPORTED. An unsupported claim blocks the PDF, and nothing is fixed silently. **Parser Health** reads the compiled PDF back and checks the name, headings, companies, titles, schools, skills, bullet order and encoding. |
| **Review** | Workspace | Base vs. tailored diff. For each change: inspect its sources, the requirements it addresses and the reason; accept, reject, regenerate or edit. Accepted versions are immutable and written to `generated/<company>/<role>/<date>-<id>/` (`resume.tex`, `resume.pdf`, `resume.json`, `audit.json`, `job-analysis.json`). |
| **Apply** | Chrome extension | On an application page for a saved job it fills profile fields, uploads that job's **accepted** résumé, reuses **Answer Bank** answers (`/answer-bank`), outlines every field that needs you, and records the application as *Ready to apply*. It never clicks Submit. Work authorization, sponsorship, EEO, disability, veteran status, salary and legal attestations are never drafted by AI. |

**Local by default.** Ollama is the default provider, and no model is assumed: pick one you have pulled in Settings › AI. The Docker backend reaches Ollama on the host at `host.docker.internal:11434` (`OLLAMA_BASE_URL`). Settings › Copilot › *Where your data goes* lists every feature, the provider it uses, and whether anything leaves the machine.

**Overleaf** is optional and never authoritative. Export `.tex`, the PDF, or an Overleaf-ready ZIP from any version, or switch to Git mode to push accepted versions to the project's Git remote and pull its state. The token lives in `OVERLEAF_GIT_TOKEN` and is never stored.

## Quick Start

```bash
git clone https://github.com/vesaias/JobNavigator.git
cd JobNavigator
cp .env.example .env

docker compose up --build -d
```

Open `http://localhost`. On first run sign in with a blank key, then set one in Settings › Advanced. The previous interface is at `/classic`.

To use a ChatGPT subscription through Codex CLI, authenticate once after the containers start:

```bash
docker compose exec backend codex login --device-auth
docker compose exec backend codex login status
```

Then pick **Codex CLI (ChatGPT Subscription)** in Settings › AI. The login lives in the `codex_auth` volume (about 100 MB with Codex's own state); no API key needed. A plan limit fails over to the fallback provider without retrying.

For the local default, install [Ollama](https://ollama.com) on the host and `ollama pull` a model. LaTeX ships in the backend image; running the backend outside Docker needs `pdflatex` (TeX Live or MiKTeX).

**First steps:**
1. Settings › AI — Ollama (default) with a model you have pulled, or another provider and key
2. Career Evidence — drop in every résumé you have, resolve any conflicts, confirm the facts, then add what they left out
3. Résumés — generate a base résumé for each role family you apply to, and accept it
4. Companies and Searches — add a few and run them, or save any posting with the extension
5. A job's Workspace — analyze, review the match and gaps, generate and accept a résumé, then apply with the extension

## Chrome Extension ("The Navigator")

- Unblocks the posting preview: frame-blocking headers are lifted only for frames the dashboard itself opens (scoped to your JobNavigator host); every other page keeps its own headers.
- Captures job ids while you browse `linkedin.com/jobs/collections/*` and imports them with full details.
- Fills ATS forms and drafts answers to free-text questions from your Persona and Q&A bank (toggle in the popup; model and prompt in Settings › AI).

Install: `chrome://extensions` › Developer mode › Load unpacked › `extension/`. The LinkedIn import needs a separate LinkedIn account in Settings › Accounts.

## Optional Integrations

**Telegram** — bot token in `.env`, chat id in Settings.

**Gmail** — `python backend/gmail_oauth_setup.py`, OAuth credentials in `.env`.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, SQLAlchemy, APScheduler, Playwright |
| Frontend | React 18, Vite, Recharts, a token-based design system ([DESIGN-SYSTEM.md](frontend/src/DESIGN-SYSTEM.md)) |
| Database | PostgreSQL 16 |
| Infrastructure | Docker Compose, Caddy, nginx |
| AI | Anthropic SDK, OpenAI SDK, Ollama, Claude Code CLI, Codex CLI |
| Extension | Chrome Manifest V3 |

## Contributing

PRs welcome, see [CONTRIBUTING.md](CONTRIBUTING.md). Good first ones: an ATS handler, a résumé template, a theme.

## Backups

Scheduled `pg_dump`s land in `backups/` (five kept). They contain the settings table, API keys included: treat them like `.env`. The folder is git-ignored.

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md). Please don't open public issues for security bugs.

## Privacy

Self-hosted. Your résumés, jobs and credentials stay on your machine; the only outside party is the AI provider you configure, and with the default Ollama provider there is none. Settings › Copilot shows exactly which features send what, and where.

## Disclaimer

Personal use. Not affiliated with any job platform; some scrapers are off by default and you are responsible for the terms of the sites you use. See [LEGAL_DISCLAIMER.md](LEGAL_DISCLAIMER.md).

## License

[MIT](LICENSE)
