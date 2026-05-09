# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Identity:** You are a developer working on an AI agent that converts PDFs and web pages from Raindrop.io "Later" bookmarks into MP3 audio files. You are working with a non-technical product manager. Explain technical decisions in plain language so they can make decisions. They define intent and outcomes — you write and test all code.

---

## Project Goal

An AWS Lambda agent that polls Raindrop.io for bookmarks tagged "Later", extracts text from each one (PDF or webpage), converts to speech via AWS Polly, and emails the combined MP3 to the user via Gmail.

---

## Commands

**Local test run** (requires env vars set in terminal):
```bash
# Copy and fill in .env.example, then:
export $(cat .env | xargs)
python lambda_function.py
```

**Build Lambda deployment package:**
```bash
./deploy.sh
# Outputs: lambda_package.zip (upload to AWS Lambda manually via console)
```

**Install dependencies locally:**
```bash
pip install -r requirements.txt
```

---

## Architecture

Everything lives in `lambda_function.py`. One file, six classes, wired by an `Orchestrator`.

```
EventBridge (every 30 min)
  └── lambda_handler()
        ├── Orchestrator.poll()      ← Phase 1: Raindrop.io → DynamoDB
        └── Orchestrator.process()  ← Phase 2: DynamoDB → Polly → Gmail
```

**Classes:**

- `Config` — loads all env vars at import time; fails loudly on missing required vars
- `StateManager` — DynamoDB wrapper; tracks bookmark state (pending/processed/failed) and batches
- `RaindropMonitor` — fetches bookmarks tagged "Later" from Raindrop.io REST API; filters locally by `tags` array (not server-side search, which is unreliable)
- `ContentExtractor` — extracts readable text from URLs: trafilatura (primary) → Jina Reader (JS-heavy sites) → BeautifulSoup (last resort); separate PDF path via pdfminer.six
- `TTSConverter` — sends text to AWS Polly in 2800-char chunks (Polly's per-request limit), returns MP3 bytes
- `AudioBuilder` — prepends chapter announcements to each article, concatenates MP3 bytes, writes ID3 CHAP/CTOC markers for skip navigation
- `EmailNotifier` — sends finished MP3 via Gmail SMTP with plain-text + HTML chapter list

**Two-phase pipeline:**

1. **Poll:** Detect new "Later" bookmarks → write to DynamoDB with a `process_after` timestamp (`BATCHDELAY` minutes out). Bookmarks added within the same window share a `batch_id`.
2. **Process:** Scan DynamoDB for batches past their `process_after` — extract text, synthesize audio, email MP3, mark items "processed".

**Content extraction chain** (each step only runs if previous returned < 150 words):
1. AMP URL de-wrapping (converts CDN AMP URLs to canonical)
2. Direct fetch + trafilatura
3. Jina Reader — targeted (semantic containers) then noise-removal mode
4. BeautifulSoup prose-element scrape

PDFs follow a separate path: download → pdfminer → filter garbled lines (>20% non-ASCII), repeated headers/footers (>5% of lines), page numbers.

Both paths run `_is_clean_text()` (< 75% alphabetic chars = garbled, discard) and `_strip_boilerplate_paragraphs()` (cookie banners, social links, newsletter CTAs, tail boilerplate).

---

## Infrastructure

- **Compute:** AWS Lambda (Python 3.11, x86_64)
- **Scheduling:** Amazon EventBridge (cron trigger, every 30 min)
- **State:** DynamoDB — table `text2voice_items`, partition key `raindrop_id`
- **TTS:** AWS Polly — default voice Joanna, standard engine (neural available in select regions)
- **Email:** Gmail SMTP port 587 (STARTTLS) — requires App Password, not real password
- **No Lambda layers needed** — audio concatenation is raw MP3 byte joining (valid because MP3 is a streaming format of independent frames)

**Lambda IAM role must include:**
- `AmazonPollyFullAccess`
- `AmazonDynamoDBFullAccess`

---

## Environment Variables

Required: `RAINDROPTOKEN`, `GMAILADDRESS`, `GMAILPASSWORD`

Optional (with defaults): `DBTABLE` (text2voice_items), `LATERTAG` (Later), `BATCHDELAY` (30), `POLLYVOICE` (Joanna), `POLLYENGINE` (standard)

See `.env.example` for full documentation.

---

## Deployment

`deploy.sh` installs Linux-compatible wheels (`--platform manylinux2014_x86_64`) because Lambda runs Amazon Linux, not macOS. Uploading Mac binaries causes silent crashes. Upload `lambda_package.zip` via the AWS Lambda console.

---

## Claude Behavior Rules

- **Never invent.** Ask clarifying questions before proceeding if uncertain.
- **Self-review.** Mentally trace all code before presenting it. For logic or UX changes, simulate the full two-phase workflow.
- **Confidence threshold.** If below 95% confidence on intent, ask multiple-choice questions.
- **Modular design.** Keep each class independently testable. Do not couple modules unnecessarily.
- **Plain-language summaries.** After any significant technical decision, add a plain-English explanation for the non-technical PM.
- **Comments.** Explain WHY, not WHAT. Keep comments to non-obvious invariants or workarounds.
