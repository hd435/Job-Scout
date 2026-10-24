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

LOCATION_MODES = ("pinned", "uk", "uk_plus_pinned")

# Employment-type classes (D-068). `unknown` is the honest "the title and description do
# not say", and it is deliberately not a valid preference value: silence is safer than noise.
EMPLOYMENT_TYPES = (
    "graduate_scheme",
    "internship",
    "placement",
    "apprenticeship",
    "entry_level_permanent",
    "experienced_hire",
)
UNKNOWN_EMPLOYMENT_TYPE = "unknown"

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
        "halifax": "Halifax",
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
_AMBIGUOUS_CITIES = frozenset(
    {"cambridge", "birmingham", "oxford", "manchester", "durham", "halifax"}
)

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


@dataclass(frozen=True)
class LocationPreferences:
    """How far a role's location may range before it stops being the user's business.

    `uk` is the shipped default: any UK location qualifies, which is what the rules did
    before locations became configurable. `pinned` restricts alerting to the listed
    cities (plus UK remote and UK hybrid when those toggles are on); `uk_plus_pinned`
    keeps the whole UK and adds pinned cities outside it.
    """

    mode: str = "uk"
    cities: tuple[str, ...] = ()
    remote_uk: bool = True
    hybrid_uk: bool = True

    @property
    def is_default(self) -> bool:
        return self == DEFAULT_LOCATION_PREFERENCES


DEFAULT_LOCATION_PREFERENCES = LocationPreferences()


def split_pinned_cities(cities: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split pinned cities into canonical UK names and raw-text patterns.

    A UK name the city knowledge recognises becomes a comparison against
    `LocationResult.city`, so ambiguity handling keeps working unchanged. Everything else
    ? a foreign city, or a town the list has never heard of ? is kept as text to look for
    in the raw location, because a finite city list must not make a configured preference
    silently ineffective.
    """
    known: list[str] = []
    patterns: list[str] = []
    for raw in cities:
        text = normalize_text(raw)
        if not text:
            continue
        canonical = _UK_CITIES.get(text)
        if canonical:
            if canonical not in known:
                known.append(canonical)
        elif text not in patterns:
            patterns.append(text)
    return tuple(known), tuple(patterns)


def known_cities() -> tuple[str, ...]:
    """Every city name the resolver can classify, for validation messages."""
    return tuple(sorted({*_UK_CITIES.values(), *_NON_UK_CITIES.values()}))


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


def normalize_location(
    raw: str | None, workplace_type: str | None = None, *, uk_employer: bool = False
) -> LocationResult:
    """Classify a raw location string under the approved location rules.

    Rules: any London location qualifies; UK remote qualifies only with explicit UK
    eligibility; any other UK location qualifies; non-UK only does not qualify;
    anything unresolvable is ambiguous and never alert-eligible.

    `uk_employer` marks a source whose board only advertises UK roles. It relaxes the
    name-collision check for towns that also exist abroad (Manchester, Halifax, ...), which
    would otherwise need review for ever. It never makes a non-UK location qualify: an
    explicit foreign country or city still excludes.
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
        kind, reason, city, country = _classify_segment(
            segment, workplace_type, uk_employer=uk_employer
        )
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
    segment: str, workplace_type: str | None, *, uk_employer: bool = False
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
        if uk_country or (uk_city and (uk_employer or uk_city not in _AMBIGUOUS_CITIES)):
            city = _UK_CITIES.get(uk_city) if uk_city else None
            return "qualifies", "uk_remote", city, "United Kingdom"
        if non_uk_country or non_uk_city:
            detail = non_uk_city or non_uk_country or "unknown"
            return "non_uk", f"non_uk_remote:{detail}", None, None
        return "ambiguous", "remote_uk_eligibility_unclear", None, None

    if uk_city:
        city_name = _UK_CITIES[uk_city]
        if uk_city in _AMBIGUOUS_CITIES and not uk_country and not uk_employer:
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


def classify_employment_type(title: str, description: str = "") -> str:
    """Classify how a role is entered, using only fields the sources actually expose.

    Order is most specific first: a "Graduate Data Science Internship" is an internship,
    not a graduate scheme, because the internship label is what makes it temporary. A
    title-based class always wins; an explicit multi-year requirement then outranks a soft
    description phrase, so "recent graduate welcome ... 3+ years of experience" is read as
    an experienced hire.
    """
    title = normalize_text(title)
    description = normalize_text(description)
    if _has_word(title, "intern") or _has_stem(title, "internship"):
        return "internship"
    if _has_stem(title, "summer analyst") or _has_stem(title, "summer internship"):
        return "internship"
    if _has_word(title, "placement") or _has_stem(title, "industrial placement"):
        return "placement"
    if _has_word(title, "year in industry"):
        return "placement"
    if _has_stem(title, "apprentice"):
        return "apprenticeship"
    if _has_stem(title, "graduate") or _has_stem(title, "new grad"):
        return "graduate_scheme"
    if any(_has_stem(f"{title} {description}", signal) for signal in GRADUATE_PROGRAMME_SIGNALS):
        return "graduate_scheme"
    if _has_word(title, "junior") or _has_word(title, "trainee") or _has_word(title, "campus"):
        return "entry_level_permanent"
    if _has_stem(title, "entry level") or _has_stem(title, "entry-level"):
        return "entry_level_permanent"
    if _has_stem(title, "early career"):
        return "entry_level_permanent"
    if _requested_experience_years(f"{title}. {description}"):
        return "experienced_hire"
    if any(_has_stem(description, signal) for signal in DESCRIPTION_EARLY_CAREER_SIGNALS):
        return "entry_level_permanent"
    return UNKNOWN_EMPLOYMENT_TYPE


def _requested_experience_years(text: str) -> int:
    """The smallest multi-year experience request the text states, or 0.

    Deliberately narrow: it matches only phrasing that attaches years to experience, so
    "our five-year strategy" is not read as a requirement.
    """
    years: list[int] = []
    for pattern in _EXPERIENCE_PATTERNS:
        for match in pattern.finditer(text):
            value = int(match.group(1))
            if 2 <= value <= 40:
                years.append(value)
    return min(years) if years else 0


_EXPERIENCE_PATTERNS = (
    re.compile(r"(\d{1,2})\+?\s*(?:-|to)?\s*\d{0,2}\s*years?\b[^.\n]{0,30}?\bexperience\b"),
    re.compile(r"\bexperience\b[^.\n]{0,20}?(\d{1,2})\+?\s*years?\b"),
    re.compile(r"(?:minimum of|at least|over|more than)\s+(\d{1,2})\+?\s*years?\b"),
)


LOCATION_KEEP = "keep"
LOCATION_OVERRIDE = "override"
LOCATION_REFUSE = "refuse"


def apply_location_scope(
    location: LocationResult,
    preferences: LocationPreferences,
    *,
    raw_location: str | None = None,
) -> tuple[str, str | None]:
    """Apply the configured location scope to an already-resolved location.

    Returns `(verdict, reason)`. `keep` means geography stands as the resolver decided it;
    `override` means the scope qualifies a location the resolver could not place (a pinned
    city the city list does not know); `refuse` means the scope excludes it, and `reason`
    names why. Geography itself is still decided by `normalize_location`, so ambiguity is
    preserved: pinning an ambiguous name does not override the collision check (D-065).
    """
    known, patterns = split_pinned_cities(preferences.cities)
    text = normalize_text(raw_location or "")
    pinned = (location.qualifies and bool(location.city and location.city in known)) or any(
        _has_word(text, pattern) for pattern in patterns
    )

    if location.qualifies:
        if location.reason == "uk_remote" and not preferences.remote_uk:
            return LOCATION_REFUSE, "remote_not_selected"
        if location.reason == "uk_hybrid" and not preferences.hybrid_uk:
            return LOCATION_REFUSE, "hybrid_not_selected"

    if preferences.mode == "uk":
        return LOCATION_KEEP, None
    if preferences.mode == "uk_plus_pinned":
        if location.qualifies:
            return LOCATION_KEEP, None
        if pinned:
            return LOCATION_OVERRIDE, "pinned_city"
        return LOCATION_REFUSE, "non_uk_not_pinned"
    # pinned: only the listed cities, plus UK remote when that toggle is on.
    if location.qualifies and (pinned or location.reason == "uk_remote"):
        return LOCATION_KEEP, None
    if pinned:
        return LOCATION_OVERRIDE, "pinned_city"
    return LOCATION_REFUSE, "not_pinned"


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
    allowed_families: tuple[str, ...] = ()
    employment_types: tuple[str, ...] = ()
    locations: LocationPreferences = DEFAULT_LOCATION_PREFERENCES
    version: str = RULES_VERSION

    @property
    def has_overrides(self) -> bool:
        """True when the active ruleset differs from the shipped defaults."""
        return self.version != RULES_VERSION

    @property
    def alerting_families(self) -> tuple[str, ...]:
        """Families that may alert; empty means "every approved family"."""
        return self.allowed_families or tuple(self.role_families)

    @property
    def alerting_employment_types(self) -> tuple[str, ...]:
        """Employment types that may alert; empty means every early-career class."""
        return self.employment_types or DEFAULT_EMPLOYMENT_TYPES

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

# The classes a role may alert under when the user has not chosen any (D-068).
DEFAULT_EMPLOYMENT_TYPES: tuple[str, ...] = (
    "graduate_scheme",
    "internship",
    "placement",
    "apprenticeship",
    "entry_level_permanent",
)

# Classes that are reported for review rather than excluded when they are not wanted.
# `unknown` is not a choice, and moving `experienced_hire` out of review would hide the
# closest thing this matcher has to a false-positive log.
REVIEW_ONLY_EMPLOYMENT_TYPES = frozenset({UNKNOWN_EMPLOYMENT_TYPE, "experienced_hire"})


def build_rules(
    *,
    extra_family_terms: Mapping[str, Sequence[str]] | None = None,
    extra_exclude_terms: Sequence[str] | None = None,
    title_exclusions: Sequence[str] | None = None,
    role_families: Sequence[str] | None = None,
    seniority_exclusions: Sequence[str] | None = None,
    employment_types: Sequence[str] | None = None,
    locations: LocationPreferences | None = None,
) -> MatchingRules:
    """Merge configured preferences into the approved ruleset.

    Two kinds of change are possible, and the difference matters. Terms are **additive**
    (extra phrases inside an approved family, extra title exclusions), so a typo can only
    narrow or widen within a category that has already been reviewed. Preferences are
    **selective**: they choose among the approved families and employment-type classes,
    and they set the location scope. Neither can invent a category, and exclusions are
    evaluated before matches, so no setting can cancel an approved exclusion.
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

    excludes = tuple(
        dict.fromkeys(
            _clean_terms(extra_exclude_terms or (), origin="extra_exclude_terms")
            + _clean_terms(title_exclusions or (), origin="title_exclusions")
        )
    )

    allowed: tuple[str, ...] = ()
    if role_families is not None:
        allowed = _clean_families(role_families)
    seniority = SENIORITY_EXCLUSIONS
    if seniority_exclusions is not None:
        # An explicit empty list means "no seniority exclusions"; omitting the key keeps
        # the approved list, so the permissive case has to be asked for.
        seniority = _clean_terms(seniority_exclusions, origin="seniority_exclusions")
    employment: tuple[str, ...] = ()
    if employment_types is not None:
        employment = _clean_employment_types(employment_types)
    location_preferences = locations or DEFAULT_LOCATION_PREFERENCES
    if location_preferences.mode not in LOCATION_MODES:
        raise ValueError(
            f"unknown location mode {location_preferences.mode!r}"
            f" (allowed: {', '.join(LOCATION_MODES)})"
        )

    resolved = replace(
        DEFAULT_RULES,
        extra_family_terms=MappingProxyType(families),
        extra_exclude_terms=excludes,
        allowed_families=allowed,
        seniority_exclusions=seniority,
        employment_types=employment,
        locations=location_preferences,
    )
    digest = _rules_digest(resolved)
    if digest == _rules_digest(DEFAULT_RULES):
        return DEFAULT_RULES
    return replace(resolved, version=f"{RULES_VERSION}+{digest}")


def _clean_families(values: Sequence[object]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("role_families must contain strings")
        name = value.strip().casefold()
        if name not in ROLE_FAMILIES:
            known = ", ".join(sorted(ROLE_FAMILIES))
            raise ValueError(f"unknown role family {name!r} (approved: {known})")
        if name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        raise ValueError("role_families cannot be empty: nothing would ever alert")
    return tuple(cleaned)


def _clean_employment_types(values: Sequence[object]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("employment_types must contain strings")
        name = value.strip().casefold()
        if name == UNKNOWN_EMPLOYMENT_TYPE:
            raise ValueError(
                "employment_types cannot include 'unknown': a role the rules cannot"
                " classify is reported for review rather than alerted"
            )
        if name not in EMPLOYMENT_TYPES:
            known = ", ".join(EMPLOYMENT_TYPES)
            raise ValueError(f"unknown employment type {name!r} (allowed: {known})")
        if name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        raise ValueError("employment_types cannot be empty: nothing would ever alert")
    return tuple(cleaned)


def _clean_terms(terms: Sequence[object], *, origin: str) -> tuple[str, ...]:
    cleaned: list[str] = []
    for term in terms:
        if not isinstance(term, str):
            raise ValueError(f"{origin} must contain strings")
        normalised = normalize_text(term)
        if not normalised:
            raise ValueError(f"{origin} contains an empty term")
        if len(normalised) < 2 or not re.search(r"[a-z0-9]", normalised):
            raise ValueError(
                f"{origin} contains an unusable term {normalised!r}:"
                " terms need at least two characters, including a letter or digit"
            )
        if normalised not in cleaned:
            cleaned.append(normalised)
    return tuple(cleaned)


def _rules_digest(rules: MatchingRules) -> str:
    """A short, stable fingerprint of everything that can change a decision.

    It covers the additive terms *and* the preference set, so a stored match decision
    can always be traced to the exact ruleset and scope that produced it.
    """
    payload = json.dumps(
        {
            "families": {
                family: sorted(terms) for family, terms in sorted(rules.extra_family_terms.items())
            },
            "excludes": sorted(rules.extra_exclude_terms),
            "allowed_families": sorted(rules.alerting_families),
            "seniority": sorted(rules.seniority_exclusions),
            "employment_types": sorted(rules.alerting_employment_types),
            "locations": {
                "mode": rules.locations.mode,
                "cities": sorted(normalize_text(city) for city in rules.locations.cities),
                "remote_uk": rules.locations.remote_uk,
                "hybrid_uk": rules.locations.hybrid_uk,
            },
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def preferences_fingerprint(rules: MatchingRules) -> str:
    """Fingerprint of the preference set alone, for `config show` output."""
    payload = json.dumps(
        {
            "allowed_families": sorted(rules.alerting_families),
            "seniority": sorted(rules.seniority_exclusions),
            "employment_types": sorted(rules.alerting_employment_types),
            "locations": {
                "mode": rules.locations.mode,
                "cities": sorted(normalize_text(city) for city in rules.locations.cities),
                "remote_uk": rules.locations.remote_uk,
                "hybrid_uk": rules.locations.hybrid_uk,
            },
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
    elif not any(name in rules.alerting_families for name in families):
        hard_exclusions.append(f"exclude:family:not_selected:{families[0]}")

    hard_exclusions.extend(f"exclude:title:{term}" for term in custom_exclusions)

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

    employment_type = classify_employment_type(title, description)
    if not hard_exclusions and not reviews:
        # The role would otherwise alert, so the employment type is the deciding gate.
        if employment_type in rules.alerting_employment_types:
            notes.append(f"employment_type:{employment_type}")
        elif employment_type in REVIEW_ONLY_EMPLOYMENT_TYPES:
            reviews.append(f"review:employment_type:{employment_type}")
        else:
            hard_exclusions.append(f"exclude:employment_type:{employment_type}")
    elif employment_type not in rules.alerting_employment_types and (
        employment_type not in REVIEW_ONLY_EMPLOYMENT_TYPES
    ):
        # A recognised early-career class the user switched off is an exclusion; an
        # unclassified or experienced-hire role only ever adds a review reason, so the
        # operator keeps seeing what the classifier is doing.
        hard_exclusions.append(f"exclude:employment_type:{employment_type}")

    location = job.location_normalized
    verdict, scope_reason = apply_location_scope(
        location, rules.locations, raw_location=job.location_raw
    )
    if location.qualifies and verdict == LOCATION_REFUSE:
        hard_exclusions.append(f"exclude:location:{scope_reason}")
    elif location.qualifies:
        notes.append(f"location:{location.reason}")
    elif verdict == LOCATION_OVERRIDE:
        # An explicitly pinned city the resolver does not know still qualifies: the user
        # named it, and a finite city list must not silently ignore that.
        notes.append(f"location:{scope_reason}")
    elif location.ambiguous:
        # Ambiguity outranks a narrow location scope: an unresolvable location is a
        # question for the operator, not a silent exclusion.
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
