#!/usr/bin/env python3
"""
Fix rollover scenario by calling the API endpoints
This reactivates all eliminated players for the new round
"""

import requests
import json
import sys

# Update this if your app is running on a different port
BASE_URL = "http://localhost:5000"

def check_players():
    """Get current player statuses"""
    try:
        response = requests.get(f"{BASE_URL}/api/players")
        if response.status_code == 200:
            players = response.json()
            active = [p for p in players if p.get('status') == 'active']
            eliminated = [p for p in players if p.get('status') == 'eliminated']

            print("\n=== CURRENT PLAYER STATUS ===")
            print(f"Total players: {len(players)}")
            print(f"Active players: {len(active)}")
            print(f"Eliminated players: {len(eliminated)}")

            return players, active, eliminated
        else:
            print(f"Error getting players: {response.status_code}")
            return [], [], []
    except requests.exceptions.ConnectionError:
        print("\n❌ ERROR: Cannot connect to the Flask app!")
        print("Make sure the app is running with: python3 lms_automation/app.py")
        return [], [], []

def reactivate_player(player_id, player_name):
    """Reactivate a single player"""
    try:
        response = requests.put(
            f"{BASE_URL}/api/players/{player_id}/status",
            json={"status": "active"},
            headers={"Content-Type": "application/json"}
        )
        if response.status_code == 200:
            print(f"  ✓ Reactivated: {player_name}")
            return True
        else:
            print(f"  ✗ Failed to reactivate {player_name}: {response.status_code}")
            return False
    except Exception as e:
        print(f"  ✗ Error reactivating {player_name}: {e}")
        return False

def main():
    print("\n========================================")
    print("    ROLLOVER SCENARIO FIX UTILITY")
    print("========================================")

    # Check current status
    all_players, active_players, eliminated_players = check_players()

    if not all_players:
        return

    if len(active_players) == 0 and len(eliminated_players) > 0:
        print("\n!!! ROLLOVER SCENARIO DETECTED !!!")
        print("All players are eliminated but the game needs to continue.")

        if '--fix' in sys.argv:
            print("\n🔧 Fixing: Reactivating all eliminated players...")

            success_count = 0
            for player in eliminated_players:
                if reactivate_player(player['id'], player['name']):
                    success_count += 1

            print(f"\n✅ SUCCESS! Reactivated {success_count} players.")
            print("You should now be able to send pick links!")

            # Verify the fix
            print("\n=== VERIFICATION ===")
            _, new_active, _ = check_players()
            if len(new_active) > 0:
                print("✅ Confirmed: Players are now active!")
            else:
                print("⚠️  Warning: Players may still not be active. Check the admin dashboard.")
        else:
            print("\n TO FIX THIS ISSUE, RUN:")
            print("   python3 fix_rollover_api.py --fix")
            print("\nThis will reactivate all eliminated players for the new round.")

    elif len(active_players) > 0:
        print("\n✅ No issue detected!")
        print(f"{len(active_players)} players are already active.")
        print("You should be able to send pick links.")
    else:
        print("\n⚠️  No players found in the system.")
        print("You may need to add players first.")

if __name__ == '__main__':
    main()