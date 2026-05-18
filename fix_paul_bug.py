#!/usr/bin/env python3
"""
AUDIT SCRIPT: Inspect Paul Crockford's pick history and identify Round 7 corruption.

Usage:
    python3 fix_paul_bug.py
"""

import psycopg2
from urllib.parse import urlparse
import os

DATABASE_URL = os.environ["DATABASE_URL"]


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
    print("  PAUL CROCKFORD PICK AUDIT")
    print("=" * 60)

    conn = get_conn()
    cur = conn.cursor()

    # Find Paul Crockford
    cur.execute("SELECT id, name, status FROM players WHERE name ILIKE %s", ('%paul crockford%',))
    players = cur.fetchall()

    if not players:
        print("ERROR: No player matching 'Paul Crockford' found.")
        cur.close()
        conn.close()
        return

    for player_id, name, status in players:
        print(f"\nPlayer: {name}  (id={player_id}, status={status})")

    if len(players) > 1:
        print("WARNING: Multiple players found. Using first match.")
    player_id = players[0][0]

    # Full pick history
    cur.execute("""
        SELECT
            p.id,
            r.round_number,
            r.id AS round_id,
            p.team_picked,
            p.is_winner,
            p.is_eliminated,
            p.auto_assigned,
            p.auto_reason,
            p.timestamp,
            p.last_edited_at
        FROM picks p
        JOIN rounds r ON r.id = p.round_id
        WHERE p.player_id = %s
        ORDER BY r.round_number, p.timestamp
    """, (player_id,))
    picks = cur.fetchall()

    print(f"\n{'─'*60}")
    print(f"{'Pick ID':<10} {'Round':<8} {'Team':<20} {'Winner':<8} {'Elim':<6} {'Auto':<6} {'Reason':<25} {'Timestamp'}")
    print(f"{'─'*60}")
    for row in picks:
        pick_id, rnd, round_id, team, winner, elim, auto, reason, ts, edited = row
        winner_str = {True: 'YES', False: 'NO', None: 'pending'}.get(winner, '?')
        print(f"{pick_id:<10} {rnd:<8} {team:<20} {winner_str:<8} {str(elim):<6} {str(auto):<6} {str(reason):<25} {ts}")
        if edited:
            print(f"{'':10} {'':8} {'':20} (last edited: {edited})")

    # Specifically check Round 7
    print(f"\n{'─'*60}")
    print("ROUND 7 DETAIL")
    print(f"{'─'*60}")
    cur.execute("""
        SELECT r.id, r.round_number FROM rounds r WHERE r.round_number = 7
    """)
    round7_rows = cur.fetchall()
    if not round7_rows:
        print("No Round 7 found in database.")
    for round_db_id, rnd_num in round7_rows:
        cur.execute("""
            SELECT id, team_picked, is_winner, is_eliminated, auto_assigned, timestamp, last_edited_at
            FROM picks
            WHERE player_id = %s AND round_id = %s
            ORDER BY timestamp
        """, (player_id, round_db_id))
        r7_picks = cur.fetchall()
        if not r7_picks:
            print(f"  Round {rnd_num} (db id={round_db_id}): NO PICKS FOUND")
        else:
            for row in r7_picks:
                pick_id, team, winner, elim, auto, ts, edited = row
                print(f"  Pick id={pick_id}: team='{team}', is_winner={winner}, is_eliminated={elim}, auto={auto}, created={ts}, edited={edited}")

        if len(r7_picks) > 1:
            print(f"\n  *** DUPLICATE PICKS DETECTED for Round {rnd_num}! ({len(r7_picks)} picks) ***")
        elif len(r7_picks) == 1 and r7_picks[0][1] != 'Arsenal':
            print(f"\n  *** INCORRECT PICK: expected 'Arsenal', found '{r7_picks[0][1]}' ***")
        elif len(r7_picks) == 1:
            print(f"\n  Pick looks correct (Arsenal).")

    cur.close()
    conn.close()
    print("\n" + "=" * 60)
    print("Audit complete. Run apply_paul_fix.py --fix to apply corrections.")
    print("=" * 60 + "\n")


if __name__ == '__main__':
    main()
