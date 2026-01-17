#!/usr/bin/env python3
"""
EMERGENCY FIX for rollover scenario
This script directly connects to the database to reactivate players
"""

import psycopg2
from urllib.parse import urlparse
import sys

# Database URL from your .env.local file
DATABASE_URL = "postgresql://postgres:RebSnrDxmegHjWEdnokgimqrIRAvtUuj@yamanote.proxy.rlwy.net:43793/railway"

def parse_database_url(url):
    """Parse database URL into connection parameters"""
    parsed = urlparse(url)
    return {
        'host': parsed.hostname,
        'port': parsed.port,
        'database': parsed.path[1:],  # Remove leading '/'
        'user': parsed.username,
        'password': parsed.password
    }

def check_and_fix_players():
    """Check player statuses and fix rollover scenario"""

    conn_params = parse_database_url(DATABASE_URL)

    try:
        # Connect to database
        print("Connecting to database...")
        conn = psycopg2.connect(**conn_params)
        cursor = conn.cursor()

        # Check current player statuses
        print("\n=== CHECKING PLAYER STATUSES ===")

        cursor.execute("SELECT COUNT(*) FROM players")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM players WHERE status = 'active'")
        active = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM players WHERE status = 'eliminated'")
        eliminated = cursor.fetchone()[0]

        print(f"Total players: {total}")
        print(f"Active players: {active}")
        print(f"Eliminated players: {eliminated}")

        if active == 0 and eliminated > 0:
            print("\n!!! ROLLOVER SCENARIO DETECTED !!!")
            print("All players are eliminated but the game needs to continue.")

            if '--fix' in sys.argv:
                print("\n🔧 APPLYING FIX...")

                # Get eliminated player names for logging
                cursor.execute("SELECT id, name FROM players WHERE status = 'eliminated'")
                eliminated_players = cursor.fetchall()

                # Reactivate all eliminated players
                cursor.execute("UPDATE players SET status = 'active' WHERE status = 'eliminated'")
                updated_count = cursor.rowcount

                # Commit the changes
                conn.commit()

                print(f"\n✅ SUCCESS! Reactivated {updated_count} players:")
                for player_id, name in eliminated_players:
                    print(f"  ✓ {name}")

                # Verify the fix
                cursor.execute("SELECT COUNT(*) FROM players WHERE status = 'active'")
                new_active = cursor.fetchone()[0]
                print(f"\nVerification: {new_active} players are now active!")
                print("\n🎯 You should now be able to send pick links!")

            else:
                print("\n TO FIX THIS ISSUE, RUN:")
                print("   python3 emergency_fix.py --fix")
                print("\nThis will reactivate all eliminated players for the new round.")

        elif active > 0:
            print("\n✅ No issue detected!")
            print(f"{active} players are already active.")
            print("You should be able to send pick links.")

        else:
            print("\n⚠️  No players found in the database.")

        cursor.close()
        conn.close()

    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("\nMake sure the database connection details are correct.")

if __name__ == '__main__':
    print("\n" + "="*50)
    print("    EMERGENCY ROLLOVER FIX")
    print("="*50)
    check_and_fix_players()