<img width="80" height="80" alt="noas-icon" src="https://github.com/user-attachments/assets/e71f9c41-a674-4fed-8676-d6704529d2b4" />

# Bookmark to audiobook with text-to-voice
An agent to help you get to all those "read later" items using free tools.

Automatically converts new bookmarks you save to Raindrop.io into an MP3 audiobook and emails it to you. 
The agent runs every 30 minutes and batches audiobooks based on the bookmarks added in a 5-minute window. It then emails the audio file to you. Totally hands free.

Simply save and tag any article or PDF in Raindrop.io with `Later`. The agent batches everything you tagged and converts it to speech using AWS Polly.
Once converted, the agent sends the audio file straight to your inbox (currently only supporting Gmail).

---

## How it works

1. **Tag** any article or PDF in Raindrop.io with `Later`
2. **Wait**: the agent runs every 30 minutes and batches bookmarks saved within a 5-minute window (so everything you save in one sitting lands in one file)
3. **Listen**: a single MP3 arrives in your inbox, with each article announced as a chapter

---

## Stack

| Layer | Service | Cost |
|---|---|---|
| Runtime | AWS Lambda | Free tier |
| State | AWS DynamoDB | Free tier |
| Voice | AWS Polly | Free tier (5M chars/month for 12 months) |
| Email | Gmail SMTP | Free |
| Bookmarks | Raindrop.io API | Free |

---

## Setup

### What you need before starting

- An [AWS account](https://aws.amazon.com) (free tier)
- A [Raindrop.io](https://raindrop.io) account
- A Gmail account with [2-Step Verification](https://myaccount.google.com/security) enabled
- Python 3.11 installed on your Mac (`brew install python3`)

---

### Step 1 — Raindrop.io token
1. Go to [app.raindrop.io/settings/integrations](https://app.raindrop.io/settings/integrations)
2. Scroll to "For Developers" → click **Create test token**
3. Copy the token

---

### Step 2 — Gmail App Password
1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Confirm 2-Step Verification is on
3. Search for **App Passwords** → create one named `Later T2S`
4. Copy the 16-character code

---

### Step 3 — Create a DynamoDB table
1. Open [AWS Console](https://console.aws.amazon.com) → search **DynamoDB** → **Create table**
2. Table name: `text2voice_items`
3. Partition key: `raindrop_id` (type: String)
4. Leave all other settings as default → **Create table**

---

### Step 4 — Create the Lambda function
1. In AWS Console → search **Lambda** → **Create function**
2. Choose **Author from scratch**
3. Name: `text2voice-agent`, Runtime: **Python 3.11**
4. Click **Create function**
5. Go to **Configuration → General configuration** → set Timeout to **14 minutes** and Memory to **512 MB**
6. Go to **Configuration → Environment variables** → add the following:

| Key | Value |
|---|---|
| `RAINDROPTOKEN` | Your Raindrop.io test token |
| `GMAILADDRESS` | Your Gmail address |
| `GMAILPASSWORD` | Your 16-character App Password |
| `DBTABLE` | `text2voice_items` |
| `LATERTAG` | `Later` |
| `BATCHDELAY` | `5` |
| `POLLYVOICE` | `Joanna` |
| `POLLYENGINE` | `standard` |
| `MAXCHARS` | `8000` |

> Type key names directly — do not copy/paste them, as invisible spaces can cause errors.

---

### Step 5 — Give Lambda permission to use DynamoDB and Polly
1. In your Lambda function → **Configuration → Permissions** → click the role name
2. In IAM → **Add permissions → Attach policies**
3. Search `AmazonDynamoDBFullAccess` → check it → **Add permissions**
4. Repeat and search `AmazonPollyFullAccess` → check it → **Add permissions**

---

### Step 6 — Deploy the code
In Terminal:
```bash
cd "/path/to/this/repo"
chmod +x deploy.sh
./deploy.sh
```

Then in Lambda → **Code** tab → **Upload from → .zip file** → select `lambda_package.zip`.

---

### Step 7 — Set up the 30-minute trigger
1. In Lambda → **Add trigger** → choose **EventBridge (CloudWatch Events)**
2. Create a new rule → type: **Schedule expression**
3. Value: `rate(30 minutes)`
4. Click **Add**

---

### Step 8 — Test it
1. Add a bookmark in Raindrop.io and tag it `Later`
2. In Lambda → **Test** → create a test event → click **Test**
3. Check the logs — you should see the bookmark detected and queued
4. After 5 minutes, click **Test** again — the MP3 will be generated and emailed to you

---

## Environment variables reference

| Key | Required | Default | Description |
|---|---|---|---|
| `RAINDROPTOKEN` | Yes | — | Raindrop.io test token |
| `GMAILADDRESS` | Yes | — | Gmail address (send + receive) |
| `GMAILPASSWORD` | Yes | — | Gmail App Password |
| `DBTABLE` | No | `text2voice_items` | DynamoDB table name |
| `LATERTAG` | No | `Later` | Raindrop.io tag to watch |
| `BATCHDELAY` | No | `5` | Minutes to wait before processing |
| `POLLYVOICE` | No | `Joanna` | AWS Polly voice ID |
| `POLLYENGINE` | No | `standard` | AWS Polly engine (`standard` or `neural`) |
| `MAXCHARS` | No | `8000` | Max characters per article before truncation |

---

## Project structure

```
lambda_function.py   Main agent — all modules in one file
requirements.txt     Python dependencies
deploy.sh            Packages the agent into a Lambda-ready ZIP
.env.example         Template for environment variables
```

### Modules inside `lambda_function.py`

| Class | What it does |
|---|---|
| `Config` | Loads all settings from environment variables |
| `StateManager` | Reads/writes bookmark state to DynamoDB |
| `RaindropMonitor` | Fetches and filters bookmarks from Raindrop.io |
| `ContentExtractor` | Extracts article text from web pages (via trafilatura) and PDFs (via pdfminer), with garbled-content filtering and a BeautifulSoup fallback |
| `TTSConverter` | Sends text to AWS Polly, handles chunking and retries |
| `AudioBuilder` | Assembles chapters into a single MP3 |
| `EmailNotifier` | Sends the MP3 via Gmail SMTP |
| `Orchestrator` | Wires all modules together |

## Known limitations

- Only supports Raindrop.io free bookmark manager
- Only supports Gmail email clients
- Only tested on Mac and iPhone; support for other systems isn't guaranteed

---

## Built by

[Noa Shavit](https://www.linkedin.com/in/noashavit), product marketer and AI builder based in San Francisco.
