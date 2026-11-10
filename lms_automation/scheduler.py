"""Background automation for Last Man Standing.

Design note — this deliberately does not reimplement any game logic. Every
rule (auto-picks, result processing, eliminations, rollover, season resume)
already lives in app.py as admin endpoints that have run five cycles. This
module drives those endpoints on a timer, in-process, as a virtual admin.
That keeps one implementation of the rules rather than two that can drift.

A single orchestrator tick inspects the current round and decides what is due,
rather than several independent timers that could fire over each other.

Run modes:
    python -m lms_automation.scheduler --status   read-only; what would happen now
    python -m lms_automation.scheduler --once     one orchestrator pass
    python -m lms_automation.scheduler            run continuously
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app import app, PICK_DEADLINE_LEAD  # noqa: E402
from models import Fixture, Pick, PickToken, Player, ReminderSchedule, Round, db  # noqa: E402

logger = logging.getLogger("lms.scheduler")

# The WhatsApp sender runs as a separate local process on the Mac mini and
# owns pacing and the WhatsApp session. Loopback only — see its README.
SENDER_URL = os.environ.get("SENDER_URL", "http://127.0.0.1:8787")
SENDER_TOKEN = os.environ.get("SENDER_TOKEN", "")

# How long after the last kickoff we keep polling for results before giving up
# on a round and leaving it for an admin.
RESULT_POLL_HORIZON = timedelta(hours=6)


@dataclass
class Plan:
    """What the orchestrator intends to do on this tick."""

    round_id: int | None = None
    round_label: str = "—"
    phase: str = "idle"
    actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _admin_client():
    """A Flask test client already authenticated as admin.

    The scheduler runs in the same process as the app, so it drives the admin
    endpoints directly instead of over HTTP. No password or network involved.
    """
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["admin_logged_in"] = True
    return client


def _active_round() -> Round | None:
    return (
        Round.query.filter(Round.status == "active")
        .order_by(Round.cycle_number.desc(), Round.round_number.desc())
        .first()
    )


def _pick_counts(round_obj: Round) -> tuple[int, int]:
    """Return (picks submitted, active players) for a round."""
    active = Player.query.filter_by(status="active").count()
    submitted = Pick.query.filter_by(round_id=round_obj.id).count()
    return submitted, active


def _unpicked_active(round_obj: Round) -> int:
    """Active players with no pick in this round.

    Not active minus submitted: once results start landing, eliminated players
    leave the active count but their picks remain, so the subtraction goes
    negative and hides anyone genuinely missing.
    """
    picked = {pick.player_id for pick in Pick.query.filter_by(round_id=round_obj.id).all()}
    return sum(
        1 for player in Player.query.filter_by(status="active").all()
        if player.id not in picked
    )


def _last_kickoff(round_obj: Round) -> datetime | None:
    latest = None
    for fixture in round_obj.fixtures or []:
        if fixture.date and fixture.time:
            dt = datetime.combine(fixture.date, fixture.time)
            if latest is None or dt > latest:
                latest = dt
    return latest


def build_plan(now: datetime | None = None) -> Plan:
    """Inspect current state and decide what is due. Performs no writes."""
    now = now or datetime.utcnow()
    plan = Plan()

    round_obj = _active_round()
    if round_obj is None:
        plan.phase = "no-active-round"
        plan.actions.append("rollover-check")
        plan.actions.append("season-check")
        # A round that ends with survivors leaves the game with nothing to do:
        # app.py marks it completed, checks for a winner, checks whether
        # everyone is out, and stops. The next round is only ever created
        # automatically on a rollover, so a normal completion stalled here
        # until someone made one by hand.
        plan.actions.append("create-next-round")
        plan.notes.append("No active round — checking whether one should be created.")
        return plan

    plan.round_id = round_obj.id
    plan.round_label = f"Round {round_obj.round_number} (cycle {round_obj.cycle_number})"

    deadline = round_obj.end_date
    kickoff = round_obj.first_kickoff_at
    if kickoff and not deadline:
        deadline = kickoff - PICK_DEADLINE_LEAD

    submitted, active_players = _pick_counts(round_obj)
    plan.notes.append(f"{submitted}/{active_players} picks in")

    if deadline is None:
        plan.phase = "no-deadline"
        plan.notes.append("Round has no deadline and no kickoff — needs fixtures loaded.")
        return plan

    plan.notes.append(f"deadline {deadline:%a %d %b %H:%M} UTC")

    tokens = PickToken.query.filter_by(round_id=round_obj.id).count()
    if tokens < active_players:
        plan.actions.append("generate-tokens")
        plan.notes.append(f"only {tokens} tokens for {active_players} active players")

    # Reminders only make sense while picks are still open.
    if now < deadline:
        reminders = ReminderSchedule.query.filter_by(round_id=round_obj.id).count()
        if reminders == 0:
            plan.actions.append("schedule-reminders")

    if now < deadline:
        plan.phase = "open"
        due = (
            ReminderSchedule.query.filter(
                ReminderSchedule.round_id == round_obj.id,
                ReminderSchedule.is_sent.is_(False),
                ReminderSchedule.scheduled_time <= now,
            ).count()
        )
        if due:
            plan.actions.append("send-reminders")
            plan.notes.append(f"{due} reminders due")
        plan.notes.append(f"{_humanise(deadline - now)} until picks close")
        return plan

    # Deadline has passed. Phases are driven by time, not by pick completeness:
    # a round whose auto-picks fall short must still progress to results rather
    # than stalling here forever.
    missing = _unpicked_active(round_obj)

    if kickoff and now < kickoff:
        # The normal window for auto-picks: after the deadline, before a ball
        # is kicked, with every team still available.
        plan.phase = "deadline-passed"
        if missing > 0:
            plan.actions.append("apply-missed-picks")
            plan.notes.append(f"{missing} players without a pick")
        return plan

    last_kickoff = _last_kickoff(round_obj)
    if missing > 0:
        if last_kickoff and now < last_kickoff:
            # The hour before kickoff was missed. A late auto-pick is still
            # possible while a match remains to be played; app.py limits it
            # to teams that have not kicked off.
            plan.actions.append("apply-missed-picks")
            plan.notes.append(
                f"{missing} players still without a pick after kickoff — "
                "auto-picking from the matches yet to start"
            )
        else:
            plan.notes.append(
                f"{missing} players still without a pick and every match has "
                "kicked off — needs an admin"
            )

    if last_kickoff and now < last_kickoff:
        plan.phase = "in-play"
        plan.notes.append(f"last kickoff {last_kickoff:%a %d %b %H:%M} UTC")
        plan.actions.append("fetch-results")
        # Resolve picks as their fixtures finish rather than waiting for the
        # whole round. Most players know their fate by Saturday evening, and
        # holding it back until Monday night meant the grid showed nothing for
        # two days. process-results only touches fixtures that already have
        # scores, and only marks the round completed once all of them are in,
        # so running it here settles what is settled and leaves the rest.
        plan.actions.append("process-results")
        return plan

    if last_kickoff and now > last_kickoff + RESULT_POLL_HORIZON:
        plan.notes.append("results still incomplete well after full time")

    plan.phase = "results-due"
    plan.actions.append("fetch-results")
    plan.actions.append("process-results")
    plan.actions.append("rollover-check")
    return plan


def _humanise(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


ACTION_ROUTES = {
    "generate-tokens": None,  # handled in-process, see run_action
    "schedule-reminders": "/api/admin/schedule-reminders/{round_id}",
    "send-reminders": None,  # needs the WhatsApp sender; not wired yet
    "apply-missed-picks": "/api/admin/rounds/{round_id}/apply-missed-picks",
    "fetch-results": "/api/rounds/{round_id}/auto-populate-results",
    "process-results": "/api/rounds/{round_id}/process-results",
    "rollover-check": "/api/admin/run-rollover-check",
    "season-check": "/api/admin/check-new-season",
}


def sender_status() -> dict | None:
    """Ask the local sender how it is doing. None if it is not reachable."""
    import requests

    try:
        response = requests.get(
            f"{SENDER_URL}/status",
            headers={"x-sender-token": SENDER_TOKEN},
            timeout=5,
        )
        return response.json() if response.ok else None
    except Exception:
        return None


def _digits(number: str) -> str:
    """Bare international digits, so one phone written several ways groups as one.

    Mirrors the sender's normalisation: without promoting 07... to 447..., a
    household member registered in national format would be treated as a
    separate phone and get their own message — reintroducing the very
    same-chat overwrite this grouping exists to prevent.
    """
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "44" + digits[1:]
    return digits


def _combined_message(group: list[dict]) -> str:
    """One message covering everyone who shares a handset.

    Sending a household a message each meant several sends to the same chat,
    and a failed send there is overwritten by the next one — leaving no draft
    and no evidence anything went missing.
    """
    first = group[0]
    round_number = first.get("round_number")
    deadline = first.get("deadline_local")
    heading = {
        "round_open": f"⚽ Round {round_number} is open",
        "nudge": "👋 Picks still needed",
        "4_hour": "⏰ 4 Hour Reminder",
        "2_hour": "🚨 2 Hour Reminder",
    }.get(first.get("reminder_type"), "📝 Reminder")

    lines = [
        heading,
        "",
        f"{len(group)} picks still needed on this phone for Round {round_number}:",
        "",
    ]
    for reminder in sorted(group, key=lambda r: r.get("player_name") or ""):
        lines.append(f"{reminder.get('player_name')}")
        lines.append(f"🎯 {reminder.get('pick_url')}")
        lines.append("")

    if deadline:
        lines.append(f"Picks close {deadline}.")
    lines.append("Miss it and a team gets picked for you.")
    lines.append("")
    lines.append("Good luck! 🍀")
    lines.append("Last Man Standing")
    return "\n".join(lines)


def send_due_reminders(dry_run: bool = False) -> str:
    """Hand any due reminders to the local WhatsApp sender.

    The sender accepts messages and delivers them on its own paced schedule,
    so a 202 means queued, not delivered. Reminders are marked sent on
    acceptance; genuine delivery failures surface in the sender's /status and
    are reported on the next tick rather than silently vanishing.
    """
    import requests

    if not SENDER_TOKEN:
        return "skipped — SENDER_TOKEN not set"

    # If the sender is in dry-run it accepts messages and delivers nothing.
    # Marking reminders sent against that would silently consume them and
    # leave players un-reminded, so treat it as a dry run here too.
    status = sender_status()
    if status is None:
        return "skipped — sender unreachable"
    if status.get("dryRun"):
        dry_run = True
    elif not status.get("ready"):
        # Say which condition is blocking. This previously reported "not paired
        # to WhatsApp yet", wording left over from the whatsapp-web.js client
        # that no longer exists — so three hours of a locked screen on 18 Aug
        # read as a pairing problem and sent the diagnosis the wrong way.
        if status.get("screenLocked"):
            return "BLOCKED — screen is locked; reminders wait until it is unlocked"
        if not status.get("whatsappRunning"):
            return "BLOCKED — WhatsApp Desktop is not running"
        return f"BLOCKED — sender not ready: {status.get('lastError') or 'unknown'}"

    client = _admin_client()
    response = client.get("/api/admin/due-reminders")
    payload = response.get_json(silent=True) or {}
    due = payload.get("due_reminders") or []

    if not due:
        return "no reminders due"

    if dry_run:
        who = ", ".join(r.get("player_name", "?") for r in due[:5])
        more = f" (+{len(due) - 5} more)" if len(due) > 5 else ""
        return f"would send {len(due)} reminders to {who}{more}"

    # One message per phone, not per player. Several households share a
    # handset, and sending them a message each meant consecutive sends to the
    # same chat — where a failed send leaves a draft that the next message
    # silently overwrites. That is how Phil and Frankie Warburton's
    # announcements vanished without even leaving a draft behind, and it also
    # blinds the only check available (scanning for "Draft:" in the chat list).
    grouped: dict[str, list[dict]] = {}
    for reminder in due:
        number = reminder.get("whatsapp_number")
        if not number or not reminder.get("message"):
            continue
        grouped.setdefault(_digits(number), []).append(reminder)

    queued, rejected, duplicate, combined = 0, 0, 0, 0
    rejected += sum(
        1 for r in due if not r.get("whatsapp_number") or not r.get("message")
    )

    for _, group in grouped.items():
        number = group[0]["whatsapp_number"]
        if len(group) == 1:
            message = group[0]["message"]
            ref = str(group[0]["reminder_id"])
        else:
            message = _combined_message(group)
            ref = ",".join(str(r["reminder_id"]) for r in group)
            combined += 1
        try:
            response = requests.post(
                f"{SENDER_URL}/send",
                json={
                    "to": number,
                    "text": message,
                    # The sender reports this back once the message has actually
                    # gone, which is when the reminders get marked sent. For a
                    # shared handset this is several reminder ids joined by
                    # commas — one message covers the whole household.
                    "ref": ref,
                },
                headers={"x-sender-token": SENDER_TOKEN},
                timeout=10,
            )
            if response.status_code == 202:
                queued += 1
            elif response.status_code == 409:
                # Already queued or awaiting acknowledgement — not an error.
                duplicate += 1
            else:
                rejected += 1
                logger.warning("sender rejected %s: %s", number, response.text[:120])
        except Exception as exc:
            rejected += 1
            logger.warning("sender unreachable for %s: %s", number, exc)

    parts = [f"queued {queued}"]
    if combined:
        parts.append(f"{combined} combined for shared phones")
    if duplicate:
        parts.append(f"{duplicate} already in flight")
    if rejected:
        parts.append(f"{rejected} rejected")
    return ", ".join(parts)


def reconcile_deliveries() -> str:
    """Mark reminders sent based on what the sender actually delivered.

    Nothing is marked on the 202 from /send: that only means the message was
    accepted onto a queue. Treating acceptance as delivery marked 48 players as
    announced when none had been contacted, three times over on 14 Aug — once
    because the sender crashed with the queue in memory, once because the
    WhatsApp session died mid-send, and once because a keystroke permission was
    missing. A message that failed stays pending and is retried on a later tick.
    """
    import requests

    if not SENDER_TOKEN:
        return "skipped — SENDER_TOKEN not set"

    try:
        response = requests.get(
            f"{SENDER_URL}/completed",
            headers={"x-sender-token": SENDER_TOKEN},
            timeout=10,
        )
        items = (response.json() or {}).get("items", []) if response.ok else []
    except Exception as exc:
        return f"sender unreachable: {exc}"

    if not items:
        return "nothing to reconcile"

    client = _admin_client()
    marked, failed, handled, digests = 0, 0, [], 0
    for item in items:
        handled.append(item["id"])
        ref = str(item.get("ref") or "")
        sent = item.get("outcome") == "sent"

        # Digests share this queue. They are recorded as published here, on
        # delivery, for the reason reminders are: being accepted onto the
        # queue is not being sent. A digest that fails is left unpublished so
        # the next tick queues it again.
        digest = _digest_ref(ref)
        if digest:
            kind, round_id = digest
            if sent:
                _mark_published(kind, round_id)
                digests += 1
            else:
                attempts = _note_digest_failure(ref)
                failed += 1
                logger.warning(
                    "digest %s failed to deliver (attempt %s of %s): %s",
                    ref, attempts, DIGEST_MAX_ATTEMPTS, (item.get("error") or "")[:120],
                )
            continue

        # A ref may cover several reminders when one message went to a shared
        # handset. Anything that is not a reminder id — a one-off message sent
        # by hand through the same sender — is none of this function's business.
        reminder_ids = [part.strip() for part in ref.split(",") if part.strip().isdigit()]
        if not reminder_ids:
            continue
        if sent:
            for reminder_id in reminder_ids:
                client.post(f"/api/admin/mark-reminder-sent/{reminder_id}")
                marked += 1
        else:
            failed += 1
            logger.warning(
                "delivery failed for reminder %s, left pending: %s",
                ref, (item.get("error") or "")[:120],
            )

    try:
        requests.post(
            f"{SENDER_URL}/completed/ack",
            json={"ids": handled},
            headers={"x-sender-token": SENDER_TOKEN},
            timeout=10,
        )
    except Exception as exc:
        # Not acknowledged means they come back next tick; marking is
        # idempotent, so a duplicate pass is harmless.
        logger.warning("could not acknowledge outcomes: %s", exc)

    return (
        f"marked {marked} delivered"
        + (f", {digests} digest(s) confirmed sent" if digests else "")
        + (f", {failed} failed and left pending" if failed else "")
    )


# Where the round digests are sent. These go to the organiser, who forwards
# them to the group. Posting to the group directly was tried and rejected:
# driving WhatsApp's search to open a group chat raced with the keystrokes and
# sent a fragment of the search text into the group as a message. A timing
# glitch there is instantly public to every player, so the irreversible step
# stays with a human.
ADMIN_WHATSAPP = os.environ.get("ADMIN_WHATSAPP", "")

# Which digests have already gone out, so a restart or a repeated tick does not
# send them twice. Kept beside the app rather than in the database to avoid a
# schema change for bookkeeping.
PUBLISH_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".published.json"
)


def _published() -> dict:
    import json

    try:
        with open(PUBLISH_STATE_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {"results": [], "table": []}


def _save_published(state: dict) -> None:
    import json

    try:
        with open(PUBLISH_STATE_PATH, "w") as fh:
            json.dump(state, fh)
    except Exception as exc:
        logger.warning("could not record published digest: %s", exc)


def _mark_published(kind: str, round_id: int) -> None:
    state = _published()
    if round_id not in state.setdefault(kind, []):
        state[kind].append(round_id)
    _save_published(state)


# A digest that keeps failing is retried on later ticks, but not for ever: a
# message WhatsApp will not take is not improved by a fortieth attempt.
DIGEST_MAX_ATTEMPTS = 3


def _digest_ref(ref: str) -> tuple[str, int] | None:
    """Split a digest's sender ref, "results-68", into its kind and round id."""
    kind, _, round_id = str(ref).partition("-")
    if kind in ("results", "table") and round_id.isdigit():
        return kind, int(round_id)
    return None


def _digest_failures(ref: str) -> int:
    return int(_published().get("failures", {}).get(ref, 0))


def _note_digest_failure(ref: str) -> int:
    state = _published()
    failures = state.setdefault("failures", {})
    failures[ref] = int(failures.get(ref, 0)) + 1
    _save_published(state)
    return failures[ref]


def _team(name: str | None) -> str:
    """A club as the group writes it — "Man City", not "Manchester City FC".

    The grid has always shortened club names to fit a phone-width cell. The
    digest did not, so the same round read as "Chelsea FC" in the list and
    "Chelsea" in the picture. One message, one spelling.
    """
    from grid_image import short_team  # lazy: grid_image pulls in PIL

    return short_team(name) or (name or "")


def _one_of(options: list[str], seed: int) -> str:
    """Pick a line deterministically, so a redraw says the same thing twice."""
    return options[seed % len(options)]


def results_quip(out: list, through: list, still_in: int, seed: int) -> str:
    """One cheeky line about the shape of the round, for the group.

    The digest states the facts; this is the bit that gets read out loud. It
    is written rather than generated — no model runs on the mini — so it
    leans on the handful of shapes a round actually takes: a wipeout on one
    team, a lone casualty, the closing stages, a quiet week.
    """
    played = len(out) + len(through)

    if still_in == 1 and through:
        winner = sorted(name for name, _ in through)[0]
        return _one_of(
            [
                f"🏆 That's your lot. {winner} takes it, and the rest of you can go back to pretending you knew all along.",
                f"🏆 Last man standing: {winner}. Someone check they're still breathing.",
            ],
            seed,
        )

    if not out:
        # A quiet round means something different to a field of forty-eight
        # than it does to the last two, who have just both got it right
        # again. The lines below were written for a crowd, so the endgame
        # has to be caught before them rather than after.
        if still_in == 2 and len(through) == 2:
            names = sorted(name for name, _ in through)
            return (
                f"🤝 Both through — {names[0]} and {names[1]} go again. "
                "One of you is going to have to blink."
            )
        if still_in <= 5:
            return _one_of(
                [
                    f"😬 Nobody out. All {still_in} of you survive to the next round.",
                    f"😬 No casualties — the last {still_in} all got it right. Again.",
                ],
                seed,
            )
        return _one_of(
            [
                "😴 Not a scratch. Everyone's through and nobody's interesting.",
                "😴 A whole round and not one casualty. Pull your fingers out.",
            ],
            seed,
        )

    if still_in == 0:
        return _one_of(
            [
                "💀 Every single one of you. Gone. The pot rolls over and we go again.",
                "💀 Total wipeout — nobody left standing. Rollover, and the pot gets fatter.",
            ],
            seed,
        )

    counts = {}
    for _, team in out:
        counts[team] = counts.get(team, 0) + 1
    team, victims = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    # One team doing most of the damage is the line that writes itself — and
    # with this crowd, a mass elimination genuinely is a local event.
    if victims >= 4 and victims >= played / 2:
        return _one_of(
            [
                f"☠️ {team} just took out half the north west — {victims} of you gone in ninety minutes.",
                f"☠️ {victims} of you trusted {team}. {victims} of you are now spectators.",
            ],
            seed,
        )
    if victims >= 3 and victims >= len(out) * 0.6:
        return _one_of(
            [
                f"☠️ {team} did the damage: {victims} of you out on one result.",
                f"☠️ {victims} binned by {team} alone. That'll sting on Monday.",
            ],
            seed,
        )

    if len(out) == 1:
        name, lost_to = out[0]
        return _one_of(
            [
                f"🪦 One casualty: {name}, undone by {lost_to}. Everyone else carries on.",
                f"🪦 Just {name} this week, done by {lost_to}. Unlucky.",
            ],
            seed,
        )

    # still_in counts active players; a player with no pick in this round
    # would not be in `through`, so only name them when both are there.
    if still_in == 2 and len(through) == 2:
        names = sorted(name for name, _ in through)
        return f"🥊 Two left — {names[0]} and {names[1]}. Neither of them is sleeping this week."

    if still_in <= 5:
        return _one_of(
            [
                f"🔥 Down to {still_in}. It's getting serious now.",
                f"🔥 {len(out)} more gone, {still_in} left. The pot's close enough to touch.",
            ],
            seed,
        )

    # A big cull with no single team to blame still deserves better than a
    # scoreline — round one routinely takes a third of the field.
    if len(out) >= 4 and len(out) >= played / 3:
        return _one_of(
            [
                f"☠️ Carnage. {len(out)} of you gone in one afternoon, {still_in} left.",
                f"☠️ {len(out)} out in a single round. {still_in} of you survived the cull.",
            ],
            seed,
        )

    return _one_of(
        [
            f"⚰️ {len(out)} out, {still_in} still standing.",
            f"⚰️ {len(out)} gone this round. {still_in} of you left to fall out with.",
        ],
        seed,
    )


def picks_quip(entries: list, seed: int) -> str:
    """One line about the shape of a locked round's picks."""
    total = len(entries)
    if total == 0:
        return "🤷 Not a single pick. Astonishing."
    if total == 1:
        return "👀 One pick in. That's the whole game now."

    counts = {}
    for _, team in entries:
        counts[team] = counts.get(team, 0) + 1
    team, backers = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    if backers == total:
        return f"🐑 Every last one of you on {team}. Either a masterclass or a mass burial."

    if backers >= total * 0.6:
        return _one_of(
            [
                f"🐑 {backers} of {total} have piled onto {team}. If that goes wrong it'll be very quiet in here.",
                f"🐑 {backers} of you on {team}. Fearless, this lot.",
            ],
            seed,
        )

    loners = sorted((name, t) for name, t in entries if counts[t] == 1)
    if loners and total >= 4:
        name, lone_team = loners[seed % len(loners)]
        return _one_of(
            [
                f"🧐 {name} is on their own with {lone_team}. Brave, or daft — we'll know Saturday.",
                f"🧐 Nobody fancied {lone_team} except {name}. One of you is wrong.",
            ],
            seed,
        )

    return f"⚖️ {total} picks across {len(counts)} teams. Nicely spread, for once."


def results_digest(round_obj: Round) -> str:
    """Who went out and who survived, ready to forward to the group."""
    picks = (
        Pick.query.filter_by(round_id=round_obj.id)
        .join(Player, Player.id == Pick.player_id)
        .add_columns(Player.name)
        .all()
    )
    out, through, byes = [], [], []
    for pick, name in picks:
        (through if pick.is_winner else out).append((name, _team(pick.team_picked)))
        if pick.is_winner and pick.auto_reason == "postponement_late":
            byes.append((name, _team(pick.team_picked)))

    # The digest is written after the round has been closed, and closing the
    # last round of a game changes everyone's status: a win takes the winner
    # off 'active', and a wipeout puts all forty-eight back on it for the next
    # cycle. Counting active players then describes the game that follows, so
    # a title win read as "one casualty, 47 still in". In those two cases the
    # round's own picks are the record of who was left.
    #
    # A later cycle existing counts as evidence too: Start New Game clears the
    # winner's status, and it can be pressed before this digest is written.
    moved_on = bool(
        Round.query.filter(Round.cycle_number > (round_obj.cycle_number or 1)).count()
    )
    won = bool(Player.query.filter_by(status="winner").count())
    if len(through) == 1 and (won or moved_on):
        still_in = 1
    elif not through and (round_obj.special_measure == "EARLY_TERMINATED" or moved_on):
        still_in = 0
    else:
        still_in = Player.query.filter_by(status="active").count()

    lines = [
        f"⚽ Round {round_obj.round_number} results",
        "",
    ]
    if out:
        lines.append(f"❌ Out ({len(out)}):")
        for name, team in sorted(out):
            lines.append(f"• {name} — {team}")
        lines.append("")
    else:
        lines.append("❌ Nobody went out this round.")
        lines.append("")

    if byes:
        lines.append(f"🎟️ Through on a bye — match postponed ({len(byes)}):")
        for name, team in sorted(byes):
            lines.append(f"• {name} — {team}")
        lines.append("")

    # Only one survivor count. "Through this round" and "still in the game" are
    # always the same number — everyone who survives a round is by definition
    # still in — so printing both said the same thing twice.
    lines.append(f"👥 Still in the game: {still_in}")
    lines.append("")
    lines.append(results_quip(out, through, still_in, round_obj.id))
    return "\n".join(lines)


EXPORTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "exports"
)


def _grid_data(cycle: int) -> dict:
    """The cycle's picks grid, with admin-only fields stripped."""
    client = _admin_client()
    data = client.get(f"/api/picks-grid-data?cycle={cycle}").get_json() or {}
    # Entry-fee payment dates are admin bookkeeping and must not travel to
    # the group with the grid.
    for player in data.get("players", []):
        player.pop("cycle_paid_at", None)
    return data


def refresh_grid_image(cycle, keep, out_name, subtitle=None, status=None) -> str | None:
    """Draw the picks grid for the players ``keep`` accepts.

    Returns the path, or None if there is nothing to draw or the render
    fails — a missing grid costs the organiser a picture, not a message.

    The grid is an image because a grid of this width is unreadable as
    WhatsApp text once there is more than one round. It is drawn for a subset
    of the field: a full table of forty-eight is mostly dead names by the
    halfway point, and each thing we publish is about a different part of it.

    Each render gets its own filename. The sender reads the file when the
    message reaches the front of its queue, not when it is handed over, so
    two digests sharing one path would both go out carrying whichever grid
    happened to be drawn last.
    """
    from grid_image import render_picks_grid

    try:
        data = _grid_data(cycle)
        data["players"] = [p for p in data.get("players", []) if keep(p)]
        if not data["players"]:
            return None
        # The grid colours a row by the player's status now. A table about a
        # past round can pin it instead: after a wipeout everyone is active
        # again, and the round's casualties would otherwise be drawn as if
        # they were still in.
        if status:
            for player in data["players"]:
                player["status"] = status
        return render_picks_grid(
            data,
            os.path.join(EXPORTS_DIR, out_name),
            f"Last Man Standing — Cycle {data.get('current_cycle', cycle)}",
            subtitle,
        )
    except Exception as exc:
        logger.warning("could not render the picks grid: %s", exc)
        return None


def _round_key(round_obj: Round) -> str:
    """The key the grid data uses for a round's column."""
    return f"C{round_obj.cycle_number or 1}-R{round_obj.round_number}"


def survivors_grid(round_obj: Round, still_in: int) -> str | None:
    """The table that goes out when picks lock: only players still in."""
    return refresh_grid_image(
        round_obj.cycle_number or 1,
        keep=lambda p: p.get("status") != "eliminated",
        out_name=f"grid-r{round_obj.round_number}-live.png",
        subtitle=f"Still in: {still_in}",
    )


def casualties_grid(round_obj: Round, gone: int) -> str | None:
    """The table that goes out with results: only this round's casualties."""
    key = _round_key(round_obj)

    def keep(player):
        return bool(((player.get("picks") or {}).get(key) or {}).get("is_eliminated"))

    return refresh_grid_image(
        round_obj.cycle_number or 1,
        keep=keep,
        out_name=f"grid-r{round_obj.round_number}-out.png",
        subtitle=f"Knocked out in Round {round_obj.round_number}: {gone}",
        status="eliminated",
    )


def _round_picks(round_obj: Round) -> list:
    """(name, team) for every pick in a round."""
    rows = (
        Pick.query.filter_by(round_id=round_obj.id)
        .join(Player, Player.id == Pick.player_id)
        .add_columns(Player.name)
        .all()
    )
    return sorted((name, _team(pick.team_picked)) for pick, name in rows)


def lock_round_picks(round_obj: Round) -> int:
    """Close the picking window for a round by expiring its pick tokens.

    Publishing the grid while links still work would let a player see what
    everyone else chose and then change their own — the links stay editable
    until they expire. Locking first is what makes early publication fair.

    Expiring the token is the same mechanism the deadline already uses, so a
    player following an old link gets the ordinary "invalid pick link" page
    rather than anything new. Note this closes the window for the last player
    to pick as well: they get no chance to amend, having had the same days as
    everyone else.
    """
    now = datetime.utcnow()
    tokens = PickToken.query.filter_by(round_id=round_obj.id).all()
    locked = 0
    for token in tokens:
        if token.expires_at is None or token.expires_at > now:
            token.expires_at = now
            locked += 1
    if locked:
        db.session.commit()
    return locked


def picks_table(round_obj: Round) -> str:
    """Every player's pick for the round, as a monospaced table."""
    rows = (
        Pick.query.filter_by(round_id=round_obj.id)
        .join(Player, Player.id == Pick.player_id)
        .add_columns(Player.name)
        .all()
    )
    entries = sorted((name, pick.team_picked, pick.auto_assigned) for pick, name in rows)
    width = max((len(n) for n, _, _ in entries), default=10)

    lines = [
        f"📋 Round {round_obj.round_number} picks ({len(entries)})",
        "",
        "```",
    ]
    for name, team, auto in entries:
        marker = " *" if auto else ""
        lines.append(f"{name.ljust(width)}  {team}{marker}")
    lines.append("```")
    if any(auto for _, _, auto in entries):
        lines.append("* auto-picked at the deadline")
    return "\n".join(lines)


def _send_to_admin(text: str, ref: str, dry_run: bool, image: str | None = None) -> str:
    import requests

    if not ADMIN_WHATSAPP:
        return "skipped — ADMIN_WHATSAPP not set"
    if not SENDER_TOKEN:
        return "skipped — SENDER_TOKEN not set"
    if dry_run:
        return f"would send {len(text)} chars to the organiser"
    try:
        response = requests.post(
            f"{SENDER_URL}/send",
            json={
                "to": ADMIN_WHATSAPP,
                "text": text,
                "ref": ref,
                # The grid travels as a picture, not a file path: a path is no
                # use to someone away from the Mac, which is exactly when the
                # digest matters.
                **({"image": os.path.abspath(image)} if image else {}),
            },
            headers={"x-sender-token": SENDER_TOKEN},
            timeout=10,
        )
        if response.status_code == 202:
            return "queued to the organiser"
        if response.status_code == 409:
            # Already on the sender's queue, or sent and not yet reconciled.
            return "in flight"
        return f"FAILED [{response.status_code}] {response.text[:100]}"
    except Exception as exc:
        return f"sender unreachable: {exc}"


def publish_digests(dry_run: bool = False) -> list[str]:
    """Send the organiser anything worth forwarding to the group.

    Runs independently of the round phase: the results digest belongs to a
    round that has just finished, by which point there may already be a new
    active round.
    """
    notes = []
    state = _published()

    # Picks table — as soon as every active player is in, whenever that
    # happens, rather than waiting for the deadline. Waiting meant the grid
    # landed at 19:00 on a Friday, which is the worst moment for the organiser
    # to be free to post it.
    #
    # The links are closed first. Publishing while they still worked would let
    # a player see everyone else's pick and then change their own, which is
    # what previously argued for waiting until the deadline. Locking removes
    # that objection.
    #
    # If some players never pick, submitted only reaches active once auto-picks
    # run at the deadline, so this still fires then.
    now = datetime.utcnow()
    for round_obj in Round.query.filter(Round.status == "active").all():
        if round_obj.id in state.get("table", []):
            continue
        if _digest_failures(f"table-{round_obj.id}") >= DIGEST_MAX_ATTEMPTS:
            continue
        active = Player.query.filter_by(status="active").count()
        submitted = Pick.query.filter_by(round_id=round_obj.id).count()
        if submitted < active:
            continue  # still waiting on picks, or on auto-picks at the deadline
        if not dry_run:
            locked = lock_round_picks(round_obj)
            if locked:
                logger.info("locked %s pick links for R%s", locked, round_obj.round_number)
        path = None if dry_run else survivors_grid(round_obj, active)
        text = "\n".join(
            [
                f"📋 Round {round_obj.round_number} picks are locked — {active} still in.",
                "",
                picks_quip(_round_picks(round_obj), round_obj.id),
            ]
        )
        # Not marked as published here. That happens in reconcile_deliveries
        # once the sender reports it actually went; until then each tick
        # offers it again and the sender answers "in flight".
        result = _send_to_admin(text, f"table-{round_obj.id}", dry_run, image=path)
        if result != "in flight":
            notes.append(
                f"picks grid R{round_obj.round_number}: {result}"
                + (f" (image: {os.path.basename(path)})" if path else "")
            )

    # Results digest — only for rounds that have finished recently. Without a
    # recency bound this happily offered to send digests for rounds that ended
    # in May, on the first run.
    cutoff = now - timedelta(days=14)
    recent = (
        Round.query.filter(Round.status == "completed")
        .order_by(Round.id.desc())
        .limit(5)
        .all()
    )
    for round_obj in recent:
        if round_obj.id in state.get("results", []):
            continue
        if _digest_failures(f"results-{round_obj.id}") >= DIGEST_MAX_ATTEMPTS:
            continue
        if not round_obj.first_kickoff_at or round_obj.first_kickoff_at < cutoff:
            continue
        if not Pick.query.filter_by(round_id=round_obj.id).first():
            continue
        # Only the players who went out this round, so the table shrinks as
        # the cycle wears on. Nobody out means nothing worth drawing.
        gone = Pick.query.filter_by(round_id=round_obj.id, is_eliminated=True).count()
        path = None if dry_run or not gone else casualties_grid(round_obj, gone)
        text = results_digest(round_obj)
        result = _send_to_admin(text, f"results-{round_obj.id}", dry_run, image=path)
        if result != "in flight":
            notes.append(
                f"results R{round_obj.round_number}: {result}"
                + (f" (image: {os.path.basename(path)})" if path else "")
            )

    return notes


def _eligible_team_counts(cycle_number: int) -> dict[str, int]:
    """How many teams each active player still has available this cycle.

    The rules only allow a round to go ahead if every active player has at
    least one eligible team, so this is checked before creating one rather
    than discovering it when someone cannot pick.
    """
    counts = {}
    for player in Player.query.filter_by(status="active").all():
        used = (
            db.session.query(Pick.team_picked)
            .join(Round, Round.id == Pick.round_id)
            .filter(Pick.player_id == player.id, Round.cycle_number == cycle_number)
            .distinct()
            .count()
        )
        counts[player.name] = 20 - used
    return counts


def create_next_round(dry_run: bool = False) -> str:
    """Create the next round once one has finished with survivors still in.

    Deliberately delegates to POST /api/rounds, the same endpoint the admin
    dashboard uses, so cycle detection, round numbering, fixture loading and
    deadline derivation all stay in one place rather than being reimplemented.

    Refuses in every case it is not certain about: an existing unfinished
    round, a rollover (which creates its own round), no completed round to
    follow, no fixtures available, or a matchday already used.
    """
    # local import: heavy at module load
    from app import fetch_upcoming_fixtures, matchdays_used_this_season

    existing = Round.query.filter(Round.status.in_(("active", "pending"))).first()
    if existing:
        return f"skipped — Round {existing.round_number} is still {existing.status}"

    last = (
        Round.query.filter_by(status="completed")
        .order_by(Round.id.desc())
        .first()
    )
    if last is None:
        return "skipped — no completed round to follow"

    # A rollover creates its own round for the new cycle; stay out of its way.
    if last.special_measure == "EARLY_TERMINATED":
        return "skipped — last round was early-terminated; rollover owns this"

    if Player.query.filter_by(status="winner").count():
        return "skipped — the game has been won; waiting for Start New Game"

    active_players = Player.query.filter_by(status="active").count()
    if active_players == 0:
        return "skipped — no active players; rollover should handle this"
    if active_players == 1:
        return "skipped — one player left; that is a winner, not a new round"

    check = fetch_upcoming_fixtures(horizon_days=45)
    if not check.get("available"):
        return f"skipped — no upcoming fixtures ({check.get('error') or 'season break'})"

    used_matchdays = matchdays_used_this_season()
    matchday = check.get("next_matchday")
    if matchday in used_matchdays:
        return f"skipped — matchday {matchday} already used; needs an admin"
    if not matchday:
        return "skipped — could not determine the next matchday"

    # Rules: a round only goes ahead if every active player has an eligible team.
    short = {n: c for n, c in _eligible_team_counts(last.cycle_number or 1).items() if c < 1}
    if short:
        return f"BLOCKED — {len(short)} players have no eligible team left: {list(short)[:3]}"

    if dry_run:
        return (
            f"would create the next round on matchday {matchday} "
            f"for {active_players} active players"
        )

    # POST /api/rounds defaults to 'pending', and the scheduler only ever looks
    # for active rounds — so a round created without this is invisible to the
    # automation: no tokens, no reminders, no announcement, and the log simply
    # reports it as still pending every five minutes. The rollover path creates
    # its rounds active for the same reason.
    client = _admin_client()
    response = client.post(
        "/api/rounds", json={"pl_matchday": matchday, "status": "active"}
    )
    body = response.get_json(silent=True) or {}
    if response.status_code == 200 and body.get("success"):
        return f"created round on matchday {matchday} ({active_players} players)"
    return f"FAILED [{response.status_code}] {body.get('error') or response.data[:120]}"


def run_action(action: str, plan: Plan, dry_run: bool = False) -> str:
    """Execute one planned action via the existing admin endpoints."""
    route = ACTION_ROUTES.get(action)

    if action == "send-reminders":
        return send_due_reminders(dry_run=dry_run)

    if action == "create-next-round":
        return create_next_round(dry_run=dry_run)

    if action == "process-results":
        # process-results does not read scores from the database — it requires
        # them in the request body. Calling it without one returned 415, so
        # this step could never have succeeded: the round would have sat
        # unprocessed and never rolled over. Scores are already in the fixtures
        # by this point, put there by fetch-results, so build the payload from
        # whichever fixtures have actually been played.
        played = Fixture.query.filter(
            Fixture.round_id == plan.round_id,
            Fixture.home_score.isnot(None),
            Fixture.away_score.isnot(None),
        ).all()
        if not played:
            return "no completed fixtures to process"
        payload = {
            "results": [
                {"fixture_id": f.id, "home_score": f.home_score, "away_score": f.away_score}
                for f in played
            ]
        }
        if dry_run:
            return f"would process {len(played)} completed fixtures"
        client = _admin_client()
        response = client.post(f"/api/rounds/{plan.round_id}/process-results", json=payload)
        body = response.get_json(silent=True) or {}
        ok = body.get("success", response.status_code == 200)
        detail = body.get("message") or body.get("error") or ""
        return f"{'ok' if ok else 'FAILED'} [{response.status_code}] {len(played)} fixtures {detail}".strip()

    if action == "generate-tokens":
        if dry_run:
            return "would generate missing pick tokens"
        created = 0
        for player in Player.query.filter_by(status="active").all():
            token = PickToken.create_for_player_round(player.id, plan.round_id)
            if token is not None:
                created += 1
        db.session.commit()
        return f"ensured tokens for {created} players"

    if route is None:
        return "no route"

    url = route.format(round_id=plan.round_id)
    if dry_run:
        if action == "apply-missed-picks":
            url += "?dry_run=true"
        else:
            return f"would POST {url}"

    client = _admin_client()
    response = client.post(url)
    body = response.get_json(silent=True) or {}
    ok = body.get("success", response.status_code == 200)
    detail = body.get("message") or body.get("error") or ""
    return f"{'ok' if ok else 'FAILED'} [{response.status_code}] {detail}".strip()


def tick(dry_run: bool = False) -> Plan:
    """One orchestrator pass."""
    with app.app_context():
        # Collect delivery outcomes before planning, so picks and pending
        # counts reflect what has actually been sent.
        if SENDER_TOKEN:
            outcome = reconcile_deliveries()
            if outcome not in ("nothing to reconcile",):
                logger.info("reconcile -> %s", outcome)

        # Digests are independent of the round phase: a results digest belongs
        # to a round that has just finished, by which point a new one may
        # already be active.
        for note in publish_digests(dry_run=dry_run):
            logger.info("publish -> %s", note)

        plan = build_plan()
        logger.info("phase=%s round=%s", plan.phase, plan.round_label)
        for note in plan.notes:
            logger.info("  note: %s", note)
        for action in plan.actions:
            result = run_action(action, plan, dry_run=dry_run)
            logger.info("  %s -> %s", action, result)
        return plan


def print_status() -> None:
    with app.app_context():
        plan = build_plan()
        now = datetime.utcnow()
        print(f"\n  now (UTC)   {now:%a %d %b %Y %H:%M}")
        print(f"  round       {plan.round_label}")
        print(f"  phase       {plan.phase}")
        for note in plan.notes:
            print(f"              {note}")
        if plan.actions:
            print("  would run   " + ", ".join(plan.actions))
        else:
            print("  would run   nothing — waiting")

        status = sender_status()
        if status is None:
            print(f"  sender      unreachable at {SENDER_URL}")
        else:
            bits = [
                "paired" if status.get("ready") else "NOT PAIRED",
                "dry-run" if status.get("dryRun") else "live",
                f"{status.get('queued', 0)} queued",
                f"{status.get('sent', 0)} sent",
            ]
            if status.get("failed"):
                bits.append(f"{status['failed']} FAILED")
            print("  sender      " + ", ".join(bits))
        print()


def run_forever(interval_minutes: int = 5) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        tick,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="orchestrator",
        max_instances=1,          # never let two passes overlap
        coalesce=True,            # a backlog collapses to one run
        # APScheduler discards a run whose time passed by more than this, and
        # the default is one second. On a machine that sleeps, every tick
        # missed while asleep would be thrown away and nothing would happen
        # until the next interval. An hour's grace means waking runs a tick
        # immediately; coalesce keeps that to a single catch-up pass.
        misfire_grace_time=3600,
        # Must be timezone-aware: the scheduler runs in UTC and the mini is on
        # BST, so a naive now() is read as UTC and defers the first tick by an
        # hour — silently, and by two hours' worth of confusion in winter.
        next_run_time=datetime.now(dt_timezone.utc),
    )
    logger.info("Scheduler started — orchestrator every %s minutes", interval_minutes)
    scheduler.start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Last Man Standing automation")
    parser.add_argument("--status", action="store_true", help="read-only state report")
    parser.add_argument("--once", action="store_true", help="run a single pass")
    parser.add_argument("--dry-run", action="store_true", help="plan without writing")
    parser.add_argument("--interval", type=int, default=5, help="minutes between passes")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    if args.status:
        print_status()
    elif args.once:
        tick(dry_run=args.dry_run)
    else:
        run_forever(args.interval)


if __name__ == "__main__":
    main()
