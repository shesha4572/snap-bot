"""
News fetcher and OpenRouter AI quote generator tailored for 20-25 yo (Global News).
- Pulls top 25 global stories from Google News RSS.
- Filters out hyper-local American news in favour of globally resonant stories
  (Big Tech, AI, global economy, pop culture, internet trends).
- Uses few-shot user/assistant message turns for reliable output on small free models.
- Enforces Structured JSON Output via json_schema strict mode: {"chosen_story": "...", "quote": "..."}
- Hard cap: 70 characters (fits safely on Snapchat caption bar on all screen sizes).
"""

import json
import logging
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
import requests

logger = logging.getLogger("snap-bot")

GOOGLE_NEWS_RSS = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"


def fetch_top_news(count: int = 25) -> list[str]:
    """Fetch top headlines (up to 25) from global Google News RSS."""
    headlines = []
    try:
        req = urllib.request.Request(
            GOOGLE_NEWS_RSS,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()

        root = ET.fromstring(data)
        items = root.findall(".//item")[:count]
        for item in items:
            title_node = item.find("title")
            if title_node is not None and title_node.text:
                clean_title = title_node.text.split(" - ")[0].strip()
                if clean_title and clean_title not in headlines:
                    headlines.append(clean_title)

        logger.info(f"Fetched {len(headlines)} global headlines from Google News (target: up to {count}).")
    except Exception as e:
        raise RuntimeError(f"Failed to fetch news headlines from RSS: {e}") from e

    if not headlines:
        raise RuntimeError("No news headlines could be retrieved.")
    return headlines


def generate_news_quote(
    api_key: str = "",
    model: str = "openrouter/free",
    headlines: list[str] = None,
) -> str:
    """
    Call OpenRouter with Few-Shot Examples and Structured JSON Output.
    Picks globally relatable stories (avoiding hyper-local US niches).
    Outputs a complete caption strictly <= 80 characters.
    """
    if not api_key:
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()

    if not api_key:
        raise ValueError(
            "OpenRouter API key is required but missing! Please set 'openrouter_api_key' "
            "in config.json or set the OPENROUTER_API_KEY environment variable."
        )

    if not headlines:
        headlines = fetch_top_news(count=25)

    news_text = "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines))

    # System prompt: role + rules only. No examples here — those go as message turns below.
    system_instruction = (
        "You are a witty 22-year-old writing a daily Snapchat caption for urban friends "
        "in India aged 20-25 who follow global tech, AI, and internet culture.\n\n"
        "RULES:\n"
        "- Choose the single most globally resonant story: tech/AI breakthroughs, "
        "viral internet moments, absurd world news, or relatable work/money struggles.\n"
        "- Skip hyper-local American stories: obscure US comedians, state-level bills, college sports.\n"
        "- Write a punchy one-liner: sarcastic, self-aware, or mildly absurd.\n"
        "- Quote must be 45–75 characters. No emojis. No quotation marks. Full sentence."
    )

    # Few-shot examples as real user/assistant turns — far more effective than
    # inline text examples for small free-tier models.
    few_shot_messages = [
        {
            "role": "user",
            "content": "News: Microsoft Teams suffers global outage affecting millions",
        },
        {
            "role": "assistant",
            "content": '{"chosen_story": "Microsoft Teams Outage", "quote": "Teams crashed and nobody in corporate knows how to work"}',
        },
        {
            "role": "user",
            "content": "News: AI model scores 98 percent on medical licensing exam",
        },
        {
            "role": "assistant",
            "content": '{"chosen_story": "AI Passes Medical Exam", "quote": "AI becoming a doctor while I still Google if colds need rest"}',
        },
        {
            "role": "user",
            "content": "News: Crypto market drops 15 percent in overnight flash crash",
        },
        {
            "role": "assistant",
            "content": '{"chosen_story": "Crypto Flash Crash", "quote": "Portfolio down 15 percent, instant noodles are back on the menu"}',
        },
    ]

    user_prompt = (
        f"Today's top {len(headlines)} global news stories:\n{news_text}\n\n"
        "Pick the single best story and write your Gen Z Snapchat caption."
    )

    # Strict JSON schema: OpenRouter enforces the shape, so we can parse
    # the content directly with json.loads - no regex needed.
    _QUOTE_SCHEMA = {
        "type": "json_schema",
        "json_schema": {
            "name": "snap_quote",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "chosen_story": {
                        "type": "string",
                        "description": "Short title of the chosen news story (3-8 words)",
                    },
                    "quote": {
                        "type": "string",
                        "description": (
                            "Witty Gen Z one-liner reaction, 45-75 characters, "
                            "no emojis, no quotation marks, full coherent sentence"
                        ),
                    },
                },
                "required": ["chosen_story", "quote"],
                "additionalProperties": False,
            },
        },
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
      try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    *few_shot_messages,
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": _QUOTE_SCHEMA,
                # 1500 is intentionally high: reasoning/thinking models (e.g. Nemotron)
                # consume most of their token budget on internal chain-of-thought before
                # emitting the final JSON. Lowering this causes empty-content responses.
                "max_tokens": 1500,
                "temperature": 0.7,
            },
            timeout=45,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                f"OpenRouter API returned HTTP {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        logger.debug(f"OpenRouter raw response: {data}")

        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"No choices in OpenRouter response: {data}")

        msg = choices[0].get("message", {})
        raw_text = (msg.get("content") or "").strip()
        if not raw_text:
            logger.warning(f"Attempt {attempt}/{max_attempts}: empty content in response, retrying...")
            continue

        # With json_schema strict mode the content IS the JSON object - parse directly.
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as je:
            raise RuntimeError(
                f"OpenRouter returned non-JSON despite json_schema mode: {je} | raw: {raw_text}"
            ) from je

        chosen_story = parsed.get("chosen_story", "").strip()
        caption = parsed.get("quote", "").strip()

        if not caption:
            raise RuntimeError(f"'quote' key missing or empty in response: {parsed}")

        # Validate that the caption is a real sentence (reject accidental stubs)
        if len(caption) < 20:
            raise RuntimeError(
                f"Generated caption is too short (got {caption!r}). Full response: {parsed}"
            )

        # Clean any quotes, asterisks, formatting artifacts
        cleaned_caption = caption.strip("\"'`*# \n\r")
        # Hard cap at 75 chars — safe for Snapchat caption bar on all screen sizes
        final_quote = cleaned_caption[:75].strip()

        if chosen_story:
            logger.info(f"Chosen News Story: \"{chosen_story}\"")
        logger.info(f"Generated Gen Z Quote ({len(final_quote)} chars): \"{final_quote}\"")

        return final_quote

      except Exception as e:
            logger.error(f"OpenRouter API call failed (attempt {attempt}/{max_attempts}): {e}")
            if attempt == max_attempts:
                raise RuntimeError(f"OpenRouter generation failed after {max_attempts} attempts: {e}") from e
            logger.warning(f"Retrying ({attempt}/{max_attempts})...")
    raise RuntimeError(f"OpenRouter returned empty content after {max_attempts} attempts")


def sanitize_for_adb(text: str) -> str:
    """
    Sanitize text for 'adb shell input text':
    - Expands contractions so words aren't dropped (we're -> were, that's -> thats)
    - Replaces spaces with '%s'
    - Strips shell-breaking characters
    """
    contractions = {
        r"\bwe're\b": "were",
        r"\bthat's\b": "thats",
        r"\bit's\b": "its",
        r"\bdon't\b": "dont",
        r"\bcan't\b": "cant",
        r"\bwon't\b": "wont",
        r"\bi'm\b": "im",
        r"\byou're\b": "youre",
        r"\bthey're\b": "theyre",
        r"\bthere's\b": "theres",
    }
    cleaned = text
    for pattern, repl in contractions.items():
        cleaned = re.sub(pattern, repl, cleaned, flags=re.IGNORECASE)

    # Remove remaining quotes
    cleaned = cleaned.replace("'", "").replace('"', "")
    # Keep safe chars
    safe_chars = re.sub(r"[^a-zA-Z0-9\s.,!?-]", "", cleaned)
    words = safe_chars.split()
    return "%s".join(words)
