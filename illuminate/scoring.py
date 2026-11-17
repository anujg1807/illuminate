"""Resume Overlap: how well a resume matches a job posting, as a percentage.

Claude reads the resume and the posting and rates six dimensions from 0 to 100.
The percentage is then computed here, in code, from those ratings, so the
arithmetic is the same for every job and can be changed without re-asking.

The dimensions and weights follow how product roles are actually screened:
what you have done and at what level matters most, then the skills asked for,
then how transferable your domain is, then context and formal requirements.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

# name -> (weight, label shown on the page, what Claude is asked to judge)
DIMENSIONS: dict[str, tuple[int, str, str]] = {
    "role": (
        25, "Role and responsibilities",
        "How closely the day-to-day work in the posting matches work the candidate has "
        "actually done (discovery, roadmap, delivery, go-to-market, analytics, stakeholder "
        "management, 0-to-1 versus scaling, and so on). Judge demonstrated work, not titles.",
    ),
    "experience": (
        20, "Experience and seniority",
        "Years of relevant experience and level of the role versus the candidate. Compare "
        "the years asked for with the candidate's relevant years, and the seniority (scope, "
        "team or people leadership, ownership) with what they have held. Being well above "
        "the level is also a mismatch, though a smaller one than being below it.",
    ),
    "skills": (
        20, "Skills and tools",
        "The specific skills, methods, tools and technical depth the posting asks for (for "
        "example SQL, experimentation, APIs, AI/ML, pricing, UX research) versus evidence "
        "of them in the resume. Required skills count more than preferred ones.",
    ),
    "domain": (
        15, "Domain and industry",
        "Industry and problem-space fit (fintech, SaaS, e-commerce, healthcare, hardware and "
        "so on). Adjacent domains earn partial credit in proportion to how transferable the "
        "knowledge is; score lower when the posting insists on domain experience.",
    ),
    "context": (
        10, "Product and company context",
        "Similarity of working context: B2B versus B2C, platform versus end-user product, "
        "company stage and scale, software versus hardware, market served.",
    ),
    "requirements": (
        10, "Stated requirements",
        "Formal must-haves: education, certifications, location or work arrangement, "
        "languages, and any other explicit condition in the posting.",
    ),
}

# If the posting states a hard requirement the candidate clearly does not meet,
# the overlap cannot exceed this, whatever the other dimensions say.
DEALBREAKER_CAP = 55

SYSTEM = """You assess how well a candidate's resume matches a job posting. Your ratings \
are shown to the candidate to help them decide which jobs to pursue, so they must be \
calibrated and honest rather than encouraging.

Rate each dimension from 0 to 100:
- 90-100: meets or exceeds everything asked, with direct evidence in the resume
- 70-89: meets most of it; gaps are minor or easily bridged
- 50-69: partial match; real gaps, but a plausible candidate
- 30-49: weak match; mostly adjacent experience
- 0-29: little or no relevant evidence

The dimensions:
{dimensions}

Rules:
- Use only what the resume and the posting say. Do not assume skills or experience the \
resume does not show, and do not penalise the candidate for things the posting does not ask.
- When the posting gives no information on a dimension, set applicable to false for it \
and give a score of 0; it is then left out of the percentage.
- Set dealbreaker to true only when the posting states a hard requirement (for example \
"minimum 10 years", "must have a CA", "must be based in Chennai") that the resume clearly \
fails. Preferences and nice-to-haves are never dealbreakers.
- years_required is the experience the posting asks for, as written (for example "5-8 \
years"), or null when it does not say. years_candidate is your estimate of the \
candidate's relevant years from the resume.
- Notes are one short sentence each, concrete, and addressed to the candidate ("you")."""


class Dimension(BaseModel):
    score: int = Field(description="0 to 100")
    applicable: bool
    note: str


class Assessment(BaseModel):
    role: Dimension
    experience: Dimension
    skills: Dimension
    domain: Dimension
    context: Dimension
    requirements: Dimension
    years_required: str | None
    years_candidate: str
    dealbreaker: bool
    dealbreaker_reason: str | None
    strengths: list[str]
    gaps: list[str]
    summary: str = Field(description="Two sentences at most")


def overall(assessment: Assessment) -> int:
    """The Resume Overlap percentage: a weighted average of the applicable dimensions."""
    total = weight_sum = 0.0
    for name, (weight, _, _) in DIMENSIONS.items():
        dimension: Dimension = getattr(assessment, name)
        if dimension.applicable:
            total += weight * max(0, min(100, dimension.score))
            weight_sum += weight
    if not weight_sum:
        return 0
    score = round(total / weight_sum)
    return min(score, DEALBREAKER_CAP) if assessment.dealbreaker else score


def to_detail(assessment: Assessment) -> dict:
    """What is stored and shown when a percentage is clicked."""
    return {
        "summary": assessment.summary,
        "years_required": assessment.years_required,
        "years_candidate": assessment.years_candidate,
        "dealbreaker": assessment.dealbreaker_reason if assessment.dealbreaker else None,
        "strengths": assessment.strengths,
        "gaps": assessment.gaps,
        "dimensions": [
            {
                "label": label,
                "weight": weight,
                "score": getattr(assessment, name).score,
                "applicable": getattr(assessment, name).applicable,
                "note": getattr(assessment, name).note,
            }
            for name, (weight, label, _) in DIMENSIONS.items()
        ],
    }


class ScoringUnavailable(Exception):
    """Scoring cannot run right now (no key, key rejected, out of credit)."""


def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


class Scorer:
    def __init__(self, model: str = "claude-opus-5-5") -> None:
        self.model = model
        self._client = None

    def reset(self) -> None:
        """Forget the client, so a newly saved API key is used."""
        self._client = None

    def available(self) -> bool:
        return has_api_key()

    def assess(self, resume_text: str, job: dict) -> Assessment:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic()
        dimensions = "\n".join(
            f"- {name} ({label}, weight {weight}): {what}"
            for name, (weight, label, what) in DIMENSIONS.items()
        )
        criteria = "\n".join(f"{key}: {value}" for key, value in (job.get("criteria") or {}).items())
        posting = (
            f"Title: {job['title']}\nCompany: {job['company']}\nLocation: {job['location']}\n"
            f"{criteria}\n\nDescription:\n{job['description']}"
        )
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=16000,
                # The instructions and the resume are identical for every job,
                # so they are cached and billed at a fraction of the price.
                system=[
                    {"type": "text", "text": SYSTEM.format(dimensions=dimensions)},
                    {
                        "type": "text",
                        "text": f"<resume>\n{resume_text}\n</resume>",
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                messages=[{
                    "role": "user",
                    "content": f"<job_posting>\n{posting}\n</job_posting>\n\nAssess the match.",
                }],
                output_format=Assessment,
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as error:
            raise ScoringUnavailable("The Anthropic API key was rejected. Check the key in .env.") from error
        except anthropic.RateLimitError as error:
            raise ScoringUnavailable("Anthropic rate limit reached; scoring will resume shortly.") from error
        except anthropic.BadRequestError as error:
            if "credit" in str(error).lower() or "billing" in str(error).lower():
                raise ScoringUnavailable("The Anthropic account is out of credit.") from error
            raise
        except anthropic.APIConnectionError as error:
            raise ScoringUnavailable("Could not reach Anthropic; scoring will retry.") from error
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise ValueError("Claude returned no assessment for this job")
        return response.parsed_output


Status = Literal["none", "pending", "done", "failed"]


def check_api_key(key: str) -> None:
    """Raise ScoringUnavailable if Anthropic does not accept this key (the check is free)."""
    import anthropic

    try:
        anthropic.Anthropic(api_key=key).models.list(limit=1)
    except anthropic.AuthenticationError as error:
        raise ScoringUnavailable("Anthropic did not accept that key. Copy it again from the Console.") from error
    except anthropic.PermissionDeniedError as error:
        raise ScoringUnavailable("That key does not have permission to use the API.") from error
    except anthropic.APIConnectionError as error:
        raise ScoringUnavailable("Could not reach Anthropic to check the key. Check your internet connection.") from error
