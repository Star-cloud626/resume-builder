"""Tailors the resume to a job description using Vertex AI (Gemini).

Given the stable :class:`Profile` and a raw job-description string, the model
returns a structured object:

  * a tailored professional summary,
  * 6-7 achievement bullets for **each** job in the skeleton (grounded in that
    job's real company / title - the model must not invent employers), and
  * a prioritised skills list relevant to the job description.

Everything is returned as validated JSON via Gemini's structured-output mode.
"""

from __future__ import annotations

from dataclasses import dataclass

from google import genai
from google.genai import types
from pydantic import BaseModel

from .config import Profile, VertexSettings


# --- Schema the model must return ------------------------------------------


class _ExperienceBullets(BaseModel):
    company: str
    bullets: list[str]


class _SkillGroup(BaseModel):
    category: str
    skills: list[str]


class _EducationDetail(BaseModel):
    institution: str
    description: str


class _TailoredResume(BaseModel):
    summary: str
    experience: list[_ExperienceBullets]
    skill_groups: list[_SkillGroup]
    education: list[_EducationDetail]


# --- Public result types ----------------------------------------------------


@dataclass
class RenderedExperience:
    company: str
    title: str
    period: str
    location: str
    bullets: list[str]


@dataclass
class SkillGroup:
    category: str
    skills: list[str]


@dataclass
class RenderedEducation:
    institution: str
    degree: str
    period: str
    location: str
    description: str = ""


@dataclass
class TailoredResume:
    summary: str
    experience: list[RenderedExperience]
    skill_groups: list[SkillGroup]
    education: list[RenderedEducation]


class GenerationError(RuntimeError):
    pass


_SYSTEM_INSTRUCTION = """\
You are an expert resume writer. You tailor an existing candidate's resume to a
specific job description.

Hard rules:
- Use ONLY the companies, job titles and periods provided. Never invent, rename,
  merge or drop an employer. Return one entry per provided job, in the same order.
- Write achievement-oriented bullet points per job. Produce 6-7 bullets for EVERY
  role, regardless of how recent it is - do NOT taper older roles to fewer bullets,
  and do NOT compress content to hit any page count. The resume can run as many
  pages as this requires. Each bullet should be substantial (roughly one to two
  lines) - not a terse fragment. Start each with a strong past-tense verb.
- EMPHASIS: in every bullet, wrap the 2-4 most important keywords or phrases in
  **double asterisks** to render them bold (Markdown style). Emphasise the things
  a recruiter scans for - core technologies, the headline metric, the system or
  architecture owned (e.g. "cut p99 latency **62%** by adding **Redis** caching in
  front of **PostgreSQL read replicas**"). Do NOT bold whole sentences or more than
  ~4 spans per bullet; bold signal, not noise. Use asterisks ONLY for this - never
  any other Markdown.
- Bullets must be DEEPLY TECHNICAL, written by and for engineers. Each bullet must
  name concrete technologies, and most should also convey the technical HOW and WHY.
  Draw specifics from the target job description's stack and the candidate's skills:
    * Name real tools, languages, frameworks, protocols, patterns and services
      (e.g. "gRPC", "Kafka", "Redis caching", "PostgreSQL read replicas",
      "Kubernetes HPA", "OAuth2 / JWT", "event-driven microservices", "CQRS").
    * State the technical approach or architecture, not just the outcome
      (e.g. "sharded the write path with a partition key on tenant_id to remove a
      hot-row bottleneck" rather than "improved database performance").
    * Reference engineering practices where relevant: system design decisions,
      trade-offs, migrations, refactors, observability, testing strategy, CI/CD,
      performance profiling, concurrency, data modelling.
  AVOID vague corporate phrasing such as "improved efficiency", "ensured high
  availability", "delivered scalable solutions", "collaborated with teams",
  "drove business value" - replace every such phrase with the specific technical
  work that produced it.
- Match the technical depth and terminology to the seniority in each job title
  (a Senior role owns architecture and technical direction; a Junior role
  implements features and fixes within an existing system).
- Bullets must be plausible for that specific company and title and must align to
  the technologies and responsibilities in the target job description.
- Quantify impact where natural (latency, throughput, p99, cost, build time, error
  rate), but do NOT fabricate precise metrics, client names, or credentials that
  would be verifiably false. Keep numbers generic/approximate when unsure, and
  prefer a concrete technical detail over an invented statistic.
- The summary is a substantial 3-4 sentence paragraph, first-person implied
  (no "I"), tailored to the role and highlighting the candidate's strongest fit.
- Skills: organise into domain-based groups (skill_groups). Choose the categories
  that actually fit the target role - typical examples are "Frontend", "Backend",
  "Infrastructure & DevOps", "Databases", "Cloud", "Testing", "Tools & Practices",
  but adapt the set to the job (a data role might use "Languages", "Data
  Engineering", "ML/Analytics", etc.). Order the groups by relevance to the job
  description; put the most role-critical domain first. Each group holds 3-8 skills.
  Across ALL groups aim for roughly 25-30 skills total. Start from the skills named
  in the job description, then EXPAND with closely-related / adjacent skills a strong
  candidate for THIS role would credibly have. You may recommend skills beyond the
  candidate's base list when they are a natural fit for the role and seniority. Do
  not pad with unrelated skills, and never repeat a skill across groups.
- Education: for each education entry provided, write a 2-sentence description that
  ties the degree to the target role - relevant coursework, focus areas, projects
  or foundational knowledge that support this specific job. Keep the institution,
  degree and period exactly as given; never invent GPAs, honors, or awards.
Return valid JSON matching the requested schema. No markdown, no commentary.\
"""


def _build_prompt(profile: Profile, job_description: str) -> str:
    jobs = "\n".join(
        f"- company: {e.company} | title: {e.title} | period: {e.period}"
        f" | location: {e.location}"
        for e in profile.experience
    )
    base_skills = ", ".join(profile.base_skills) if profile.base_skills else "(none provided)"
    education = (
        "\n".join(
            f"- institution: {e.institution} | degree: {e.degree} | period: {e.period}"
            for e in profile.education
        )
        or "(none provided)"
    )
    return (
        f"Candidate name: {profile.contact.full_name}\n"
        f"Candidate headline: {profile.contact.headline}\n\n"
        f"Work history (STABLE - do not change these facts):\n{jobs}\n\n"
        f"Education (STABLE - do not change these facts):\n{education}\n\n"
        f"Candidate base skills: {base_skills}\n\n"
        f"TARGET JOB DESCRIPTION:\n{job_description.strip()}\n\n"
        "Produce the tailored resume now."
    )


def _require_vertex(job_description: str, vertex: VertexSettings) -> None:
    if not job_description.strip():
        raise GenerationError("Job description is empty.")
    if not vertex.project:
        raise GenerationError(
            "Vertex AI is not configured. Set GOOGLE_CLOUD_PROJECT (and "
            "GOOGLE_APPLICATION_CREDENTIALS or run `gcloud auth application-default "
            "login`) in your .env file."
        )


def _client(vertex: VertexSettings) -> genai.Client:
    return genai.Client(vertexai=True, project=vertex.project, location=vertex.location)


def generate(
    profile: Profile,
    job_description: str,
    vertex: VertexSettings,
) -> TailoredResume:
    _require_vertex(job_description, vertex)

    try:
        client = _client(vertex)  # keep a reference so its transport isn't GC-closed
        response = client.models.generate_content(
            model=vertex.model,
            contents=_build_prompt(profile, job_description),
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
                temperature=0.4,
                response_mime_type="application/json",
                response_schema=_TailoredResume,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean message to the UI
        raise GenerationError(f"Vertex AI request failed: {exc}") from exc

    parsed: _TailoredResume | None = getattr(response, "parsed", None)
    if parsed is None:
        raise GenerationError("The model returned no usable output. Try again.")

    # Match the AI bullets back to the stable skeleton (by order, company as fallback).
    bullets_by_company = {b.company.strip().lower(): b.bullets for b in parsed.experience}
    rendered: list[RenderedExperience] = []
    for i, e in enumerate(profile.experience):
        bullets = (
            parsed.experience[i].bullets
            if i < len(parsed.experience)
            else bullets_by_company.get(e.company.strip().lower(), [])
        )
        rendered.append(
            RenderedExperience(
                company=e.company,
                title=e.title,
                period=e.period,
                location=e.location,
                bullets=[b.strip() for b in bullets if b.strip()],
            )
        )

    skill_groups = [
        SkillGroup(
            category=g.category.strip(),
            skills=[s.strip() for s in g.skills if s.strip()],
        )
        for g in parsed.skill_groups
        if g.category.strip() and any(s.strip() for s in g.skills)
    ]

    desc_by_institution = {
        d.institution.strip().lower(): d.description for d in parsed.education
    }
    education = []
    for i, e in enumerate(profile.education):
        description = (
            parsed.education[i].description
            if i < len(parsed.education)
            else desc_by_institution.get(e.institution.strip().lower(), "")
        )
        education.append(
            RenderedEducation(
                institution=e.institution,
                degree=e.degree,
                period=e.period,
                location=e.location,
                description=description.strip(),
            )
        )

    return TailoredResume(
        summary=parsed.summary.strip(),
        experience=rendered,
        skill_groups=skill_groups,
        education=education,
    )


# --- Cover letter -----------------------------------------------------------


class _CoverLetter(BaseModel):
    body: str


_COVER_LETTER_INSTRUCTION = """\
You are an expert career writer producing a concise, professional cover-letter body.

Hard rules:
- Write the BODY only (no date, no address block, no "Dear ..." greeting, no
  "Sincerely" sign-off - those are added around your text).
- STRICT LIMIT: at most 5 sentences. Fewer is fine; never more.
- Tone: professional, confident, specific - not flowery or generic.
- Tailor it to the target job description and ground it in the candidate's real
  experience, seniority and top skills. Reference the kind of role/company.
- Do NOT invent employers, metrics, or credentials the candidate does not have.
- Write in the first person ("I"). Return valid JSON matching the schema.\
"""


def generate_cover_letter(
    profile: Profile,
    job_description: str,
    vertex: VertexSettings,
) -> str:
    """Return a professional cover-letter body of at most 5 sentences."""
    _require_vertex(job_description, vertex)

    top_experience = ", ".join(
        f"{e.title} at {e.company}" for e in profile.experience[:2]
    ) or "(none provided)"
    base_skills = ", ".join(profile.base_skills[:10]) if profile.base_skills else "(none)"
    prompt = (
        f"Candidate name: {profile.contact.full_name}\n"
        f"Candidate headline: {profile.contact.headline}\n"
        f"Most recent experience: {top_experience}\n"
        f"Key skills: {base_skills}\n\n"
        f"TARGET JOB DESCRIPTION:\n{job_description.strip()}\n\n"
        "Write the cover-letter body now (max 5 sentences)."
    )

    try:
        client = _client(vertex)  # keep a reference so its transport isn't GC-closed
        response = client.models.generate_content(
            model=vertex.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_COVER_LETTER_INSTRUCTION,
                temperature=0.5,
                response_mime_type="application/json",
                response_schema=_CoverLetter,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean message to the UI
        raise GenerationError(f"Vertex AI request failed: {exc}") from exc

    parsed: _CoverLetter | None = getattr(response, "parsed", None)
    if parsed is None or not parsed.body.strip():
        raise GenerationError("The model returned no cover letter. Try again.")

    return parsed.body.strip()


# --- Screening questions & answers ------------------------------------------


@dataclass
class QAPair:
    question: str
    answer: str


class _Answer(BaseModel):
    question: str
    answer: str


class _Answers(BaseModel):
    items: list[_Answer]


_ANSWERS_INSTRUCTION = """\
You are helping a specific candidate answer a job application's screening questions.

Hard rules:
- Answer EACH question provided, once, in the SAME order. Return one item per
  question, echoing the question text back verbatim in the "question" field.
- Each answer is 3-5 sentences: substantial but focused. Never fewer than 3.
- Write in the first person ("I"), professional and specific - not flowery.
- Ground every answer in the candidate's real experience, seniority and skills, and
  connect it to the target job description where relevant.
- Do NOT invent employers, metrics, degrees or credentials the candidate does not
  have. If a question asks about something absent from the profile, answer honestly
  from transferable experience rather than fabricating.
- Plain prose only: no Markdown, no bullet points, no headings.
Return valid JSON matching the requested schema.\
"""


def parse_questions(raw: str) -> list[str]:
    """Split a textarea blob into individual questions (one per non-empty line)."""
    return [line.strip() for line in raw.splitlines() if line.strip()]


def generate_answers(
    profile: Profile,
    job_description: str,
    questions: list[str],
    vertex: VertexSettings,
) -> list[QAPair]:
    """Answer each screening question in 3-5 sentences, grounded in the profile."""
    if not questions:
        raise GenerationError("Add at least one question.")
    _require_vertex(job_description, vertex)

    top_experience = ", ".join(
        f"{e.title} at {e.company}" for e in profile.experience[:3]
    ) or "(none provided)"
    base_skills = ", ".join(profile.base_skills[:15]) if profile.base_skills else "(none)"
    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    prompt = (
        f"Candidate name: {profile.contact.full_name}\n"
        f"Candidate headline: {profile.contact.headline}\n"
        f"Recent experience: {top_experience}\n"
        f"Key skills: {base_skills}\n\n"
        f"TARGET JOB DESCRIPTION:\n{job_description.strip()}\n\n"
        f"SCREENING QUESTIONS (answer each, in order):\n{numbered}\n\n"
        "Answer every question now (3-5 sentences each)."
    )

    try:
        client = _client(vertex)  # keep a reference so its transport isn't GC-closed
        response = client.models.generate_content(
            model=vertex.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_ANSWERS_INSTRUCTION,
                temperature=0.5,
                response_mime_type="application/json",
                response_schema=_Answers,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean message to the UI
        raise GenerationError(f"Vertex AI request failed: {exc}") from exc

    parsed: _Answers | None = getattr(response, "parsed", None)
    if parsed is None or not parsed.items:
        raise GenerationError("The model returned no answers. Try again.")

    # Pair answers back to the questions we asked, by order (model may reword them).
    pairs: list[QAPair] = []
    for i, q in enumerate(questions):
        answer = parsed.items[i].answer.strip() if i < len(parsed.items) else ""
        pairs.append(QAPair(question=q, answer=answer))
    return pairs
