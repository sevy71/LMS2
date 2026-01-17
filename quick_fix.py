#!/usr/bin/env python3
"""
Quick fix for rollover scenario - reactivates all eliminated players
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set environment variable to use local database
os.environ['DATABASE_URL'] = 'sqlite:///lms.db'

from lms_automation.app import app, db
from lms_automation.models import Player, Round, Pick

def main():
    with app.app_context():
        print("\n=== CHECKING PLAYER STATUSES ===\n")

        # Get current player counts
        all_players = Player.query.all()
        active_players = Player.query.filter_by(status='active').all()
        eliminated_players = Player.query.filter_by(status='eliminated').all()

        print(f"Total players: {len(all_players)}")
        print(f"Active players: {len(active_players)}")
        print(f"Eliminated players: {len(eliminated_players)}")

        if len(active_players) == 0 and len(eliminated_players) > 0:
            print("\n!!! ROLLOVER SCENARIO DETECTED !!!")
            print("All players are eliminated but the game needs to continue.")

            # Check for --fix flag
            if '--fix' in sys.argv:
                print("\nReactivating all eliminated players...")

                for player in eliminated_players:
                    player.status = 'active'
                    db.session.add(player)
                    print(f"  ✓ Reactivated: {player.name}")

                db.session.commit()

                # Verify the fix
                new_active_count = Player.query.filter_by(status='active').count()
                print(f"\n✅ SUCCESS! {new_active_count} players are now active.")
                print("\nYou should now be able to send pick links!")
            else:
                print("\n TO FIX: Run this command:")
                print("   python3 quick_fix.py --fix")
                print("\nThis will reactivate all eliminated players for the new round.")

        elif len(active_players) > 0:
            print("\n✅ Players are already active. No fix needed.")
            print("You should be able to send pick links.")

        else:
            print("\n⚠️  No players found in the system.")

if __name__ == '__main__':
    main()