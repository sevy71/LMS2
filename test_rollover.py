#!/usr/bin/env python3
"""
Test script to verify rollover handling functionality
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set environment variable to use local database for testing
os.environ['DATABASE_URL'] = 'sqlite:///test_rollover.db'

from lms_automation.app import app, db, handle_rollover_scenario
from lms_automation.models import Player, Round, Pick
from datetime import datetime

def test_rollover_handling():
    """Test the rollover handling functionality"""
    with app.app_context():
        # Clean database for testing
        db.drop_all()
        db.create_all()

        print("=== TESTING ROLLOVER HANDLING ===\n")

        # 1. Create test players
        print("1. Creating test players...")
        players = []
        for i in range(5):
            player = Player(name=f"Test Player {i+1}", status='active')
            db.session.add(player)
            players.append(player)

        db.session.commit()
        print(f"   Created {len(players)} test players")

        # 2. Create a round and picks
        print("\n2. Creating test round...")
        test_round = Round(
            round_number=1,
            status='active',
            start_date=datetime.now()
        )
        db.session.add(test_round)
        db.session.commit()
        print(f"   Created round {test_round.round_number}")

        # 3. Create picks for all players
        print("\n3. Creating picks for all players...")
        for player in players:
            pick = Pick(
                player_id=player.id,
                round_id=test_round.id,
                team_picked="Test Team",
                is_winner=False,
                is_eliminated=True  # All lose
            )
            db.session.add(pick)

        db.session.commit()
        print("   All players have losing picks")

        # 4. Simulate round completion with all players eliminated
        print("\n4. Simulating all players being eliminated...")
        for player in players:
            player.status = 'eliminated'

        test_round.status = 'completed'
        db.session.commit()

        active_before = Player.query.filter_by(status='active').count()
        eliminated_before = Player.query.filter_by(status='eliminated').count()
        print(f"   Before rollover: {active_before} active, {eliminated_before} eliminated")

        # 5. Test rollover handling
        print("\n5. Testing rollover handling...")
        rollover_result = handle_rollover_scenario()

        if rollover_result:
            print("   ✅ Rollover detected and handled!")

            active_after = Player.query.filter_by(status='active').count()
            eliminated_after = Player.query.filter_by(status='eliminated').count()

            print(f"   After rollover: {active_after} active, {eliminated_after} eliminated")

            if active_after == len(players) and eliminated_after == 0:
                print("\n✅ TEST PASSED: All players were correctly reactivated!")
            else:
                print("\n❌ TEST FAILED: Not all players were reactivated")
        else:
            print("   ❌ Rollover was not detected")
            print("\n❌ TEST FAILED")

        # Clean up test database
        db.drop_all()
        os.remove('test_rollover.db') if os.path.exists('test_rollover.db') else None

if __name__ == '__main__':
    test_rollover_handling()