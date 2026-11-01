# Mobilizon recurring events

A script for creating recurring events in Mobilizon from the definitions in `events.yaml`.

## Adding or changing an event

Make a pull request with your edits to `events.yaml` according to the syntax below.  A good place to start is by copying an existing event block and changing the placeholders to match your event.

## YAML configuration

See `events.example.yaml` as a starting template. Each event requires:

| Field | Meaning |
| --- | --- |
| `id` | Unique local identifier, also available as a template token. |
| `dtstart` | Quoted local ISO-8601 date and time without an offset, for example `2030-01-15T18:30:00`. |
| `timezone` | [IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones), for example `Europe/Paris`. Recurrences retain this local wall-clock time through daylight-saving changes. |
| `duration_minutes` | Positive integer used to calculate the end time. |
| `rrule` | Mapping of [dateutil `rrule`](https://dateutil.readthedocs.io/en/stable/rrule.html) scheduling inputs. `freq` is required. |
| `upcoming_count` | Optional positive integer publication buffer. Required when `rrule` has neither `count` nor `until`. It is not an `rrule` parameter. |
| `skip_starts` | Optional nonempty list of original local occurrence datetimes to omit. Each value uses the event timezone and has no offset, for example `"2030-12-17T18:30:00"`. |
| `organizer_actor_id` | Personal organizer actor ID. The script never guesses an identity. |
| `attributed_to` | Optional group scope with required `id` and `preferred_username`. |
| `template` | Values used for each created event. `title` and `description` are required. |

## Recurrence rules

`rrule` is a typed YAML mapping following the parameters from [dateutil.rrule.rrule](https://dateutil.readthedocs.io/en/stable/rrule.html).

```yaml
rrule:
  freq: MONTHLY
  interval: 1
  byweekday: [TU]
  bysetpos: 3
upcoming_count: 3
```

The above example is the third Tuesday of each month. `byweekday: [TU]` produces the month's Tuesday candidates. `bysetpos: 3` selects the third Tuesday.  Note that it omits `rrule.count` and uses `upcoming_count: 3`, so each run maintains the earliest three future recurrence dates. Existing matching events in those three dates count toward the buffer; the script creates only missing ones. It never deletes events, including additional future events beyond the buffer.

`freq` must be one of `YEARLY`, `MONTHLY`, `WEEKLY`, `DAILY`, `HOURLY`, `MINUTELY`, or `SECONDLY`. The other supported `rrule` parameters are:

| Parameter | Value |
| --- | --- |
| `interval`, `count` | Positive integer. `count` caps the total recurrence set, including past dates. |
| `until` | ISO-8601 UTC datetime ending in `Z` or `+00:00`, for example `"2030-12-31T23:59:59Z"`. |
| `wkst` | Weekday: `MO`, `TU`, `WE`, `TH`, `FR`, `SA`, or `SU`. |
| `bysetpos` | Nonzero integer from `-366` through `366`, or a list of those integers. |
| `bymonth` | Integer from `1` through `12`, or a list. |
| `bymonthday` | Nonzero integer from `-31` through `31`, or a list. |
| `byyearday` | Nonzero integer from `-366` to `366`, or a list. |
| `byeaster` | Integer day offset from Easter 🐰, or list of offsets. |
| `byweekno` | Nonzero integer from `-53` to `53`, or a list. |
| `byweekday` | Weekday name or list of names. An ordinal may prefix a name, such as `3TU` or `-1FR`; ordinal weekdays require `YEARLY` or `MONTHLY` frequency. |
| `byhour` | Integer from `0` through `23`, or a list. |
| `byminute`, `bysecond` | Integer from `0` through `59`, or a list. |

**Note:** Do not put both `count` and `until` in the same rule. A rule with `count` or `until` is finite. For a finite rule without `upcoming_count`, the script selects remaining future occurrences and creates any that are missing. When `upcoming_count` is present on a finite rule, it retains that rolling number of earliest future occurrences.

## Publishing Policy 

A run captures the current time and excludes occurrences starting at or before it. `skip_starts` entries are also excluded before `upcoming_count` is applied, so skipped occurrences do not reduce the future-event list.

To manually reschedule one occurrence, add its original scheduled local datetime to `skip_starts`, commit the configuration, and wait for it to reach the workflow before changing the event in Mobilizon. The script will not recreate that original occurrence. The moved event remains a manual one-off and is not updated by this script.

Template strings include `{{event_id}}`, `{{occurrence_date}}`, `{{occurrence_start_local}}`, `{{occurrence_start_utc}}`, and `{{occurrence_end_local}}`.

Optional template keys are `visibility` (`PUBLIC`, `UNLISTED`, `RESTRICTED`, or `PRIVATE`), `join_options` (`FREE`, `RESTRICTED`, `INVITE`, or `EXTERNAL`), `status` (`TENTATIVE`, `CONFIRMED`, or `CANCELLED`), `draft`, `category`, `tags`, `online_address`, `phone_address`, `external_participation_url`, `picture_media_uuid`, `language`, `physical_address`, `options`, `metadata`, and `contacts`. `picture_media_uuid` must refer to media uploaded before this script runs.

`physical_address` accepts `geom`, `street`, `locality`, `postal_code`, `region`, `country`, `description`, `type`, `url`, `id`, `origin_id`, and `timezone`. `geom` is a [WGS-84](https://en.wikipedia.org/wiki/World_Geodetic_System) `longitude;latitude` value. `options` accepts `maximum_attendee_capacity`, `remaining_attendee_capacity`, `show_remaining_attendee_capacity`, `anonymous_participation`, `offers`, `participation_conditions`, `attendees`, `program`, `comment_moderation`, `show_participation_price`, `hide_number_of_participants`, `show_start_time`, `show_end_time`, `hide_organizer_when_group_event`, and `is_online`. The script always sets `options.timezone` to the event timezone.

## Run modes

```
usage: recurring_events.py [-h] [--config CONFIG] [--debug] [--dry-run | --offline]

Create missing Mobilizon events from a YAML recurrence configuration.

options:
  -h, --help       show this help message and exit
  --config CONFIG  YAML configuration path.
  --debug          Print Mobilizon response diagnostics and exception traces to stderr.
  --dry-run        Perform read-only idempotency checks without creating events.
  --offline        Validate and list recurrent events without network access.
```

For each configured person, the script reads every paginated `organizedEvents` result. For a group, it queries the configured group's paginated events over the eligible date range.

A personal event matches when its rendered title, `beginsOn` time within 60 seconds, organizer, and lack of group attribution match. A group event matches when its rendered title, time, and group attribution match, regardless of the creating actor. Multiple matching results fail the run. Matching events are never updated. This read-before-create check makes repeat runs idempotent.

`--dry-run` performs those authenticated read-only lookups and prints `would create` or `already exists`; it never invokes `createEvent`. `--offline` does no GraphQL calls, validates the YAML, and prints the eligible future occurrences selected by the publication policy.

## Local development

Since the script normally runs in CI, these steps are only needed for making changes to the python script.  Here's the steps to install the necessary dependencies:

1. This project uses [uv](https://docs.astral.sh/uv/) and targets Python 3.14. For local development, install uv and create the managed Python 3.14 environment by running:

   ```sh
   uv sync --locked
   ```

2. Copy `events.example.yaml` to `events.yaml` and replace its fictional actor and group IDs. The checked-in `events.yaml` has no events, so the scheduled workflow remains inactive until configured.

3. Validate the configuration and list the eligible occurrences selected by its publication policy without network access:

   ```sh
   uv run --locked python recurring_events.py --config events.yaml --offline
   ```

4. Configure exactly one authentication mode from [Mobilizon authentication](#mobilizon-authentication), then perform authenticated read-only matching before enabling creation. Add `--debug` to print response diagnostics and exception traces to standard error when troubleshooting. Debug output never includes request headers, request bodies, credentials, or full response payloads.

   ```sh
   uv run --locked --python 3.14 python recurring_events.py --config events.yaml --dry-run --debug
   ```

5. Run normal scheduler mode to create each eligible, unmatched occurrence:

   ```sh
   uv run --locked --python 3.14 python recurring_events.py --config events.yaml
   ```


## Development resources
- [Mobilizon GraphQL API documentation](https://docs.mobilizon.org/5.%20Interoperability/3.graphql_api/)
- [Upstream schema](https://framagit.org/kaihuri/mobilizon/-/blob/main/lib/graphql/schema/)
- [dateutil RRULE documentation](https://dateutil.readthedocs.io/en/stable/rrule.html)
