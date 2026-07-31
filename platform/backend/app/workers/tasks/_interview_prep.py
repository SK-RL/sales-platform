"""AI-powered interview preparation using Claude API."""

from app.ai_models import CLAUDE_SONNET
import json
import logging
from app.config import get_settings

logger = logging.getLogger(__name__)


def generate_interview_prep(
    resume_text: str,
    job_title: str,
    company_name: str,
    job_description: str,
    company_info: str = "",
) -> dict:
    """Generate interview preparation materials using Claude.

    Returns dict with questions, talking_points, and company_research.
    """
    settings = get_settings()
    if not settings.anthropic_api_key.get_secret_value():
        return {
            "questions": [],
            "talking_points": [],
            "company_research": [],
            "error": True,
            "error_message": "AI interview prep requires an Anthropic API key.",
        }

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())

        prompt = f"""You are a senior technical interviewer and career coach preparing a candidate for a job interview.

CANDIDATE RESUME:
{resume_text[:2500]}

TARGET ROLE:
Title: {job_title}
Company: {company_name}

JOB DESCRIPTION:
{job_description[:2500] if job_description else "Not available."}

{f"COMPANY INFO: {company_info[:500]}" if company_info else ""}

Generate comprehensive interview preparation. Return ONLY valid JSON (no markdown, no backticks) in this exact structure:

{{
  "questions": [
    {{
      "category": "technical|behavioral|situational|culture_fit",
      "question": "The interview question",
      "why_asked": "Why the interviewer asks this for this specific role",
      "suggested_answer": "A strong answer based on the candidate's resume (use STAR format for behavioral)",
      "tips": "Specific tips for nailing this answer"
    }}
  ],
  "talking_points": [
    {{
      "topic": "Key selling point from resume",
      "relevance": "Why this matters for this specific role",
      "how_to_present": "How to frame this in the interview"
    }}
  ],
  "company_research": [
    {{
      "topic": "Thing to research about the company",
      "why": "Why this helps in the interview",
      "question_to_ask": "A smart question to ask the interviewer about this"
    }}
  ],
  "red_flags": [
    "Resume gap or weakness to prepare for"
  ]
}}

Generate exactly: 8 questions (mix of categories), 4 talking points, 3 company research items, and 2 red flags.
Base all suggested answers on ACTUAL resume content — never fabricate experience."""

        message = client.messages.create(
            model=CLAUDE_SONNET,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = message.content[0].text.strip()

        # Parse JSON
        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start != -1 and end > start:
            result = json.loads(response_text[start:end])
        else:
            result = json.loads(response_text)

        result["error"] = False
        return result

    except json.JSONDecodeError as e:
        logger.error("Interview prep JSON parse failed: %s", e)
        return {
            "questions": [],
            "talking_points": [],
            "company_research": [],
            "red_flags": [],
            "error": True,
            "error_message": "Failed to parse AI response.",
        }
    except Exception as e:
        logger.error("Interview prep generation failed: %s", e)
        return {
            "questions": [],
            "talking_points": [],
            "company_research": [],
            "red_flags": [],
            "error": True,
            "error_message": f"Generation failed: {str(e)}",
        }
