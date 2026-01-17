#!/usr/bin/env python3
"""
Fix the current round numbering after rollover
This will update the current active round to be Round 1 of Cycle 2
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
        'database': parsed.path[1:],
        'user': parsed.username,
        'password': parsed.password
    }

def fix_current_round():
    """Fix the current round numbering"""
    conn_params = parse_database_url(DATABASE_URL)

    try:
        print("Connecting to database...")
        conn = psycopg2.connect(**conn_params)
        cursor = conn.cursor()

        print("\n=== CHECKING CURRENT ROUNDS ===")

        # Get the last completed round
        cursor.execute("""
            SELECT id, round_number, cycle_number, status
            FROM rounds
            WHERE status = 'completed'
            ORDER BY id DESC
            LIMIT 1
        """)
        last_completed = cursor.fetchone()

        if last_completed:
            print(f"Last completed round: ID={last_completed[0]}, Round={last_completed[1]}, Cycle={last_completed[2]}, Status={last_completed[3]}")

        # Get the current active/pending round
        cursor.execute("""
            SELECT id, round_number, cycle_number, status
            FROM rounds
            WHERE status IN ('active', 'pending')
            ORDER BY id DESC
            LIMIT 1
        """)
        current_round = cursor.fetchone()

        if current_round:
            print(f"Current round: ID={current_round[0]}, Round={current_round[1]}, Cycle={current_round[2]}, Status={current_round[3]}")

            if '--fix' in sys.argv:
                print("\n🔧 APPLYING FIX...")

                # Determine the correct cycle number
                if last_completed:
                    last_cycle = last_completed[2] or 1
                    next_cycle = last_cycle + 1
                else:
                    next_cycle = 2  # Default to cycle 2 if no completed rounds

                # Update the current round to be Round 1 of the next cycle
                cursor.execute("""
                    UPDATE rounds
                    SET round_number = 1, cycle_number = %s
                    WHERE id = %s
                """, (next_cycle, current_round[0]))

                conn.commit()

                print(f"\n✅ SUCCESS! Updated round {current_round[0]} to:")
                print(f"   - Round Number: 1")
                print(f"   - Cycle Number: {next_cycle}")
                print(f"\nPick messages will now say 'Round 1' instead of 'Round {current_round[1]}'!")

            else:
                print("\n TO FIX THIS, RUN:")
                print("   python3 fix_current_round.py --fix")
                print(f"\nThis will update the current round to be Round 1 of the next cycle.")

        else:
            print("\nNo active or pending rounds found.")

        cursor.close()
        conn.close()

    except Exception as e:
        print(f"\n❌ Error: {e}")

if __name__ == '__main__':
    print("\n" + "="*50)
    print("    FIX CURRENT ROUND NUMBERING")
    print("="*50)
    fix_current_round()