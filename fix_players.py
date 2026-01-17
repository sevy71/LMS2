#!/usr/bin/env python3
"""
Emergency fix for rollover scenario
Run with: python3 -m flask --app lms_automation.app shell < fix_players.py
Or execute the commands directly in Flask shell
"""

# This script should be run in Flask shell context
# Start Flask shell with: python3 -m flask --app lms_automation.app shell

from lms_automation.models import Player, db

print("\n=== ROLLOVER FIX - CHECKING PLAYER STATUSES ===\n")

# Get current player counts
all_players = Player.query.all()
active_players = Player.query.filter_by(status='active').all()
eliminated_players = Player.query.filter_by(status='eliminated').all()

print(f"Total players: {len(all_players)}")
print(f"Active players: {len(active_players)}")
print(f"Eliminated players: {len(eliminated_players)}")

if len(active_players) == 0 and len(eliminated_players) > 0:
    print("\n!!! ROLLOVER SCENARIO DETECTED !!!")
    print("All players are eliminated. Reactivating all players...")

    for player in eliminated_players:
        player.status = 'active'
        print(f"  ✓ Reactivating: {player.name}")

    db.session.commit()

    # Verify the fix
    new_active_count = Player.query.filter_by(status='active').count()
    print(f"\n✅ SUCCESS! {new_active_count} players are now active.")
    print("You can now send pick links!")

elif len(active_players) > 0:
    print("\n✅ Players are already active. No fix needed.")

else:
    print("\n⚠️  No players found in the system.")

print("\nDone!")
exit()