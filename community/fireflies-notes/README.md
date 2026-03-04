# Fireflies Meeting Notes

## What It Does
Voice access to your Fireflies.ai meeting data. List recent meetings, hear summaries, get action items, search by keyword or participant, check speaker analytics and sentiment, ask Fred natural language questions about your meetings, or send the Fireflies bot to a live call. All by voice.

## Suggested Trigger Words
- "fireflies"
- "meeting notes"
- "meeting summary"
- "action items"
- "summarize my meeting"
- "meeting recap"
- "recent meetings"
- "last meeting"
- "who talked the most"
- "ask fred"
- "search meetings"
- "send fireflies"

## Setup
1. Log in to [app.fireflies.ai](https://app.fireflies.ai)
2. Go to **Settings > Integrations > API**
3. Copy your API key
4. Save it to persistent storage as `fireflies_prefs.json`:
   ```json
   {"api_key": "your_key_here"}
   ```

**Note:** Free/Pro plans have a 50 requests/day API limit. Business plan recommended for heavy use.

## Features
- **Recent meetings** — List your latest meetings with titles, dates, and one-line summaries
- **Meeting summary** — Hear the AI-generated summary and topics for any meeting
- **Action items** — Get action items read back in a concise spoken list
- **Search** — Find meetings by keyword or participant email
- **Speaker analytics** — Who talked the most, word counts, filler words, questions asked
- **Sentiment analysis** — Was the meeting positive, negative, or neutral?
- **AskFred AI Q&A** — Ask natural language questions across your meetings with multi-turn follow-ups
- **Add bot to live meeting** — Send the Fireflies notetaker to a Zoom, Meet, or Teams call
- **Delete transcript** — Remove a meeting transcript (with confirmation)

## Example Conversation
> **User:** "What are my recent meetings?"
> **AI:** "You have 5 recent meetings. Team Standup, today, 15 minutes. Quick sync on sprint priorities. Product Review, yesterday, 45 minutes. Reviewed Q1 roadmap and feature priorities. Investor Call, Monday, 1 hour. Discussed Series A timeline and metrics."

> **User:** "Summarize the investor call"
> **AI:** "Here's the summary of Investor Call, 1 hour. The team presented Q1 metrics showing 40% growth. Discussion focused on Series A timeline, target raise of 5M, and key milestones needed before close. Topics covered: fundraising, metrics, growth, timeline."

> **User:** "What are my action items?"
> **AI:** "1. Send updated financial model to investors by Friday. 2. Schedule follow-up call with lead partner. 3. Prepare customer reference list for due diligence."

> **User:** "Who talked the most?"
> **AI:** "Speaker breakdown for Investor Call. Sarah spoke 45% of the time, 2400 words, 12 questions, 8 filler words. Mike spoke 35% of the time, 1800 words, 3 questions, 15 filler words."

> **User:** "Ask Fred what pricing was discussed"
> **AI:** "Let me ask Fred about that. In the investor call, the team discussed a SaaS pricing model at $49/month for pro and $199/month for business tiers, with annual discounts of 20%."

> **User:** "Done"
> **AI:** "Got it. See you next time."

## SDK Features Used
- `speak()`, `user_response()`, `run_io_loop()` — multi-turn voice conversation
- `text_to_text_response()` — LLM intent classification, JSON parsing, voice formatting
- `run_confirmation_loop()` — delete and add-bot confirmations
- `check_if_file_exists()`, `read_file()`, `write_file()`, `delete_file()` — persistent preferences
- `requests.post()` via `asyncio.to_thread()` — Fireflies GraphQL API calls
