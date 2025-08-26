from flask import Flask, render_template, request, jsonify
from flask_migrate import Migrate
import os
from dotenv import load_dotenv
from datetime import datetime
import urllib.parse

app = Flask(__name__)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
# Database configuration
# Railway provides multiple URLs. Let's try them in order.
# DATABASE_PUBLIC_URL is for external connections, good for debugging.
# DATABASE_URL is for internal connections, preferred for production.
database_uri = None
if os.environ.get('DATABASE_PUBLIC_URL'):
    database_uri = os.environ.get('DATABASE_PUBLIC_URL')
    print("Using DATABASE_PUBLIC_URL")
elif os.environ.get('DATABASE_URL'):
    database_uri = os.environ.get('DATABASE_URL')
    print("Using DATABASE_URL")

if database_uri:
    # SQLAlchemy prefers 'postgresql' over 'postgres'
    app.config['SQLALCHEMY_DATABASE_URI'] = database_uri.replace('postgres://', 'postgresql://')
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///lms.db'
    print("Using local SQLite database.")

print(f"Database URI set to: {app.config['SQLALCHEMY_DATABASE_URI']}")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Import models and db
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from models import db, Player, Round, Fixture, Pick, PickToken

# Initialize db with app
db.init_app(app)
migrate = Migrate(app, db)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin_dashboard')
def admin_dashboard():
    players = Player.query.all()
    current_round = Round.query.filter_by(status='active').first()
    return render_template('admin_dashboard.html', players=players, current_round=current_round)

@app.route('/send_picks')
def send_picks():
    current_round = Round.query.filter_by(status='active').first()
    if not current_round:
        return "No active round found", 404

    active_players = Player.query.filter_by(status='active').all()
    
    for player in active_players:
        pick_token = PickToken.create_for_player_round(player.id, current_round.id)
        db.session.commit() # Commit to get the token
        base_url = os.environ.get('BASE_URL', request.url_root.replace('http://', 'https://'))
        pick_url = pick_token.get_pick_url(base_url)
        
        message = f"""🏆 Last Man Standing - Round {current_round.round_number}

Hi {player.name}! 

It's time to make your pick for Round {current_round.round_number} (Premier League Matchday {current_round.pl_matchday}).

⚠️ Remember:
• Pick a team you think will WIN
• You can only use each team ONCE
• If your team loses or draws, you're out!
• This link expires in 7 days and can only be used once

Good luck! 🍀

Click your unique link to submit your pick:
{pick_url}
"""
        
        player.whatsapp_link = f"https://web.whatsapp.com/send?phone={player.whatsapp_number.replace('+', '')}&text={urllib.parse.quote(message)}"

    return render_template('send_picks.html', players=active_players, round=current_round)

@app.route('/api/players', methods=['GET', 'POST'])
def handle_players():
    if request.method == 'GET':
        players = Player.query.all()
        return jsonify([{
            'id': p.id,
            'name': p.name,
            'status': p.status,
            'unreachable': p.unreachable
        } for p in players])
    
    elif request.method == 'POST':
        try:
            data = request.get_json()
            
            if not data or not data.get('name') or not data.get('whatsapp_number'):
                return jsonify({'success': False, 'error': 'Name and WhatsApp number are required'}), 400
            
            # Check if player already exists
            existing_player = Player.query.filter_by(whatsapp_number=data['whatsapp_number']).first()
            if existing_player:
                return jsonify({'success': False, 'error': 'Player with this WhatsApp number already exists'}), 400
            
            # Create new player
            player = Player(
                name=data['name'].strip(),
                whatsapp_number=data['whatsapp_number'].strip()
            )
            
            db.session.add(player)
            db.session.commit()
            
            return jsonify({'success': True, 'id': player.id})
            
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/players/bulk', methods=['POST'])
def bulk_import_players():
    try:
        data = request.get_json()
        
        if not data or not data.get('players'):
            return jsonify({'success': False, 'error': 'Players data is required'}), 400
        
        players_data = data['players']
        created_count = 0
        errors = []
        
        for i, player_data in enumerate(players_data):
            try:
                if not player_data.get('name'):
                    errors.append(f"Line {i+1}: Missing name")
                    continue
                
                name = player_data['name'].strip()
                whatsapp = player_data.get('whatsapp_number', '').strip()
                
                # Check if player with same name already exists
                existing_player = Player.query.filter_by(name=name).first()
                if existing_player:
                    errors.append(f"Line {i+1}: Player with name '{name}' already exists")
                    continue
                
                # If WhatsApp number is provided, check for duplicates
                if whatsapp:
                    existing_whatsapp = Player.query.filter_by(whatsapp_number=whatsapp).first()
                    if existing_whatsapp:
                        errors.append(f"Line {i+1}: Player with WhatsApp number {whatsapp} already exists")
                        continue
                
                # Create new player
                player = Player(
                    name=name,
                    whatsapp_number=whatsapp
                )
                
                db.session.add(player)
                created_count += 1
                
            except Exception as e:
                errors.append(f"Line {i+1}: {str(e)}")
        
        if created_count > 0:
            db.session.commit()
        
        if errors and created_count == 0:
            return jsonify({'success': False, 'error': 'No players could be imported', 'errors': errors}), 400
        
        response_data = {'success': True, 'count': created_count}
        if errors:
            response_data['warnings'] = errors
        
        return jsonify(response_data)
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/players/<int:player_id>', methods=['PUT', 'DELETE'])
def handle_player_by_id(player_id):
    player = Player.query.get_or_404(player_id)
    
    if request.method == 'PUT':
        try:
            data = request.get_json()
            
            if not data or not data.get('name'):
                return jsonify({'success': False, 'error': 'Player name is required'}), 400
            
            name = data['name'].strip()
            whatsapp = data.get('whatsapp_number', '').strip()
            
            # Check if another player with the same name exists
            existing_player = Player.query.filter(Player.name == name, Player.id != player_id).first()
            if existing_player:
                return jsonify({'success': False, 'error': 'Player with this name already exists'}), 400
            
            # Check if another player with the same WhatsApp number exists (if provided)
            if whatsapp:
                existing_whatsapp = Player.query.filter(Player.whatsapp_number == whatsapp, Player.id != player_id).first()
                if existing_whatsapp:
                    return jsonify({'success': False, 'error': 'Player with this WhatsApp number already exists'}), 400
            
            # Update player
            player.name = name
            player.whatsapp_number = whatsapp
            
            db.session.commit()
            
            return jsonify({'success': True})
            
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
    
    elif request.method == 'DELETE':
        try:
            # Check if player has any picks
            picks_count = Pick.query.filter_by(player_id=player_id).count()
            if picks_count > 0:
                return jsonify({'success': False, 'error': f'Cannot delete player with {picks_count} existing picks'}), 400
            
            # Delete player
            db.session.delete(player)
            db.session.commit()
            
            return jsonify({'success': True})
            
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/rounds', methods=['GET', 'POST'])
def handle_rounds():
    if request.method == 'GET':
        rounds = Round.query.all()
        return jsonify([{
            'id': r.id,
            'round_number': r.round_number,
            'status': r.status,
            'start_date': r.start_date.isoformat() if r.start_date else None,
            'end_date': r.end_date.isoformat() if r.end_date else None
        } for r in rounds])
    
    elif request.method == 'POST':
        try:
            data = request.get_json()
            
            if not data or not data.get('round_number'):
                return jsonify({'success': False, 'error': 'Round number is required'}), 400
            
            round_number = data['round_number']
            
            # Check if round number already exists
            existing_round = Round.query.filter_by(round_number=round_number).first()
            if existing_round:
                return jsonify({'success': False, 'error': f'Round {round_number} already exists'}), 400
            
            # Parse dates if provided
            start_date = None
            end_date = None
            
            if data.get('start_date'):
                try:
                    start_date = datetime.fromisoformat(data['start_date'].replace('T', ' '))
                except ValueError:
                    return jsonify({'success': False, 'error': 'Invalid start date format'}), 400
            
            if data.get('end_date'):
                try:
                    end_date = datetime.fromisoformat(data['end_date'].replace('T', ' '))
                except ValueError:
                    return jsonify({'success': False, 'error': 'Invalid end date format'}), 400
            
            # Validate date logic
            if start_date and end_date and start_date >= end_date:
                return jsonify({'success': False, 'error': 'End date must be after start date'}), 400
            
            # Get PL matchday
            pl_matchday = data.get('pl_matchday')
            if not pl_matchday:
                return jsonify({'success': False, 'error': 'Premier League matchday is required'}), 400
            
            # Create new round
            new_round = Round(
                round_number=round_number,
                pl_matchday=pl_matchday,
                start_date=start_date,
                end_date=end_date,
                status=data.get('status', 'pending')
            )
            
            db.session.add(new_round)
            db.session.flush()  # Get the ID before committing
            
            # Fetch and populate fixtures
            try:
                from football_api import FootballDataAPI
                api = FootballDataAPI()
                fixtures_data = api.get_premier_league_fixtures(pl_matchday)
                formatted_fixtures = api.format_fixtures_for_db(fixtures_data, pl_matchday)
                
                # Create fixture records
                for fixture_data in formatted_fixtures:
                    fixture = Fixture(
                        round_id=new_round.id,
                        event_id=fixture_data['event_id'],
                        home_team=fixture_data['home_team'],
                        away_team=fixture_data['away_team'],
                        date=fixture_data['date'],
                        time=fixture_data['time'],
                        home_score=fixture_data['home_score'],
                        away_score=fixture_data['away_score'],
                        status=fixture_data['status']
                    )
                    db.session.add(fixture)
                
                db.session.commit()
                
                return jsonify({
                    'success': True, 
                    'id': new_round.id, 
                    'round_number': new_round.round_number,
                    'pl_matchday': new_round.pl_matchday,
                    'fixtures_added': len(formatted_fixtures)
                })
                
            except Exception as fixture_error:
                # If fixture fetching fails, still create the round but without fixtures
                db.session.commit()
                return jsonify({
                    'success': True, 
                    'id': new_round.id, 
                    'round_number': new_round.round_number,
                    'pl_matchday': new_round.pl_matchday,
                    'fixtures_added': 0,
                    'warning': f'Round created but fixtures could not be fetched: {str(fixture_error)}'
                })
            
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/matchdays')
def get_available_matchdays():
    """Get available Premier League matchdays"""
    try:
        from football_api import FootballDataAPI
        api = FootballDataAPI()
        
        print("Starting matchdays request...")
        
        # Get fixtures for current season (2025/26)
        fixtures_data = api.get_premier_league_fixtures()
        
        matchdays = set()
        for match in fixtures_data.get('matches', []):
            if match.get('matchday'):
                matchdays.add(match['matchday'])
        
        matchdays_list = sorted(list(matchdays))
        print(f"Found matchdays: {matchdays_list}")
        
        # Get matchday info for each (simplified to avoid rate limiting)
        matchday_data = []
        for matchday in matchdays_list:
            # Create simple matchday info without making additional API calls
            matchday_data.append({
                'matchday': matchday,
                'fixture_count': len([m for m in fixtures_data.get('matches', []) if m.get('matchday') == matchday]),
                'earliest_date': None,
                'latest_date': None
            })
        
        print(f"Returning {len(matchday_data)} matchdays")
        return jsonify({'success': True, 'matchdays': matchday_data})
    except Exception as e:
        print(f"Error in get_available_matchdays: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/matchdays/<int:matchday>')
def get_matchday_info(matchday):
    """Get information about a specific matchday"""
    try:
        from football_api import FootballDataAPI
        api = FootballDataAPI()
        
        # Get fixtures for this specific matchday
        fixtures_data = api.get_premier_league_fixtures(matchday=matchday)
        matches = fixtures_data.get('matches', [])
        
        dates = []
        for match in matches:
            if match.get('utcDate'):
                try:
                    dt = datetime.fromisoformat(match['utcDate'].replace('Z', '+00:00'))
                    dates.append(dt.date())
                except ValueError:
                    pass
        
        info = {
            'matchday': matchday,
            'fixture_count': len(matches),
            'earliest_date': min(dates) if dates else None,
            'latest_date': max(dates) if dates else None
        }
        
        return jsonify({'success': True, 'info': info})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/rounds/<int:round_id>')
def get_round_by_id(round_id):
    """Get detailed information about a specific round"""
    try:
        round_obj = Round.query.get_or_404(round_id)
        fixtures = Fixture.query.filter_by(round_id=round_id).all()
        
        return jsonify({
            'success': True,
            'round': {
                'id': round_obj.id,
                'round_number': round_obj.round_number,
                'pl_matchday': round_obj.pl_matchday,
                'status': round_obj.status,
                'start_date': round_obj.start_date.isoformat() if round_obj.start_date else None,
                'end_date': round_obj.end_date.isoformat() if round_obj.end_date else None,
                'fixture_count': len(fixtures)
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/reset-game', methods=['POST'])
def reset_game():
    """Reset the game by deleting all game data except players"""
    try:
        # Count items before deletion for reporting
        rounds_count = Round.query.count()
        fixtures_count = Fixture.query.count()
        picks_count = Pick.query.count()
        players_count = Player.query.count()
        
        # Delete all picks
        Pick.query.delete()
        
        # Delete all fixtures
        Fixture.query.delete()
        
        # Delete all rounds
        Round.query.delete()
        
        # Reset all players to active status
        Player.query.update({'status': 'active', 'unreachable': False})
        
        # Commit all changes
        db.session.commit()
        
        return jsonify({
            'success': True,
            'rounds_deleted': rounds_count,
            'fixtures_deleted': fixtures_count,
            'picks_deleted': picks_count,
            'players_reset': players_count
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/pick/<token>', methods=['GET', 'POST'])
def make_pick(token):
    # Find the pick token
    pick_token = PickToken.query.filter_by(token=token).first()
    
    if not pick_token:
        return render_template('pick_error.html', error="Invalid pick link"), 404
    
    if not pick_token.is_valid():
        error = "This pick link has expired" if pick_token.expires_at and datetime.utcnow() > pick_token.expires_at else "This pick link has already been used"
        return render_template('pick_error.html', error=error), 400
    
    player = pick_token.player
    round_obj = pick_token.round
    
    # Check if player already has a pick for this round
    existing_pick = Pick.query.filter_by(player_id=player.id, round_id=round_obj.id).first()
    if existing_pick:
        return render_template('pick_success.html', 
                             player=player, 
                             round=round_obj, 
                             team_picked=existing_pick.team_picked,
                             already_picked=True)
    
    # Get fixtures for this round
    fixtures = Fixture.query.filter_by(round_id=round_obj.id).all()
    
    # Get player's previous picks to prevent reusing teams
    previous_picks = Pick.query.filter_by(player_id=player.id).all()
    used_teams = [pick.team_picked for pick in previous_picks]
    
    if request.method == 'POST':
        team_picked = request.form.get('team_picked')
        
        if not team_picked:
            return render_template('pick_form.html', 
                                 player=player, 
                                 round=round_obj, 
                                 fixtures=fixtures, 
                                 used_teams=used_teams,
                                 error="Please select a team")
        
        if team_picked in used_teams:
            return render_template('pick_form.html', 
                                 player=player, 
                                 round=round_obj, 
                                 fixtures=fixtures, 
                                 used_teams=used_teams,
                                 error="You have already picked this team in a previous round")
        
        # Validate team exists in fixtures
        valid_teams = []
        for fixture in fixtures:
            valid_teams.extend([fixture.home_team, fixture.away_team])
        
        if team_picked not in valid_teams:
            return render_template('pick_form.html', 
                                 player=player, 
                                 round=round_obj, 
                                 fixtures=fixtures, 
                                 used_teams=used_teams,
                                 error="Invalid team selection")
        
        # Create the pick
        pick = Pick(
            player_id=player.id,
            round_id=round_obj.id,
            team_picked=team_picked
        )
        
        db.session.add(pick)
        pick_token.mark_used()
        db.session.commit()
        
        return render_template('pick_success.html', 
                             player=player, 
                             round=round_obj, 
                             team_picked=team_picked,
                             already_picked=False)
    
    # GET request - show the pick form
    return render_template('pick_form.html', 
                         player=player, 
                         round=round_obj, 
                         fixtures=fixtures, 
                         used_teams=used_teams)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
