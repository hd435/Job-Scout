"""Deterministic, explainable matching and location normalization.

The matcher is a pure decision layer: no network, no database, no notifications
(architecture section 7). Reasons are stable, machine-readable strings so dry-run
output and tests can assert on them:

    family:data
    early_career:graduate
    location:london
    exclude:seniority:senior
    exclude:family:sales
    exclude:location:non_uk_country
    review:seniority_unclear
    review:location:location_unresolved
    note:sponsorship_mentioned
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType

from .models import Job, LocationResult, MatchDecision, MatchOutcome

RULES_VERSION = "m1.1"

_SEGMENT_SPLIT = re.compile(r"[;|\n]+")

_UK_COUNTRY_TOKENS = (
    "united kingdom",
    "uk",
    "u.k.",
    "great britain",
    "britain",
    "england",
    "scotland",
    "wales",
    "northern ireland",
)

_UK_CITIES: Mapping[str, str] = MappingProxyType(
    {
        "london": "London",
        "manchester": "Manchester",
        "birmingham": "Birmingham",
        "leeds": "Leeds",
        "edinburgh": "Edinburgh",
        "glasgow": "Glasgow",
        "bristol": "Bristol",
        "cambridge": "Cambridge",
        "oxford": "Oxford",
        "sheffield": "Sheffield",
        "nottingham": "Nottingham",
        "newcastle": "Newcastle",
        "cardiff": "Cardiff",
        "belfast": "Belfast",
        "reading": "Reading",
        "slough": "Slough",
        "coventry": "Coventry",
        "milton keynes": "Milton Keynes",
        "liverpool": "Liverpool",
        "southampton": "Southampton",
        "swindon": "Swindon",
        "knutsford": "Knutsford",
        "northampton": "Northampton",
        "chester": "Chester",
        "warrington": "Warrington",
        "bracknell": "Bracknell",
        "brighton": "Brighton",
        "derby": "Derby",
        "leicester": "Leicester",
        "aberdeen": "Aberdeen",
        "swansea": "Swansea",
        "exeter": "Exeter",
        "york": "York",
        "bath": "Bath",
        "guildford": "Guildford",
        "watford": "Watford",
        "croydon": "Croydon",
        "basingstoke": "Basingstoke",
        "peterborough": "Peterborough",
        "middlesbrough": "Middlesbrough",
        "sunderland": "Sunderland",
    }
)

# Cities that exist in more than one country: only resolvable with a country token.
_AMBIGUOUS_CITIES = frozenset({"cambridge", "birmingham", "oxford", "manchester", "durham"})

_NON_UK_COUNTRY_TOKENS = (
    "united states",
    "usa",
    "u.s.",
    "us",
    "america",
    "canada",
    "india",
    "germany",
    "france",
    "netherlands",
    "belgium",
    "spain",
    "portugal",
    "italy",
    "switzerland",
    "austria",
    "poland",
    "romania",
    "ukraine",
    "turkey",
    "israel",
    "singapore",
    "australia",
    "new zealand",
    "japan",
    "china",
    "hong kong",
    "taiwan",
    "south korea",
    "korea",
    "ireland",
    "brazil",
    "mexico",
    "denmark",
    "norway",
    "sweden",
    "finland",
    "estonia",
    "czech republic",
    "united arab emirates",
    "uae",
    "saudi arabia",
    "qatar",
)

_NON_UK_CITIES: Mapping[str, str] = MappingProxyType(
    {
        "new york": "New York",
        "san francisco": "San Francisco",
        "palo alto": "Palo Alto",
        "seattle": "Seattle",
        "denver": "Denver",
        "washington": "Washington",
        "boston": "Boston",
        "austin": "Austin",
        "chicago": "Chicago",
        "los angeles": "Los Angeles",
        "atlanta": "Atlanta",
        "miami": "Miami",
        "toronto": "Toronto",
        "vancouver": "Vancouver",
        "munich": "Munich",
        "berlin": "Berlin",
        "paris": "Paris",
        "amsterdam": "Amsterdam",
        "dublin": "Dublin",
        "tokyo": "Tokyo",
        "sydney": "Sydney",
        "melbourne": "Melbourne",
        "bengaluru": "Bengaluru",
        "bangalore": "Bangalore",
        "hyderabad": "Hyderabad",
        "pune": "Pune",
        "gurugram": "Gurugram",
        "mumbai": "Mumbai",
        "new delhi": "New Delhi",
        "warsaw": "Warsaw",
        "madrid": "Madrid",
        "barcelona": "Barcelona",
        "lisbon": "Lisbon",
        "milan": "Milan",
        "zurich": "Zurich",
        "geneva": "Geneva",
        "stockholm": "Stockholm",
        "copenhagen": "Copenhagen",
        "oslo": "Oslo",
        "helsinki": "Helsinki",
        "vienna": "Vienna",
        "prague": "Prague",
        "budapest": "Budapest",
        "tel aviv": "Tel Aviv",
        "dubai": "Dubai",
        "riyadh": "Riyadh",
        "seoul": "Seoul",
        "mexico city": "Mexico City",
        "sao paulo": "Sao Paulo",
        "bogota": "Bogota",
    }
)


def normalize_text(value: str) -> str:
    """Casefold and flatten text for deterministic keyword matching."""
    text = value.casefold().replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")
    text = re.sub(r"[_/]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def _has_stem(text: str, stem: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(stem)}", text) is not None


def _first_word(text: str, words: tuple[str, ...]) -> str | None:
    for word in words:
        if _has_word(text, word):
            return word
    return None


def _first_phrase(text: str, phrases: Mapping[str, str]) -> str | None:
    """Longest matching phrase wins so 'new york' is not shadowed by 'york'."""
    for phrase in sorted(phrases, key=len, reverse=True):
        if _has_word(text, phrase):
            return phrase
    return None


def normalize_location(raw: str | None, workplace_type: str | None = None) -> LocationResult:
    """Classify a raw location string under the approved location rules.

    Rules: any London location qualifies; UK remote qualifies only with explicit UK
    eligibility; any other UK location qualifies; non-UK only does not qualify;
    anything unresolvable is ambiguous and never alert-eligible.
    """
    text = normalize_text(raw or "")
    segments = [segment.strip() for segment in _SEGMENT_SPLIT.split(text) if segment.strip()]

    if not segments:
        return LocationResult(
            qualifies=False,
            reason="location_missing",
            ambiguous=True,
            workplace_type=workplace_type,
        )

    ambiguous_reasons: list[str] = []
    non_uk_reasons: list[str] = []
    for segment in segments:
        kind, reason, city, country = _classify_segment(segment, workplace_type)
        if kind == "qualifies":
            return LocationResult(
                qualifies=True,
                reason=reason,
                city=city,
                country=country,
                workplace_type=workplace_type,
                ambiguous=False,
            )
        if kind == "ambiguous":
            ambiguous_reasons.append(reason)
        else:
            non_uk_reasons.append(reason)

    if ambiguous_reasons:
        return LocationResult(
            qualifies=False,
            reason=f"location_ambiguous:{ambiguous_reasons[0]}",
            ambiguous=True,
            workplace_type=workplace_type,
        )
    return LocationResult(
        qualifies=False,
        reason=non_uk_reasons[0],
        country=None,
        workplace_type=workplace_type,
    )


def _classify_segment(
    segment: str, workplace_type: str | None
) -> tuple[str, str, str | None, str | None]:
    text = segment
    # Northern Ireland is part of the UK, so remove it before the "ireland" check.
    text_without_ni = text.replace("northern ireland", "")

    uk_country = _first_word(text, _UK_COUNTRY_TOKENS)
    uk_city = _first_phrase(text, _UK_CITIES)
    non_uk_country = _first_word(text_without_ni, _NON_UK_COUNTRY_TOKENS)
    non_uk_city = _first_phrase(text, _NON_UK_CITIES)
    if non_uk_city:
        # 'New York' must not be resolved as the UK city of York.
        uk_city = None

    is_remote = _has_stem(text, "remote")
    resolved_workplace = "remote" if is_remote else workplace_type

    if is_remote:
        if uk_country or (uk_city and uk_city not in _AMBIGUOUS_CITIES):
            city = _UK_CITIES.get(uk_city) if uk_city else None
            return "qualifies", "uk_remote", city, "United Kingdom"
        if non_uk_country or non_uk_city:
            detail = non_uk_city or non_uk_country or "unknown"
            return "non_uk", f"non_uk_remote:{detail}", None, None
        return "ambiguous", "remote_uk_eligibility_unclear", None, None

    if uk_city:
        city_name = _UK_CITIES[uk_city]
        if uk_city in _AMBIGUOUS_CITIES and not uk_country:
            return "ambiguous", f"city_country_unclear:{uk_city}", city_name, None
        if non_uk_country or non_uk_city:
            return "ambiguous", f"conflicting_country:{uk_city}", city_name, None
        reason = "london" if uk_city == "london" else f"uk_city:{uk_city}"
        return "qualifies", reason, city_name, "United Kingdom"

    if uk_country:
        if non_uk_country or non_uk_city:
            detail = non_uk_city or non_uk_country or "unknown"
            return "ambiguous", f"conflicting_country:{detail}", None, None
        if resolved_workplace == "remote":
            return "qualifies", "uk_remote", None, "United Kingdom"
        if resolved_workplace == "hybrid":
            return "qualifies", "uk_hybrid", None, "United Kingdom"
        return "qualifies", "uk_country", None, "United Kingdom"

    if non_uk_city:
        return "non_uk", f"non_uk_city:{non_uk_city}", None, None
    if non_uk_country:
        return "non_uk", f"non_uk_country:{non_uk_country}", None, None
    return "ambiguous", "location_unresolved", None, None


@dataclass(frozen=True)
class MatchingRules:
    role_families: Mapping[str, tuple[str, ...]]
    excluded_families: Mapping[str, tuple[str, ...]]
    early_career_stems: tuple[str, ...]
    early_career_words: tuple[str, ...]
    seniority_exclusions: tuple[str, ...]
    graduate_programme_signals: tuple[str, ...]
    phd_signals: tuple[str, ...]
    description_early_career_signals: tuple[str, ...]
    sponsorship_signals: tuple[str, ...]
    extra_family_terms: Mapping[str, tuple[str, ...]] = MappingProxyType({})
    extra_exclude_terms: tuple[str, ...] = ()
    version: str = RULES_VERSION

    @property
    def has_overrides(self) -> bool:
        return bool(self.extra_family_terms) or bool(self.extra_exclude_terms)

    def family_stems(self, family: str) -> tuple[str, ...]:
        """Approved phrases for a family plus any configured additions."""
        return self.role_families[family] + self.extra_family_terms.get(family, ())


ROLE_FAMILIES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "data": (
            "data scien",
            "data analyst",
            "data analytic",
            "data engineer",
            "data architect",
            "data platform",
            "data governance",
            "analytics engineer",
            "business intelligence",
            "bi analyst",
            "bi developer",
            "machine learning",
            "artificial intelligence",
            "ai engineer",
            "ai research",
            "ml engineer",
            "applied scientist",
            "research scientist",
            "statistic",
            "nlp",
            "computer vision",
            "decision scien",
        ),
        "software": (
            "software engineer",
            "software developer",
            "software development",
            "full stack",
            "full-stack",
            "frontend",
            "front end",
            "front-end",
            "backend",
            "back end",
            "back-end",
            "web developer",
            "platform engineer",
            "devops",
            "dev ops",
            "site reliability",
            "sre",
            "cloud engineer",
            "mobile engineer",
            "ios engineer",
            "android engineer",
            "security engineer",
            "infrastructure engineer",
            "forward deployed",
            "implementation engineer",
            "solutions engineer",
            "systems engineer",
            "embedded engineer",
            "firmware",
            "qa engineer",
            "quality engineer",
            "test engineer",
        ),
        "quant": (
            "quant",
            "quantitative",
            "risk analyst",
            "risk analysis",
            "credit risk",
            "market risk",
            "operational risk",
            "model risk",
            "risk modelling",
            "risk modeling",
            "actuar",
            "treasury analyst",
            "trading analyst",
        ),
        "finance": (
            "financial analyst",
            "finance analyst",
            "financial analysis",
            "fp&a",
            "financial planning",
            "investment analyst",
            "equity research",
            "corporate finance",
            "financial modelling",
            "financial modeling",
            "finance graduate",
            "graduate finance",
            "finance intern",
        ),
        "consulting": (
            "consult",
            "deployment strategist",
            "strategy analyst",
            "advisory",
        ),
        "business_analysis": (
            "business analyst",
            "business analysis",
            "systems analyst",
            "product analyst",
            "process analyst",
            "operations analyst",
            "commercial analyst",
            "insight analyst",
            "reporting analyst",
        ),
    }
)

EXCLUDED_FAMILIES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "sales": (
            "sales",
            "account executive",
            "business development",
            "revenue operations",
            "customer success",
            "partnership",
        ),
        "marketing": (
            "marketing",
            "brand",
            "communications",
            "public relations",
            "social media",
        ),
        "hr": (
            "human resources",
            "recruiter",
            "recruitment",
            "talent acquisition",
            "people operations",
            "people partner",
        ),
        "legal": (
            "legal",
            "counsel",
            "paralegal",
            "patent",
        ),
        "accounting": (
            "accountant",
            "accounting",
            "audit",
            "tax",
            "payroll",
        ),
        "design": (
            "product design",
            "graphic design",
            "visual design",
            "interaction design",
            "ux",
        ),
    }
)

EARLY_CAREER_STEMS = (
    "graduate",
    "entry level",
    "entry-level",
    "early career",
    "internship",
    "industrial placement",
    "new grad",
    "apprentice",
    "placement year",
)

EARLY_CAREER_WORDS = (
    "intern",
    "junior",
    "trainee",
    "placement",
    "campus",
)

SENIORITY_EXCLUSIONS = (
    "senior",
    "snr",
    "sr",
    "lead",
    "principal",
    "staff",
    "head",
    "director",
    "vice president",
    "vp",
    "chief",
    "manager",
)

GRADUATE_PROGRAMME_SIGNALS = (
    "graduate programme",
    "graduate program",
    "graduate scheme",
    "graduate pathway",
    "graduate development programme",
)

PHD_SIGNALS = ("phd", "ph.d", "doctorate")

DESCRIPTION_EARLY_CAREER_SIGNALS = (
    "graduate programme",
    "graduate program",
    "graduate scheme",
    "recent graduate",
    "final year student",
    "penultimate year",
    "entry level",
    "entry-level",
    "no previous experience",
)

SPONSORSHIP_SIGNALS = (
    "visa sponsorship",
    "sponsorship available",
    "sponsorship is available",
    "we sponsor",
    "skilled worker visa",
    "certificate of sponsorship",
    "tier 2 visa",
)

DEFAULT_RULES = MatchingRules(
    role_families=ROLE_FAMILIES,
    excluded_families=EXCLUDED_FAMILIES,
    early_career_stems=EARLY_CAREER_STEMS,
    early_career_words=EARLY_CAREER_WORDS,
    seniority_exclusions=SENIORITY_EXCLUSIONS,
    graduate_programme_signals=GRADUATE_PROGRAMME_SIGNALS,
    phd_signals=PHD_SIGNALS,
    description_early_career_signals=DESCRIPTION_EARLY_CAREER_SIGNALS,
    sponsorship_signals=SPONSORSHIP_SIGNALS,
)


def build_rules(
    *,
    extra_family_terms: Mapping[str, Sequence[str]] | None = None,
    extra_exclude_terms: Sequence[str] | None = None,
) -> MatchingRules:
    """Merge configuration overrides into the approved ruleset.

    Overrides are additive by construction: they may add phrases *inside* an
    approved role family and add extra title exclusions. Unknown family names are
    rejected, so overrides cannot invent a category, and exclusions are evaluated
    before matches, so an added term cannot cancel an approved exclusion.
    """
    families: dict[str, tuple[str, ...]] = {}
    for family, raw_terms in (extra_family_terms or {}).items():
        if family not in ROLE_FAMILIES:
            known = ", ".join(sorted(ROLE_FAMILIES))
            raise ValueError(f"unknown role family '{family}' (approved: {known})")
        if isinstance(raw_terms, str) or not isinstance(raw_terms, Sequence):
            raise ValueError(f"extra terms for '{family}' must be an array of strings")
        terms = _clean_terms(raw_terms, origin=f"extra_family_terms.{family}")
        if terms:
            families[family] = terms

    excludes = _clean_terms(extra_exclude_terms or (), origin="extra_exclude_terms")
    if not families and not excludes:
        return DEFAULT_RULES

    return replace(
        DEFAULT_RULES,
        extra_family_terms=MappingProxyType(families),
        extra_exclude_terms=excludes,
        version=f"{RULES_VERSION}+{_override_digest(families, excludes)}",
    )


def _clean_terms(terms: Sequence[object], *, origin: str) -> tuple[str, ...]:
    cleaned: list[str] = []
    for term in terms:
        if not isinstance(term, str):
            raise ValueError(f"{origin} must contain strings")
        normalised = normalize_text(term)
        if not normalised:
            raise ValueError(f"{origin} contains an empty term")
        if normalised not in cleaned:
            cleaned.append(normalised)
    return tuple(cleaned)


def _override_digest(families: Mapping[str, tuple[str, ...]], excludes: Sequence[str]) -> str:
    payload = json.dumps(
        {
            "families": {family: sorted(terms) for family, terms in sorted(families.items())},
            "excludes": sorted(excludes),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def match_job(job: Job, rules: MatchingRules = DEFAULT_RULES) -> MatchDecision:
    """Decide whether a role matches the approved early-career/location rules."""
    title = normalize_text(job.title)
    description = normalize_text(job.description or "")

    hard_exclusions: list[str] = []
    reviews: list[str] = []
    notes: list[str] = []

    families = tuple(
        name
        for name in rules.role_families
        if any(_has_stem(title, stem) for stem in rules.family_stems(name))
    )
    blocked = tuple(
        name
        for name, stems in rules.excluded_families.items()
        if any(_has_stem(title, stem) for stem in stems)
    )
    custom_exclusions = tuple(term for term in rules.extra_exclude_terms if _has_stem(title, term))

    notes.extend(f"family:{name}" for name in families)
    if blocked and families:
        reviews.extend(f"review:family_conflict:{name}" for name in blocked)
    elif blocked:
        hard_exclusions.extend(f"exclude:family:{name}" for name in blocked)
    elif not families:
        hard_exclusions.append("exclude:family:none")

    hard_exclusions.extend(f"exclude:custom:{term}" for term in custom_exclusions)

    graduate_programme = any(
        _has_stem(title, signal) for signal in rules.graduate_programme_signals
    )
    seniority_hits = tuple(
        signal for signal in rules.seniority_exclusions if _has_word(title, signal)
    )
    early_hits = tuple(
        dict.fromkeys(
            [signal for signal in rules.early_career_stems if _has_stem(title, signal)]
            + [signal for signal in rules.early_career_words if _has_word(title, signal)]
        )
    )

    if seniority_hits:
        if graduate_programme:
            notes.append(f"note:graduate_programme_override:{seniority_hits[0]}")
        else:
            hard_exclusions.extend(f"exclude:seniority:{hit}" for hit in seniority_hits)

    phd_hit = _first_word(title, rules.phd_signals)
    if phd_hit and not early_hits:
        hard_exclusions.append(f"exclude:phd_requirement:{phd_hit}")

    if early_hits:
        notes.extend(f"early_career:{hit}" for hit in early_hits)
    elif not seniority_hits:
        description_hit = next(
            (
                signal
                for signal in rules.description_early_career_signals
                if _has_stem(description, signal)
            ),
            None,
        )
        if description_hit:
            notes.append(f"early_career:description:{description_hit}")
        elif _has_word(title, "associate"):
            reviews.append("review:seniority_unclear:associate")
        else:
            reviews.append("review:seniority_unclear")

    sponsorship_hit = next(
        (
            signal
            for signal in rules.sponsorship_signals
            if _has_stem(title, signal) or _has_stem(description, signal)
        ),
        None,
    )
    if sponsorship_hit:
        notes.append(f"note:sponsorship_mentioned:{sponsorship_hit}")

    location = job.location_normalized
    if location.qualifies:
        notes.append(f"location:{location.reason}")
    elif location.ambiguous:
        reviews.append(f"review:location:{location.reason}")
    else:
        hard_exclusions.append(f"exclude:location:{location.reason}")

    if hard_exclusions:
        decision = MatchOutcome.NO_MATCH
        reasons = tuple(hard_exclusions + reviews + notes)
    elif reviews:
        decision = MatchOutcome.NEEDS_REVIEW
        reasons = tuple(reviews + notes)
    else:
        decision = MatchOutcome.MATCH
        reasons = tuple(notes)
    return MatchDecision(decision=decision, reasons=reasons, rules_version=rules.version)
