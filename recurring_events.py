#!/usr/bin/env python3
"""
Mobilizon recurring events script.
"""

import argparse
import os
import re
import sys
import traceback
import typing
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Protocol, TypeAlias, TypedDict, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests  # type: ignore[import-untyped]
import yaml  # type: ignore[import-untyped]
from dateutil import rrule as dateutil_rrule  # type: ignore[import-untyped]

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
TemplateData: TypeAlias = dict[str, JsonValue]
GraphQLData: TypeAlias = dict[str, object]
GraphQLVariables: TypeAlias = dict[str, JsonValue]
RRuleArguments: TypeAlias = dict[str, object]
IntegerFieldBounds: TypeAlias = tuple[int | None, int | None, bool]


class GraphQLRequest(TypedDict):
    query: str
    variables: GraphQLVariables


class CommandLineArguments(Protocol):
    config: str
    debug: bool
    dry_run: bool
    offline: bool


REQUEST_TIMEOUT_SECONDS: tuple[int, int] = (10, 30)
PAGE_SIZE: int = 100
MATCH_TOLERANCE: timedelta = timedelta(seconds=60)
TOKEN_PATTERN: re.Pattern[str] = re.compile(r"{{\s*([a-z_]+)\s*}}")

STATIC_AUTH_VARIABLES: tuple[str, ...] = ("MOBILIZON_TOKEN",)
MACHINE_USER_AUTH_VARIABLES: tuple[str, ...] = (
    "MOBILIZON_MACHINE_USER_EMAIL",
    "MOBILIZON_MACHINE_USER_PASSWORD",
)

MACHINE_USER_LOGIN_ERROR: str = (
    "Mobilizon login failed; no event creations were attempted."
)

MACHINE_USER_LOGIN_MUTATION: str = """
mutation Login($email: String!, $password: String!) {
  login(email: $email, password: $password) {
    accessToken
  }
}
"""

GROUP_ORGANIZED_EVENTS_QUERY: str = """
query GroupOrganizedEvents(
  $preferredUsername: String!
  $afterDatetime: DateTime!
  $beforeDatetime: DateTime!
  $page: Int!
  $limit: Int!
) {
  group(preferredUsername: $preferredUsername) {
    id
    organizedEvents(
      afterDatetime: $afterDatetime
      beforeDatetime: $beforeDatetime
      order: BEGINS_ON
      orderDirection: ASC
      page: $page
      limit: $limit
    ) {
      elements {
        id
        uuid
        title
        beginsOn
        endsOn
        visibility
        draft
        organizerActor { id }
        attributedTo { id }
      }
    }
  }
}
"""

PERSON_ORGANIZED_EVENTS_QUERY: str = """
query PersonOrganizedEvents($id: ID!, $page: Int!, $limit: Int!) {
  person(id: $id) {
    id
    organizedEvents(page: $page, limit: $limit) {
      elements {
        id
        uuid
        title
        beginsOn
        endsOn
        visibility
        draft
        organizerActor { id }
        attributedTo { id }
      }
    }
  }
}
"""

CREATE_EVENT_MUTATION: str = """
mutation CreateEvent(
  $title: String!
  $description: String!
  $beginsOn: DateTime!
  $endsOn: DateTime
  $status: EventStatus
  $visibility: EventVisibility
  $joinOptions: EventJoinOptions
  $externalParticipationUrl: String
  $tags: [String]
  $picture: MediaInput
  $publishAt: DateTime
  $onlineAddress: String
  $phoneAddress: String
  $organizerActorId: ID!
  $attributedToId: ID
  $category: EventCategory
  $physicalAddress: AddressInput
  $options: EventOptionsInput
  $metadata: [EventMetadataInput]
  $draft: Boolean
  $contacts: [Contact]
  $language: String
) {
  createEvent(
    title: $title
    description: $description
    beginsOn: $beginsOn
    endsOn: $endsOn
    status: $status
    visibility: $visibility
    joinOptions: $joinOptions
    externalParticipationUrl: $externalParticipationUrl
    tags: $tags
    picture: $picture
    publishAt: $publishAt
    onlineAddress: $onlineAddress
    phoneAddress: $phoneAddress
    organizerActorId: $organizerActorId
    attributedToId: $attributedToId
    category: $category
    physicalAddress: $physicalAddress
    options: $options
    metadata: $metadata
    draft: $draft
    contacts: $contacts
    language: $language
  ) {
    id
    uuid
    title
    beginsOn
    endsOn
  }
}
"""

EVENT_KEYS: set[str] = {
    "id",
    "dtstart",
    "timezone",
    "duration_minutes",
    "rrule",
    "upcoming_count",
    "skip_starts",
    "organizer_actor_id",
    "attributed_to",
    "template",
}
RRULE_KEYS: set[str] = {
    "freq",
    "interval",
    "wkst",
    "count",
    "until",
    "bysetpos",
    "bymonth",
    "bymonthday",
    "byyearday",
    "byeaster",
    "byweekno",
    "byweekday",
    "byhour",
    "byminute",
    "bysecond",
}
FREQUENCY_VALUES: dict[str, int] = {
    "YEARLY": dateutil_rrule.YEARLY,
    "MONTHLY": dateutil_rrule.MONTHLY,
    "WEEKLY": dateutil_rrule.WEEKLY,
    "DAILY": dateutil_rrule.DAILY,
    "HOURLY": dateutil_rrule.HOURLY,
    "MINUTELY": dateutil_rrule.MINUTELY,
    "SECONDLY": dateutil_rrule.SECONDLY,
}
WEEKDAY_VALUES: dict[str, dateutil_rrule.weekday] = {
    "MO": dateutil_rrule.MO,
    "TU": dateutil_rrule.TU,
    "WE": dateutil_rrule.WE,
    "TH": dateutil_rrule.TH,
    "FR": dateutil_rrule.FR,
    "SA": dateutil_rrule.SA,
    "SU": dateutil_rrule.SU,
}
WEEKDAY_PATTERN: re.Pattern[str] = re.compile(
    r"(?P<ordinal>[+-]?\d+)?(?P<weekday>MO|TU|WE|TH|FR|SA|SU)$"
)
TEMPLATE_KEYS: set[str] = {
    "title",
    "description",
    "visibility",
    "join_options",
    "status",
    "draft",
    "category",
    "tags",
    "online_address",
    "phone_address",
    "external_participation_url",
    "picture_media_uuid",
    "language",
    "physical_address",
    "options",
    "metadata",
    "contacts",
}
ADDRESS_FIELD_MAP: dict[str, str] = {
    "geom": "geom",
    "street": "street",
    "locality": "locality",
    "postal_code": "postalCode",
    "region": "region",
    "country": "country",
    "description": "description",
    "type": "type",
    "url": "url",
    "id": "id",
    "origin_id": "originId",
    "timezone": "timezone",
}
OPTIONS_FIELD_MAP: dict[str, str] = {
    "maximum_attendee_capacity": "maximumAttendeeCapacity",
    "remaining_attendee_capacity": "remainingAttendeeCapacity",
    "show_remaining_attendee_capacity": "showRemainingAttendeeCapacity",
    "anonymous_participation": "anonymousParticipation",
    "offers": "offers",
    "participation_conditions": "participationConditions",
    "attendees": "attendees",
    "program": "program",
    "comment_moderation": "commentModeration",
    "show_participation_price": "showParticipationPrice",
    "hide_number_of_participants": "hideNumberOfParticipants",
    "show_start_time": "showStartTime",
    "show_end_time": "showEndTime",
    "timezone": "timezone",
    "hide_organizer_when_group_event": "hideOrganizerWhenGroupEvent",
    "is_online": "isOnline",
}
BOOLEAN_OPTION_FIELDS: set[str] = {
    "show_remaining_attendee_capacity",
    "anonymous_participation",
    "show_participation_price",
    "hide_number_of_participants",
    "show_start_time",
    "show_end_time",
    "hide_organizer_when_group_event",
    "is_online",
}
INTEGER_OPTION_FIELDS: set[str] = {
    "maximum_attendee_capacity",
    "remaining_attendee_capacity",
}
TEXT_OPTION_FIELDS: set[str] = {"program", "comment_moderation", "timezone"}


class ConfigurationError(ValueError):
    """The local YAML configuration is invalid."""


class ApiError(RuntimeError):
    """A read or write request to Mobilizon failed."""


def debug_api_error(message: str, enabled: bool, detail: str) -> ApiError:
    if enabled:
        return ApiError(f"{message} Debug: {detail}")
    return ApiError(message)


def graphql_error_detail(errors: object) -> str:
    if not isinstance(errors, list):
        return "The GraphQL errors field was not a list."

    messages: list[str] = []
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        message = error.get("message")
        if isinstance(message, str):
            messages.append(message)
    if not messages:
        return "The GraphQL response contained errors without text messages."
    return "GraphQL error messages: " + "; ".join(
        repr(message[:500]) for message in messages[:3]
    )


@dataclass(frozen=True)
class Attribution:
    id: str
    preferred_username: str


@dataclass(frozen=True)
class EventDefinition:
    id: str
    dtstart: datetime
    duration: timedelta
    timezone_name: str
    organizer_actor_id: str
    attributed_to: Attribution | None
    template: TemplateData
    rule: dateutil_rrule.rrule
    recurrence_count: int | None
    upcoming_count: int | None
    skip_starts: frozenset[datetime]
    is_finite: bool


@dataclass(frozen=True)
class Occurrence:
    definition: EventDefinition
    starts_at: datetime
    ends_at: datetime
    template: TemplateData

    @property
    def title(self) -> str:
        return cast(str, self.template["title"])

    @property
    def description(self) -> str:
        return cast(str, self.template["description"])


@dataclass(frozen=True)
class StaticTokenConfiguration:
    token: str


@dataclass(frozen=True)
class MachineUserConfiguration:
    email: str
    password: str


AuthenticationConfiguration: TypeAlias = (
    StaticTokenConfiguration | MachineUserConfiguration
)


def new_mobilizon_session() -> requests.Session:
    """Create the private transport used for every Mobilizon request in one run."""
    session = requests.Session()
    session.verify = True
    session.auth = None
    return session


def post_mobilizon(
    session: requests.Session,
    url: str,
    headers: Mapping[str, str],
    *,
    json_body: GraphQLRequest,
) -> requests.Response:
    """Send one request without accepting or retaining session cookies."""
    session.cookies.clear()
    try:
        return session.post(
            url,
            headers=dict(headers),
            json=json_body,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    finally:
        session.cookies.clear()


class MobilizonClient:
    """A minimal client for the Mobilizon GraphQL API."""

    endpoint: str
    headers: dict[str, str]
    session: requests.Session
    debug: bool

    def __init__(
        self,
        session: requests.Session,
        endpoint: str,
        token: str,
        debug: bool,
    ) -> None:
        self.session = session
        self.endpoint = endpoint
        self.debug = debug
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def graphql(self, query: str, variables: Mapping[str, JsonValue]) -> GraphQLData:
        request_body: GraphQLRequest = {
            "query": query,
            "variables": dict(variables),
        }
        try:
            response: requests.Response = post_mobilizon(
                self.session,
                self.endpoint,
                self.headers,
                json_body=request_body,
            )
        except requests.RequestException as error:
            raise debug_api_error(
                "Mobilizon GraphQL request failed.",
                self.debug,
                f"{type(error).__name__}: {error}",
            ) from error

        if response.status_code == 401:
            raise debug_api_error(
                "Mobilizon GraphQL authentication failed.",
                self.debug,
                "HTTP 401",
            )
        if response.is_redirect or not 200 <= response.status_code < 300:
            raise debug_api_error(
                "Mobilizon GraphQL request failed.",
                self.debug,
                f"HTTP {response.status_code}",
            )

        try:
            body: object = response.json()
        except ValueError as error:
            raise debug_api_error(
                "Mobilizon GraphQL request failed.",
                self.debug,
                f"Response JSON was invalid: {error}",
            ) from error
        if not isinstance(body, Mapping):
            raise debug_api_error(
                "Mobilizon GraphQL request failed.",
                self.debug,
                "Response JSON root was not an object.",
            )
        response_body: Mapping[str, object] = cast(Mapping[str, object], body)

        errors = response_body.get("errors")
        if errors:
            raise debug_api_error(
                "Mobilizon GraphQL request failed.",
                self.debug,
                graphql_error_detail(errors),
            )

        data: object | None = response_body.get("data")
        if not isinstance(data, Mapping):
            raise debug_api_error(
                "Mobilizon GraphQL request failed.",
                self.debug,
                "Response data was not an object.",
            )
        return dict(cast(Mapping[str, object], data))

    def list_organized_events(
        self, definition: EventDefinition, eligible: list[Occurrence]
    ) -> list[GraphQLData]:
        if definition.attributed_to:
            earliest: datetime = min(item.starts_at for item in eligible) - timedelta(
                minutes=1
            )
            latest: datetime = max(item.ends_at for item in eligible) + timedelta(
                minutes=1
            )
            return self._paginate_events(
                GROUP_ORGANIZED_EVENTS_QUERY,
                {
                    "preferredUsername": definition.attributed_to.preferred_username,
                    "afterDatetime": format_datetime(earliest),
                    "beforeDatetime": format_datetime(latest),
                },
                "group",
                definition.attributed_to.id,
            )

        return self._paginate_events(
            PERSON_ORGANIZED_EVENTS_QUERY,
            {"id": definition.organizer_actor_id},
            "person",
            definition.organizer_actor_id,
        )

    def _paginate_events(
        self,
        query: str,
        base_variables: Mapping[str, JsonValue],
        owner_key: str,
        expected_owner_id: str,
    ) -> list[GraphQLData]:
        page = 1
        events: list[GraphQLData] = []

        while True:
            data: dict[str, object] = self.graphql(
                query,
                {**base_variables, "page": page, "limit": PAGE_SIZE},
            )
            owner: object | None = data.get(owner_key)
            owner_id = owner.get("id")
            if not isinstance(owner, Mapping):
                raise ApiError(
                    f"Mobilizon did not return the requested {owner_key} owner."
                )
            if str(owner_id) != expected_owner_id:
                raise ApiError(
                    f"Mobilizon returned a {owner_key} owner ({owner_id}) that does not match "
                    f"the configured ID ({expected_owner_id})."
                )

            listing = owner.get("organizedEvents")
            elements = listing.get("elements") if isinstance(listing, Mapping) else None
            if not isinstance(elements, list) or not all(
                isinstance(item, Mapping) for item in elements
            ):
                raise ApiError("Mobilizon returned an invalid organized-events page.")

            page_events: list[Mapping[str, object]] = cast(
                list[Mapping[str, object]], elements
            )
            events.extend(dict(item) for item in page_events)
            if len(page_events) < PAGE_SIZE:
                return events
            page += 1

    def create_event(self, occurrence: Occurrence) -> GraphQLData:
        data: dict[str, object] = self.graphql(
            CREATE_EVENT_MUTATION, create_variables(occurrence)
        )
        created: object | None = data.get("createEvent")
        if not isinstance(created, Mapping):
            raise ApiError("Mobilizon did not return the created event.")
        return dict(cast(Mapping[str, object], created))


def ensure_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{path} must be a mapping.")
    if not all(isinstance(key, str) for key in value):
        raise ConfigurationError(f"{path} keys must be strings.")
    return cast(Mapping[str, object], value)


def require_allowed_keys(
    value: Mapping[str, object], allowed: set[str], path: str
) -> None:
    unknown: list[str] = sorted(set(value).difference(allowed))
    if unknown:
        raise ConfigurationError(
            f"{path} has unsupported field(s): {', '.join(unknown)}."
        )


def require_nonempty_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{path} must be a nonempty string.")
    return value


def require_boolean(value: object, path: str) -> None:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{path} must be a boolean.")


def require_positive_integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{path} must be a positive integer.")
    return value


def require_valid_local_time(
    value: datetime, event_timezone: ZoneInfo, path: str
) -> datetime:
    """Reject nonexistent and ambiguous wall-clock times in the configured timezone."""
    local_value: datetime = value.replace(tzinfo=None)
    candidate_instants: set[datetime] = set()
    for fold in (0, 1):
        candidate: datetime = local_value.replace(tzinfo=event_timezone, fold=fold)
        round_trip: datetime = candidate.astimezone(timezone.utc).astimezone(
            event_timezone
        )
        if round_trip.replace(tzinfo=None) == local_value:
            candidate_instants.add(candidate.astimezone(timezone.utc))

    if not candidate_instants:
        raise ConfigurationError(
            f"{path} is a nonexistent local time in {event_timezone.key}."
        )
    if len(candidate_instants) > 1:
        raise ConfigurationError(
            f"{path} is an ambiguous local time in {event_timezone.key}."
        )
    return local_value.replace(tzinfo=event_timezone)


def parse_local_dtstart(value: object, timezone_name: str, path: str) -> datetime:
    raw: str = require_nonempty_string(value, path)
    if "T" not in raw:
        raise ConfigurationError(
            f"{path} must be an ISO-8601 local datetime with a time."
        )
    try:
        parsed: datetime = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ConfigurationError(
            f"{path} must be an ISO-8601 local datetime."
        ) from error
    if parsed.tzinfo is not None or raw.endswith("Z"):
        raise ConfigurationError(f"{path} must not include a UTC offset or timezone.")
    try:
        event_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ConfigurationError(
            f"timezone must be a valid IANA timezone: {timezone_name}."
        ) from error
    return require_valid_local_time(parsed, event_timezone, path)


def require_integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{path} must be an integer.")
    return value


def parse_integer_values(
    value: object,
    path: str,
    minimum: int | None = None,
    maximum: int | None = None,
    allow_zero: bool = True,
) -> tuple[int, ...]:
    values = value if isinstance(value, list) else [value]
    if not values:
        raise ConfigurationError(f"{path} must not be an empty list.")

    parsed: list[int] = []
    for index, item in enumerate(values):
        item_path: str = f"{path}[{index}]" if isinstance(value, list) else path
        number: int = require_integer(item, item_path)
        if not allow_zero and number == 0:
            raise ConfigurationError(f"{item_path} must not be zero.")
        if minimum is not None and number < minimum:
            raise ConfigurationError(f"{item_path} must be at least {minimum}.")
        if maximum is not None and number > maximum:
            raise ConfigurationError(f"{item_path} must be at most {maximum}.")
        parsed.append(number)
    return tuple(parsed)


def parse_weekday(
    value: object, path: str, allow_ordinal: bool
) -> dateutil_rrule.weekday:
    raw: str = require_nonempty_string(value, path).upper()
    match: re.Match[str] | None = WEEKDAY_PATTERN.fullmatch(raw)
    if match is None:
        example: str = "MO or TU" if not allow_ordinal else "MO, TU, 3TU, or -1FR"
        raise ConfigurationError(f"{path} must be a weekday name such as {example}.")

    ordinal_text = match.group("ordinal")
    if ordinal_text is None:
        return WEEKDAY_VALUES[match.group("weekday")]
    if not allow_ordinal:
        raise ConfigurationError(f"{path} must be a weekday name without an ordinal.")

    ordinal = int(ordinal_text)
    if ordinal == 0 or not -53 <= ordinal <= 53:
        raise ConfigurationError(
            f"{path} weekday ordinal must be between -53 and 53, excluding 0."
        )
    return WEEKDAY_VALUES[match.group("weekday")](ordinal)


def parse_weekday_values(
    value: object, path: str
) -> tuple[dateutil_rrule.weekday, ...]:
    values = value if isinstance(value, list) else [value]
    if not values:
        raise ConfigurationError(f"{path} must not be an empty list.")
    return tuple(
        parse_weekday(
            item,
            f"{path}[{index}]" if isinstance(value, list) else path,
            allow_ordinal=True,
        )
        for index, item in enumerate(values)
    )


def parse_until(value: object, path: str) -> datetime:
    raw: str = require_nonempty_string(value, path)
    if "T" not in raw:
        raise ConfigurationError(
            f"{path} must be an ISO-8601 UTC datetime with a time."
        )
    normalized: str = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    try:
        parsed: datetime = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ConfigurationError(f"{path} must be an ISO-8601 UTC datetime.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConfigurationError(f"{path} must include a UTC offset or Z.")
    if parsed.utcoffset() != timedelta(0):
        raise ConfigurationError(f"{path} must use UTC (+00:00 or Z).")
    return parsed.astimezone(timezone.utc)


def parse_rrule(
    rule_value: object, dtstart: datetime, path: str
) -> tuple[dateutil_rrule.rrule, int | None, bool]:
    rule: Mapping[str, object] = ensure_mapping(rule_value, path)
    require_allowed_keys(rule, RRULE_KEYS, path)
    if "freq" not in rule:
        raise ConfigurationError(f"{path}.freq is required.")

    raw_frequency: str = require_nonempty_string(rule["freq"], f"{path}.freq").upper()
    frequency: int | None = FREQUENCY_VALUES.get(raw_frequency)
    if frequency is None:
        raise ConfigurationError(
            f"{path}.freq must be one of {', '.join(FREQUENCY_VALUES)}."
        )

    recurrence_count: int | None = None
    until: datetime | None = None
    arguments: RRuleArguments = {"freq": frequency, "dtstart": dtstart}
    if "interval" in rule:
        arguments["interval"] = require_positive_integer(
            rule["interval"], f"{path}.interval"
        )
    if "wkst" in rule:
        arguments["wkst"] = parse_weekday(
            rule["wkst"], f"{path}.wkst", allow_ordinal=False
        )
    if "count" in rule:
        recurrence_count = require_positive_integer(rule["count"], f"{path}.count")
        arguments["count"] = recurrence_count
    if "until" in rule:
        until = parse_until(rule["until"], f"{path}.until")
        arguments["until"] = until
    if recurrence_count is not None and until is not None:
        raise ConfigurationError(f"{path} must not contain both count and until.")

    integer_fields: dict[str, IntegerFieldBounds] = {
        "bysetpos": (-366, 366, False),
        "bymonth": (1, 12, True),
        "bymonthday": (-31, 31, False),
        "byyearday": (-366, 366, False),
        "byeaster": (None, None, True),
        "byweekno": (-53, 53, False),
        "byhour": (0, 23, True),
        "byminute": (0, 59, True),
        "bysecond": (0, 59, True),
    }
    for field, (minimum, maximum, allow_zero) in integer_fields.items():
        if field in rule:
            arguments[field] = parse_integer_values(
                rule[field], f"{path}.{field}", minimum, maximum, allow_zero
            )
    if "byweekday" in rule:
        weekdays: tuple[dateutil_rrule.weekday, ...] = parse_weekday_values(
            rule["byweekday"], f"{path}.byweekday"
        )
        if (
            any(weekday.n for weekday in weekdays)
            and frequency > dateutil_rrule.MONTHLY
        ):
            raise ConfigurationError(
                f"{path}.byweekday ordinal values require YEARLY or MONTHLY freq."
            )
        arguments["byweekday"] = weekdays

    try:
        parsed = dateutil_rrule.rrule(**arguments)
    except (TypeError, ValueError, OverflowError) as error:
        raise ConfigurationError(
            f"{path} is not a valid dateutil recurrence mapping: {error}"
        ) from error
    return parsed, recurrence_count, recurrence_count is not None or until is not None


def parse_skip_starts(
    value: object, timezone_name: str, path: str
) -> frozenset[datetime]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"{path} must be a nonempty list of local datetimes.")

    skip_starts = frozenset(
        parse_local_dtstart(item, timezone_name, f"{path}[{index}]")
        for index, item in enumerate(value)
    )
    if len(skip_starts) != len(value):
        raise ConfigurationError(f"{path} must not contain duplicate datetimes.")
    return skip_starts


def selected_recurrence_starts(
    definition: EventDefinition, run_timestamp: datetime
) -> tuple[datetime, ...]:
    event_timezone: tzinfo | None = definition.dtstart.tzinfo
    if not isinstance(event_timezone, ZoneInfo):
        raise ConfigurationError(f"event {definition.id} has an invalid timezone.")

    selected: list[datetime] = []
    try:
        for index, generated_start in enumerate(definition.rule, start=1):
            starts_at = require_valid_local_time(
                generated_start,
                event_timezone,
                f"event {definition.id} occurrence {index}",
            )
            if starts_at.astimezone(timezone.utc) <= run_timestamp:
                continue
            if starts_at in definition.skip_starts:
                continue
            selected.append(starts_at)
            if (
                definition.upcoming_count is not None
                and len(selected) >= definition.upcoming_count
            ):
                break
    except (TypeError, ValueError, OverflowError) as error:
        raise ConfigurationError(
            f"event {definition.id} could not generate recurrence dates."
        ) from error
    return tuple(selected)


def validate_attribution(value: object, path: str) -> Attribution | None:
    if value is None:
        return None
    data: Mapping[str, object] = ensure_mapping(value, path)
    require_allowed_keys(data, {"id", "preferred_username"}, path)
    if set(data) != {"id", "preferred_username"}:
        raise ConfigurationError(f"{path} requires id and preferred_username.")
    return Attribution(
        id=require_nonempty_string(data["id"], f"{path}.id"),
        preferred_username=require_nonempty_string(
            data["preferred_username"], f"{path}.preferred_username"
        ),
    )


def validate_address(value: object, path: str) -> None:
    if value is None:
        return
    address: Mapping[str, object] = ensure_mapping(value, path)
    require_allowed_keys(address, set(ADDRESS_FIELD_MAP), path)
    for key, item in address.items():
        if item is not None:
            require_nonempty_string(item, f"{path}.{key}")
    if "timezone" in address and address["timezone"] is not None:
        try:
            ZoneInfo(require_nonempty_string(address["timezone"], f"{path}.timezone"))
        except ZoneInfoNotFoundError as error:
            raise ConfigurationError(
                f"{path}.timezone must be a valid IANA timezone."
            ) from error


def validate_options(value: object, timezone_name: str, path: str) -> None:
    if value is None:
        return
    options: typing.Mapping[str, object] = ensure_mapping(value, path)
    require_allowed_keys(options, set(OPTIONS_FIELD_MAP), path)
    if options.get("timezone") not in (None, timezone_name):
        raise ConfigurationError(
            f"{path}.timezone conflicts with the event timezone {timezone_name}."
        )
    for key, item in options.items():
        if item is None:
            continue
        item_path: str = f"{path}.{key}"
        if key in BOOLEAN_OPTION_FIELDS:
            require_boolean(item, item_path)
        elif key in INTEGER_OPTION_FIELDS:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ConfigurationError(f"{item_path} must be an integer.")
        elif key in TEXT_OPTION_FIELDS:
            require_nonempty_string(item, item_path)
        elif key == "attendees":
            validate_string_list(item, item_path)
        elif key == "offers":
            validate_offer_list(item, item_path)
        elif key == "participation_conditions":
            validate_participation_condition_list(item, item_path)


def validate_string_list(value: object, path: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"{path} must be a list of strings.")


def validate_offer_list(value: object, path: str) -> None:
    if not isinstance(value, list):
        raise ConfigurationError(f"{path} must be a list.")
    for index, item in enumerate(value):
        offer: Mapping[str, object] = ensure_mapping(item, f"{path}[{index}]")
        require_allowed_keys(
            offer, {"price", "price_currency", "url"}, f"{path}[{index}]"
        )
        if "price" in offer and (
            isinstance(offer["price"], bool)
            or not isinstance(offer["price"], (int, float))
        ):
            raise ConfigurationError(f"{path}[{index}].price must be a number.")
        for key in ("price_currency", "url"):
            if key in offer and offer[key] is not None:
                require_nonempty_string(offer[key], f"{path}[{index}].{key}")


def validate_participation_condition_list(value: object, path: str) -> None:
    if not isinstance(value, list):
        raise ConfigurationError(f"{path} must be a list.")
    for index, item in enumerate(value):
        condition: Mapping[str, object] = ensure_mapping(item, f"{path}[{index}]")
        require_allowed_keys(condition, {"title", "content", "url"}, f"{path}[{index}]")
        for key, field_value in condition.items():
            if field_value is not None:
                require_nonempty_string(field_value, f"{path}[{index}].{key}")


def validate_metadata(value: object, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ConfigurationError(f"{path} must be a list.")
    for index, item in enumerate(value):
        metadata: Mapping[str, object] = ensure_mapping(item, f"{path}[{index}]")
        require_allowed_keys(
            metadata, {"key", "title", "value", "type"}, f"{path}[{index}]"
        )
        if "key" not in metadata or "value" not in metadata:
            raise ConfigurationError(f"{path}[{index}] requires key and value.")
        require_nonempty_string(metadata["key"], f"{path}[{index}].key")
        require_nonempty_string(metadata["value"], f"{path}[{index}].value")
        if "title" in metadata and metadata["title"] is not None:
            require_nonempty_string(metadata["title"], f"{path}[{index}].title")
        if "type" in metadata and metadata["type"] not in {
            "STRING",
            "INTEGER",
            "BOOLEAN",
        }:
            raise ConfigurationError(
                f"{path}[{index}].type must be STRING, INTEGER, or BOOLEAN."
            )


def validate_contacts(value: object, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ConfigurationError(f"{path} must be a list.")
    for index, item in enumerate(value):
        contact: Mapping[str, object] = ensure_mapping(item, f"{path}[{index}]")
        require_allowed_keys(contact, {"id"}, f"{path}[{index}]")
        if set(contact) != {"id"}:
            raise ConfigurationError(f"{path}[{index}] requires id.")
        require_nonempty_string(contact["id"], f"{path}[{index}].id")


def validate_template(value: object, timezone_name: str, path: str) -> TemplateData:
    template: dict[str, object] = dict(ensure_mapping(value, path))
    require_allowed_keys(template, TEMPLATE_KEYS, path)
    for required in ("title", "description"):
        if required not in template:
            raise ConfigurationError(f"{path} requires {required}.")
        require_nonempty_string(template[required], f"{path}.{required}")

    enum_fields: dict[str, set[str]] = {
        "visibility": {"PUBLIC", "UNLISTED", "RESTRICTED", "PRIVATE"},
        "join_options": {"FREE", "RESTRICTED", "INVITE", "EXTERNAL"},
        "status": {"TENTATIVE", "CONFIRMED", "CANCELLED"},
    }
    for key, choices in enum_fields.items():
        if (
            key in template
            and template[key] is not None
            and template[key] not in choices
        ):
            raise ConfigurationError(
                f"{path}.{key} must be one of {', '.join(sorted(choices))}."
            )
    if "draft" in template and template["draft"] is not None:
        require_boolean(template["draft"], f"{path}.draft")
    for key in (
        "category",
        "online_address",
        "phone_address",
        "external_participation_url",
        "language",
    ):
        if key in template and template[key] is not None:
            require_nonempty_string(template[key], f"{path}.{key}")
    if "tags" in template and template["tags"] is not None:
        validate_string_list(template["tags"], f"{path}.tags")
    if "picture_media_uuid" in template and template["picture_media_uuid"] is not None:
        picture_uuid = require_nonempty_string(
            template["picture_media_uuid"], f"{path}.picture_media_uuid"
        )
        try:
            uuid.UUID(picture_uuid)
        except ValueError as error:
            raise ConfigurationError(
                f"{path}.picture_media_uuid must be a UUID."
            ) from error
    validate_address(template.get("physical_address"), f"{path}.physical_address")
    validate_options(template.get("options"), timezone_name, f"{path}.options")
    validate_metadata(template.get("metadata"), f"{path}.metadata")
    validate_contacts(template.get("contacts"), f"{path}.contacts")
    return cast(TemplateData, template)


def load_config(config_path: Path) -> list[EventDefinition]:
    try:
        raw: object = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigurationError(
            f"Cannot read configuration file {config_path}: {error}"
        ) from error
    except yaml.YAMLError as error:
        raise ConfigurationError(
            f"Cannot parse YAML configuration {config_path}: {error}"
        ) from error

    config: Mapping[str, object] = ensure_mapping(raw, "configuration")
    require_allowed_keys(config, {"version", "events"}, "configuration")
    if config.get("version") != 1:
        raise ConfigurationError("configuration.version must be 1.")
    events = config.get("events")
    if not isinstance(events, list):
        raise ConfigurationError("configuration.events must be a list.")

    definitions: list[EventDefinition] = []
    seen_ids: set[str] = set()
    for index, raw_event in enumerate(events):
        path = f"events[{index}]"
        event: Mapping[str, object] = ensure_mapping(raw_event, path)
        require_allowed_keys(event, EVENT_KEYS, path)
        required: set[str] = {
            "id",
            "dtstart",
            "timezone",
            "duration_minutes",
            "rrule",
            "organizer_actor_id",
            "template",
        }
        missing: list[str] = sorted(required.difference(event))
        if missing:
            raise ConfigurationError(
                f"{path} is missing required field(s): {', '.join(missing)}."
            )
        event_id: str = require_nonempty_string(event["id"], f"{path}.id")
        if event_id in seen_ids:
            raise ConfigurationError(f"events contains duplicate id: {event_id}.")
        seen_ids.add(event_id)
        timezone_name: str = require_nonempty_string(
            event["timezone"], f"{path}.timezone"
        )
        dtstart: datetime = parse_local_dtstart(
            event["dtstart"], timezone_name, f"{path}.dtstart"
        )
        duration = timedelta(
            minutes=require_positive_integer(
                event["duration_minutes"], f"{path}.duration_minutes"
            )
        )
        rule, recurrence_count, is_finite = parse_rrule(
            event["rrule"], dtstart, f"{path}.rrule"
        )
        upcoming_count: int | None = (
            require_positive_integer(event["upcoming_count"], f"{path}.upcoming_count")
            if "upcoming_count" in event
            else None
        )
        skip_starts: frozenset[datetime] = (
            parse_skip_starts(
                event["skip_starts"], timezone_name, f"{path}.skip_starts"
            )
            if "skip_starts" in event
            else frozenset()
        )
        if not is_finite and upcoming_count is None:
            raise ConfigurationError(
                f"{path}.upcoming_count is required when {path}.rrule has no count or until."
            )
        attribution: Attribution | None = validate_attribution(
            event.get("attributed_to"), f"{path}.attributed_to"
        )
        definitions.append(
            EventDefinition(
                id=event_id,
                dtstart=dtstart,
                duration=duration,
                timezone_name=timezone_name,
                organizer_actor_id=require_nonempty_string(
                    event["organizer_actor_id"], f"{path}.organizer_actor_id"
                ),
                attributed_to=attribution,
                template=validate_template(
                    event["template"], timezone_name, f"{path}.template"
                ),
                rule=rule,
                recurrence_count=recurrence_count,
                upcoming_count=upcoming_count,
                skip_starts=skip_starts,
                is_finite=is_finite,
            )
        )
    return definitions


def format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def render_string(value: str, tokens: Mapping[str, str], path: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token not in tokens:
            raise ConfigurationError(f"{path} uses unknown template token {token!r}.")
        return tokens[token]

    rendered: str = TOKEN_PATTERN.sub(replace, value)
    if "{{" in rendered or "}}" in rendered:
        raise ConfigurationError(f"{path} contains malformed template token syntax.")
    return rendered


def render_value(value: JsonValue, tokens: Mapping[str, str], path: str) -> JsonValue:
    if isinstance(value, str):
        return render_string(value, tokens, path)
    if isinstance(value, list):
        return [
            render_value(item, tokens, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        mapping_value: Mapping[str, JsonValue] = cast(Mapping[str, JsonValue], value)
        return {
            key: render_value(item, tokens, f"{path}.{key}")
            for key, item in mapping_value.items()
        }
    return value


def generate_occurrences(
    definitions: Iterable[EventDefinition], run_timestamp: datetime
) -> list[Occurrence]:
    generated: list[Occurrence] = []
    for definition in definitions:
        for starts_at in selected_recurrence_starts(definition, run_timestamp):
            ends_at: datetime = (
                starts_at.astimezone(timezone.utc) + definition.duration
            ).astimezone(starts_at.tzinfo)
            if ends_at.astimezone(timezone.utc) <= starts_at.astimezone(timezone.utc):
                raise ConfigurationError(
                    f"event {definition.id} has a non-positive end time."
                )
            tokens: dict[str, str] = {
                "event_id": definition.id,
                "occurrence_date": starts_at.date().isoformat(),
                "occurrence_start_local": starts_at.isoformat(timespec="seconds"),
                "occurrence_start_utc": format_datetime(starts_at),
                "occurrence_end_local": ends_at.isoformat(timespec="seconds"),
            }
            template = cast(
                TemplateData,
                render_value(
                    definition.template, tokens, f"event {definition.id}.template"
                ),
            )
            title = template["title"]
            description = template["description"]
            if not isinstance(title, str) or not title.strip() or len(title) < 3:
                raise ConfigurationError(
                    f"event {definition.id} rendered title must contain at least three characters."
                )
            if not isinstance(description, str) or not description.strip():
                raise ConfigurationError(
                    f"event {definition.id} rendered description must be nonempty."
                )
            generated.append(Occurrence(definition, starts_at, ends_at, template))
    return generated


def ensure_unique_eligible_occurrence_identities(
    occurrences: Iterable[Occurrence],
) -> None:
    """Reject configurations whose selected occurrences match the same event."""
    by_identity: dict[tuple[str, str | None, str | None], list[Occurrence]] = {}
    for occurrence in occurrences:
        attribution_id: str | None = (
            occurrence.definition.attributed_to.id
            if occurrence.definition.attributed_to is not None
            else None
        )
        organizer_id: str | None = (
            occurrence.definition.organizer_actor_id if attribution_id is None else None
        )
        identity: tuple[str, str | None, str | None] = (
            occurrence.title,
            organizer_id,
            attribution_id,
        )
        by_identity.setdefault(identity, []).append(occurrence)

    for matching_occurrences in by_identity.values():
        matching_occurrences.sort(
            key=lambda item: item.starts_at.astimezone(timezone.utc)
        )
        for first, second in zip(matching_occurrences, matching_occurrences[1:]):
            start_difference: timedelta = abs(
                first.starts_at.astimezone(timezone.utc)
                - second.starts_at.astimezone(timezone.utc)
            )
            if start_difference <= MATCH_TOLERANCE:
                raise ConfigurationError(
                    f"events {first.definition.id} and {second.definition.id} generate "
                    "the same matching identity within 60 seconds; make their titles, "
                    "organizers, group attributions, or start times distinct."
                )


def validate_mobilizon_endpoint(value: str) -> str:
    error_message = "MOBILIZON_GRAPHQL_URL must be an absolute instance /api URL."
    if not value or value != value.strip() or "\r" in value or "\n" in value:
        raise ConfigurationError(error_message)
    try:
        parsed: SplitResult = urlsplit(value)
        hostname: str | None = parsed.hostname
        parsed.port
    except ValueError:
        raise ConfigurationError(error_message) from None
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or hostname is None:
        raise ConfigurationError(error_message)
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(error_message)
    if parsed.query or parsed.fragment or parsed.path not in {"/api", "/api/"}:
        raise ConfigurationError(error_message)
    if parsed.scheme == "http" and hostname.lower() not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ConfigurationError(error_message)

    return urlunsplit((parsed.scheme, parsed.netloc, "/api", "", ""))


def require_environment_credential(environment: Mapping[str, str], name: str) -> str:
    value = environment[name]
    if value == "" or "\r" in value or "\n" in value:
        raise ConfigurationError(f"{name} must be nonempty and contain no line breaks.")
    return value


def read_mobilizon_endpoint(
    environment: Mapping[str, str] | None = None,
) -> str:
    values = os.environ if environment is None else environment
    raw_url: str | None = values.get("MOBILIZON_GRAPHQL_URL")
    if raw_url is None:
        raise ConfigurationError(
            "MOBILIZON_GRAPHQL_URL must be set for normal and --dry-run runs."
        )
    return validate_mobilizon_endpoint(raw_url)


def read_authentication_configuration(
    environment: Mapping[str, str] | None = None,
) -> AuthenticationConfiguration:
    values = os.environ if environment is None else environment
    mode_variables = (
        ("static", STATIC_AUTH_VARIABLES),
        ("machine", MACHINE_USER_AUTH_VARIABLES),
    )
    mentioned_modes: list[str] = [
        name
        for name, variable_names in mode_variables
        if any(variable_name in values for variable_name in variable_names)
    ]
    if len(mentioned_modes) != 1:
        raise ConfigurationError(
            "Configure exactly one authentication mode using MOBILIZON_TOKEN or "
            "MOBILIZON_MACHINE_USER_EMAIL with MOBILIZON_MACHINE_USER_PASSWORD."
        )

    if mentioned_modes[0] == "static":
        return StaticTokenConfiguration(
            require_environment_credential(values, "MOBILIZON_TOKEN")
        )

    missing: list[str] = [
        name for name in MACHINE_USER_AUTH_VARIABLES if name not in values
    ]
    if missing:
        raise ConfigurationError(
            f"{', '.join(missing)} must be set for machine-user authentication."
        )
    return MachineUserConfiguration(
        email=require_environment_credential(values, "MOBILIZON_MACHINE_USER_EMAIL"),
        password=require_environment_credential(
            values, "MOBILIZON_MACHINE_USER_PASSWORD"
        ),
    )


def login_machine_user(
    session: requests.Session,
    endpoint: str,
    configuration: MachineUserConfiguration,
    debug: bool,
) -> str:
    request_body: GraphQLRequest = {
        "query": MACHINE_USER_LOGIN_MUTATION,
        "variables": {
            "email": configuration.email,
            "password": configuration.password,
        },
    }
    try:
        response = post_mobilizon(
            session,
            endpoint,
            {"Accept": "application/json", "Content-Type": "application/json"},
            json_body=request_body,
        )
    except requests.RequestException as error:
        raise debug_api_error(
            MACHINE_USER_LOGIN_ERROR,
            debug,
            f"{type(error).__name__}: {error}",
        ) from error

    if response.is_redirect or response.status_code != 200:
        raise debug_api_error(
            MACHINE_USER_LOGIN_ERROR,
            debug,
            f"HTTP {response.status_code}",
        )
    try:
        body: object = response.json()
    except ValueError as error:
        raise debug_api_error(
            MACHINE_USER_LOGIN_ERROR,
            debug,
            f"Response JSON was invalid: {error}",
        ) from error
    if not isinstance(body, Mapping):
        raise debug_api_error(
            MACHINE_USER_LOGIN_ERROR,
            debug,
            "Response JSON root was not an object.",
        )
    response_body = cast(Mapping[str, object], body)
    errors = response_body.get("errors")
    if errors:
        raise debug_api_error(
            MACHINE_USER_LOGIN_ERROR,
            debug,
            graphql_error_detail(errors),
        )
    data = response_body.get("data")
    if not isinstance(data, Mapping):
        raise debug_api_error(
            MACHINE_USER_LOGIN_ERROR,
            debug,
            "Response data was not an object.",
        )
    login = cast(Mapping[str, object], data).get("login")
    if not isinstance(login, Mapping):
        raise debug_api_error(
            MACHINE_USER_LOGIN_ERROR,
            debug,
            "Response data did not contain a login object.",
        )
    access_token = cast(Mapping[str, object], login).get("accessToken")
    if (
        not isinstance(access_token, str)
        or access_token == ""
        or "\r" in access_token
        or "\n" in access_token
    ):
        raise debug_api_error(
            MACHINE_USER_LOGIN_ERROR,
            debug,
            "Response login.accessToken was missing or invalid.",
        )
    return access_token


def graph_ql_address(address: Mapping[str, JsonValue]) -> GraphQLVariables:
    return {ADDRESS_FIELD_MAP[key]: value for key, value in address.items()}


def graph_ql_options(
    options: Mapping[str, JsonValue], timezone_name: str
) -> GraphQLVariables:
    graphql_options: GraphQLVariables = {}
    for key, value in options.items():
        if key == "offers" and value is not None:
            offers = cast(list[Mapping[str, JsonValue]], value)
            graphql_options[OPTIONS_FIELD_MAP[key]] = [
                {
                    "price": offer.get("price"),
                    "priceCurrency": offer.get("price_currency"),
                    "url": offer.get("url"),
                }
                for offer in offers
            ]
        else:
            graphql_options[OPTIONS_FIELD_MAP[key]] = value
    graphql_options["timezone"] = timezone_name
    return graphql_options


def create_variables(occurrence: Occurrence) -> GraphQLVariables:
    definition = occurrence.definition
    template = occurrence.template
    options = cast(Mapping[str, JsonValue], template.get("options") or {})
    variables: GraphQLVariables = {
        "title": occurrence.title,
        "description": occurrence.description,
        "beginsOn": format_datetime(occurrence.starts_at),
        "endsOn": format_datetime(occurrence.ends_at),
        "organizerActorId": definition.organizer_actor_id,
        "options": graph_ql_options(options, definition.timezone_name),
    }
    if definition.attributed_to is not None:
        variables["attributedToId"] = definition.attributed_to.id

    direct_fields: dict[str, str] = {
        "status": "status",
        "visibility": "visibility",
        "join_options": "joinOptions",
        "external_participation_url": "externalParticipationUrl",
        "tags": "tags",
        "online_address": "onlineAddress",
        "phone_address": "phoneAddress",
        "category": "category",
        "metadata": "metadata",
        "draft": "draft",
        "contacts": "contacts",
        "language": "language",
    }
    for template_key, graphql_key in direct_fields.items():
        if template_key in template:
            variables[graphql_key] = template[template_key]
    if "picture_media_uuid" in template:
        picture_uuid = template["picture_media_uuid"]
        variables["picture"] = (
            None if picture_uuid is None else {"mediaUuid": picture_uuid}
        )
    if "physical_address" in template:
        address = template["physical_address"]
        variables["physicalAddress"] = (
            None
            if address is None
            else graph_ql_address(cast(Mapping[str, JsonValue], address))
        )
    return variables


def parse_api_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ApiError("Mobilizon returned an event with an invalid beginsOn value.")
    raw = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ApiError(
            "Mobilizon returned an event with an invalid beginsOn value."
        ) from error
    if parsed.tzinfo is None:
        raise ApiError(
            "Mobilizon returned an event with a timezone-naive beginsOn value."
        )
    return parsed.astimezone(timezone.utc)


def actor_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return None
    actor = cast(Mapping[str, object], value)
    identifier = actor.get("id")
    if identifier is None:
        return None
    return str(identifier)


def matching_events(
    occurrence: Occurrence, existing_events: Iterable[Mapping[str, object]]
) -> list[Mapping[str, object]]:
    expected_attribution = (
        occurrence.definition.attributed_to.id
        if occurrence.definition.attributed_to
        else None
    )
    matches: list[Mapping[str, object]] = []
    for event in existing_events:
        if event.get("title") != occurrence.title:
            continue
        if expected_attribution is None and (
            actor_id(event.get("organizerActor"))
            != occurrence.definition.organizer_actor_id
        ):
            continue
        if actor_id(event.get("attributedTo")) != expected_attribution:
            continue
        actual_start = parse_api_datetime(event.get("beginsOn"))
        expected_start = occurrence.starts_at.astimezone(timezone.utc)
        if abs(actual_start - expected_start) <= MATCH_TOLERANCE:
            matches.append(event)
    return matches


def print_offline(occurrences: Iterable[Occurrence]) -> None:
    for occurrence in occurrences:
        print(
            f"{occurrence.definition.id}: eligible; "
            f"start_local={occurrence.starts_at.isoformat(timespec='seconds')}; "
            f"end_local={occurrence.ends_at.isoformat(timespec='seconds')}; "
            f"start_utc={format_datetime(occurrence.starts_at)}"
        )


def run_authenticated(
    args: CommandLineArguments,
    definitions: Sequence[EventDefinition],
    occurrences: Sequence[Occurrence],
    session: requests.Session,
    endpoint: str,
    access_token: str,
    debug: bool,
) -> int:
    client = MobilizonClient(session, endpoint, access_token, debug)
    selected_by_definition: dict[str, list[Occurrence]] = {}
    for occurrence in occurrences:
        selected_by_definition.setdefault(occurrence.definition.id, []).append(
            occurrence
        )

    actions: list[tuple[Occurrence, bool]] = []
    for definition in definitions:
        selected = selected_by_definition.get(definition.id, [])
        if not selected:
            print(f"{definition.id}: all recurrence occurrences are in the past")
            continue
        existing = client.list_organized_events(definition, selected)
        for occurrence in selected:
            matches = matching_events(occurrence, existing)
            if len(matches) > 1:
                raise ApiError(
                    f"{definition.id}: duplicate matching events for "
                    f"{occurrence.starts_at.isoformat(timespec='seconds')}"
                )
            actions.append((occurrence, bool(matches)))

    for occurrence, exists in actions:
        starts_at = occurrence.starts_at.isoformat(timespec="seconds")
        if exists:
            print(f"{occurrence.definition.id}: already exists {starts_at}")
        elif args.dry_run:
            print(f"{occurrence.definition.id}: would create {starts_at}")
        else:
            created = client.create_event(occurrence)
            identifier = created.get("uuid") or created.get("id") or "unknown id"
            print(f"{occurrence.definition.id}: created {starts_at} ({identifier})")
    return 0


def run(args: CommandLineArguments) -> int:
    definitions: list[EventDefinition] = load_config(Path(args.config))
    run_timestamp: datetime = datetime.now(timezone.utc)
    occurrences: list[Occurrence] = generate_occurrences(definitions, run_timestamp)
    ensure_unique_eligible_occurrence_identities(occurrences)

    if args.offline:
        print_offline(occurrences)
        return 0

    endpoint: str = read_mobilizon_endpoint()
    authentication: AuthenticationConfiguration = read_authentication_configuration()
    with new_mobilizon_session() as session:
        if isinstance(authentication, StaticTokenConfiguration):
            return run_authenticated(
                args,
                definitions,
                occurrences,
                session,
                endpoint,
                authentication.token,
                args.debug,
            )

        access_token: str = login_machine_user(
            session,
            endpoint,
            authentication,
            args.debug,
        )
        return run_authenticated(
            args,
            definitions,
            occurrences,
            session,
            endpoint,
            access_token,
            args.debug,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create missing Mobilizon events from a YAML recurrence configuration."
    )
    parser.add_argument(
        "--config", default="events.yaml", help="YAML configuration path."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print Mobilizon response diagnostics and exception traces to stderr.",
    )
    modes: argparse._MutuallyExclusiveGroup = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform read-only idempotency checks without creating events.",
    )
    modes.add_argument(
        "--offline",
        action="store_true",
        help="Validate and list recurrent events without network access.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args: CommandLineArguments = cast(
        CommandLineArguments, build_parser().parse_args(argv)
    )
    try:
        return run(args)
    except ConfigurationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except ApiError as error:
        print(f"error: {error}", file=sys.stderr)
        if args.debug:
            traceback.print_exception(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
