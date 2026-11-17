"""Resume Overlap: how well a resume matches a job posting, as a percentage.

Claude reads the resume and the posting and rates six dimensions from 0 to 100.
The percentage is then computed here, in code, from those ratings, so the
arithmetic is the same for every job and can be changed without re-asking.

The dimensions and weights follow how product roles are actually screened:
what you have done and at what level matters most, then the skills asked for,
then how transferable your domain is, then context and formal requirements.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
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


def dimensions_text() -> str:
    return "\n".join(
        f"- {name} ({label}, weight {weight}): {what}"
        for name, (weight, label, what) in DIMENSIONS.items()
    )


def posting_text(job: dict) -> str:
    criteria = "\n".join(f"{key}: {value}" for key, value in (job.get("criteria") or {}).items())
    return (
        f"Title: {job['title']}\nCompany: {job['company']}\nLocation: {job['location']}\n"
        f"{criteria}\n\nDescription:\n{job['description']}"
    )


class Scorer:
    """Scores through the Anthropic API, billed to an API key."""

    name = "API key"

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
        dimensions = dimensions_text()
        posting = posting_text(job)
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



# ---- scoring through Claude Code, on the user's Claude subscription ---------

CLAUDE_TIMEOUT_SECONDS = 300
# Where the Claude Code installer puts the command, in case it is not on PATH.
_CLAUDE_LOCATIONS = ("~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude")


def find_claude() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for place in _CLAUDE_LOCATIONS:
        path = Path(place).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


class ClaudeCodeScorer:
    """Scores by running Claude Code in non-interactive mode on this computer.

    Claude Code uses whatever Claude account it is signed in to, so no API key
    is involved: each assessment counts towards that plan's usage limits.
    """

    name = "Claude subscription"

    def __init__(self, model: str = "claude-opus-5-5", run=subprocess.run) -> None:
        self.model = model
        self._run = run

    def reset(self) -> None:
        pass

    def available(self) -> bool:
        return find_claude() is not None

    def assess(self, resume_text: str, job: dict) -> Assessment:
        claude = find_claude()
        if claude is None:
            raise ScoringUnavailable("Claude Code is not installed on this computer (see the README).")
        system = (
            SYSTEM.format(dimensions=dimensions_text())
            + f"\n\n<resume>\n{resume_text}\n</resume>"
        )
        prompt = f"<job_posting>\n{posting_text(job)}\n</job_posting>\n\nAssess the match."
        command = [
            claude, "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(Assessment.model_json_schema()),
            "--system-prompt", system,
            "--model", self.model,
            "--tools", "",                 # no file, shell or web access: it only reads the text given
            "--strict-mcp-config",         # and no connectors
            "--no-session-persistence",    # nothing added to your Claude Code history
        ]
        # Without an API key in its environment, Claude Code uses the signed-in
        # subscription; this keeps an API key, if one is set, from being billed.
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        with tempfile.TemporaryDirectory() as empty:  # so no project files or settings are picked up
            try:
                done = self._run(
                    command, input=prompt, capture_output=True, text=True,
                    timeout=CLAUDE_TIMEOUT_SECONDS, cwd=empty, env=env,
                )
            except subprocess.TimeoutExpired as error:
                raise ScoringUnavailable("Claude Code took too long to answer; scoring will retry.") from error
        try:
            reply = json.loads(done.stdout)
        except json.JSONDecodeError:
            reply = {"is_error": True, "result": (done.stderr or done.stdout).strip()}
        if done.returncode != 0 or reply.get("is_error"):
            raise _claude_problem(str(reply.get("result") or done.stderr or "unknown error"))
        output = reply.get("structured_output")
        if output is None:
            raise ValueError("Claude Code returned no assessment for this job")
        return Assessment.model_validate(output)


def _claude_problem(message: str) -> Exception:
    text = message.lower()
    if "limit" in text or "usage" in text and "reset" in text:
        return ScoringUnavailable(
            "Your Claude usage limit has been reached; Resume Overlap continues when it resets."
        )
    if "log in" in text or "login" in text or "not logged" in text or "authenticat" in text or "/login" in text:
        return ScoringUnavailable(
            "Claude Code is not signed in. Open Terminal, run claude, and sign in with your Claude account."
        )
    if "overloaded" in text or "network" in text or "connect" in text:
        return ScoringUnavailable("Claude is unavailable right now; scoring will retry.")
    return ValueError(f"Claude Code failed: {message[:300]}")


class AutoScorer:
    """Uses the API key if one is saved, otherwise the Claude subscription via Claude Code."""

    def __init__(self, model: str = "claude-opus-5-5", method: str = "auto") -> None:
        self.method = method
        self.api = Scorer(model)
        self.subscription = ClaudeCodeScorer(model)

    def _pick(self):
        if self.method == "api":
            return self.api
        if self.method == "claude-code":
            return self.subscription
        return self.api if self.api.available() else self.subscription

    @property
    def name(self) -> str:
        return self._pick().name

    def reset(self) -> None:
        self.api.reset()

    def available(self) -> bool:
        return self._pick().available()

    def assess(self, resume_text: str, job: dict) -> Assessment:
        return self._pick().assess(resume_text, job)
