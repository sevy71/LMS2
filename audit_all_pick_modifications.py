#!/usr/bin/env python3
"""
AUDIT: Find all modified picks and surface suspicious changes.

Queries picks that have last_edited_at set (i.e. were changed after creation),
groups them by player, and flags cases where the team was likely changed after
fixture results were already known — a strong signal of data corruption.

Usage:
    python3 audit_all_pick_modifications.py
    python3 audit_all_pick_modifications.py --suspicious-only
"""

import psycopg2
from urllib.parse import urlparse
import os
import sys
from collections import defaultdict

DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    "postgresql://postgres:REDACTED@REDACTED/railway"
)

SUSPICIOUS_ONLY = '--suspicious-only' in sys.argv


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


def fmt(val):
    return str(val) if val is not None else 'NULL'


def main():
    print("\n" + "=" * 70)
    print("  PICK MODIFICATION AUDIT")
    print("=" * 70)

    conn = get_conn()
    cur = conn.cursor()

    # ── All modified picks ────────────────────────────────────────────────────
    cur.execute("""
        SELECT
            pk.id,
            pk.player_id,
            pl.name            AS player_name,
            pl.status          AS player_status,
            pk.round_id,
            r.round_number,
            r.status           AS round_status,
            pk.team_picked,
            pk.is_winner,
            pk.is_eliminated,
            pk.auto_assigned,
            pk.auto_reason,
            pk.timestamp       AS created_at,
            pk.last_edited_at,
            -- How long after creation was the edit?
            EXTRACT(EPOCH FROM (pk.last_edited_at - pk.timestamp)) AS edit_lag_seconds,
            -- Were fixture results already in when the pick was edited?
            -- Proxy: if is_winner is NOT NULL, results were processed before/around the edit
            pk.is_winner IS NOT NULL AS result_already_set
        FROM picks pk
        JOIN players pl ON pl.id = pk.player_id
        JOIN rounds r ON r.id = pk.round_id
        WHERE pk.last_edited_at IS NOT NULL
        ORDER BY pl.name, r.round_number, pk.last_edited_at
    """)
    rows = cur.fetchall()

    if not rows:
        print("\nNo picks with last_edited_at found. No modifications on record.")
        cur.close()
        conn.close()
        return

    print(f"\nTotal modified picks: {len(rows)}\n")

    # Group by player
    by_player = defaultdict(list)
    for row in rows:
        by_player[row[2]].append(row)

    suspicious_count = 0
    all_suspicious = []

    for player_name in sorted(by_player.keys()):
        player_rows = by_player[player_name]
        player_status = player_rows[0][3]

        # Determine if any are suspicious
        suspicious_in_player = [
            r for r in player_rows
            if r[15]  # result_already_set
        ]

        if SUSPICIOUS_ONLY and not suspicious_in_player:
            continue

        print(f"{'─' * 70}")
        print(f"Player: {player_name}  (status={player_status})  — {len(player_rows)} modified pick(s)")

        for row in player_rows:
            (pick_id, player_id, pname, pstatus,
             round_id, round_number, round_status,
             team_picked, is_winner, is_eliminated,
             auto_assigned, auto_reason,
             created_at, last_edited_at, edit_lag_seconds,
             result_already_set) = row

            # Classify edit lag
            lag_s = int(edit_lag_seconds) if edit_lag_seconds is not None else 0
            if lag_s < 60:
                lag_label = f"{lag_s}s (immediate)"
            elif lag_s < 3600:
                lag_label = f"{lag_s // 60}m {lag_s % 60}s"
            elif lag_s < 86400:
                lag_label = f"{lag_s // 3600}h {(lag_s % 3600) // 60}m"
            else:
                lag_label = f"{lag_s // 86400}d {(lag_s % 86400) // 3600}h"

            is_suspicious = bool(result_already_set)
            flag = "  *** SUSPICIOUS ***" if is_suspicious else ""

            print(f"\n  Pick id={pick_id}  Round {round_number} ({round_status}){flag}")
            print(f"    team_picked   = '{team_picked}'")
            print(f"    is_winner     = {fmt(is_winner)}   is_eliminated = {fmt(is_eliminated)}")
            print(f"    auto_assigned = {auto_assigned} ({auto_reason})")
            print(f"    created_at    = {created_at}")
            print(f"    last_edited_at= {last_edited_at}  (lag: {lag_label})")
            print(f"    result_set_at_edit_time = {result_already_set}")

            if is_suspicious:
                suspicious_count += 1
                all_suspicious.append({
                    'pick_id': pick_id,
                    'player': player_name,
                    'round': round_number,
                    'team': team_picked,
                    'is_winner': is_winner,
                    'is_eliminated': is_eliminated,
                    'created_at': created_at,
                    'edited_at': last_edited_at,
                    'lag': lag_label,
                })

    # ── Summary of suspicious picks ───────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"SUMMARY")
    print(f"{'=' * 70}")
    print(f"Total modified picks : {len(rows)}")
    print(f"Suspicious (result already set when edited): {suspicious_count}")

    if all_suspicious:
        print(f"\nSuspicious picks requiring investigation:")
        print(f"  {'Pick ID':<10} {'Player':<22} {'Round':<8} {'Team':<22} {'Winner':<8} {'Edit lag'}")
        print(f"  {'─'*10} {'─'*22} {'─'*8} {'─'*22} {'─'*8} {'─'*12}")
        for s in all_suspicious:
            winner_str = {True: 'YES', False: 'NO', None: 'pending'}.get(s['is_winner'], '?')
            print(f"  {s['pick_id']:<10} {s['player']:<22} {s['round']:<8} {s['team']:<22} {winner_str:<8} {s['lag']}")

        print(f"\nThese picks were edited AFTER match results were already recorded.")
        print(f"Verify each one against original pick evidence (screenshots, WhatsApp logs).")
    else:
        print(f"\nNo suspicious modifications found.")

    # ── Additional pattern check: picks edited to a losing team ──────────────
    losing_edits = [s for s in all_suspicious if s['is_winner'] is False]
    if losing_edits:
        print(f"\n{'─' * 70}")
        print(f"HIGH PRIORITY: {len(losing_edits)} pick(s) edited AFTER results where is_winner=False:")
        print(f"(These players may have been wrongly eliminated)")
        for s in losing_edits:
            print(f"  Pick {s['pick_id']} — {s['player']} Round {s['round']}: team='{s['team']}', edited {s['lag']} after creation")

    cur.close()
    conn.close()
    print("\n" + "=" * 70 + "\n")


if __name__ == '__main__':
    main()
