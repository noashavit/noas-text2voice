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
  5. Converts all text to speech via AWS Polly, with "Chapter N: Title" announcements
  6. Combines everything into one MP3 and emails it to you via Gmail

ENVIRONMENT VARIABLES (set these in Lambda → Configuration → Environment variables):
  RAINDROPTOKEN     Your Raindrop.io test token
  GMAILADDRESS      Your Gmail address (used to send AND receive)
  GMAILPASSWORD     A Gmail App Password (NOT your real password — see setup guide)

  POLLYVOICE        (optional) Default: Joanna — see voice list below
  POLLYENGINE       (optional) Default: neural — "neural" sounds natural, "standard" saves quota
  DBTABLE           (optional) Default: text2voice_items
  LATERTAG          (optional) Default: Later
  BATCHDELAY        (optional) Default: 30

FREE TIER NOTES:
  - AWS Lambda:    1M requests/month free
  - DynamoDB:      25 GB free forever
  - AWS Polly:     5M standard chars/month free for 12 months (1M neural chars/month)
  - Gmail:         Free forever, 500 emails/day

POLLY VOICE OPTIONS (set via POLLYVOICE env var):
  Joanna, Kendra, Kimberly, Salli   — US English, female
  Matthew, Joey, Justin             — US English, male
  Amy, Emma                         — British English, female
  Brian                             — British English, male

DEPENDENCIES (pip install these into a folder before zipping — see deploy.sh):
  requests, pdfminer.six, beautifulsoup4, pydub, boto3

NO LAMBDA LAYER REQUIRED:
  Audio is handled using pure Python and raw byte concatenation — no ffmpeg needed.
"""

import io
import os
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple

import re
from collections import Counter

import boto3
import requests
import trafilatura
from botocore.exceptions import ClientError
from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text as pdf_extract_text

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
# Lambda pre-configures its own root logger before our code runs, which makes
# basicConfig() a silent no-op. Instead, we set the level directly on the root
# logger (which Lambda has already wired to CloudWatch) so our INFO messages
# are captured. Without this, all log output is invisible in the console.
logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger("text2voice")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# Loading from environment variables — Lambda lets you set these in the console
# without touching the code. Raises a clear error if a required one is missing.
# ─────────────────────────────────────────────────────────────────────────────
class Config:
    # Required — Lambda will fail immediately with a clear KeyError if missing
    RAINDROP_TOKEN = os.environ["RAINDROPTOKEN"].strip()
    GMAIL_ADDRESS = os.environ["GMAILADDRESS"].strip()
    GMAIL_APP_PASSWORD = os.environ["GMAILPASSWORD"].strip()

    # Optional with sensible defaults
    DYNAMODB_TABLE = os.environ.get("DBTABLE", "text2voice_items")
    LATER_TAG = os.environ.get("LATERTAG", "Later").lstrip("#")  # strip # if accidentally included
    BATCH_DELAY_MINUTES = int(os.environ.get("BATCHDELAY", "30"))
    AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    # AWS Polly TTS settings
    # Free tier: 5 million characters/month for 12 months (neural voices: 1M/month)
    # Neural voices sound much more natural — use "standard" engine to save quota
    # Available neural voices: Joanna, Matthew, Amy, Brian, Emma, Joey, Kendra...
    # Full list: https://docs.aws.amazon.com/polly/latest/dg/ntts-voices-main.html
    POLLY_VOICE = os.environ.get("POLLYVOICE", "Joanna")
    # "standard" works in all AWS regions. "neural" sounds more natural but is
    # only available in select regions (us-east-1, us-west-2, eu-west-1, etc.)
    # If you get a ValidationException about engine not supported, use "standard".
    POLLY_ENGINE = os.environ.get("POLLYENGINE", "standard")



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

        # Look for any pending item whose batch window hasn't expired yet.
        # #pa aliases "process_after" — DynamoDB's reserved word "AFTER" can
        # cause silent failures when attribute names are used without aliases.
        resp = self.table.scan(
            FilterExpression="#s = :pending AND #pa > :now",
            ExpressionAttributeNames={"#s": "status", "#pa": "process_after"},
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
                    "excerpt": item.get("excerpt", ""),
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
        Find all 'pending' batches whose timer has expired.
        Returns a dict of {batch_id: [list of items in that batch]}.

        Uses #pa as an alias for "process_after" — required because DynamoDB
        treats AFTER as a reserved word and silently drops unaliased attributes
        from filter expressions, causing the scan to always return nothing.
        """
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        log.info("Phase 2 scan — current time: %s", now_str)

        # First: log ALL pending items in the table so we can see their state
        debug_resp = self.table.scan(
            FilterExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":pending": "pending"},
        )
        for row in debug_resp.get("Items", []):
            log.info(
                "DynamoDB item: '%s' | process_after: %s | ready: %s",
                row.get("title"),
                row.get("process_after"),
                row.get("process_after", "") <= now_str,
            )

        # Now fetch only the items whose timer has expired
        resp = self.table.scan(
            FilterExpression="#s = :pending AND #pa <= :now",
            ExpressionAttributeNames={"#s": "status", "#pa": "process_after"},
            ExpressionAttributeValues={
                ":pending": "pending",
                ":now": now_str,
            },
        )
        items = resp.get("Items", [])

        # Handle DynamoDB pagination — scan returns max 1MB per call
        while "LastEvaluatedKey" in resp:
            resp = self.table.scan(
                FilterExpression="#s = :pending AND #pa <= :now",
                ExpressionAttributeNames={"#s": "status", "#pa": "process_after"},
                ExpressionAttributeValues={":pending": "pending", ":now": now_str},
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))

        log.info("%d item(s) ready to process", len(items))
        batches: Dict[str, List[Dict]] = {}
        for row in items:
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
        Fetch the 50 most recent bookmarks across all collections, then filter
        locally by checking each bookmark's `tags` array for LATER_TAG.

        WHY NOT USE SEARCH PARAM: Raindrop.io's server-side tag search syntax
        is inconsistent across API versions. Filtering locally on the `tags`
        field is simpler and guaranteed to work — every bookmark object includes
        a `tags` list we can inspect directly.
        """
        resp = self.session.get(
            f"{self.BASE_URL}/raindrops/0",
            params={
                "sort": "-created",  # newest first
                "perpage": 50,
            },
            timeout=15,
        )
        resp.raise_for_status()
        all_items = resp.json().get("items", [])
        log.info("Raindrop.io returned %d total bookmark(s)", len(all_items))

        # Filter locally — check if LATER_TAG appears in each bookmark's tags list.
        # Case-insensitive so "later", "Later", and "LATER" all match.
        tag_to_match = Config.LATER_TAG.lower()
        tagged = [
            bm for bm in all_items
            if tag_to_match in [t.lower() for t in bm.get("tags", [])]
        ]
        log.info(
            "Found %d bookmark(s) tagged '%s'", len(tagged), Config.LATER_TAG
        )
        return tagged

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
                "excerpt": bm.get("excerpt", ""),  # Raindrop's stored preview — used as fallback
            })
            log.info("New bookmark: [%s] %s", raindrop_id, bm.get("title"))

        return new_items


# ─────────────────────────────────────────────────────────────────────────────
# CONTENT EXTRACTOR
#
# Given a bookmark URL, extracts the readable text from it.
#
# For web pages: trafilatura is the primary extractor — it is purpose-built to
#   strip navigation, metadata, ads, and code from HTML, including React/Next.js
#   pages. BeautifulSoup is kept as a fallback for sites trafilatura can't parse.
#
# For PDFs: pdfminer.six extracts raw text, then a cleaning pass removes lines
#   with garbled/non-printable characters (bad font encodings), repeated
#   headers/footers, and page numbers.
#
# Both paths run a final sanity check: if the result still contains too many
# non-prose characters (code symbols, escape sequences), it is discarded and the
# Raindrop excerpt is used instead.
# ─────────────────────────────────────────────────────────────────────────────
class ContentExtractor:
    # Full browser headers — many sites check for Accept/Accept-Language in
    # addition to User-Agent and return 403 if they look bot-like
    BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    # HTML elements that contain navigation/ads/boilerplate — used by the
    # BeautifulSoup fallback path; trafilatura handles this internally.
    NOISE_TAGS = [
        "nav", "header", "footer", "aside", "script", "style",
        "noscript", "iframe", "figure", "figcaption", "form",
    ]
    # Characters that appear in code/JSON/CSS but rarely in prose.
    # If these make up more than 12% of the extracted text, it is likely garbage.
    CODE_CHARS = set('{}[]\\|<>=_^~`@$')

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
                text = self._extract_pdf(url)
            else:
                text = self._extract_webpage(url)
        except Exception as exc:
            log.error("Extraction failed for '%s': %s", item["title"], exc)
            text = ""

        # Sanity check: if the text is too garbled (too many code/symbol chars),
        # discard it. This catches cases where extraction "succeeds" but returns
        # minified JS, JSON blobs, or font-encoding garbage instead of prose.
        if text and not self._is_clean_text(text):
            log.warning(
                "'%s': extracted text failed sanity check (too many code characters) — discarding.",
                item["title"],
            )
            text = ""

        if text:
            return text

        # Final fallback: the short excerpt Raindrop stores for every bookmark.
        # Not the full article, but better than skipping the chapter entirely.
        excerpt = item.get("excerpt", "").strip()
        if excerpt:
            log.info(
                "Using Raindrop excerpt as fallback for '%s' (%d chars)",
                item["title"], len(excerpt),
            )
            return (
                f"Note: the full article could not be retrieved from this website. "
                f"Here is a short preview:\n\n{excerpt}"
            )
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
        """Download a PDF and extract its text, then filter out garbled lines."""
        log.info("Extracting PDF: %s", url)
        resp = requests.get(url, headers=self.BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
        raw = pdf_extract_text(io.BytesIO(resp.content))
        filtered = self._filter_pdf_text(raw)
        log.info(
            "PDF extraction: %d raw chars → %d after filtering", len(raw), len(filtered)
        )
        return self._clean(filtered)

    def _filter_pdf_text(self, text: str) -> str:
        """
        Remove common PDF artifacts from pdfminer output:

        1. Garbled lines — PDFs with embedded custom fonts and no Unicode mapping
           produce strings of random characters. We detect these by checking what
           fraction of each line's characters fall outside printable ASCII.

        2. Repeated short lines — page headers and footers (e.g. "Databricks | 2025",
           "CONFIDENTIAL") repeat on every page. Any short line that appears more than
           twice is almost certainly a running header or footer.

        3. Page numbers — lines that are just a number, or match patterns like
           "Page 3 of 12" or "3 / 12".
        """
        lines = text.splitlines()

        # Count how often each short line appears (long lines are never headers)
        short_line_counts = Counter(
            ln.strip() for ln in lines
            if ln.strip() and len(ln.strip()) < 80
        )

        # How many non-empty lines are there total? Used to set the repeat threshold.
        total_nonempty = sum(1 for ln in lines if ln.strip())
        # A line repeated in more than 5% of all lines is treated as a header/footer.
        # Floor of 3 so we don't over-filter very short documents.
        repeat_threshold = max(3, total_nonempty * 0.05)

        page_number_re = re.compile(
            r"^\d+$"                          # bare number: "12"
            r"|^[Pp]age\s+\d+(\s+of\s+\d+)?$"  # "Page 3" or "Page 3 of 12"
            r"|^\d+\s*/\s*\d+$"              # "3 / 12"
        )

        filtered = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                filtered.append("")
                continue

            # Drop repeated header/footer lines
            if len(stripped) < 80 and short_line_counts[stripped] > repeat_threshold:
                continue

            # Drop page number lines
            if page_number_re.match(stripped):
                continue

            # Drop lines with too many non-printable or non-ASCII characters.
            # Garbled font encoding shows up as characters with ord > 127 or < 32.
            non_printable = sum(
                1 for c in stripped
                if ord(c) > 127 or (ord(c) < 32 and c not in "\t")
            )
            if non_printable / len(stripped) > 0.20:
                continue

            filtered.append(line)

        return "\n".join(filtered)

    def _extract_webpage(self, url: str) -> str:
        """
        Fetch a webpage and extract the main readable content.

        Extraction chain (each step only runs if the previous returned too little):
          1. Convert AMP CDN URLs to their canonical form before fetching.
          2. trafilatura — fast, no JS, excellent for server-rendered pages.
          3. Jina Reader — free service that renders JS-heavy pages (React/Next.js,
             Substack, etc.) via a headless browser and returns clean text.
          4. BeautifulSoup — dumb HTML scrape, last resort.
        """
        # Step 0: Unwrap AMP CDN URLs before fetching so we get the real article.
        url = self._deamp_url(url)

        log.info("Extracting webpage: %s", url)
        resp = requests.get(url, headers=self.BROWSER_HEADERS, timeout=15)
        resp.raise_for_status()

        # ── Primary: trafilatura ────────────────────────────────────────────
        # favor_precision=True tells trafilatura to skip any block it isn't
        # confident about — we'd rather have less text than garbled text.
        try:
            text = trafilatura.extract(
                resp.text,
                url=url,           # helps trafilatura apply site-specific heuristics
                include_comments=False,
                include_tables=False,
                favor_precision=True,
                no_fallback=False,  # allow trafilatura's own fallback extractors
            )
        except Exception as exc:
            log.warning("trafilatura raised an exception for %s: %s", url, exc)
            text = None

        # Require at least 150 words — fewer likely means navigation fragments,
        # not a real article (catches JS-rendered pages where requests gets a shell).
        if text and self._sufficient_content(text):
            log.info(
                "trafilatura extracted %d chars (%d words) from %s",
                len(text), len(text.split()), url,
            )
            return self._clean(text)

        word_count = len(text.split()) if text else 0
        log.info(
            "trafilatura returned only %d word(s) from %s — trying Jina Reader", word_count, url
        )

        # ── Fallback 1: Jina Reader ─────────────────────────────────────────
        # r.jina.ai renders the page with a headless browser, then returns clean
        # article text. Solves JS-heavy sites (React/Next.js, Substack) that
        # return an empty HTML shell to a plain requests.get() call.
        jina_text = self._extract_via_jina(url)
        if jina_text and self._sufficient_content(jina_text):
            log.info(
                "Jina Reader extracted %d chars (%d words) from %s",
                len(jina_text), len(jina_text.split()), url,
            )
            return self._clean(jina_text)

        word_count = len(jina_text.split()) if jina_text else 0
        log.info(
            "Jina Reader returned only %d word(s) from %s — falling back to BeautifulSoup",
            word_count, url,
        )

        # ── Fallback 2: BeautifulSoup ───────────────────────────────────────
        soup = BeautifulSoup(resp.text, "html.parser")

        for tag_name in self.NOISE_TAGS:
            for el in soup.find_all(tag_name):
                el.decompose()

        container = (
            soup.find("article")
            or soup.find("main")
            or soup.find(attrs={"role": "main"})
            or soup.find("body")
            or soup
        )

        # Only collect text from known prose elements — avoids picking up
        # JSON blobs and template code that frameworks embed in div/span elements.
        prose_tags = ["h1", "h2", "h3", "h4", "p", "li", "blockquote"]
        blocks = []
        for el in container.find_all(prose_tags):
            t = el.get_text(separator=" ").strip()
            if len(t) > 20:
                blocks.append(t)

        if blocks:
            return self._clean("\n".join(blocks))

        return self._clean(container.get_text(separator="\n"))

    @staticmethod
    def _deamp_url(url: str) -> str:
        """
        Convert Google AMP CDN URLs to the canonical article URL.

        AMP CDN format:  https://{publisher}.cdn.ampproject.org/c/s/{domain}/{path}
        Canonical format: https://{domain}/{path}  (with /amp/ path segment stripped)

        Example:
          https://www-cnbc-com.cdn.ampproject.org/c/s/www.cnbc.com/amp/2026/04/21/article.html
          → https://www.cnbc.com/2026/04/21/article.html
        """
        amp_match = re.match(r'https?://[^/]+\.cdn\.ampproject\.org/c/s/(.+)', url)
        if amp_match:
            canonical = 'https://' + amp_match.group(1)
            # Some publishers (e.g. CNBC) use /amp/ in their AMP path — strip it
            canonical = re.sub(r'/amp/', '/', canonical)
            log.info("De-AMPed URL: %s → %s", url, canonical)
            return canonical
        return url

    @staticmethod
    def _sufficient_content(text: str) -> bool:
        """
        Return True if the text has enough words to be real article content.
        Fewer than 150 words usually means we got navigation fragments or an
        empty JS shell rather than the actual article body.
        """
        return bool(text) and len(text.split()) >= 150

    def _extract_via_jina(self, url: str) -> str:
        """
        Fetch clean article text via Jina Reader (r.jina.ai).

        Jina renders the page with a headless browser before extracting text,
        which makes it the right tool for React/Next.js sites and other pages
        that load content via JavaScript. No API key required.

        Jina returns Markdown-formatted text, so we strip formatting symbols
        before returning — otherwise Polly reads "asterisk asterisk bold text
        asterisk asterisk" aloud.

        Returns empty string on any failure so callers can fall through cleanly.
        """
        jina_url = f"https://r.jina.ai/{url}"
        log.info("Trying Jina Reader: %s", jina_url)
        try:
            resp = requests.get(
                jina_url,
                headers={"Accept": "text/plain", **self.BROWSER_HEADERS},
                timeout=30,  # Jina renders JS so it's slower than a plain fetch
            )
            resp.raise_for_status()
            return self._strip_markdown(resp.text.strip())
        except Exception as exc:
            log.warning("Jina Reader failed for %s: %s", url, exc)
            return ""

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """
        Remove Markdown syntax from Jina Reader output so Polly reads clean prose.

        Jina returns text with bold (**word**), headers (## Heading), links
        ([text](url)), and code fences — all of which Polly reads as literal
        symbols. This strips them down to speakable plain text.
        """
        # Drop fenced code blocks entirely — code is not speakable prose
        text = re.sub(r'```[\s\S]*?```', '', text)
        # Strip inline code backticks but keep the content word
        text = re.sub(r'`([^`\n]+)`', r'\1', text)
        # Strip Markdown headers — keep the heading text
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
        # Strip bold and italic markers (**, *, __, _)
        text = re.sub(r'\*{1,3}([^*\n]+)\*{1,3}', r'\1', text)
        text = re.sub(r'_{1,3}([^_\n]+)_{1,3}', r'\1', text)
        # Convert Markdown links [text](url) → text only
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
        # Drop image tags entirely
        text = re.sub(r'!\[[^\]]*\]\([^)]+\)', '', text)
        # Drop bare URLs (https://... standalone)
        text = re.sub(r'https?://\S+', '', text)
        # Strip blockquote markers
        text = re.sub(r'^>\s*', '', text, flags=re.MULTILINE)
        # Strip horizontal rules
        text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
        # Strip Jina's metadata header lines (Title:, URL Source:, Published Time:)
        text = re.sub(r'^(Title|URL Source|Published Time|Markdown Content):\s*.*$', '', text, flags=re.MULTILINE)
        # Collapse runs of blank lines left behind by removals
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    @staticmethod
    def _is_clean_text(text: str) -> bool:
        """
        Return True if the text looks like readable prose.
        Return False if it appears to be code, JSON, minified CSS, or font garbage.

        We measure what fraction of characters are typical code/symbol characters.
        Prose articles very rarely exceed 5%. We use 12% as a generous threshold
        to avoid false positives on articles that discuss code topics.
        """
        if not text or len(text) < 50:
            return False
        code_char_count = sum(1 for c in text if c in ContentExtractor.CODE_CHARS)
        ratio = code_char_count / len(text)
        if ratio > 0.12:
            log.warning(
                "Text sanity check: %.1f%% code characters — treating as garbled.", ratio * 100
            )
            return False
        return True

    @staticmethod
    def _clean(text: str) -> str:
        """Strip blank lines and extra whitespace from extracted text."""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# TTS CONVERTER — AWS Polly
#
# Converts text to MP3 audio bytes using AWS Polly — Amazon's text-to-speech
# service. Already integrated with your AWS account via boto3 (no new packages,
# no new API keys). Just needs AmazonPollyFullAccess added to the Lambda IAM role.
#
# Free tier: 5 million standard characters/month for 12 months.
#            1 million neural characters/month for 12 months.
# After free tier: ~$4 per million standard chars (very cheap).
#
# Polly's per-request limit is 3,000 characters, so long articles are chunked
# at sentence boundaries and the resulting MP3 parts are concatenated.
#
# Docs: https://docs.aws.amazon.com/polly/latest/dg/API_SynthesizeSpeech.html
# ─────────────────────────────────────────────────────────────────────────────
class TTSConverter:
    # Polly's hard limit per request — stay under it with a small buffer
    POLLY_CHUNK_SIZE = 2800

    def __init__(self):
        self.client = boto3.client("polly", region_name=Config.AWS_REGION)

    def convert(self, text: str) -> bytes:
        """
        Convert text to MP3 bytes using AWS Polly.
        Chunks long text and concatenates the results into one MP3.
        """
        chunks = self._chunk_text(text)
        log.info("TTS: %d chunk(s) for %d total chars", len(chunks), len(text))
        parts = [self._call_polly(chunk) for chunk in chunks]
        return b"".join(parts)

    def _call_polly(self, text: str) -> bytes:
        """Send one chunk of text to Polly and return raw MP3 bytes."""
        response = self.client.synthesize_speech(
            Text=text,
            OutputFormat="mp3",
            VoiceId=Config.POLLY_VOICE,
            Engine=Config.POLLY_ENGINE,
        )
        return response["AudioStream"].read()

    @staticmethod
    def _chunk_text(text: str) -> List[str]:
        """
        Split text into chunks no larger than POLLY_CHUNK_SIZE characters.
        Breaks at sentence boundaries to avoid cutting words mid-audio.
        """
        limit = TTSConverter.POLLY_CHUNK_SIZE
        if len(text) <= limit:
            return [text]

        chunks = []
        while text:
            if len(text) <= limit:
                chunks.append(text)
                break
            cut = text.rfind(". ", 0, limit)
            if cut == -1:
                cut = text.rfind(" ", 0, limit)
            if cut == -1:
                cut = limit
            chunks.append(text[: cut + 1].strip())
            text = text[cut + 1:].strip()

        return chunks


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
            text = item["text"]

            # Merge the spoken chapter title into the content text.
            # Polly will read the announcement and then the article
            # as one continuous, natural-sounding narration.
            full_text = f"Chapter {i}. {item['title']}.\n\n{text}"
            log.info(
                "Chapter %d — first 300 chars sent to Polly: %s",
                i, repr(full_text[:300]),
            )
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
                # Leave items as "pending" so the next Lambda run retries automatically.
                # We only mark individual items "failed" inside _run_pipeline when
                # content extraction fails for that specific item — not for transient
                # errors like a wrong API key or a temporary network issue.

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
