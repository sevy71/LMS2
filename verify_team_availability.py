#!/usr/bin/env python3
"""
Verify that all teams are available again after a rollover
"""

import psycopg2
from urllib.parse import urlparse

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

def verify_team_availability():
    """Verify team availability per cycle"""
    conn_params = parse_database_url(DATABASE_URL)

    try:
        print("Connecting to database...")
        conn = psycopg2.connect(**conn_params)
        cursor = conn.cursor()

        print("\n=== TEAM AVAILABILITY VERIFICATION ===\n")

        # Get a sample player
        cursor.execute("SELECT id, name FROM players WHERE status = 'active' LIMIT 1")
        player = cursor.fetchone()

        if not player:
            print("No active players found.")
            return

        player_id, player_name = player
        print(f"Checking team availability for: {player_name}")

        # Get current round
        cursor.execute("""
            SELECT id, round_number, cycle_number, status
            FROM rounds
            WHERE status IN ('active', 'pending')
            ORDER BY id DESC
            LIMIT 1
        """)
        current_round = cursor.fetchone()

        if not current_round:
            print("No active/pending round found.")
            return

        round_id, round_number, cycle_number, status = current_round
        print(f"Current round: Round {round_number}, Cycle {cycle_number}, Status: {status}\n")

        # Get all teams in current round
        cursor.execute("""
            SELECT DISTINCT home_team FROM fixtures WHERE round_id = %s
            UNION
            SELECT DISTINCT away_team FROM fixtures WHERE round_id = %s
        """, (round_id, round_id))
        all_teams = [row[0] for row in cursor.fetchall()]
        print(f"Total teams in current round: {len(all_teams)}")

        # Get teams used by this player in ALL cycles
        cursor.execute("""
            SELECT DISTINCT p.team_picked, r.cycle_number
            FROM picks p
            JOIN rounds r ON p.round_id = r.id
            WHERE p.player_id = %s
            ORDER BY r.cycle_number, p.team_picked
        """, (player_id,))
        all_picks = cursor.fetchall()

        # Group by cycle
        picks_by_cycle = {}
        for team, cycle in all_picks:
            if cycle not in picks_by_cycle:
                picks_by_cycle[cycle] = []
            picks_by_cycle[cycle].append(team)

        print("\n--- Teams Used Per Cycle ---")
        for cycle in sorted(picks_by_cycle.keys()):
            teams = picks_by_cycle[cycle]
            print(f"Cycle {cycle}: {len(teams)} teams used")
            print(f"  Teams: {', '.join(teams)}")

        # Get teams used in CURRENT cycle only
        cursor.execute("""
            SELECT DISTINCT p.team_picked
            FROM picks p
            JOIN rounds r ON p.round_id = r.id
            WHERE p.player_id = %s AND r.cycle_number = %s
        """, (player_id, cycle_number))
        current_cycle_teams = [row[0] for row in cursor.fetchall()]

        print(f"\n--- Current Cycle ({cycle_number}) Restrictions ---")
        print(f"Teams used this cycle: {len(current_cycle_teams)}")
        if current_cycle_teams:
            print(f"  Teams: {', '.join(current_cycle_teams)}")
        else:
            print(f"  No teams used yet this cycle - ALL TEAMS AVAILABLE!")

        # Calculate available teams
        available_teams = [t for t in all_teams if t not in current_cycle_teams]
        print(f"\nAvailable teams for next pick: {len(available_teams)}/{len(all_teams)}")

        if cycle_number > 1 and len(current_cycle_teams) == 0:
            print("\n✅ ROLLOVER VERIFICATION PASSED!")
            print(f"   - We're in Cycle {cycle_number} (a rollover has occurred)")
            print(f"   - No teams have been used yet in this cycle")
            print(f"   - All {len(available_teams)} teams are available for selection")
        elif len(available_teams) == len(all_teams):
            print("\n✅ ALL TEAMS AVAILABLE!")
            print(f"   This is either the first round or a new cycle.")
        else:
            print(f"\n📊 NORMAL GAME STATE")
            print(f"   {len(available_teams)} teams still available in Cycle {cycle_number}")

        cursor.close()
        conn.close()

    except Exception as e:
        print(f"\n❌ Error: {e}")

if __name__ == '__main__':
    print("\n" + "="*60)
    print("    TEAM AVAILABILITY VERIFICATION")
    print("="*60)
    verify_team_availability()