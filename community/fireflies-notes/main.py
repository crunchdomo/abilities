import asyncio
import json
from datetime import datetime

import requests
from src.agent.capability import MatchingCapability
from src.agent.capability_worker import CapabilityWorker
from src.main import AgentWorker

# =============================================================================
# FIREFLIES MEETING NOTES
# Voice access to your Fireflies.ai meeting transcripts, summaries, action
# items, speaker analytics, sentiment analysis, and AI Q&A (AskFred).
# Say "fireflies" or "meeting notes" to get started.
# =============================================================================

FIREFLIES_ENDPOINT = "https://api.fireflies.ai/graphql"
PREFS_FILE = "fireflies_prefs.json"

EXIT_WORDS = {
    "stop", "exit", "quit", "done", "cancel", "bye", "goodbye", "leave",
    "nothing else", "all good", "nope", "no thanks",
    "i'm good", "that's all", "all done", "finished",
}

# ---------------------------------------------------------------------------
# Intent classification prompt
# ---------------------------------------------------------------------------
INTENT_SYSTEM_PROMPT = (
    "You are a voice assistant that helps users access their Fireflies meeting "
    "data. Classify the user's request and extract relevant parameters. "
    "Return ONLY valid JSON, no other text."
)

INTENT_PROMPT = """Classify this request into one of these intents:

1. "recent_meetings" - wants to see recent/latest meetings
2. "meeting_summary" - wants a summary of a specific meeting
3. "action_items" - wants action items from a meeting
4. "search" - wants to find meetings by keyword or participant
5. "speaker_analytics" - wants to know who spoke, how much, talk ratios
6. "sentiment" - wants to know the tone/mood of a meeting
7. "askfred" - wants to ask a natural language question about meeting content
8. "add_bot" - wants to send the Fireflies bot to a live meeting
9. "delete" - wants to delete a meeting transcript
10. "exit" - wants to stop or leave
11. "unknown" - can't determine intent

Also extract any meeting identifier (title, date, or description like "last meeting", "the standup").

User input: "{user_input}"

Return ONLY JSON:
{{"intent": "string", "meeting_hint": "string or null", "search_keyword": "string or null", "participant_email": "string or null", "meeting_link": "string or null", "question_for_fred": "string or null"}}
"""

# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------
RECENT_MEETINGS_QUERY = """
query RecentMeetings($limit: Int) {
    transcripts(limit: $limit, mine: true) {
        id
        title
        date
        duration
        summary {
            gist
            meeting_type
        }
    }
}
"""

MEETING_SUMMARY_QUERY = """
query MeetingSummary($transcriptId: String!) {
    transcript(id: $transcriptId) {
        id
        title
        date
        duration
        summary {
            short_summary
            overview
            gist
            topics_discussed
            meeting_type
        }
    }
}
"""

ACTION_ITEMS_QUERY = """
query ActionItems($transcriptId: String!) {
    transcript(id: $transcriptId) {
        title
        summary {
            action_items
        }
    }
}
"""

SEARCH_QUERY = """
query SearchMeetings($keyword: String, $limit: Int, $participants: [String]) {
    transcripts(keyword: $keyword, limit: $limit, participants: $participants) {
        id
        title
        date
        duration
        summary {
            gist
        }
    }
}
"""

SPEAKER_ANALYTICS_QUERY = """
query SpeakerAnalytics($transcriptId: String!) {
    transcript(id: $transcriptId) {
        title
        duration
        analytics {
            speakers {
                name
                duration
                word_count
                duration_pct
                filler_words
                questions
                longest_monologue
                words_per_minute
            }
        }
    }
}
"""

SENTIMENT_QUERY = """
query Sentiment($transcriptId: String!) {
    transcript(id: $transcriptId) {
        title
        analytics {
            sentiments {
                positive_pct
                negative_pct
                neutral_pct
            }
        }
    }
}
"""

ASKFRED_CREATE_QUERY = """
mutation CreateAskFredThread($question: String!, $transcriptIds: [String]) {
    createAskFredThread(question: $question, transcript_ids: $transcriptIds) {
        id
        messages { role content }
        suggested_questions
    }
}
"""

ASKFRED_CONTINUE_QUERY = """
mutation ContinueAskFredThread($threadId: String!, $question: String!) {
    continueAskFredThread(thread_id: $threadId, question: $question) {
        id
        messages { role content }
        suggested_questions
    }
}
"""

ADD_BOT_MUTATION = """
mutation AddToLiveMeeting($meetingLink: String!, $title: String, $duration: Int) {
    addToLiveMeeting(meeting_link: $meetingLink, title: $title, duration: $duration) {
        success
    }
}
"""

DELETE_TRANSCRIPT_MUTATION = """
mutation DeleteTranscript($id: String!) {
    deleteTranscript(id: $id) {
        title
        date
    }
}
"""


class FirefliesNotesCapability(MatchingCapability):
    worker: AgentWorker = None
    capability_worker: CapabilityWorker = None

    api_key: str = None
    recent_meetings: list = None
    current_meeting: dict = None
    askfred_thread_id: str = None
    prefs: dict = None

    @classmethod
    def register_capability(cls) -> "MatchingCapability":
        return cls(
            unique_name="fireflies_notes",
            matching_hotwords=[
                "fireflies", "meeting notes", "meeting summary",
                "action items", "what happened in my meeting",
                "summarize my meeting", "meeting recap",
                "who talked the most", "speaker analytics",
                "meeting sentiment", "ask fred",
                "send fireflies", "add fireflies to meeting",
                "find meeting", "search meetings",
                "my meetings", "recent meetings",
                "last meeting", "meeting transcript",
            ],
        )

    def call(self, worker: AgentWorker):
        self.worker = worker
        self.capability_worker = CapabilityWorker(self.worker)
        self.recent_meetings = []
        self.current_meeting = None
        self.askfred_thread_id = None
        self.prefs = {}
        self.worker.session_tasks.create(self.run())

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------
    def log(self, msg):
        self.worker.editor_logging_handler.info(f"[Fireflies] {msg}")

    def log_err(self, msg):
        self.worker.editor_logging_handler.error(f"[Fireflies] {msg}")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    async def load_prefs(self):
        try:
            if await self.capability_worker.check_if_file_exists(PREFS_FILE, False):
                raw = await self.capability_worker.read_file(PREFS_FILE, False)
                self.prefs = json.loads(raw)
                self.api_key = self.prefs.get("api_key", "")
                self.log(f"Loaded prefs, API key present: {bool(self.api_key)}")
            else:
                self.prefs = {}
                self.api_key = "8dd89eb5-a269-4fe2-baab-ecdd394097a4"
                await self.save_prefs()
        except Exception as e:
            self.log_err(f"Failed to load prefs: {e}")
            self.prefs = {}
            self.api_key = ""

    async def save_prefs(self):
        try:
            self.prefs["api_key"] = self.api_key
            await self.capability_worker.delete_file(PREFS_FILE, False)
            await self.capability_worker.write_file(
                PREFS_FILE, json.dumps(self.prefs), False
            )
        except Exception as e:
            self.log_err(f"Failed to save prefs: {e}")

    # ------------------------------------------------------------------
    # GraphQL helper
    # ------------------------------------------------------------------
    def _graphql_request(self, query: str, variables: dict = None) -> dict:
        """Blocking GraphQL call — run via asyncio.to_thread."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = requests.post(
            FIREFLIES_ENDPOINT, headers=headers, json=payload, timeout=15
        )

        if resp.status_code == 429:
            raise Exception("rate_limited")
        if resp.status_code == 401:
            raise Exception("auth_failed")

        resp.raise_for_status()
        data = resp.json()

        if "errors" in data:
            error_msg = data["errors"][0].get("message", "Unknown error")
            self.log_err(f"GraphQL: {error_msg}")
            raise Exception(f"graphql_error: {error_msg}")

        return data.get("data", {})

    async def graphql_query(self, query: str, variables: dict = None) -> dict:
        return await asyncio.to_thread(self._graphql_request, query, variables)

    async def safe_graphql(self, query: str, variables: dict = None) -> dict | None:
        """GraphQL call with spoken error handling. Returns None on failure."""
        try:
            return await self.graphql_query(query, variables)
        except Exception as e:
            err = str(e)
            if "rate_limited" in err:
                await self.capability_worker.speak(
                    "I've hit the Fireflies rate limit. Try again in a few minutes."
                )
            elif "auth_failed" in err:
                await self.capability_worker.speak(
                    "Your Fireflies API key seems invalid. Check it in your settings."
                )
            else:
                self.log_err(f"API error: {e}")
                await self.capability_worker.speak(
                    "I couldn't reach Fireflies right now. Try again in a moment."
                )
            return None

    # ------------------------------------------------------------------
    # LLM helpers
    # ------------------------------------------------------------------
    def parse_llm_json(self, raw: str) -> dict:
        clean = raw.replace("```json", "").replace("```", "").strip()
        try:
            return json.loads(clean)
        except (json.JSONDecodeError, ValueError):
            return {}

    def classify_intent(self, user_input: str) -> dict:
        prompt = INTENT_PROMPT.format(user_input=user_input)
        raw = self.capability_worker.text_to_text_response(
            prompt, system_prompt=INTENT_SYSTEM_PROMPT
        )
        result = self.parse_llm_json(raw)
        if "intent" not in result:
            result["intent"] = "unknown"
        self.log(f"Intent: {result.get('intent')} from: {user_input[:60]}")
        return result

    # ------------------------------------------------------------------
    # Voice formatting
    # ------------------------------------------------------------------
    def format_date_for_speech(self, timestamp) -> str:
        if not timestamp:
            return "unknown date"
        try:
            dt = datetime.fromtimestamp(timestamp / 1000)
            today = datetime.now()
            if dt.date() == today.date():
                return "today"
            elif (today - dt).days == 1:
                return "yesterday"
            elif (today - dt).days < 7:
                return dt.strftime("%A")
            else:
                return dt.strftime("%B %d")
        except Exception:
            return "unknown date"

    def format_duration(self, duration) -> str:
        if not duration:
            return ""
        try:
            mins = int(float(duration))
        except (ValueError, TypeError):
            return ""
        if mins < 60:
            return f"{mins} minutes"
        hours = mins // 60
        remaining = mins % 60
        if remaining == 0:
            return f"{hours} hour{'s' if hours > 1 else ''}"
        return f"{hours} hour{'s' if hours > 1 else ''} and {remaining} minutes"

    def extract_url_from_speech(self, speech: str) -> str | None:
        result = self.capability_worker.text_to_text_response(
            f"The user spoke a meeting URL but it was transcribed messily. "
            f"Reconstruct the valid URL. Return ONLY the URL, nothing else.\n\n"
            f"Transcription: {speech}",
            system_prompt=(
                "Extract meeting URLs from speech. Common patterns: "
                "'zoom dot us slash j slash [numbers]', "
                "'meet dot google dot com slash [code]'. "
                "Return only the valid https:// URL or 'NONE' if not recoverable."
            ),
        )
        result = result.strip()
        if result.startswith("http"):
            return result
        return None

    # ------------------------------------------------------------------
    # Meeting resolution
    # ------------------------------------------------------------------
    async def resolve_meeting(self, hint: str) -> dict | None:
        if not hint or hint.lower() in [
            "last meeting", "latest meeting", "most recent", "my last meeting",
        ]:
            if self.recent_meetings:
                return self.recent_meetings[0]
            data = await self.safe_graphql(RECENT_MEETINGS_QUERY, {"limit": 1})
            if data:
                transcripts = data.get("transcripts", [])
                if transcripts:
                    return transcripts[0]
            return None

        # Keyword search
        data = await self.safe_graphql(SEARCH_QUERY, {"keyword": hint, "limit": 5})
        if not data:
            return None

        meetings = data.get("transcripts", [])
        if len(meetings) == 1:
            return meetings[0]

        if len(meetings) > 1:
            return await self.disambiguate_meeting(meetings)

        # No results — try shorter query
        words = hint.split()
        if len(words) > 2:
            shorter = " ".join(words[:2])
            data = await self.safe_graphql(
                SEARCH_QUERY, {"keyword": shorter, "limit": 5}
            )
            if data:
                meetings = data.get("transcripts", [])
                if len(meetings) == 1:
                    return meetings[0]
                if len(meetings) > 1:
                    return await self.disambiguate_meeting(meetings)

        return None

    async def disambiguate_meeting(self, meetings: list) -> dict | None:
        options = []
        for i, m in enumerate(meetings[:5]):
            title = m.get("title", "Untitled")
            date = self.format_date_for_speech(m.get("date"))
            options.append(f"{i + 1}. {title}, {date}")

        options_text = ". ".join(options)
        response = await self.capability_worker.run_io_loop(
            f"I found {len(meetings)} meetings. {options_text}. Which one?"
        )
        if not response:
            return meetings[0]

        # Use LLM to match selection
        raw = self.capability_worker.text_to_text_response(
            f"The user was given these meeting options:\n{options_text}\n\n"
            f"They responded: \"{response}\"\n\n"
            f"Return ONLY the option number (1-{min(len(meetings), 5)}) they selected. "
            f"If unclear, return 1.",
            system_prompt="Extract a number from user input. Return only the digit.",
        )
        try:
            idx = int(raw.strip()) - 1
            if 0 <= idx < len(meetings):
                return meetings[idx]
        except (ValueError, IndexError):
            pass
        return meetings[0]

    # ------------------------------------------------------------------
    # Feature handlers
    # ------------------------------------------------------------------
    async def handle_recent_meetings(self):
        await self.capability_worker.speak("One sec, pulling up your meetings.")
        data = await self.safe_graphql(RECENT_MEETINGS_QUERY, {"limit": 5})
        if not data:
            return

        transcripts = data.get("transcripts", [])
        self.recent_meetings = transcripts

        if not transcripts:
            await self.capability_worker.speak(
                "You don't have any meetings in Fireflies yet. "
                "Make sure the Fireflies bot has been invited to your calls."
            )
            return

        count = len(transcripts)
        await self.capability_worker.speak(f"You have {count} recent meetings.")

        for m in transcripts[:3]:
            title = m.get("title", "Untitled")
            date = self.format_date_for_speech(m.get("date"))
            gist = m.get("summary", {}).get("gist", "")
            duration = self.format_duration(m.get("duration"))
            line = f"{title}, {date}"
            if duration:
                line += f", {duration}"
            if gist:
                line += f". {gist}"
            await self.capability_worker.speak(line)

        if count > 3:
            await self.capability_worker.speak(
                f"Plus {count - 3} more. Want details on any of these?"
            )

    async def handle_meeting_summary(self, meeting_hint: str):
        await self.capability_worker.speak("Let me grab that summary.")
        meeting = await self.resolve_meeting(meeting_hint)
        if not meeting:
            await self.capability_worker.speak(
                "I couldn't find that meeting. Try the exact name or say last meeting."
            )
            return

        data = await self.safe_graphql(
            MEETING_SUMMARY_QUERY, {"transcriptId": meeting["id"]}
        )
        if not data:
            return

        transcript = data.get("transcript", {})
        summary = transcript.get("summary", {})
        title = transcript.get("title", "your meeting")
        duration = self.format_duration(transcript.get("duration"))

        short_summary = (
            summary.get("short_summary")
            or summary.get("gist")
            or "No summary available."
        )

        header = f"Here's the summary of {title}"
        if duration:
            header += f", {duration}"
        await self.capability_worker.speak(f"{header}.")
        await self.capability_worker.speak(short_summary)

        topics = summary.get("topics_discussed", [])
        if topics and isinstance(topics, list):
            topics_str = ", ".join(topics[:5])
            await self.capability_worker.speak(f"Topics covered: {topics_str}.")

        self.current_meeting = meeting

    async def handle_action_items(self, meeting_hint: str):
        await self.capability_worker.speak("Checking for action items.")
        meeting = await self.resolve_meeting(meeting_hint)
        if not meeting:
            await self.capability_worker.speak(
                "Which meeting? Try saying the name or last meeting."
            )
            return

        data = await self.safe_graphql(
            ACTION_ITEMS_QUERY, {"transcriptId": meeting["id"]}
        )
        if not data:
            return

        action_items_raw = (
            data.get("transcript", {}).get("summary", {}).get("action_items", "")
        )
        if not action_items_raw:
            await self.capability_worker.speak(
                "No action items were captured for that meeting."
            )
            return

        spoken = self.capability_worker.text_to_text_response(
            f"Convert these meeting action items into a concise spoken list. "
            f"Number each item. Keep each to 1 sentence. Max 5 items. "
            f"No markdown.\n\n{action_items_raw}",
            system_prompt=(
                "You format text for voice output. Be concise. "
                "No bullet points or markdown."
            ),
        )
        await self.capability_worker.speak(spoken)
        self.current_meeting = meeting

    async def handle_search(self, keyword: str = None, participant_email: str = None):
        await self.capability_worker.speak("Searching your meetings.")
        variables = {"limit": 5}
        if keyword:
            variables["keyword"] = keyword
        if participant_email:
            variables["participants"] = [participant_email]

        data = await self.safe_graphql(SEARCH_QUERY, variables)
        if not data:
            return

        transcripts = data.get("transcripts", [])
        self.recent_meetings = transcripts

        if not transcripts:
            await self.capability_worker.speak(
                "No meetings found matching that search."
            )
            return

        await self.capability_worker.speak(f"Found {len(transcripts)} meetings.")
        for m in transcripts[:3]:
            title = m.get("title", "Untitled")
            date = self.format_date_for_speech(m.get("date"))
            gist = m.get("summary", {}).get("gist", "")
            line = f"{title}, {date}"
            if gist:
                line += f". {gist}"
            await self.capability_worker.speak(line)

    async def handle_speaker_analytics(self, meeting_hint: str):
        await self.capability_worker.speak("Pulling up speaker stats.")
        meeting = await self.resolve_meeting(meeting_hint)
        if not meeting:
            await self.capability_worker.speak("Which meeting should I analyze?")
            return

        data = await self.safe_graphql(
            SPEAKER_ANALYTICS_QUERY, {"transcriptId": meeting["id"]}
        )
        if not data:
            return

        speakers = (
            data.get("transcript", {}).get("analytics", {}).get("speakers", [])
        )
        if not speakers:
            await self.capability_worker.speak(
                "No speaker data available for that meeting."
            )
            return

        title = data.get("transcript", {}).get("title", "the meeting")
        await self.capability_worker.speak(f"Speaker breakdown for {title}.")

        speakers_sorted = sorted(
            speakers, key=lambda s: s.get("duration_pct", 0), reverse=True
        )
        for s in speakers_sorted[:4]:
            name = s.get("name", "Unknown speaker")
            pct = round(s.get("duration_pct", 0))
            words = s.get("word_count", 0)
            fillers = s.get("filler_words", 0)
            questions = s.get("questions", 0)
            await self.capability_worker.speak(
                f"{name} spoke {pct}% of the time, {words} words, "
                f"{questions} questions, {fillers} filler words."
            )
        self.current_meeting = meeting

    async def handle_sentiment(self, meeting_hint: str):
        await self.capability_worker.speak("Checking the tone of that meeting.")
        meeting = await self.resolve_meeting(meeting_hint)
        if not meeting:
            await self.capability_worker.speak("Which meeting?")
            return

        data = await self.safe_graphql(
            SENTIMENT_QUERY, {"transcriptId": meeting["id"]}
        )
        if not data:
            return

        sentiments = (
            data.get("transcript", {}).get("analytics", {}).get("sentiments", {})
        )
        if not sentiments:
            await self.capability_worker.speak(
                "No sentiment data for that meeting."
            )
            return

        pos = round(sentiments.get("positive_pct", 0))
        neg = round(sentiments.get("negative_pct", 0))
        neu = round(sentiments.get("neutral_pct", 0))
        title = data.get("transcript", {}).get("title", "the meeting")

        interpretation = self.capability_worker.text_to_text_response(
            f"Meeting: {title}. Sentiment: {pos}% positive, {neg}% negative, "
            f"{neu}% neutral. Give a 1-2 sentence natural interpretation of "
            f"this meeting's tone.",
            system_prompt=(
                "You interpret meeting sentiment data for voice output. "
                "1-2 sentences max."
            ),
        )
        await self.capability_worker.speak(interpretation)
        self.current_meeting = meeting

    async def handle_askfred(self, question: str, meeting_hint: str = None):
        variables = {"question": question}

        if meeting_hint:
            meeting = await self.resolve_meeting(meeting_hint)
            if meeting:
                variables["transcriptIds"] = [meeting["id"]]

        await self.capability_worker.speak("Let me ask Fred about that.")

        if self.askfred_thread_id:
            data = await self.safe_graphql(
                ASKFRED_CONTINUE_QUERY,
                {"threadId": self.askfred_thread_id, "question": question},
            )
            if not data:
                return
            thread = data.get("continueAskFredThread", {})
        else:
            data = await self.safe_graphql(ASKFRED_CREATE_QUERY, variables)
            if not data:
                return
            thread = data.get("createAskFredThread", {})
            self.askfred_thread_id = thread.get("id")

        messages = thread.get("messages", [])
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        if assistant_msgs:
            answer = assistant_msgs[-1].get("content", "Fred didn't have an answer.")
            spoken = self.capability_worker.text_to_text_response(
                f"Condense this answer for voice output. Keep the key facts, "
                f"remove any markdown. 3-4 sentences max:\n\n{answer}",
                system_prompt=(
                    "You condense text for voice output. Be concise but "
                    "preserve key details."
                ),
            )
            await self.capability_worker.speak(spoken)
        else:
            await self.capability_worker.speak(
                "Fred couldn't find an answer to that."
            )

        suggestions = thread.get("suggested_questions", [])
        if suggestions:
            await self.capability_worker.speak(
                f"You could also ask: {suggestions[0]}"
            )

    async def handle_add_bot(self, meeting_link: str = None):
        if not meeting_link:
            response = await self.capability_worker.run_io_loop(
                "What's the meeting link? Say the Zoom or Google Meet URL."
            )
            if response:
                meeting_link = self.extract_url_from_speech(response)

        if not meeting_link:
            await self.capability_worker.speak(
                "I couldn't get a valid meeting link. "
                "Try saying the full Zoom or Google Meet URL."
            )
            return

        confirmed = await self.capability_worker.run_confirmation_loop(
            "Send Fireflies to join the meeting?"
        )
        if not confirmed:
            await self.capability_worker.speak("Okay, cancelled.")
            return

        await self.capability_worker.speak("Sending the bot now.")
        data = await self.safe_graphql(
            ADD_BOT_MUTATION, {"meetingLink": meeting_link, "duration": 60}
        )
        if not data:
            return

        success = data.get("addToLiveMeeting", {}).get("success", False)
        if success:
            await self.capability_worker.speak(
                "Done. The Fireflies bot will join shortly."
            )
        else:
            await self.capability_worker.speak(
                "Something went wrong. The bot couldn't be sent."
            )

    async def handle_delete(self, meeting_hint: str):
        meeting = await self.resolve_meeting(meeting_hint)
        if not meeting:
            await self.capability_worker.speak(
                "Which meeting do you want to delete?"
            )
            return

        title = meeting.get("title", "this meeting")
        confirmed = await self.capability_worker.run_confirmation_loop(
            f"Are you sure you want to delete {title}? This can't be undone."
        )
        if not confirmed:
            await self.capability_worker.speak("Okay, keeping it.")
            return

        await self.capability_worker.speak("Deleting now.")
        data = await self.safe_graphql(
            DELETE_TRANSCRIPT_MUTATION, {"id": meeting["id"]}
        )
        if data:
            await self.capability_worker.speak(f"Deleted {title}.")

    # ------------------------------------------------------------------
    # Intent routing
    # ------------------------------------------------------------------
    async def route_intent(self, result: dict):
        intent = result.get("intent", "unknown")
        hint = result.get("meeting_hint")
        keyword = result.get("search_keyword")
        email = result.get("participant_email")
        link = result.get("meeting_link")
        question = result.get("question_for_fred")

        if intent == "recent_meetings":
            await self.handle_recent_meetings()
        elif intent == "meeting_summary":
            await self.handle_meeting_summary(hint)
        elif intent == "action_items":
            await self.handle_action_items(hint)
        elif intent == "search":
            await self.handle_search(keyword=keyword, participant_email=email)
        elif intent == "speaker_analytics":
            await self.handle_speaker_analytics(hint)
        elif intent == "sentiment":
            await self.handle_sentiment(hint)
        elif intent == "askfred":
            q = question or hint or "summarize my recent meetings"
            await self.handle_askfred(q, meeting_hint=hint)
        elif intent == "add_bot":
            await self.handle_add_bot(meeting_link=link)
        elif intent == "delete":
            await self.handle_delete(hint)
        else:
            await self.capability_worker.speak(
                "I can list your meetings, summarize them, read action items, "
                "search by keyword, show speaker stats, check sentiment, "
                "ask Fred a question, or send the bot to a live call. "
                "What would you like?"
            )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run(self):
        try:
            await self.load_prefs()

            # Check for API key
            if not self.api_key:
                await self.capability_worker.speak(
                    "You need to connect your Fireflies account first. "
                    "Go to the Fireflies dashboard, copy your API key from "
                    "Settings, Integrations, API, and have your developer "
                    "add it to the config."
                )
                return

            # Validate connection
            await self.capability_worker.speak("Connecting to Fireflies.")
            test_data = await self.safe_graphql(
                RECENT_MEETINGS_QUERY, {"limit": 1}
            )
            if test_data is None:
                return
            self.recent_meetings = test_data.get("transcripts", [])

            # Check if trigger context contains a specific request
            history = self.worker.agent_memory.full_message_history
            trigger_text = ""
            if history:
                last_msg = history[-1] if isinstance(history, list) else None
                if last_msg and isinstance(last_msg, dict):
                    trigger_text = last_msg.get("content", "")

            # Try fast-path: if the trigger itself contains a clear request
            if trigger_text and len(trigger_text) > 15:
                result = self.classify_intent(trigger_text)
                if result.get("intent") not in ("unknown", "exit"):
                    await self.route_intent(result)
                    # After handling, ask if they need anything else
                    follow_up = await self.capability_worker.run_io_loop(
                        "Anything else about your meetings?"
                    )
                    if not follow_up or any(
                        w in (follow_up or "").lower() for w in EXIT_WORDS
                    ):
                        await self.capability_worker.speak("Got it. See you.")
                        return
                    # Fall into the main loop with this input
                    result = self.classify_intent(follow_up)
                    if result.get("intent") == "exit":
                        await self.capability_worker.speak("Got it. See you.")
                        return
                    await self.route_intent(result)

            # If no fast-path, greet
            if not trigger_text or len(trigger_text) <= 15:
                await self.capability_worker.speak(
                    "What would you like to know about your meetings?"
                )

            # Conversation loop
            idle_count = 0
            while True:
                user_input = await self.capability_worker.user_response()

                if not user_input:
                    idle_count += 1
                    if idle_count >= 2:
                        await self.capability_worker.speak(
                            "Still here if you need anything. "
                            "Otherwise I'll close out."
                        )
                        follow_up = await self.capability_worker.user_response()
                        if not follow_up or any(
                            w in (follow_up or "").lower() for w in EXIT_WORDS
                        ):
                            await self.capability_worker.speak("See you next time.")
                            break
                        user_input = follow_up
                        idle_count = 0
                    else:
                        continue

                idle_count = 0

                # Quick exit check
                lower = user_input.lower().strip()
                if any(w in lower for w in EXIT_WORDS):
                    await self.capability_worker.speak("Got it. See you next time.")
                    break

                # Classify and route
                result = self.classify_intent(user_input)
                if result.get("intent") == "exit":
                    await self.capability_worker.speak("Got it. See you next time.")
                    break

                await self.route_intent(result)

            # Track usage
            self.prefs["times_used"] = self.prefs.get("times_used", 0) + 1
            await self.save_prefs()

        except Exception as e:
            self.log_err(f"Fatal error: {e}")
            await self.capability_worker.speak("Something went wrong. Try again later.")
        finally:
            self.capability_worker.resume_normal_flow()
