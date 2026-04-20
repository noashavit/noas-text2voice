<img width="80" height="80" alt="noas-icon" src="https://github.com/user-attachments/assets/e71f9c41-a674-4fed-8676-d6704529d2b4" />

# Bookmark to audiobook with text-to-voice
An agent to help you get to all those "read later" items using free tools.

Automatically converts new bookmarks you save to Raindrop.io into an MP3 audiobook and emails it to you. 
The agent runs every 5 mins and batches audiobooks based on the bookmarks added in a 30 mins window. It then emails the audiofile to you. Totally hands free.

Simply save and tag any article or PDF in Raindrop.io with `Later`. The agent batches everything you tagged every 30 mins and converts it to speech using ElevenLabs. 
Once converted, the agent sends the audio file straight to your inbox (currently only supporting Gmail).

---

## How it works

1. **Tag** any article or PDF in Raindrop.io with `Later`
2. **Wait**: the agent runs every 30 minutes and batches bookmarks for 5 minutes (so everything you save in one sitting lands in one file)
3. **Listen**: a single MP3 arrives in your inbox, with each article announced as a chapter

---

## Stack

| Layer | Service | Cost |
|---|---|---|
| Runtime | AWS Lambda | Free tier |
| State | AWS DynamoDB | Free tier |
| Voice | ElevenLabs API | Free (10k chars/month) |
| Email | Gmail SMTP | Free |
| Bookmarks | Raindrop.io API | Free |

---

## Setup

### What you need before starting

- An [AWS account](https://aws.amazon.com) (free tier)
- A [Raindrop.io](https://raindrop.io) account
- An [ElevenLabs](https://elevenlabs.io) account (free tier)
- A Gmail account with [2-Step Verification](https://myaccount.google.com/security) enabled
- Python 3.11 installed on your Mac (`brew install python3`)

---

### Step 1 — Raindrop.io token
1. Go to [app.raindrop.io/settings/integrations](https://app.raindrop.io/settings/integrations)
2. Scroll to "For Developers" → click **Create test token**
3. Copy the token

---

### Step 2 — ElevenLabs API key
1. Sign up at [elevenlabs.io](https://elevenlabs.io)
2. Go to Settings → API Keys → copy your key
3. When creating the key, enable only: **Text to Speech → Access** and **Voices → Read**

> **Free tier limit:** 10,000 characters/month ≈ 1–2 average articles. Upgrade to the $5/month Starter plan for more.

---

### Step 3 — Gmail App Password
1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Confirm 2-Step Verification is on
3. Search for **App Passwords** → create one named `Later T2S`
4. Copy the 16-character code

---

### Step 4 — Create a DynamoDB table
1. Open [AWS Console](https://console.aws.amazon.com) → search **DynamoDB** → **Create table**
2. Table name: `text2voice_items`
3. Partition key: `raindrop_id` (type: String)
4. Leave all other settings as default → **Create table**

---

### Step 5 — Create the Lambda function
1. In AWS Console → search **Lambda** → **Create function**
2. Choose **Author from scratch**
3. Name: `text2voice-agent`, Runtime: **Python 3.11**
4. Click **Create function**
5. Go to **Configuration → General configuration** → set Timeout to **14 minutes**
6. Go to **Configuration → Environment variables** → add the following:

| Key | Value |
|---|---|
| `RAINDROPTOKEN` | Your Raindrop.io test token |
| `ELEVENLABSKEY` | Your ElevenLabs API key |
| `ELEVENLABSVOICE` | `21m00Tcm4TlvDq8ikWAM` |
| `GMAILADDRESS` | Your Gmail address |
| `GMAILPASSWORD` | Your 16-character App Password |
| `DBTABLE` | `text2voice_items` |
| `LATERTAG` | `Later` |
| `BATCHDELAY` | `5` |

> Type key names directly — do not copy/paste them, as invisible spaces can cause errors.

---

### Step 6 — Give Lambda permission to use DynamoDB
1. In your Lambda function → **Configuration → Permissions** → click the role name
2. In IAM → **Add permissions → Attach policies**
3. Search `AmazonDynamoDBFullAccess` → check it → **Add permissions**

---

### Step 7 — Deploy the code
In Terminal:
```bash
cd "/path/to/this/repo"
chmod +x deploy.sh
./deploy.sh
```

Then in Lambda → **Code** tab → **Upload from → .zip file** → select `lambda_package.zip`.

---

### Step 8 — Set up the 30-minute trigger
1. In Lambda → **Add trigger** → choose **EventBridge (CloudWatch Events)**
2. Create a new rule → type: **Schedule expression**
3. Value: `rate(30 minutes)`
4. Click **Add**

---

### Step 9 — Test it
1. Add a bookmark in Raindrop.io and tag it `Later`
2. In Lambda → **Test** → create a test event → click **Test**
3. Check the logs — you should see the bookmark detected and queued
4. After 30 minutes, click **Test** again — the MP3 will be generated and emailed to you

---

## Environment variables reference

| Key | Required | Default | Description |
|---|---|---|---|
| `RAINDROPTOKEN` | Yes | — | Raindrop.io test token |
| `ELEVENLABSKEY` | Yes | — | ElevenLabs API key |
| `GMAILADDRESS` | Yes | — | Gmail address (send + receive) |
| `GMAILPASSWORD` | Yes | — | Gmail App Password |
| `ELEVENLABSVOICE` | No | `21m00Tcm4TlvDq8ikWAM` | ElevenLabs voice ID |
| `DBTABLE` | No | `text2voice_items` | DynamoDB table name |
| `LATERTAG` | No | `Later` | Raindrop.io tag to watch |
| `BATCHDELAY` | No | `5` | Minutes to wait before processing |

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
| `ContentExtractor` | Pulls text from web pages and PDFs |
| `TTSConverter` | Sends text to ElevenLabs, handles chunking and rate limits |
| `AudioBuilder` | Assembles chapters into a single MP3 |
| `EmailNotifier` | Sends the MP3 via Gmail SMTP |
| `Orchestrator` | Wires all modules together |

## Known limitations

- Only supports Raindrop.io free bookmark manager
- Only support Gmail email clients
- Only tested in Mac and iPhone, support for other systems isn't garunteed  
---

## Built by

[Noa Shavit](https://www.linkedin.com/in/noashavit), product marketer and AI builder based in San Francisco.
