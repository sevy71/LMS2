#!/usr/bin/env python3
"""
FIX SCRIPT: Correct Paul Crockford's Round 7 pick to 'Arsenal' and remove duplicates.

Dry-run by default. Pass --fix to commit changes.

Usage:
    python3 apply_paul_fix.py          # dry run (safe)
    python3 apply_paul_fix.py --fix    # apply changes
"""

import psycopg2
from urllib.parse import urlparse
import os
import sys

DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    "postgresql://postgres:REDACTED@REDACTED/railway"
)

DRY_RUN = '--fix' not in sys.argv


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


def main():
    print("\n" + "=" * 60)
    print("  PAUL CROCKFORD ROUND 7 FIX")
    print(f"  Mode: {'DRY RUN (no changes)' if DRY_RUN else '*** LIVE FIX ***'}")
    print("=" * 60)

    conn = get_conn()
    cur = conn.cursor()

    # Find Paul Crockford
    cur.execute("SELECT id, name FROM players WHERE name ILIKE %s", ('%paul crockford%',))
    players = cur.fetchall()
    if not players:
        print("ERROR: Player 'Paul Crockford' not found.")
        cur.close()
        conn.close()
        return
    if len(players) > 1:
        print(f"WARNING: {len(players)} players matched. Using first: {players[0]}")
    player_id, player_name = players[0]
    print(f"\nPlayer: {player_name} (id={player_id})")

    # Find Round 7
    cur.execute("SELECT id, round_number FROM rounds WHERE round_number = 7")
    rounds = cur.fetchall()
    if not rounds:
        print("ERROR: Round 7 not found in database.")
        cur.close()
        conn.close()
        return

    for round_id, round_number in rounds:
        print(f"\nProcessing Round {round_number} (db id={round_id})...")

        # Get all picks for this player+round
        cur.execute("""
            SELECT id, team_picked, is_winner, is_eliminated, timestamp
            FROM picks
            WHERE player_id = %s AND round_id = %s
            ORDER BY timestamp
        """, (player_id, round_id))
        picks = cur.fetchall()

        if not picks:
            print(f"  No picks found for this round.")
            continue

        print(f"  Found {len(picks)} pick(s):")
        for p in picks:
            print(f"    id={p[0]}, team='{p[1]}', is_winner={p[2]}, is_eliminated={p[3]}, ts={p[4]}")

        # Determine the canonical pick: keep the earliest one and set team to Arsenal
        canonical_pick_id = picks[0][0]
        duplicate_ids = [p[0] for p in picks[1:]]

        # Step 1: Delete duplicates
        if duplicate_ids:
            print(f"\n  STEP 1: Delete {len(duplicate_ids)} duplicate pick(s): ids={duplicate_ids}")
            if not DRY_RUN:
                cur.execute("DELETE FROM picks WHERE id = ANY(%s)", (duplicate_ids,))
                print(f"  Deleted {cur.rowcount} duplicate(s).")
            else:
                print(f"  [DRY RUN] Would delete pick ids: {duplicate_ids}")
        else:
            print(f"\n  STEP 1: No duplicates to remove.")

        # Step 2: Set team_picked to Arsenal on the canonical pick
        current_team = picks[0][1]
        if current_team != 'Arsenal':
            print(f"\n  STEP 2: Correct team from '{current_team}' → 'Arsenal' on pick id={canonical_pick_id}")
            if not DRY_RUN:
                cur.execute("""
                    UPDATE picks
                    SET team_picked = 'Arsenal',
                        last_edited_at = NOW()
                    WHERE id = %s
                """, (canonical_pick_id,))
                print(f"  Updated {cur.rowcount} pick(s).")
            else:
                print(f"  [DRY RUN] Would set team_picked='Arsenal' on pick id={canonical_pick_id}")
        else:
            print(f"\n  STEP 2: Team already 'Arsenal' — no change needed.")

        # Commit or rollback
        if not DRY_RUN:
            conn.commit()
            print("\n  Changes committed.")
        else:
            conn.rollback()
            print("\n  [DRY RUN] No changes committed.")

        # Verification
        cur.execute("""
            SELECT id, team_picked, is_winner, is_eliminated, timestamp, last_edited_at
            FROM picks
            WHERE player_id = %s AND round_id = %s
            ORDER BY timestamp
        """, (player_id, round_id))
        final_picks = cur.fetchall()
        print(f"\n  VERIFICATION — picks after operation:")
        for p in final_picks:
            print(f"    id={p[0]}, team='{p[1]}', is_winner={p[2]}, is_eliminated={p[3]}, ts={p[4]}, edited={p[5]}")

        if len(final_picks) == 1 and final_picks[0][1] == 'Arsenal':
            print("\n  SUCCESS: Single Arsenal pick confirmed for Round 7.")
        elif DRY_RUN:
            print("\n  (Dry run — re-run with --fix to apply.)")
        else:
            print("\n  WARNING: Unexpected state after fix — please audit manually.")

    cur.close()
    conn.close()
    print("\n" + "=" * 60 + "\n")


if __name__ == '__main__':
    main()
