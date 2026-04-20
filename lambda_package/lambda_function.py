#!/usr/bin/env python3
"""
Later Text2Voice — AWS Lambda Agent
=====================================
Triggered every 5 minutes by Amazon EventBridge (free).
State is persisted between runs in Amazon DynamoDB (free tier).

WHAT THIS DOES (plain English):
  1. Every 5 minutes, checks Raindrop.io for bookmarks tagged "Later"
  2. Any new ones get queued in DynamoDB with a 30-minute timer
  3. Bookmarks added within that 30-minute window are grouped into one audio file
  4. After 30 minutes, extracts text from each bookmark (PDF or webpage)
  5. Converts all text to speech via ElevenLabs, with "Chapter N: Title" announcements
  6. Combines everything into one MP3 and emails it to you via Gmail

ENVIRONMENT VARIABLES (set these in Lambda → Configuration → Environment variables):
  RAINDROPTOKEN     Your Raindrop.io test token
  ELEVENLABSKEY     Your ElevenLabs API key
  GMAILADDRESS      Your Gmail address (used to send AND receive)
  GMAILPASSWORD     A Gmail App Password (NOT your real password — see setup guide)

  ELEVENLABSVOICE   (optional) Default: 21m00Tcm4TlvDq8ikWAM (Rachel voice)
  DBTABLE           (optional) Default: text2voice_items
  LATERTAG          (optional) Default: Later
  BATCHDELAY        (optional) Default: 30

FREE TIER NOTES:
  - AWS Lambda:    1M requests/month free → you'll use ~9,000/month (every 5 min)
  - DynamoDB:      25 GB free forever
  - ElevenLabs:    10,000 characters/month free (~1-2 average articles)
  - Gmail:         Free forever, 500 emails/day (more than enough)

DEPENDENCIES (pip install these into a folder before zipping — see deploy.sh):
  requests, pdfminer.six, beautifulsoup4, pydub, boto3

NO LAMBDA LAYER REQUIRED:
  Audio is handled using pure Python and raw byte concatenation — no ffmpeg needed.
"""

import io
import os
import time
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import boto3
import requests
from botocore.exceptions import ClientError
from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text as pdf_extract_text

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("text2voice")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# Loading from environment variables — Lambda lets you set these in the console
# without touching the code. Raises a clear error if a required one is missing.
# ─────────────────────────────────────────────────────────────────────────────
class Config:
    # Required — Lambda will fail immediately with a clear KeyError if missing
    RAINDROP_TOKEN = os.environ["RAINDROPTOKEN"]
    ELEVENLABS_API_KEY = os.environ["ELEVENLABSKEY"]
    GMAIL_ADDRESS = os.environ["GMAILADDRESS"]
    GMAIL_APP_PASSWORD = os.environ["GMAILPASSWORD"]

    # Optional with sensible defaults
    # Voice ID "21m00Tcm4TlvDq8ikWAM" = Rachel (warm, clear American English)
    # Browse all voices at: https://elevenlabs.io/app/voice-library
    ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABSVOICE", "21m00Tcm4TlvDq8ikWAM")
    DYNAMODB_TABLE = os.environ.get("DBTABLE", "text2voice_items")
    LATER_TAG = os.environ.get("LATERTAG", "Later")
    BATCH_DELAY_MINUTES = int(os.environ.get("BATCHDELAY", "30"))
    AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    # ElevenLabs API limits
    # Free tier: 10,000 chars/month. Each API call: max ~4,500 chars (safe limit).
    ELEVENLABS_CHUNK_SIZE = 4500
    # Seconds to wait between ElevenLabs API calls to avoid rate-limit errors
    ELEVENLABS_RATE_DELAY = 1.5


# ─────────────────────────────────────────────────────────────────────────────
# STATE MANAGER — DynamoDB
#
# Think of this as the agent's long-term memory. Because Lambda shuts down
# after each run, we need somewhere to store:
#   - Which bookmarks we've already seen (so we don't re-process them)
#   - Which bookmarks are waiting for their 30-minute window to close
#
# DynamoDB table schema:
#   raindrop_id   (String, partition key) — unique ID from Raindrop.io
#   status        (String) — "pending" | "processed" | "failed"
#   batch_id      (String) — ISO timestamp: groups items detected together
#   process_after (String) — ISO timestamp: when this batch becomes ready
#   title         (String)
#   link          (String)
#   item_type     (String) — "link", "article", "document", etc.
#   file_url      (String) — only present for files uploaded to Raindrop
#   created_at    (String) — when we first detected this bookmark
# ─────────────────────────────────────────────────────────────────────────────
class StateManager:
    def __init__(self):
        self.table = boto3.resource("dynamodb", region_name=Config.AWS_REGION).Table(
            Config.DYNAMODB_TABLE
        )

    def is_known(self, raindrop_id: str) -> bool:
        """Return True if we've seen this bookmark before (pending OR already processed)."""
        resp = self.table.get_item(Key={"raindrop_id": raindrop_id})
        return "Item" in resp

    def get_or_create_open_batch(self) -> Tuple[str, str]:
        """
        Find the current open batch (a group of bookmarks still within their 30-min window).
        If no open batch exists, create a fresh one.

        Returns: (batch_id, process_after) — both are ISO timestamp strings.

        WHY: All bookmarks added within the same 30-minute window should be grouped
        into a single audio file, not sent as separate emails.
        """
        now = datetime.now(timezone.utc)

        # Look for any pending item whose batch window hasn't expired yet
        resp = self.table.scan(
            FilterExpression="#s = :pending AND process_after > :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":pending": "pending",
                ":now": now.isoformat(),
            },
        )

        if resp.get("Items"):
            # An open batch exists — join it so all items share the same deadline
            existing = resp["Items"][0]
            log.info(
                "Joining existing batch '%s' (ready after: %s)",
                existing["batch_id"],
                existing["process_after"],
            )
            return existing["batch_id"], existing["process_after"]

        # No open batch — create a new one
        batch_id = now.isoformat()
        process_after = (now + timedelta(minutes=Config.BATCH_DELAY_MINUTES)).isoformat()
        log.info("Creating new batch '%s' (ready after: %s)", batch_id, process_after)
        return batch_id, process_after

    def add_to_batch(self, item: Dict, batch_id: str, process_after: str):
        """
        Store a bookmark in DynamoDB as 'pending'.
        Uses a condition check to silently skip duplicates (safe to call multiple times).
        """
        try:
            self.table.put_item(
                Item={
                    "raindrop_id": item["raindrop_id"],
                    "status": "pending",
                    "batch_id": batch_id,
                    "process_after": process_after,
                    "title": item["title"],
                    "link": item["link"],
                    "item_type": item["item_type"],
                    "file_url": item.get("file_url", ""),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                # Don't overwrite if it already exists (prevents double-processing)
                ConditionExpression="attribute_not_exists(raindrop_id)",
            )
            log.info("Stored '%s' in batch '%s'", item["title"], batch_id)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                log.debug("'%s' already in DynamoDB — skipped.", item["raindrop_id"])
            else:
                raise

    def get_ready_batches(self) -> Dict[str, List[Dict]]:
        """
        Find all 'pending' batches whose 30-minute timer has expired.
        Returns a dict of {batch_id: [list of items in that batch]}.
        """
        now = datetime.now(timezone.utc)
        resp = self.table.scan(
            FilterExpression="#s = :pending AND process_after <= :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":pending": "pending",
                ":now": now.isoformat(),
            },
        )
        batches: Dict[str, List[Dict]] = {}
        for row in resp.get("Items", []):
            batches.setdefault(row["batch_id"], []).append(row)
        return batches

    def mark_status(self, raindrop_id: str, status: str):
        """Update the status of a single bookmark (e.g., 'processed' or 'failed')."""
        self.table.update_item(
            Key={"raindrop_id": raindrop_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status},
        )


# ─────────────────────────────────────────────────────────────────────────────
# RAINDROP.IO MONITOR
#
# Fetches bookmarks tagged "Later" from the Raindrop.io API and identifies
# ones that haven't been processed yet.
#
# API docs: https://developer.raindrop.io/v1/raindrops/multiple
# Get your token: https://app.raindrop.io/settings/integrations → "Test token"
#
# Tag search syntax: the `search` param supports `#TagName` to filter by tag.
# Collection ID 0 = "All" (searches across every collection the user has).
# ─────────────────────────────────────────────────────────────────────────────
class RaindropMonitor:
    BASE_URL = "https://api.raindrop.io/rest/v1"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {Config.RAINDROP_TOKEN}"

    def fetch_tagged_bookmarks(self) -> List[Dict]:
        """
        Return the 50 most recent bookmarks tagged with LATER_TAG.
        50 is plenty — if someone adds more than 50 bookmarks in 5 minutes, we
        have bigger problems.
        """
        resp = self.session.get(
            f"{self.BASE_URL}/raindrops/0",
            params={
                "search": f"#{Config.LATER_TAG}",  # #Later searches by tag
                "sort": "-created",                 # newest first
                "perpage": 50,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("items", [])

    def get_new_bookmarks(self, state: StateManager) -> List[Dict]:
        """
        Compare Raindrop.io results against DynamoDB state.
        Returns only bookmarks we haven't seen before, normalized for downstream use.
        """
        new_items = []
        for bm in self.fetch_tagged_bookmarks():
            raindrop_id = str(bm["_id"])
            if state.is_known(raindrop_id):
                continue  # Already tracked — skip

            # Raindrop.io stores uploaded files under the 'file' property
            file_url = ""
            if bm.get("file") and bm["file"].get("url"):
                file_url = bm["file"]["url"]

            new_items.append({
                "raindrop_id": raindrop_id,
                "title": bm.get("title") or "Untitled",
                "link": bm.get("link", ""),
                "item_type": bm.get("type", "link"),
                "file_url": file_url,
            })
            log.info("New bookmark: [%s] %s", raindrop_id, bm.get("title"))

        return new_items


# ─────────────────────────────────────────────────────────────────────────────
# CONTENT EXTRACTOR
#
# Given a bookmark URL, extracts the readable text from it.
#
# For PDFs: downloads the file and uses pdfminer.six to extract text
# For web pages: fetches the HTML and uses BeautifulSoup to find the main
#   content, stripping navigation, headers, footers, and ads.
#
# NOTE: Some websites block scrapers (paywall, JS-only content). For those,
# extraction will return empty string and the chapter will be skipped with
# a warning in the logs. Selenium support can be added later for JS-heavy pages.
# ─────────────────────────────────────────────────────────────────────────────
class ContentExtractor:
    # Pretend to be a real browser so websites don't block us
    BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )
    }
    # HTML elements that contain navigation/ads/boilerplate — strip before extracting
    NOISE_TAGS = [
        "nav", "header", "footer", "aside", "script", "style",
        "noscript", "iframe", "figure", "figcaption", "form",
    ]

    def extract(self, item: Dict) -> str:
        """
        Main entry point. Returns clean text for the bookmark, or '' on failure.
        Prefers the Raindrop-hosted file URL if available (e.g. uploaded PDFs),
        then falls back to the bookmark's original link.
        """
        url = item.get("file_url") or item.get("link", "")
        if not url:
            log.warning("'%s' has no URL — skipping.", item["title"])
            return ""
        try:
            if self._is_pdf(url):
                return self._extract_pdf(url)
            return self._extract_webpage(url)
        except Exception as exc:
            log.error("Extraction failed for '%s': %s", item["title"], exc)
            return ""

    def _is_pdf(self, url: str) -> bool:
        """Check if a URL points to a PDF (by extension, then by Content-Type header)."""
        if url.lower().split("?")[0].endswith(".pdf"):
            return True
        try:
            head = requests.head(
                url, headers=self.BROWSER_HEADERS, timeout=8, allow_redirects=True
            )
            return "application/pdf" in head.headers.get("Content-Type", "")
        except Exception:
            return False

    def _extract_pdf(self, url: str) -> str:
        """Download a PDF and extract its text using pdfminer.six."""
        log.info("Extracting PDF: %s", url)
        resp = requests.get(url, headers=self.BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
        text = pdf_extract_text(io.BytesIO(resp.content))
        return self._clean(text)

    def _extract_webpage(self, url: str) -> str:
        """Fetch a webpage and extract the main readable content."""
        log.info("Extracting webpage: %s", url)
        resp = requests.get(url, headers=self.BROWSER_HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove clutter elements first
        for tag_name in self.NOISE_TAGS:
            for el in soup.find_all(tag_name):
                el.decompose()

        # Look for the main content area using semantic HTML tags (best practice)
        content = (
            soup.find("article")
            or soup.find("main")
            or soup.find(attrs={"role": "main"})
            or soup.find("body")
            or soup
        )
        return self._clean(content.get_text(separator="\n"))

    @staticmethod
    def _clean(text: str) -> str:
        """Strip blank lines and extra whitespace from extracted text."""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# TTS CONVERTER — ElevenLabs
#
# Converts text to MP3 audio bytes using the ElevenLabs API.
#
# Key challenges handled here:
#   1. CHUNKING: ElevenLabs has a per-request character limit (~5000 chars).
#      Long articles are split at sentence boundaries and converted in parts,
#      then the audio chunks are merged back into one.
#   2. RATE LIMITS: If we hit the API limit, we wait with exponential backoff
#      and retry (e.g. wait 2s, then 4s, then 8s...).
#
# API docs: https://elevenlabs.io/docs/api-reference/text-to-speech
# ─────────────────────────────────────────────────────────────────────────────
class TTSConverter:
    TTS_ENDPOINT = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    MAX_RETRIES = 4

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["xi-api-key"] = Config.ELEVENLABS_API_KEY

    def convert(self, text: str) -> bytes:
        """
        Convert text to MP3 bytes. Handles chunking and merging internally.
        Returns a single MP3 byte string ready to be used in pydub.
        """
        chunks = self._chunk_text(text)
        log.info("TTS: %d chunk(s) for %d total chars", len(chunks), len(text))

        mp3_parts = []
        for i, chunk in enumerate(chunks):
            mp3_parts.append(self._call_api(chunk))
            # Brief pause between API calls to stay within rate limits
            if i < len(chunks) - 1:
                time.sleep(Config.ELEVENLABS_RATE_DELAY)

        return self._merge_mp3s(mp3_parts)

    def _call_api(self, text: str, attempt: int = 0) -> bytes:
        """
        POST one chunk of text to ElevenLabs.
        Retries with exponential backoff on HTTP 429 (rate limit exceeded).
        """
        url = self.TTS_ENDPOINT.format(voice_id=Config.ELEVENLABS_VOICE_ID)
        resp = self.session.post(
            url,
            json={
                "text": text,
                "model_id": "eleven_monolingual_v1",
                "voice_settings": {
                    "stability": 0.5,        # 0=more expressive, 1=more consistent
                    "similarity_boost": 0.75, # how closely to match the voice clone
                },
            },
            timeout=90,  # Long timeout: generating audio takes time for long chunks
        )

        if resp.status_code == 429:
            # Rate limited — back off and retry
            wait_seconds = 2 ** attempt  # 2, 4, 8, 16 seconds
            log.warning(
                "ElevenLabs rate limit hit. Waiting %ds (attempt %d/%d).",
                wait_seconds, attempt + 1, self.MAX_RETRIES,
            )
            if attempt >= self.MAX_RETRIES:
                raise RuntimeError("ElevenLabs: max retries exceeded on rate limit.")
            time.sleep(wait_seconds)
            return self._call_api(text, attempt + 1)

        resp.raise_for_status()
        return resp.content  # Raw MP3 bytes

    @staticmethod
    def _chunk_text(text: str) -> List[str]:
        """
        Split text into chunks no larger than CHUNK_SIZE characters.
        Breaks at sentence endings ('. ') or word boundaries to avoid
        cutting words mid-stream in the audio.
        """
        limit = Config.ELEVENLABS_CHUNK_SIZE
        if len(text) <= limit:
            return [text]

        chunks = []
        while text:
            if len(text) <= limit:
                chunks.append(text)
                break
            # Try to break at a sentence boundary first
            cut = text.rfind(". ", 0, limit)
            if cut == -1:
                # No sentence boundary — try a word boundary
                cut = text.rfind(" ", 0, limit)
            if cut == -1:
                # No word boundary either — hard cut (rare edge case)
                cut = limit
            chunks.append(text[: cut + 1].strip())
            text = text[cut + 1 :].strip()

        return chunks

    @staticmethod
    def _merge_mp3s(parts: List[bytes]) -> bytes:
        """
        Concatenate MP3 byte strings into one.
        MP3 is a streaming format made of independent frames, so joining raw bytes
        produces a valid file that all standard players handle correctly.
        No ffmpeg or external library required.
        """
        return b"".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# AUDIO BUILDER
#
# Assembles the final MP3 from individual per-bookmark audio clips.
# The chapter announcement ("Chapter 1. Title.") is prepended directly to the
# text before sending to ElevenLabs, so the voice reads it naturally as part
# of the same audio clip — no audio editing library needed.
#
# Each chapter's MP3 bytes are concatenated in sequence. MP3 is a streaming
# format built from independent frames, so raw byte concatenation produces a
# valid file that all standard players handle correctly.
# ─────────────────────────────────────────────────────────────────────────────
class AudioBuilder:
    def __init__(self, tts: TTSConverter):
        self.tts = tts

    def build(self, items_with_text: List[Dict]) -> bytes:
        """
        Build the final combined MP3.

        items_with_text: list of dicts, each with 'title' and 'text' keys.
        Returns: single MP3 byte string with all chapters in sequence.
        """
        valid_items = [it for it in items_with_text if it.get("text", "").strip()]
        chapter_audio_parts = []

        for i, item in enumerate(valid_items, start=1):
            log.info(
                "Building chapter %d/%d: '%s' (%d chars)",
                i, len(valid_items), item["title"], len(item["text"]),
            )
            # Merge the spoken chapter title into the content text.
            # ElevenLabs will read the announcement and then the article
            # as one continuous, natural-sounding narration.
            full_text = f"Chapter {i}. {item['title']}.\n\n{item['text']}"
            chapter_audio_parts.append(self.tts.convert(full_text))

        # Join all chapter MP3 bytes into one file
        combined = b"".join(chapter_audio_parts)
        log.info("Final audio assembled: %d chapter(s), %.1f MB", len(valid_items), len(combined) / 1_000_000)
        return combined


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL NOTIFIER — Gmail SMTP
#
# Sends the finished MP3 to yourself via Gmail.
# Uses Python's built-in smtplib — no third-party service or dependency needed.
#
# SETUP (one-time, takes 2 minutes):
#   1. Make sure 2-Step Verification is on for your Google account:
#      myaccount.google.com → Security → 2-Step Verification
#   2. Generate an App Password:
#      myaccount.google.com → Security → App Passwords
#      App name: "Later T2S" → click Create → copy the 16-character code
#   3. Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in Lambda env vars
#
# WHY APP PASSWORD (not your real password):
#   Google blocks regular password logins from apps for security.
#   An App Password is a special one-time code that only works for this purpose.
#   You can revoke it any time from your Google account settings.
# ─────────────────────────────────────────────────────────────────────────────
class EmailNotifier:
    GMAIL_SMTP_HOST = "smtp.gmail.com"
    GMAIL_SMTP_PORT = 587  # TLS port

    def send(self, mp3_bytes: bytes, chapter_count: int, batch_created_at: str):
        """
        Send the MP3 to yourself as a Gmail attachment.

        mp3_bytes:        the audio file content
        chapter_count:    number of articles/chapters in the file
        batch_created_at: ISO timestamp string (used to label the file and subject)
        """
        date_str = batch_created_at[:10]  # Extract "YYYY-MM-DD" from the ISO string
        filename = f"later_audio_{date_str}.mp3"
        subject = f"Your Later Audio — {chapter_count} chapter(s) — {date_str}"
        body = (
            f"Hi,\n\n"
            f"Your {chapter_count} 'Later' bookmark(s) have been converted to audio.\n"
            f"Listen to the attached MP3 whenever suits you.\n\n"
            f"— Text2Voice Agent"
        )

        # Build the email using Python's standard email library
        msg = MIMEMultipart()
        msg["From"] = Config.GMAIL_ADDRESS
        msg["To"] = Config.GMAIL_ADDRESS  # Sending to yourself
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        # Attach the MP3 file
        attachment = MIMEBase("audio", "mpeg")
        attachment.set_payload(mp3_bytes)
        encoders.encode_base64(attachment)  # Required encoding step for binary attachments
        attachment.add_header(
            "Content-Disposition", f'attachment; filename="{filename}"'
        )
        msg.attach(attachment)

        log.info(
            "Sending email to %s via Gmail (%.1f MB MP3, %d chapters)",
            Config.GMAIL_ADDRESS, len(mp3_bytes) / 1_000_000, chapter_count,
        )

        # Connect to Gmail's SMTP server and send
        with smtplib.SMTP(self.GMAIL_SMTP_HOST, self.GMAIL_SMTP_PORT) as smtp:
            smtp.ehlo()       # Introduce ourselves to the server
            smtp.starttls()   # Upgrade the connection to encrypted TLS
            smtp.login(Config.GMAIL_ADDRESS, Config.GMAIL_APP_PASSWORD)
            smtp.send_message(msg)

        log.info("Email sent successfully via Gmail.")


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR
#
# The "conductor" — wires all modules together and runs the two-phase pipeline.
#
# Phase 1 — poll():
#   Ask Raindrop.io "any new 'Later' bookmarks?"
#   If yes → store them in DynamoDB with a 30-minute countdown
#
# Phase 2 — process():
#   Check DynamoDB: "any batches whose 30-minute window has closed?"
#   If yes → extract text, convert to speech, build MP3, send email
# ─────────────────────────────────────────────────────────────────────────────
class Orchestrator:
    def __init__(self):
        self.state = StateManager()
        self.monitor = RaindropMonitor()
        self.extractor = ContentExtractor()
        self.tts = TTSConverter()
        self.builder = AudioBuilder(self.tts)
        self.notifier = EmailNotifier()

    # ── Phase 1 ──────────────────────────────────────────────────────────────
    def poll(self):
        """Detect new bookmarks and queue them into the current open batch."""
        log.info("=== Phase 1: Polling Raindrop.io for new 'Later' bookmarks ===")
        try:
            new_items = self.monitor.get_new_bookmarks(self.state)
        except Exception as exc:
            log.error("Raindrop.io poll failed: %s", exc)
            return

        if not new_items:
            log.info("No new bookmarks found.")
            return

        # Get (or create) the currently open batch
        # All bookmarks added within the 30-min window share the same batch
        batch_id, process_after = self.state.get_or_create_open_batch()

        for item in new_items:
            self.state.add_to_batch(item, batch_id, process_after)

        log.info(
            "Queued %d new bookmark(s) into batch '%s'.", len(new_items), batch_id
        )

    # ── Phase 2 ──────────────────────────────────────────────────────────────
    def process(self):
        """Find batches whose 30-minute window has expired and process them."""
        log.info("=== Phase 2: Checking for batches ready to process ===")
        ready_batches = self.state.get_ready_batches()

        if not ready_batches:
            log.info("No batches ready yet.")
            return

        for batch_id, items in ready_batches.items():
            log.info(
                "Processing batch '%s' with %d item(s)...", batch_id, len(items)
            )
            try:
                self._run_pipeline(batch_id, items)
            except Exception as exc:
                log.error(
                    "Pipeline failed for batch '%s': %s", batch_id, exc, exc_info=True
                )
                # Mark all items in this batch as failed so we can investigate
                for item in items:
                    self.state.mark_status(item["raindrop_id"], "failed")

    def _run_pipeline(self, batch_id: str, items: List[Dict]):
        """
        Full pipeline for one batch: extract → TTS → assemble → email.
        Items that fail extraction are skipped (logged as warnings).
        """
        # Step 1: Extract text from each bookmark
        items_with_text = []
        for item in items:
            text = self.extractor.extract(item)
            if text:
                items_with_text.append({**item, "text": text})
            else:
                log.warning(
                    "Could not extract text from '%s' — skipping this chapter.",
                    item["title"],
                )
                self.state.mark_status(item["raindrop_id"], "failed")

        if not items_with_text:
            log.warning(
                "Batch '%s' had no extractable content — no email sent.", batch_id
            )
            return

        # Step 2: Build the combined MP3
        mp3_bytes = self.builder.build(items_with_text)

        # Step 3: Send the email
        self.notifier.send(mp3_bytes, len(items_with_text), batch_id)

        # Step 4: Mark all successfully processed items
        for item in items_with_text:
            self.state.mark_status(item["raindrop_id"], "processed")

        log.info("Batch '%s' complete. %d chapter(s) delivered.", batch_id, len(items_with_text))


# ─────────────────────────────────────────────────────────────────────────────
# LAMBDA ENTRY POINT
#
# AWS Lambda calls this function every 5 minutes (triggered by EventBridge).
# The `event` and `context` parameters are provided by Lambda automatically —
# we don't use them here but they're required by the Lambda interface.
# ─────────────────────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    log.info(
        "Lambda invoked. RequestId: %s", getattr(context, "aws_request_id", "local")
    )
    orch = Orchestrator()
    orch.poll()     # Phase 1: check for new bookmarks
    orch.process()  # Phase 2: process any ready batches
    return {"statusCode": 200, "body": "OK"}


# ─────────────────────────────────────────────────────────────────────────────
# LOCAL TEST ENTRY POINT
#
# Lets you run and test the agent on your Mac before deploying to Lambda.
# Set all env vars (from .env.example) in your terminal first, then run:
#   python lambda_function.py
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    log.info("Running in local mode (not inside Lambda).")

    class _FakeContext:
        aws_request_id = "local-test"

    result = lambda_handler({}, _FakeContext())
    log.info("Run complete. Result: %s", result)
    sys.exit(0)
