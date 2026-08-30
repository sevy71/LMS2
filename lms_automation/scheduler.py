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
    missing = active_players - submitted

    if kickoff and now < kickoff:
        # The only window where auto-picks are allowed — app.py blocks them
        # once the first ball is kicked.
        plan.phase = "deadline-passed"
        if missing > 0:
            plan.actions.append("apply-missed-picks")
            plan.notes.append(f"{missing} players without a pick")
        return plan

    if missing > 0:
        plan.notes.append(
            f"{missing} players still without a pick, and kickoff has passed — "
            "auto-pick is no longer permitted, needs an admin"
        )

    last_kickoff = _last_kickoff(round_obj)
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
    marked, failed, handled = 0, 0, []
    for item in items:
        handled.append(item["id"])
        if item.get("outcome") == "sent":
            # A ref may cover several reminders when one message went to a
            # shared handset.
            for reminder_id in str(item["ref"]).split(","):
                client.post(f"/api/admin/mark-reminder-sent/{reminder_id.strip()}")
                marked += 1
        else:
            failed += 1
            logger.warning(
                "delivery failed for reminder %s, left pending: %s",
                item.get("ref"), (item.get("error") or "")[:120],
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

    return f"marked {marked} delivered" + (f", {failed} failed and left pending" if failed else "")


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


def _mark_published(kind: str, round_id: int) -> None:
    import json

    state = _published()
    state.setdefault(kind, []).append(round_id)
    try:
        with open(PUBLISH_STATE_PATH, "w") as fh:
            json.dump(state, fh)
    except Exception as exc:
        logger.warning("could not record published digest: %s", exc)


def results_digest(round_obj: Round) -> str:
    """Who went out and who survived, ready to forward to the group."""
    picks = (
        Pick.query.filter_by(round_id=round_obj.id)
        .join(Player, Player.id == Pick.player_id)
        .add_columns(Player.name)
        .all()
    )
    out, through = [], []
    for pick, name in picks:
        (through if pick.is_winner else out).append((name, pick.team_picked))

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

    lines.append(f"✅ Through: {len(through)}")
    lines.append(f"👥 Still in the game: {still_in}")
    return "\n".join(lines)


GRID_IMAGE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "exports", "picks_grid.png"
)


def refresh_grid_image() -> str | None:
    """Redraw the cumulative picks grid. Returns the path, or None on failure.

    The grid is the format the group already reads in other sweepstakes:
    players down the side, rounds across, eliminated in red. It is an image
    because a grid of this width is unreadable as WhatsApp text once there is
    more than one round.
    """
    from grid_image import render_picks_grid

    try:
        client = _admin_client()
        data = client.get("/api/picks-grid-data?cycle=current").get_json() or {}
        # Entry-fee payment dates are admin bookkeeping and must not travel to
        # the group with the grid.
        for player in data.get("players", []):
            player.pop("cycle_paid_at", None)
        cycle = data.get("current_cycle", "")
        return render_picks_grid(
            data, GRID_IMAGE_PATH, f"Last Man Standing — Cycle {cycle}"
        )
    except Exception as exc:
        logger.warning("could not render the picks grid: %s", exc)
        return None


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
        if response.status_code in (202, 409):
            return "queued to the organiser"
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
        active = Player.query.filter_by(status="active").count()
        submitted = Pick.query.filter_by(round_id=round_obj.id).count()
        if submitted < active:
            continue  # still waiting on picks, or on auto-picks at the deadline
        if not dry_run:
            locked = lock_round_picks(round_obj)
            if locked:
                logger.info("locked %s pick links for R%s", locked, round_obj.round_number)
        path = None if dry_run else refresh_grid_image()
        text = f"📋 Round {round_obj.round_number} picks are locked. Ready to post to the group."
        result = _send_to_admin(text, f"table-{round_obj.id}", dry_run, image=path)
        notes.append(
            f"picks grid R{round_obj.round_number}: {result}"
            + (f" (image: {os.path.basename(path)})" if path else "")
        )
        if not dry_run and result.startswith("queued"):
            _mark_published("table", round_obj.id)

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
        if not round_obj.first_kickoff_at or round_obj.first_kickoff_at < cutoff:
            continue
        if not Pick.query.filter_by(round_id=round_obj.id).first():
            continue
        path = None if dry_run else refresh_grid_image()
        text = results_digest(round_obj)
        result = _send_to_admin(text, f"results-{round_obj.id}", dry_run, image=path)
        notes.append(
            f"results R{round_obj.round_number}: {result}"
            + (" (grid refreshed)" if path else "")
        )
        if not dry_run and result.startswith("queued"):
            _mark_published("results", round_obj.id)

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
    from app import fetch_upcoming_fixtures  # local import: heavy at module load

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

    active_players = Player.query.filter_by(status="active").count()
    if active_players == 0:
        return "skipped — no active players; rollover should handle this"
    if active_players == 1:
        return "skipped — one player left; that is a winner, not a new round"

    check = fetch_upcoming_fixtures(horizon_days=45)
    if not check.get("available"):
        return f"skipped — no upcoming fixtures ({check.get('error') or 'season break'})"

    used_matchdays = {
        r.pl_matchday for r in Round.query.filter(Round.pl_matchday.isnot(None)).all()
    }
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
