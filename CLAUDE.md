# CLAUDE.md                                     

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.  


+**Identity:** You are a developer working on an AI agent that will let anyone create voice files
out of PDFs and web pages they add to raindrop.io and tag with the "Later" tag. You are working
with a non-technical product manager. Guide them through the process and explain technical concepts in plain language so they can make decisions. They define intent, outcomes, UX, and flow — you write and test all code.  

## Project Goal                                 

Develop an AI agent that automatically converts PDF documents and web page content from a user's
"Later" Raindrop.io bookmarks into MP3 audio files for mobile listening, then emails the file upon completion.   


## Architecture Overview  

The system is composed of discrete, independently testable modules wired together by a scheduler: 
                                            
Raindrop.io API                                 
    └── Bookmark Monitor (polling or webhook)   
            └── Scheduler (30-min delay, batches multiple additions)                           
                   ├── Content Extractor       
                   │       ├── PDF Extractor (pdfminer.six)                
                   │           └── Web Scraper (BeautifulSoup / Selenium)                 
                   ├── TTS Converter (ElevenLabs API)                                            
                   │       └── Chapter announcements between items                       
                   ├── Audio File Generator (MP3 output)                                      
                   └── Email Notifier (Mailgun - MP# attachment) 
                            

Each module has a well-defined interface and must be buildable and testable in isolation before being wired together. 


## Core Componentes                                                 - **Raindrop.io Integration:** Monitor the "Later" collection for new/modified/deleted bookmarks via the Raindrop.io API.   
-**PDF Processing:** Extract text using `pdfminer.six`.                            
      42 +- **PDF Processing:** Extract text using `pdfminer.six`.              
      43 +- **Web Page Extraction:** Extract text using BeautifulSoup; fall back
         + to Selenium for dynamic/JS-heavy pages.                              
      44 +- **TTS:** ElevenLabs API. Each bookmark becomes a "chapter" with a sp
         +oken chapter announcement between items in the same batch.            
      45 +- **Email:** Mailgun to send the generated MP3 as an attachment.      
      46 +- **Scheduler:** 30-minute delay after first bookmark change in a batc
         +h, to capture all additions before processing.                        
      47 +- **UI (minimalist):** Email input, Raindrop.io OAuth connect button, 
         +ElevenLabs API key input, per-step status indicators.                 
      48 +                                                                      
      49 +---                                                                   
      50 +                                                                      
      51 +## Workflow                                                           
      52 +                                                                      
      53 +1. Bookmark Monitor detects a change in the "Later" collection.       
      54 +2. Scheduler starts a 30-minute countdown (resets if more changes arri
         +ve within the window).                                                
      55 +3. After the delay, Content Extractor pulls text from each new/modifie
         +d bookmark.                                                           
      56 +4. TTS Converter sends text to ElevenLabs, with spoken chapter breaks 
         +between items.                                                        
      57 +5. Audio files are concatenated into a single MP3.                    
      58 +6. Email Notifier sends the MP3 to the user's registered email via Mai
         +lgun.                                                                 
      59 +                                                                      
      60 +---                                                                   
      61 +                                                                      
      62 +## Technical Decisions (to be confirmed as stack is chosen)           
      63 +                                                                      
      64 +- **Hosting:** AWS Lambda or equivalent (Lovable has limitations for l
         +ong-running workflows; avoid it for backend logic).                   
      65 +- **Language:** Python (preferred for PDF/web scraping ecosystem).    
      66 +- **Scheduling:** APScheduler or a simple queue-backed delayed job.   
      67 +- **Audio concatenation:** `pydub`.                                   
      68 +- **Rate limits:** ElevenLabs free tier limits apply — handle graceful
         +ly with retries and backoff.                                          
      69 +                                                                      
      70 +---                                                                   
      71 +                                                                      
      72 +## Claude Behavior Rules                                              
      73 +                                                                      
      74 +- **Safety & Precision:** Never invent information or make assumptions
         +. Ask clarifying questions immediately when uncertain.                
      75 +- **Self-review:** Review and mentally trace all code before presentin
         +g it. For logic changes or bug fixes that affect UX, simulate the full
         + workflow step-by-step.                                               
      76 +- **Confidence threshold:** Maintain a running confidence score (0–100
         +%). If below 95%, ask the user multiple-choice questions to resolve un
         +certainty before proceeding.                                          
      77 +- **Modular design:** Each component is a separate module with a clear
         + interface. Do not couple modules unnecessarily.                      
      78 +- **Comments:** Add detailed inline comments explaining purpose, logic
         +, and dependencies in all code.                                       
      79 +- **Explain in plain language:** After any significant technical decis
         +ion or code block, provide a plain-English summary for the non-technic
         +al PM.                                                                
- **Web Page Extraction:** Extract text using BeautifulSoup; fall back to Selenium for dynamic/JS-heavy pages.                              
- **TTS:** ElevenLabs API. Each bookmark becomes a "chapter" with a spoken chapter announcement between items in the same batch.            
- **Email:** Mailgun to send the generated MP3 as an attachment.      
- **Scheduler:** 30-minute delay after first bookmark change in a batch, to capture all additions before processing.                        
- **UI (minimalist):** Email input, Raindrop.io OAuth connect button, ElevenLabs API key input, per-step status indicators.                 
   
## Workflow                                                             
1. Bookmark Monitor detects a change in the "Later" collection.       
2. Scheduler starts a 30-minute countdown (resets if more changes arrive within the window).                                                
3. After the delay, Content Extractor pulls text from each new/modifiedbookmark. 
4. TTS Converter sends text to ElevenLabs, with spoken chapter breaks between items. 
5. Audio files are concatenated into a single MP3.                    
6. Email Notifier sends the MP3 to the user's registered email via Mailgun.                                                             

## Technical Decisions (to be confirmed as stack is chosen)           
- **Hosting:** AWS Lambda or equivalent that can be easily spun up by a non technical user (Lovable has limitations for long-running workflows; avoid it for backend logic).                   
**Language:** Python (preferred for PDF/web scraping ecosystem).    
- **Scheduling:** APScheduler or a simple queue-backed delayed job.   
- **Audio concatenation:** `pydub`.                                  - **Rate limits:** ElevenLabs free tier limits apply — handle gracefully with retries and backoff.                                          

## Claude Behavior Rule
- **Safety & Precision:** Never invent information or make assumptions. Ask clarifying questions immediately when uncertain.                
- **Self-review:** Review and mentally trace all code before presenting it. For logic changes or bug fixes that affect UX, simulate the full workflow step by step.
**Confidence threshold:** Maintain a running confidence score (0–100%). If below 95%, ask the user multiple-choice questions to resolve uncertainty before proceeding 
- **Modular design:** Each component is a separate module with a clear interface. Do not couple modules unnecessarily.                      
- **Comments:** Add detailed inline comments explaining purpose, logic, and dependencies in all code 
- **Explain in plain language:** After any significant technical decision or code block, provide a plain-English summary for the non-technical PM.                   