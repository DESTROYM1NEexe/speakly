"""AI lesson analysis, response validation, and Telegram lesson formatting."""

import html
import json
import logging
import re
from typing import Any, Dict, Optional

from openai import AsyncOpenAI

import config


logger = logging.getLogger(__name__)
openai_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)


def validate_analysis_payload(analysis: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(analysis, dict):
        return None
    for key in ("expressions", "vocabulary", "natural_english", "native_recommendations"):
        value = analysis.get(key, [])
        if not isinstance(value, list):
            return None
        analysis[key] = [item for item in value if isinstance(item, dict)]
    if not isinstance(analysis.get("summary", ""), str):
        return None
    if not isinstance(analysis.get("estimated_level", ""), str):
        return None

    ielts = analysis.get("ielts")
    if ielts is not None:
        if not isinstance(ielts, dict):
            return None
        for key in (
            "vocabulary", "collocations", "speaking_expressions",
            "academic_alternatives", "mini_quiz", "practice_questions",
        ):
            values = ielts.get(key, [])
            if not isinstance(values, list):
                return None
            ielts[key] = [item for item in values if isinstance(item, (dict, str))]
    return analysis


async def analyze_transcript(
    transcript: str,
    user_level: str,
    user_settings: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not transcript or not transcript.strip():
        return None

    settings = user_settings or {}
    goal = str(settings.get("learning_goal", "General English"))
    dialect = str(settings.get("dialect", "American"))
    difficulty = str(settings.get("vocabulary_difficulty", "Adaptive"))
    level_bands = {
        "A1": "A1-A2", "A2": "A2-B1", "B1": "B1-B2",
        "B2": "B2-C1", "C1": "C1-C2", "C2": "C1-C2",
    }
    target_band = level_bands.get(user_level, "B1-B2")
    prompt = f"""
You are an expert {dialect} English teacher. Analyze this video transcript for a CEFR
{user_level} learner whose goal is {goal}. Target mostly {target_band} vocabulary;
preference: {difficulty}.

Select useful transcript-based expressions and vocabulary. Prioritize phrasal verbs,
idioms, collocations, useful verbs, conversational English, and goal-relevant academic
vocabulary. Exclude basic words such as good, bad, house, go, make, and thing unless
they are unusually meaningful in context. For IELTS goals, prioritize B2-C1 academic
vocabulary, collocations, speaking expressions, and precise alternatives.

Return one JSON object with exactly these keys:
{{
  "expressions": [{{"expression":"", "transcription":"", "translation":"", "cefr":"", "example":"", "explanation":""}}],
  "vocabulary": [{{"word":"", "transcription":"", "translation":"", "cefr":"", "example":"", "priority":"Must Learn or Useful", "category":""}}],
  "natural_english": [{{"phrase":"", "meaning":"", "explanation":""}}],
  "native_recommendations": [{{"instead_of":"", "natural":"", "meaning":"", "example":""}}],
  "ielts": {{
    "estimated_level":"B2",
    "vocabulary":[{{"word":"", "translation":"", "example":""}}],
    "collocations":[{{"phrase":"", "meaning":"", "example":""}}],
    "speaking_expressions":[{{"phrase":"", "meaning":"", "example":""}}],
    "academic_alternatives":[{{"alternative":"", "meaning":"", "example":""}}],
    "mini_quiz":[{{"question":"", "answer":""}}],
    "practice_questions":["five IELTS-style speaking questions"]
  }},
  "estimated_level":"A1-C2",
  "summary":"Short English summary appropriate for {user_level}"
}}

Maximum 10 expressions, 10 vocabulary words, 5 natural English phrases and 5 items
per IELTS vocabulary section. Include a three-question quiz and exactly five IELTS
speaking questions. Clearly label native recommendations as additional suggestions;
never claim they appeared in the transcript. Do not invent transcript vocabulary.
Return valid JSON only.

Transcript:
{transcript[:config.MAX_TRANSCRIPT_LENGTH]}
"""

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are an expert English teacher. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            response_format={"type": "json_object"},
            timeout=config.OPENAI_TIMEOUT,
        )
        response_text = (response.choices[0].message.content or "").strip()
        response_text = re.sub(r"^```json\s*|^```\s*|\s*```$", "", response_text, flags=re.IGNORECASE).strip()
        try:
            analysis = json.loads(response_text)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", response_text)
            try:
                analysis = json.loads(match.group(0)) if match else None
            except json.JSONDecodeError:
                analysis = None

        valid = validate_analysis_payload(analysis)
        if valid:
            return valid

        logger.warning("OpenAI returned invalid lesson JSON; retrying once")
        retry = await openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Return valid JSON only matching the requested schema."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
            timeout=config.OPENAI_TIMEOUT,
        )
        retry_text = (retry.choices[0].message.content or "").strip()
        try:
            return validate_analysis_payload(json.loads(retry_text))
        except json.JSONDecodeError:
            logger.error("OpenAI returned invalid lesson JSON after retry: %s", retry_text[:1000])
            return None
    except Exception:
        logger.exception("Error analyzing transcript with OpenAI")
        return None


def _escape(value: Any) -> str:
    return html.escape(str(value or ""))


def format_lesson(analysis: Dict[str, Any], video_title: str) -> str:
    if not analysis:
        return "❌ Could not analyze transcript. Please try another video."

    lines = [
        "🎬 <b>English breakdown</b>",
        f"<b>{_escape(video_title)}</b>",
        f"📊 Level: {_escape(analysis.get('estimated_level') or 'Not estimated')}",
        "",
    ]
    for expression in analysis.get("expressions", [])[:10]:
        lines.extend([
            f"🔥 <b>{_escape(expression.get('expression'))}</b>",
            f"/{_escape(expression.get('transcription'))}/" if expression.get("transcription") else "",
            f"🇷🇺 {_escape(expression.get('translation'))}",
            f"📈 {_escape(expression.get('cefr'))}",
            f"💬 <i>{_escape(expression.get('example'))}</i>" if expression.get("example") else "",
            f"🧩 {_escape(expression.get('explanation'))}" if expression.get("explanation") else "",
            "",
        ])

    for index, word in enumerate(analysis.get("vocabulary", [])[:10], 1):
        lines.extend([
            f"<b>{index}. {_escape(word.get('word'))}</b>",
            f"/{_escape(word.get('transcription'))}/" if word.get("transcription") else "",
            f"🇷🇺 {_escape(word.get('translation'))}",
            f"📈 {_escape(word.get('cefr'))}",
            f"💬 <i>{_escape(word.get('example'))}</i>" if word.get("example") else "",
            "",
        ])

    natural_items = analysis.get("natural_english", [])[:5]
    if natural_items:
        lines.extend(["🗣️ <b>Natural English</b>", ""])
        for item in natural_items:
            lines.append(f"🔥 <b>{_escape(item.get('phrase'))}</b> = {_escape(item.get('meaning'))}")
            if item.get("explanation"):
                lines.append(f"💡 {_escape(item.get('explanation'))}")
            lines.append("")

    recommendations = analysis.get("native_recommendations", [])[:5]
    if recommendations:
        lines.extend(["🇺🇸 <b>Native English (extra suggestions)</b>", ""])
        for item in recommendations:
            lines.append(
                f"Instead of <i>{_escape(item.get('instead_of'))}</i>: "
                f"<b>{_escape(item.get('natural'))}</b> = {_escape(item.get('meaning'))}"
            )
            if item.get("example"):
                lines.append(f"💬 <i>{_escape(item.get('example'))}</i>")
            lines.append("")

    ielts = analysis.get("ielts")
    if isinstance(ielts, dict):
        lines.extend(["🎓 <b>IELTS Focus</b>", ""])
        if ielts.get("estimated_level"):
            lines.append(f"📈 Estimated level: {_escape(ielts['estimated_level'])}")
        for section in ("vocabulary", "collocations", "speaking_expressions", "academic_alternatives"):
            items = ielts.get(section, [])
            if not items:
                continue
            lines.append(f"<b>{_escape(section.replace('_', ' ').title())}</b>")
            for item in items[:5]:
                value = item if isinstance(item, str) else (
                    item.get("word") or item.get("phrase") or item.get("expression") or item.get("alternative") or ""
                )
                detail = "" if isinstance(item, str) else item.get("meaning") or item.get("example") or ""
                lines.append(f"• {_escape(value)}" + (f" = {_escape(detail)}" if detail else ""))
        quiz = ielts.get("mini_quiz", [])
        if quiz:
            lines.extend(["<b>Mini quiz</b>"])
            for index, item in enumerate(quiz[:3], 1):
                if isinstance(item, dict):
                    lines.append(f"{index}. {_escape(item.get('question'))}\nAnswer: {_escape(item.get('answer'))}")
        questions = ielts.get("practice_questions", [])
        if questions:
            lines.append("<b>IELTS Speaking Practice</b>")
            lines.extend(f"{index}. {_escape(question)}" for index, question in enumerate(questions[:5], 1))

    if analysis.get("summary"):
        lines.extend(["", "📖 <b>Summary</b>", "", _escape(analysis["summary"])])
    return "\n".join(line for line in lines if line is not None)