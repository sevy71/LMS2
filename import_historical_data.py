#!/usr/bin/env python3
"""
Import historical Last Man Standing data for Rounds 1 and 2
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'lms_automation'))

from app import app
from models import db, Player, Round, Pick

# Historical data
historical_data = [
    {"id": 1, "name": "A. Frost", "round1": "liverpool", "round2": "Arsenal"},
    {"id": 2, "name": "A. Urmson", "round1": "liverpool", "round2": "Arsenal"},
    {"id": 3, "name": "B. Wood", "round1": "liverpool", "round2": "Arsenal"},
    {"id": 4, "name": "C. Hollows", "round1": "spurs", "round2": "Arsenal"},
    {"id": 5, "name": "D. Brindle", "round1": "Forest", "round2": "Arsenal"},
    {"id": 6, "name": "D. Groves", "round1": "liverpool", "round2": "Arsenal"},
    {"id": 7, "name": "G. Leigh", "round1": "liverpool", "round2": "Arsenal"},
    {"id": 8, "name": "J. Burn", "round1": "liverpool", "round2": "Arsenal"},
    {"id": 9, "name": "j. Cruickshank", "round1": "liverpool", "round2": "Arsenal"},
    {"id": 10, "name": "P. Riley", "round1": "liverpool", "round2": "Arsenal"},
    {"id": 11, "name": "P. Warby", "round1": "Forest", "round2": "Arsenal"},
    {"id": 12, "name": "R. Amis", "round1": "liverpool", "round2": "Arsenal"},
    {"id": 13, "name": "R. Burrows", "round1": "liverpool", "round2": "Arsenal"},
    {"id": 14, "name": "T. Leigh", "round1": "liverpool", "round2": "Arsenal"},
    {"id": 15, "name": "V. Hughes", "round1": "spurs", "round2": "Arsenal"},
    {"id": 16, "name": "G. Boyle", "round1": "spurs", "round2": "Chelsea"},
    {"id": 17, "name": "J. Vertigans", "round1": "Forest", "round2": "Chelsea"},
    {"id": 18, "name": "J. Winning", "round1": "Liverpool", "round2": "Chelsea"},
    {"id": 19, "name": "M. Waight", "round1": "liverpool", "round2": "Chelsea"},
    {"id": 20, "name": "S.Hall", "round1": "Spurs", "round2": "Chelsea"},
    {"id": 21, "name": "J. Lyne", "round1": "spurs", "round2": "Liverpool"},
    {"id": 22, "name": "S. Shooter", "round1": "leeds", "round2": "Liverpool"},
    {"id": 23, "name": "D. Foley", "round1": "Arsenal", "round2": "Man City"},
    {"id": 24, "name": "A. Ferguson", "round1": "spurs", "round2": "Man utd"},
    {"id": 25, "name": "A. Shooter", "round1": "liverpool", "round2": "Man Utd"},
    {"id": 26, "name": "C. Harris", "round1": "liverpool", "round2": "Man utd"},
    {"id": 27, "name": "P. Crockford", "round1": "spurs", "round2": "Man utd"},
    {"id": 28, "name": "S. Graham-Betts", "round1": "Man City", "round2": "Man utd"},
    {"id": 29, "name": "A. Walkden", "round1": "Forest", "round2": "Sunderland"},
    {"id": 30, "name": "K. Cambell", "round1": "spurs", "round2": "sunderland"},
    {"id": 31, "name": "F. Mulley", "round1": "Man City", "round2": "Villa"},
    {"id": 32, "name": "F. Warby", "round1": "Spurs", "round2": "Villa"},
    {"id": 33, "name": "P. Morrison", "round1": "Forest", "round2": "villa"},
    {"id": 34, "name": "A. Claes", "round1": "Chelsea", "round2": None},
    {"id": 35, "name": "A. Faccini", "round1": "West Ham", "round2": None},
    {"id": 36, "name": "A. Sirignano", "round1": "Brighton", "round2": None},
    {"id": 37, "name": "A. Symons", "round1": "Chelsea", "round2": None},
    {"id": 38, "name": "D. Evans", "round1": "Brighton", "round2": None},
    {"id": 39, "name": "D. Riley", "round1": "Everton", "round2": None},
    {"id": 40, "name": "E. Sandys", "round1": "Chelsea", "round2": None},
    {"id": 41, "name": "M. Prescott", "round1": "Brighton", "round2": None},
    {"id": 42, "name": "O. Riley", "round1": "West Ham", "round2": None},
    {"id": 43, "name": "R. Sadler", "round1": "West Ham", "round2": None},
    {"id": 44, "name": "S. Jones", "round1": "Chelsea", "round2": None},
    {"id": 45, "name": "Sarah", "round1": "Fulham", "round2": None},
    {"id": 46, "name": "T. Thompson", "round1": "Chelsea", "round2": None},
]

# Winning teams (normalized to lowercase for consistent comparison)
round1_winners = {"liverpool", "spurs", "sunderland", "man city", "forest", "arsenal", "leeds"}
round2_winners = {"chelsea", "spurs", "burnley", "brentford", "bournemouth", "arsenal", "everton", "liverpool"}

def normalize_team_name(team):
    """Normalize team names for consistent comparison"""
    if not team:
        return None
    
    team = team.lower().strip()
    
    # Normalize common variations
    team_mapping = {
        "man utd": "man utd",
        "man united": "man utd", 
        "manchester united": "man utd",
        "man city": "man city",
        "manchester city": "man city",
        "nottingham forest": "forest",
        "tottenham": "spurs",
        "aston villa": "villa",
        "leeds united": "leeds"
    }
    
    return team_mapping.get(team, team)

def determine_elimination_status(round1_pick, round2_pick):
    """
    Determine if a player should be eliminated based on their picks
    Returns: 'active' if still in game, 'eliminated' if out
    """
    round1_normalized = normalize_team_name(round1_pick)
    round2_normalized = normalize_team_name(round2_pick)
    
    # Check Round 1
    if round1_normalized not in round1_winners:
        return 'eliminated'  # Eliminated in Round 1
    
    # If they made it through Round 1, check Round 2
    if round2_pick is None:
        return 'eliminated'  # Eliminated in Round 1 (didn't make Round 2 pick)
    
    if round2_normalized not in round2_winners:
        return 'eliminated'  # Eliminated in Round 2
    
    return 'active'  # Still in the game

def import_historical_data():
    """Import historical data into the database"""
    
    with app.app_context():
        try:
            print("Starting historical data import...")
            
            # Create Round 1 and Round 2
            round1 = Round(
                round_number=1,
                pl_matchday=1,
                status='completed'
            )
            
            round2 = Round(
                round_number=2,
                pl_matchday=2,
                status='completed'
            )
            
            db.session.add(round1)
            db.session.add(round2)
            db.session.flush()  # Get the round IDs
            
            print(f"Created Round 1 (ID: {round1.id}) and Round 2 (ID: {round2.id})")
            
            players_created = 0
            picks_created = 0
            eliminated_count = 0
            active_count = 0
            
            for player_data in historical_data:
                # Determine elimination status
                status = determine_elimination_status(
                    player_data['round1'], 
                    player_data['round2']
                )
                
                if status == 'eliminated':
                    eliminated_count += 1
                else:
                    active_count += 1
                
                # Create player
                player = Player(
                    name=player_data['name'],
                    status=status,
                    whatsapp_number=None  # No WhatsApp data in historical import
                )
                
                db.session.add(player)
                db.session.flush()  # Get the player ID
                players_created += 1
                
                # Create Round 1 pick
                round1_normalized = normalize_team_name(player_data['round1'])
                round1_won = round1_normalized in round1_winners
                
                pick1 = Pick(
                    player_id=player.id,
                    round_id=round1.id,
                    team_picked=player_data['round1'],
                    is_winner=round1_won,
                    is_eliminated=not round1_won
                )
                
                db.session.add(pick1)
                picks_created += 1
                
                # Create Round 2 pick if exists
                if player_data['round2'] is not None:
                    round2_normalized = normalize_team_name(player_data['round2'])
                    round2_won = round2_normalized in round2_winners
                    
                    pick2 = Pick(
                        player_id=player.id,
                        round_id=round2.id,
                        team_picked=player_data['round2'],
                        is_winner=round2_won,
                        is_eliminated=not round2_won and round1_won  # Only eliminated in R2 if they won R1
                    )
                    
                    db.session.add(pick2)
                    picks_created += 1
            
            # Commit all changes
            db.session.commit()
            
            print(f"\n✅ Import completed successfully!")
            print(f"📊 Summary:")
            print(f"   • Players created: {players_created}")
            print(f"   • Picks created: {picks_created}")
            print(f"   • Active players: {active_count}")
            print(f"   • Eliminated players: {eliminated_count}")
            print(f"   • Rounds created: 2 (completed)")
            
            # Show some sample data
            print(f"\n📋 Sample active players:")
            active_players = Player.query.filter_by(status='active').limit(5).all()
            for player in active_players:
                r1_pick = Pick.query.filter_by(player_id=player.id, round_id=1).first()
                r2_pick = Pick.query.filter_by(player_id=player.id, round_id=2).first()
                print(f"   • {player.name}: R1={r1_pick.team_picked if r1_pick else 'None'}, R2={r2_pick.team_picked if r2_pick else 'None'}")
            
        except Exception as e:
            db.session.rollback()
            print(f"❌ Error during import: {e}")
            raise

if __name__ == "__main__":
    import_historical_data()