#!/usr/bin/env python3
"""
FIX: Paul Crockford Round 27 pick corruption.

Pick ID 616 has team_picked='Brentford FC' but should be 'Arsenal FC'.
Arsenal won, so is_winner should be True and Paul should not be eliminated.

Usage:
    python3 fix_round_27_paul.py           # dry run (safe, no changes)
    python3 fix_round_27_paul.py --fix     # apply changes
"""

import psycopg2
from urllib.parse import urlparse
import os
import sys
from datetime import datetime

DATABASE_URL = os.environ["DATABASE_URL"]

DRY_RUN = '--fix' not in sys.argv

TARGET_PICK_ID = 616
CORRECT_TEAM = 'Arsenal FC'
CORRUPT_TEAM = 'Brentford FC'


def get_conn():
    url = DATABASE_URL.replace('postgres://', 'postgresql://')
    parsed = urlparse(url)
    return psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port,
        database=parsed.path[1:],
        user=parsed.username,
        password=parsed.password,
    )


def log(msg):
    print(msg)


def main():
    log("\n" + "=" * 60)
    log("  FIX: PAUL CROCKFORD ROUND 27 PICK CORRUPTION")
    log(f"  Mode: {'DRY RUN — no changes will be written' if DRY_RUN else '*** LIVE FIX — changes will be committed ***'}")
    log("=" * 60)

    conn = get_conn()
    cur = conn.cursor()

    # ── Step 1: Fetch the target pick ────────────────────────────
    log(f"\n[1] Fetching Pick ID {TARGET_PICK_ID}...")
    cur.execute("""
        SELECT
            pk.id,
            pk.player_id,
            pl.name,
            pl.status AS player_status,
            pk.round_id,
            r.round_number,
            pk.team_picked,
            pk.is_winner,
            pk.is_eliminated,
            pk.auto_assigned,
            pk.auto_reason,
            pk.timestamp,
            pk.last_edited_at
        FROM picks pk
        JOIN players pl ON pl.id = pk.player_id
        JOIN rounds r ON r.id = pk.round_id
        WHERE pk.id = %s
    """, (TARGET_PICK_ID,))
    row = cur.fetchone()

    if not row:
        log(f"ERROR: Pick ID {TARGET_PICK_ID} not found in database.")
        cur.close()
        conn.close()
        return

    (pick_id, player_id, player_name, player_status,
     round_id, round_number, team_picked,
     is_winner, is_eliminated, auto_assigned, auto_reason,
     created_at, last_edited_at) = row

    log(f"  Pick found:")
    log(f"    pick_id      = {pick_id}")
    log(f"    player       = {player_name} (id={player_id}, status={player_status})")
    log(f"    round        = Round {round_number} (id={round_id})")
    log(f"    team_picked  = '{team_picked}'")
    log(f"    is_winner    = {is_winner}")
    log(f"    is_eliminated= {is_eliminated}")
    log(f"    auto_assigned= {auto_assigned} ({auto_reason})")
    log(f"    created_at   = {created_at}")
    log(f"    last_edited  = {last_edited_at}")

    # ── Step 2: Validate preconditions ───────────────────────────
    log(f"\n[2] Validating preconditions...")
    errors = []
    if player_name.lower() != 'paul crockford':
        errors.append(f"Player name mismatch: expected 'Paul Crockford', got '{player_name}'")
    if team_picked != CORRUPT_TEAM:
        errors.append(f"Team mismatch: expected '{CORRUPT_TEAM}', got '{team_picked}' (may already be fixed)")
    if errors:
        for e in errors:
            log(f"  WARNING: {e}")
        if any('mismatch' in e and 'already' not in e for e in errors):
            log("\nAborting — precondition failures. Check the pick manually.")
            cur.close()
            conn.close()
            return
    else:
        log(f"  OK — pick belongs to Paul Crockford and has the corrupt team value.")

    # ── Step 3: Show planned changes ─────────────────────────────
    log(f"\n[3] Planned changes:")
    log(f"  picks (id={pick_id}):")
    log(f"    team_picked  : '{team_picked}' → '{CORRECT_TEAM}'")
    log(f"    is_winner    : {is_winner} → True")
    log(f"    is_eliminated: {is_eliminated} → False")
    log(f"    last_edited_at: {last_edited_at} → NOW()")
    log(f"  players (id={player_id}):")
    log(f"    status       : '{player_status}' → 'active'")

    if DRY_RUN:
        log(f"\n[DRY RUN] No changes written. Re-run with --fix to apply.")
        cur.close()
        conn.close()
        log("\n" + "=" * 60 + "\n")
        return

    # ── Step 4: Apply pick fix ────────────────────────────────────
    log(f"\n[4] Applying pick fix...")
    cur.execute("""
        UPDATE picks
        SET
            team_picked   = %s,
            is_winner     = TRUE,
            is_eliminated = FALSE,
            last_edited_at = NOW()
        WHERE id = %s
    """, (CORRECT_TEAM, pick_id))
    pick_rows_updated = cur.rowcount
    log(f"  picks updated: {pick_rows_updated} row(s)")

    # ── Step 5: Restore player status ────────────────────────────
    log(f"\n[5] Restoring player status to 'active'...")
    cur.execute("""
        UPDATE players
        SET status = 'active'
        WHERE id = %s AND status = 'eliminated'
    """, (player_id,))
    player_rows_updated = cur.rowcount
    log(f"  players updated: {player_rows_updated} row(s)")

    # ── Step 6: Commit ────────────────────────────────────────────
    conn.commit()
    log(f"\n[6] Committed.")

    # ── Step 7: Verify ───────────────────────────────────────────
    log(f"\n[7] Verification...")
    cur.execute("""
        SELECT pk.team_picked, pk.is_winner, pk.is_eliminated, pk.last_edited_at,
               pl.status
        FROM picks pk
        JOIN players pl ON pl.id = pk.player_id
        WHERE pk.id = %s
    """, (pick_id,))
    v = cur.fetchone()
    if v:
        team_v, winner_v, elim_v, edited_v, status_v = v
        log(f"  pick.team_picked  = '{team_v}'   (expected '{CORRECT_TEAM}')")
        log(f"  pick.is_winner    = {winner_v}     (expected True)")
        log(f"  pick.is_eliminated= {elim_v}   (expected False)")
        log(f"  pick.last_edited_at= {edited_v}")
        log(f"  player.status     = '{status_v}' (expected 'active')")

        ok = (team_v == CORRECT_TEAM and winner_v is True and elim_v is False and status_v == 'active')
        log(f"\n  {'SUCCESS: All values correct.' if ok else 'WARNING: One or more values unexpected — check manually.'}")
    else:
        log("  ERROR: Could not re-fetch pick after update.")

    cur.close()
    conn.close()
    log("\n" + "=" * 60 + "\n")


if __name__ == '__main__':
    main()
