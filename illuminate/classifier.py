"""Decide, from a job's title (and company), whether it is a Product Manager role.

LinkedIn's keyword search is fuzzy: searching "product manager" also returns
project managers, product designers, engineers and so on. Every job is kept in
the database with one of three labels so nothing is silently dropped:

    match  the title clearly says product management
    maybe  plausibly product management, but the title is ambiguous
    no     a different function
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MATCH, MAYBE, NO = "match", "maybe", "no"


@dataclass(frozen=True)
class Verdict:
    label: str
    reason: str


_EXPANSIONS = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "mgr": "manager",
    "mngr": "manager",
    "mgmt": "management",
    "asst": "assistant",
    "assoc": "associate",
    "dir": "director",
    "products": "product",
    "managers": "manager",
    "programme": "program",
}

# Words that, right after "product", name a different function
# ("Head of Product Design", "Manager - Product Support").
_OTHER_FUNCTIONS = {
    "marketing", "marketer", "design", "designer", "support", "sales",
    "engineering", "engineer", "development", "developer", "quality",
    "control", "controller", "security", "analyst", "analytics", "operations",
    "ops", "compliance", "research", "researcher", "safety", "testing", "test",
    "data", "partnerships", "solutions", "delivery", "service", "services",
    "training", "content", "legal", "counsel", "finance", "costing",
    "sourcing", "supply", "certification", "owner", "specialist", "executive",
    "architect", "architecture", "documentation", "photography", "success",
    "consultant", "consulting", "implementation", "integrity", "stewardship",
    "validation", "verification", "lifecycle", "registration", "regulatory",
    "application", "applications", "evangelist", "expert", "trainer",
}

# Role words that mean a title merely containing "product" is not product management.
_OTHER_ROLES = _OTHER_FUNCTIONS | {
    "software", "hardware", "mechanical", "electrical", "electronics",
    "project", "program", "production", "scientist", "tester", "qa",
    "accountant", "recruiter", "technician", "photographer", "writer",
    "merchandiser", "merchandising", "procurement", "intern", "trainee",
    "associate", "coordinator", "administrator", "officer", "advisor",
    "representative", "technical", "firmware", "embedded", "frontend",
    "backend", "fullstack", "devops", "sde", "sdet",
}

_PHARMA = re.compile(
    r"\b(pharma\w*|laborator(y|ies)|lifesciences?|life sciences?|biotech\w*|"
    r"therapeutics?|formulations?|remedies|drugs?|biologics|vaccines?|"
    r"healthcare products|medicines?)\b"
)
_PHARMA_TITLE = re.compile(
    r"\b(pmt|brand|therapy|derma\w*|cardio\w*|onco\w*|gynae\w*|ortho\w*|"
    r"diabet\w*|neuro\w*|respiratory|gastro\w*|nutraceutical\w*|otc|rx)\b"
)

_STRONG = re.compile(r"\bproduct (manager|management)\b")
_SENIOR = r"head|director|vp|avp|svp|evp|president|chief|gm|general manager"
_SENIOR_OF_PRODUCT = re.compile(rf"\b({_SENIOR}) (of |for )?(the )?product\b ?(\w+)?")
_MANAGER_OF_PRODUCT = re.compile(r"\b(manager|lead|leader|principal) (of |for )?product\b ?(\w+)?")
_PRODUCT_LEADER = re.compile(r"\bproduct (head|lead|leader|director|vp|chief|officer)\b")
_ABBREVIATIONS = {
    "apm": "APM can mean Associate Product Manager",
    "spm": "SPM can mean Senior Product Manager",
    "gpm": "GPM can mean Group Product Manager",
    "pm": "PM can mean Product Manager or Project Manager",
    "cpo": "CPO can mean Chief Product Officer",
}


# With these words nearby, "PM", "APM" or "SPM" stand for something else
# (SAP Plant Maintenance, Sales Performance Management, a 2 PM shift, ...).
_NOT_PRODUCT_ABBREVIATION = {
    "project", "program", "shift", "sap", "oracle", "maintenance", "performance",
    "portfolio", "property", "plant", "preventive", "engineer", "terminals",
}


def normalize(text: str) -> str:
    text = text.lower().replace("&", " and ")
    text = re.sub(r"vice[\s-]+president", "vp", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(_EXPANSIONS.get(word, word) for word in text.split())


def classify(title: str, company: str = "") -> Verdict:
    t = normalize(title)
    tokens = t.split()
    token_set = set(tokens)
    company_n = normalize(company)

    if _STRONG.search(t):
        if _PHARMA.search(company_n) or _PHARMA.search(t) or _PHARMA_TITLE.search(t):
            return Verdict(MAYBE, "Pharma \"product manager\" roles are usually brand marketing")
        if token_set & {"sales", "presales"}:
            return Verdict(MAYBE, "Says product manager, but also mentions sales")
        return Verdict(MATCH, "Title says product manager / product management")

    if "chief product officer" in t or _PRODUCT_LEADER.search(t):
        return Verdict(MATCH, "Product leadership title")

    m = _SENIOR_OF_PRODUCT.search(t)
    if m and (m.group(4) or "") not in _OTHER_FUNCTIONS:
        return Verdict(MATCH, "Product leadership title")

    m = _MANAGER_OF_PRODUCT.search(t)
    if m and (m.group(3) or "") not in _OTHER_FUNCTIONS:
        if token_set & {"sales", "presales"}:
            return Verdict(MAYBE, "Product role that also mentions sales")
        return Verdict(MATCH, "Title places the role in product")

    if "product owner" in t:
        return Verdict(MATCH, "Title says product owner")

    if "product marketing" in t:
        return Verdict(NO, "Product marketing role")
    if re.search(r"\b(project|program) (manager|management|lead|leader|coordinator)\b", t):
        return Verdict(NO, "Project / program management role")

    for abbreviation, meaning in _ABBREVIATIONS.items():
        if abbreviation in token_set:
            index = tokens.index(abbreviation)
            after_a_number = index > 0 and tokens[index - 1].isdigit()
            if after_a_number or token_set & _NOT_PRODUCT_ABBREVIATION:
                continue
            return Verdict(MAYBE, meaning)

    if "product" in token_set and not (token_set & _OTHER_ROLES):
        return Verdict(MAYBE, "Mentions product, but the role is unclear from the title")

    return Verdict(NO, "Not a product management title")
