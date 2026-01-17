#!/usr/bin/env python3
"""
Fix rollover scenario where all players are eliminated but game needs to continue.
This script will:
1. Check current player statuses
2. Identify players who should advance (were active before last elimination)
3. Reset their status to 'active' for the new round
"""

import os
import sys
from datetime import datetime

# Add the project directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lms_automation.app import app, db
from lms_automation.models import Player, Round, Pick

def analyze_situation():
    """Analyze the current game state"""
    print("\n=== CURRENT GAME STATE ===\n")

    # Check all players and their statuses
    all_players = Player.query.all()
    active_players = Player.query.filter_by(status='active').all()
    eliminated_players = Player.query.filter_by(status='eliminated').all()
    winner_players = Player.query.filter_by(status='winner').all()

    print(f"Total players: {len(all_players)}")
    print(f"Active players: {len(active_players)}")
    print(f"Eliminated players: {len(eliminated_players)}")
    print(f"Winners: {len(winner_players)}")

    # Check rounds
    all_rounds = Round.query.order_by(Round.created_at.desc()).all()
    active_round = Round.query.filter_by(status='active').first()
    completed_rounds = Round.query.filter_by(status='completed').order_by(Round.created_at.desc()).all()

    print(f"\nTotal rounds: {len(all_rounds)}")
    print(f"Active round: {active_round.round_number if active_round else 'None'}")
    print(f"Completed rounds: {len(completed_rounds)}")

    if completed_rounds:
        last_completed = completed_rounds[0]
        print(f"\nLast completed round: Round {last_completed.round_number}")

        # Find players who had picks in the last completed round
        last_round_picks = Pick.query.filter_by(round_id=last_completed.id).all()
        players_with_picks = {pick.player for pick in last_round_picks}

        # Separate by elimination status
        winners_last_round = [p for p in players_with_picks if any(
            pick.is_winner for pick in last_round_picks if pick.player_id == p.id
        )]
        losers_last_round = [p for p in players_with_picks if any(
            pick.is_eliminated for pick in last_round_picks if pick.player_id == p.id
        )]

        print(f"Players who won in last round: {len(winners_last_round)}")
        print(f"Players who lost in last round: {len(losers_last_round)}")

        if winners_last_round:
            print("\nWinners from last round (should be active):")
            for p in winners_last_round:
                print(f"  - {p.name} (current status: {p.status})")

    return {
        'active_players': active_players,
        'eliminated_players': eliminated_players,
        'last_round_winners': winners_last_round if completed_rounds else [],
        'active_round': active_round,
        'last_completed_round': completed_rounds[0] if completed_rounds else None
    }

def fix_rollover_scenario(dry_run=True):
    """Fix the rollover scenario by reactivating appropriate players"""

    state = analyze_situation()

    print("\n=== ROLLOVER FIX ===\n")

    if state['active_players']:
        print("There are already active players. No fix needed.")
        return

    if not state['last_completed_round']:
        print("No completed rounds found. Cannot determine who should advance.")
        return

    # In a rollover scenario, we need to reactivate players who should continue
    # This typically means the winners from the last completed round

    players_to_reactivate = []

    # Option 1: If there were winners in the last round who are now eliminated, reactivate them
    for player in state['last_round_winners']:
        if player.status == 'eliminated':
            players_to_reactivate.append(player)

    # Option 2: If no winners (all lost), this is a true rollover - need to decide who advances
    if not players_to_reactivate and not state['last_round_winners']:
        print("TRUE ROLLOVER DETECTED: All players lost in the last round!")
        print("\nIn a rollover scenario, typically ALL players who participated advance.")

        # Get all players who had picks in the last round
        last_round_picks = Pick.query.filter_by(round_id=state['last_completed_round'].id).all()
        players_who_participated = {pick.player for pick in last_round_picks}

        # Reactivate all who participated (they all advance in rollover)
        for player in players_who_participated:
            if player.status == 'eliminated':
                players_to_reactivate.append(player)

    if not players_to_reactivate:
        print("No players found to reactivate. Manual intervention may be needed.")
        return

    print(f"\nPlayers to reactivate: {len(players_to_reactivate)}")
    for player in players_to_reactivate:
        print(f"  - {player.name}")

    if dry_run:
        print("\n[DRY RUN] No changes made. Run with --execute to apply changes.")
    else:
        print("\nReactivating players...")
        for player in players_to_reactivate:
            player.status = 'active'
            db.session.add(player)

        db.session.commit()
        print(f"Successfully reactivated {len(players_to_reactivate)} players!")

        # Verify the fix
        active_count = Player.query.filter_by(status='active').count()
        print(f"\nNew active player count: {active_count}")

def main():
    """Main entry point"""
    with app.app_context():
        # Check if we should execute or just dry run
        execute = '--execute' in sys.argv

        if not execute:
            print("=" * 60)
            print("ROLLOVER SCENARIO FIX - DRY RUN MODE")
            print("=" * 60)
            print("\nThis script will analyze and fix the rollover scenario.")
            print("Currently running in DRY RUN mode (no changes will be made).")
            print("\nTo apply the fix, run: python fix_rollover.py --execute")
        else:
            print("=" * 60)
            print("ROLLOVER SCENARIO FIX - EXECUTE MODE")
            print("=" * 60)

        fix_rollover_scenario(dry_run=not execute)

if __name__ == '__main__':
    main()