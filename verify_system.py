#!/usr/bin/env python3
"""
Verify the Last Man Standing system is working correctly with historical data
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'lms_automation'))

from app import app
from models import db, Player, Round, Pick

def verify_system():
    """Comprehensive system verification"""
    
    with app.app_context():
        print("🔍 SYSTEM VERIFICATION REPORT")
        print("=" * 50)
        
        # Basic counts
        total_players = Player.query.count()
        active_players = Player.query.filter_by(status='active').count()
        eliminated_players = Player.query.filter_by(status='eliminated').count()
        total_rounds = Round.query.count()
        total_picks = Pick.query.count()
        
        print(f"📊 Database Summary:")
        print(f"   Players: {total_players} total ({active_players} active, {eliminated_players} eliminated)")
        print(f"   Rounds: {total_rounds}")
        print(f"   Picks: {total_picks}")
        
        # Round status
        print(f"\n🏆 Round Status:")
        rounds = Round.query.order_by(Round.round_number).all()
        for round_obj in rounds:
            picks_count = Pick.query.filter_by(round_id=round_obj.id).count()
            winners_count = Pick.query.filter_by(round_id=round_obj.id, is_winner=True).count()
            print(f"   Round {round_obj.round_number}: {round_obj.status} ({picks_count} picks, {winners_count} winners)")
        
        # Active players for next round
        print(f"\n🎯 Players ready for Round 3:")
        active_list = Player.query.filter_by(status='active').order_by(Player.name).all()
        
        # Group by previous picks to show variety
        team_usage = {}
        for player in active_list:
            used_teams = []
            picks = Pick.query.filter_by(player_id=player.id).all()
            for pick in picks:
                used_teams.append(pick.team_picked)
            team_usage[player.name] = used_teams
        
        for i, player in enumerate(active_list[:10], 1):  # Show first 10
            used_teams_str = ", ".join(team_usage[player.name])
            print(f"   {i:2}. {player.name:<15} (used: {used_teams_str})")
        
        if len(active_list) > 10:
            print(f"   ... and {len(active_list) - 10} more players")
        
        # Show team usage statistics
        print(f"\n📈 Team Usage Statistics:")
        team_counts = {}
        all_picks = Pick.query.all()
        for pick in all_picks:
            team = pick.team_picked.lower()
            team_counts[team] = team_counts.get(team, 0) + 1
        
        sorted_teams = sorted(team_counts.items(), key=lambda x: x[1], reverse=True)
        for team, count in sorted_teams[:10]:  # Top 10 most picked teams
            print(f"   {team.title():<15}: {count} picks")
        
        # Verify elimination logic
        print(f"\n🔍 Elimination Verification:")
        round1_winners = {"liverpool", "spurs", "sunderland", "man city", "forest", "arsenal", "leeds"}
        round2_winners = {"chelsea", "spurs", "burnley", "brentford", "bournemouth", "arsenal", "everton", "liverpool"}
        
        correct_eliminations = 0
        incorrect_eliminations = 0
        
        for player in Player.query.all():
            r1_pick = Pick.query.filter_by(player_id=player.id, round_id=1).first()
            r2_pick = Pick.query.filter_by(player_id=player.id, round_id=2).first()
            
            if not r1_pick:
                continue
                
            r1_team = r1_pick.team_picked.lower().strip()
            should_survive_r1 = r1_team in round1_winners
            
            if not should_survive_r1:
                # Should be eliminated after R1
                if player.status == 'eliminated' and not r2_pick:
                    correct_eliminations += 1
                else:
                    incorrect_eliminations += 1
            else:
                # Survived R1, check R2
                if r2_pick:
                    r2_team = r2_pick.team_picked.lower().strip()
                    should_survive_r2 = r2_team in round2_winners
                    
                    if should_survive_r2 and player.status == 'active':
                        correct_eliminations += 1
                    elif not should_survive_r2 and player.status == 'eliminated':
                        correct_eliminations += 1
                    else:
                        incorrect_eliminations += 1
                else:
                    # No R2 pick but survived R1 - should be eliminated
                    if player.status == 'eliminated':
                        correct_eliminations += 1
                    else:
                        incorrect_eliminations += 1
        
        print(f"   Correct eliminations: {correct_eliminations}")
        print(f"   Incorrect eliminations: {incorrect_eliminations}")
        
        if incorrect_eliminations == 0:
            print("   ✅ All eliminations are correct!")
        else:
            print("   ⚠️  Some eliminations may be incorrect")
        
        # Check if ready for next round
        current_round = Round.query.filter_by(status='active').first()
        if current_round:
            print(f"\n🚀 System Status: READY")
            print(f"   Active round: Round {current_round.round_number}")
            print(f"   {active_players} players ready to make picks")
            print(f"   System is ready to continue the competition!")
        else:
            print(f"\n⚠️  System Status: NO ACTIVE ROUND")
            print("   Please create an active round to continue the game")

if __name__ == "__main__":
    verify_system()