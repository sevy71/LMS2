from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from flask_migrate import Migrate
import os
from dotenv import load_dotenv
from datetime import datetime
import urllib.parse
from functools import wraps

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# --- Database configuration ---
database_uri = os.environ.get('DATABASE_PUBLIC_URL') or os.environ.get('DATABASE_URL')
if database_uri:
    # SQLAlchemy prefers 'postgresql' over 'postgres'
    app.config['SQLALCHEMY_DATABASE_URI'] = database_uri.replace('postgres://', 'postgresql://')
    print("Using DATABASE_PUBLIC_URL" if os.environ.get('DATABASE_PUBLIC_URL') else "Using DATABASE_URL")
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

# Admin authentication
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')  # Change this!

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            session.permanent = True
            next_page = request.args.get('next') or url_for('admin_dashboard')
            return redirect(next_page)
        else:
            flash('Invalid password', 'error')
    
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    flash('You have been logged out', 'info')
    return redirect(url_for('index'))

@app.route('/admin/change-password', methods=['POST'])
@admin_required
def change_admin_password():
    try:
        global ADMIN_PASSWORD
        data = request.get_json()
        current_password = data.get('current_password')
        new_password = data.get('new_password')
        
        if not current_password or not new_password:
            return jsonify({'success': False, 'error': 'Current and new password are required'}), 400
        
        if current_password != ADMIN_PASSWORD:
            return jsonify({'success': False, 'error': 'Current password is incorrect'}), 400
        
        if len(new_password) < 6:
            return jsonify({'success': False, 'error': 'New password must be at least 6 characters'}), 400
        
        # In a real app, you'd update the password in a database or config file
        # For now, we'll just update the environment variable (temporary)
        ADMIN_PASSWORD = new_password
        os.environ['ADMIN_PASSWORD'] = new_password
        
        return jsonify({
            'success': True,
            'message': 'Password changed successfully. Please update your ADMIN_PASSWORD environment variable in Railway.'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/picks-grid')
@admin_required
def picks_grid():
    """Display a grid of all player picks for all rounds."""
    return render_template('picks_grid.html')

@app.route('/api/picks-grid-data')
@admin_required
def get_picks_grid_data():
    """Provide data for the picks grid."""
    try:
        rounds = Round.query.order_by(Round.round_number).all()
        players = Player.query.order_by(Player.name).all()
        picks = Pick.query.all()

        # Create mappings
        picks_map = {}
        results_map = {}
        
        for pick in picks:
            key = (pick.player_id, pick.round_id)
            picks_map[key] = pick.team_picked
            results_map[key] = {
                'is_winner': pick.is_winner,
                'is_eliminated': pick.is_eliminated
            }

        # Prepare player data
        players_data = []
        for player in players:
            player_picks = {}
            
            for r in rounds:
                key = (player.id, r.id)
                if key in picks_map:
                    team = picks_map[key]
                    result = results_map[key]
                    player_picks[r.round_number] = {
                        'team': team,
                        'is_winner': result['is_winner'],
                        'is_eliminated': result['is_eliminated']
                    }
                else:
                    player_picks[r.round_number] = None
            
            players_data.append({
                'name': player.name,
                'status': player.status,
                'picks': player_picks
            })

        return jsonify({
            'success': True,
            'rounds': [r.round_number for r in rounds],
            'players': players_data
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin_dashboard')
@admin_required
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
        # Get base URL - prioritize Railway deployment URL
        base_url = os.environ.get('BASE_URL')
        if not base_url:
            # Fallback to request URL but ensure it's HTTPS for production
            base_url = request.url_root.rstrip('/')
            if base_url.startswith('http://') and 'localhost' not in base_url and '127.0.0.1' not in base_url:
                base_url = base_url.replace('http://', 'https://')
        
        # Ensure base_url has protocol
        if not base_url.startswith(('http://', 'https://')):
            base_url = f"https://{base_url}"
        
        pick_url = pick_token.get_pick_url(base_url)
        
        # Debug logging
        print(f"Generated pick URL for {player.name}: {pick_url}")
        
        # Generate general registration link
        registration_url = f"{base_url}/register"
        
        # Format message with better mobile WhatsApp compatibility
        message_lines = [
            f"🏆 Last Man Standing - Round {current_round.round_number}",
            "",
            f"Hi {player.name}!",
            "",
            f"Time to make your pick for Round {current_round.round_number} (PL Matchday {current_round.pl_matchday}).",
            "",
            "⚠️ Remember:",
            "• Pick a team you think will WIN",
            "• You can only use each team ONCE", 
            "• If your team loses or draws, you're out!",
            "• Link expires in 7 days",
            "",
            "Good luck! 🍀",
            "",
            "Your pick link:",
            pick_url,
            "",
            "👥 Want to invite friends/family?",
            "Share this registration link:",
            registration_url
        ]
        
        message = "\n".join(message_lines)
        
        # Don't encode the URL at all - WhatsApp mobile is very sensitive to URL encoding
        # Just encode line breaks and special characters, preserve the URL completely
        encoded_message = message.replace('\n', '%0A')
        
        # Only generate WhatsApp link if player has a WhatsApp number
        if player.whatsapp_number:
            player.whatsapp_link = f"https://web.whatsapp.com/send?phone={player.whatsapp_number.replace('+', '')}&text={encoded_message}"
            # Debug logging
            print(f"WhatsApp link for {player.name}: {player.whatsapp_link[:100]}...")
        else:
            player.whatsapp_link = None
            print(f"No WhatsApp number for {player.name}, link generation skipped")
        
        print(f"Pick URL in message: {pick_url}")

    return render_template('send_picks.html', players=active_players, round=current_round)

@app.route('/api/players', methods=['GET', 'POST'])
@admin_required
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
            
            if not data or not data.get('name'):
                return jsonify({'success': False, 'error': 'Player name is required'}), 400
            
            # Check if player with same name already exists
            existing_player = Player.query.filter_by(name=data['name'].strip()).first()
            if existing_player:
                return jsonify({'success': False, 'error': 'Player with this name already exists'}), 400
            
            # Create new player
            player = Player(
                name=data['name'].strip(),
                whatsapp_number=data.get('whatsapp_number', '').strip() or None
            )
            
            db.session.add(player)
            db.session.commit()
            
            return jsonify({'success': True, 'id': player.id})
            
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/players/bulk', methods=['POST'])
@admin_required
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
                
                # WhatsApp numbers can be shared among multiple players (family members)
                # No need to check for WhatsApp duplicates anymore
                
                # Create new player
                player = Player(
                    name=name,
                    whatsapp_number=whatsapp or None
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
@admin_required
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
            
            # WhatsApp numbers can be shared among multiple players (family members)
            # No need to check for WhatsApp duplicates anymore
            
            # Update player
            player.name = name
            player.whatsapp_number = whatsapp or None
            
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
@admin_required
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
                
                if formatted_fixtures:
                    # Create fixture records from API data
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
                else:
                    # No fixtures from API, create fallback fixtures
                    raise Exception("No fixtures returned from API")
                
            except Exception as fixture_error:
                print(f"API failed, creating fallback fixtures: {fixture_error}")
                # Create fallback Premier League fixtures for the round
                fallback_fixtures = [
                    ("Arsenal", "Chelsea"), ("Liverpool", "Manchester City"), 
                    ("Manchester United", "Tottenham"), ("Newcastle", "Brighton"),
                    ("Aston Villa", "West Ham"), ("Crystal Palace", "Everton"),
                    ("Fulham", "Brentford"), ("Wolves", "Nottingham Forest"),
                    ("Bournemouth", "Sheffield United"), ("Burnley", "Luton Town")
                ]
                
                for i, (home_team, away_team) in enumerate(fallback_fixtures):
                    fixture = Fixture(
                        round_id=new_round.id,
                        event_id=f"fallback_{new_round.id}_{i}",
                        home_team=home_team,
                        away_team=away_team,
                        date=None,
                        time=None,
                        home_score=None,
                        away_score=None,
                        status='scheduled'
                    )
                    db.session.add(fixture)
                
                db.session.commit()
                
                return jsonify({
                    'success': True, 
                    'id': new_round.id, 
                    'round_number': new_round.round_number,
                    'pl_matchday': new_round.pl_matchday,
                    'fixtures_added': len(fallback_fixtures),
                    'warning': f'Round created with fallback fixtures (API failed): {str(fixture_error)}'
                })
            
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/test-matchdays')
def test_matchdays():
    """Test endpoint for debugging"""
    try:
        print("Testing matchdays endpoint...")
        matchday_data = []
        for matchday in range(1, 39):
            matchday_data.append({
                'matchday': matchday,
                'fixture_count': 10,
                'earliest_date': None,
                'latest_date': None
            })
        return jsonify({'success': True, 'matchdays': matchday_data, 'source': 'test'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/matchdays')
@admin_required
def get_available_matchdays():
    """Get available Premier League matchdays"""
    print("=== Matchdays endpoint called ===")
    
    # Start with fallback approach to ensure it always works
    try:
        matchday_data = []
        for matchday in range(1, 39):
            matchday_data.append({
                'matchday': matchday,
                'fixture_count': 10,  # Typical PL matchday has 10 fixtures
                'earliest_date': None,
                'latest_date': None
            })
        
        print(f"Generated fallback matchdays: {len(matchday_data)} items")
        
        # Optional: Try to get real data from API if available
        try:
            from football_api import FootballDataAPI
            api = FootballDataAPI()
            print("Attempting to get real matchday data from API...")
            
            fixtures_data = api.get_premier_league_fixtures(season='2025')
            if fixtures_data and fixtures_data.get('matches'):
                print(f"Got {len(fixtures_data['matches'])} matches from API")
                
                # Extract real matchdays
                real_matchdays = set()
                for match in fixtures_data.get('matches', []):
                    if match.get('matchday'):
                        real_matchdays.add(match['matchday'])
                
                if real_matchdays:
                    print(f"Found real matchdays: {sorted(real_matchdays)}")
                    # Replace fallback with real data
                    matchday_data = []
                    for matchday in sorted(real_matchdays):
                        fixture_count = len([m for m in fixtures_data['matches'] if m.get('matchday') == matchday])
                        matchday_data.append({
                            'matchday': matchday,
                            'fixture_count': fixture_count,
                            'earliest_date': None,
                            'latest_date': None
                        })
                    print("Using real API data")
                    return jsonify({'success': True, 'matchdays': matchday_data, 'source': 'api'})
            
        except Exception as api_error:
            print(f"API failed (using fallback): {api_error}")
        
        # Return fallback data
        print("Using fallback matchday data")
        return jsonify({'success': True, 'matchdays': matchday_data, 'source': 'fallback'})
        
    except Exception as e:
        print(f"Critical error in matchdays endpoint: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Matchdays endpoint failed: {str(e)}'}), 500

@app.route('/api/matchdays/<int:matchday>')
@admin_required
def get_matchday_info(matchday):
    """Get information about a specific matchday"""
    print(f"=== Getting info for matchday {matchday} ===")
    
    # Validate matchday range
    if matchday < 1 or matchday > 38:
        return jsonify({'success': False, 'error': 'Invalid matchday. Must be between 1 and 38'}), 400
    
    try:
        # Start with fallback info
        info = {
            'matchday': matchday,
            'fixture_count': 10,  # Typical PL matchday has 10 fixtures
            'earliest_date': None,
            'latest_date': None
        }
        
        # Try to get real API data to enhance the info
        try:
            from football_api import FootballDataAPI
            api = FootballDataAPI()
            print(f"Attempting to get real data for matchday {matchday}")
            
            fixtures_data = api.get_premier_league_fixtures(matchday=matchday, season='2025')
            matches = fixtures_data.get('matches', [])
            
            if matches:
                print(f"Got {len(matches)} matches for matchday {matchday}")
                
                # Extract dates
                dates = []
                for match in matches:
                    if match.get('utcDate'):
                        try:
                            dt = datetime.fromisoformat(match['utcDate'].replace('Z', '+00:00'))
                            dates.append(dt.date())
                        except ValueError:
                            pass
                
                # Update info with real data
                info = {
                    'matchday': matchday,
                    'fixture_count': len(matches),
                    'earliest_date': min(dates).isoformat() if dates else None,
                    'latest_date': max(dates).isoformat() if dates else None
                }
                print(f"Using real API data: {info}")
                return jsonify({'success': True, 'info': info, 'source': 'api'})
            
        except Exception as api_error:
            print(f"API failed for matchday {matchday}: {api_error}")
        
        # Return fallback info
        print(f"Using fallback data for matchday {matchday}")
        return jsonify({'success': True, 'info': info, 'source': 'fallback'})
        
    except Exception as e:
        print(f"Critical error getting matchday {matchday} info: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Failed to get matchday info: {str(e)}'}), 500

@app.route('/api/rounds/<int:round_id>')
@admin_required
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

@app.route('/api/rounds/<int:round_id>/fixtures', methods=['POST'])
@admin_required
def add_fixtures_to_round(round_id):
    """Add fixtures to an existing round"""
    try:
        round_obj = Round.query.get_or_404(round_id)
        
        # Check if round already has fixtures
        existing_fixtures = Fixture.query.filter_by(round_id=round_id).count()
        if existing_fixtures > 0:
            return jsonify({'success': False, 'error': f'Round already has {existing_fixtures} fixtures'}), 400
        
        # Try to get fixtures from API
        try:
            from football_api import FootballDataAPI
            api = FootballDataAPI()
            fixtures_data = api.get_premier_league_fixtures(round_obj.pl_matchday)
            formatted_fixtures = api.format_fixtures_for_db(fixtures_data, round_obj.pl_matchday)
            
            if formatted_fixtures:
                # Create fixture records from API data
                for fixture_data in formatted_fixtures:
                    fixture = Fixture(
                        round_id=round_obj.id,
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
                    'fixtures_added': len(formatted_fixtures),
                    'source': 'api'
                })
            else:
                raise Exception("No fixtures returned from API")
                
        except Exception as api_error:
            print(f"API failed, creating fallback fixtures for round {round_id}: {api_error}")
            # Create fallback Premier League fixtures
            fallback_fixtures = [
                ("Arsenal", "Chelsea"), ("Liverpool", "Manchester City"), 
                ("Manchester United", "Tottenham"), ("Newcastle", "Brighton"),
                ("Aston Villa", "West Ham"), ("Crystal Palace", "Everton"),
                ("Fulham", "Brentford"), ("Wolves", "Nottingham Forest"),
                ("Bournemouth", "Sheffield United"), ("Burnley", "Luton Town")
            ]
            
            for i, (home_team, away_team) in enumerate(fallback_fixtures):
                fixture = Fixture(
                    round_id=round_obj.id,
                    event_id=f"fallback_{round_obj.id}_{i}",
                    home_team=home_team,
                    away_team=away_team,
                    date=None,
                    time=None,
                    home_score=None,
                    away_score=None,
                    status='scheduled'
                )
                db.session.add(fixture)
            
            db.session.commit()
            
            return jsonify({
                'success': True,
                'fixtures_added': len(fallback_fixtures),
                'source': 'fallback',
                'warning': f'Used fallback fixtures due to API error: {str(api_error)}'
            })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/rounds/<int:round_id>/picks')
@admin_required
def get_round_picks(round_id):
    """Get all picks and fixtures for a round"""
    try:
        round_obj = Round.query.get_or_404(round_id)
        fixtures = Fixture.query.filter_by(round_id=round_id).all()
        picks = Pick.query.filter_by(round_id=round_id).all()
        
        # Format fixtures data
        fixtures_data = []
        for fixture in fixtures:
            fixtures_data.append({
                'id': fixture.id,
                'home_team': fixture.home_team,
                'away_team': fixture.away_team,
                'home_score': fixture.home_score,
                'away_score': fixture.away_score,
                'status': fixture.status,
                'date': fixture.date.isoformat() if fixture.date else None,
                'time': fixture.time.isoformat() if fixture.time else None
            })
        
        # Format picks data
        picks_data = []
        for pick in picks:
            picks_data.append({
                'id': pick.id,
                'player_name': pick.player.name,
                'team_picked': pick.team_picked,
                'is_winner': pick.is_winner,
                'is_eliminated': pick.is_eliminated
            })
        
        return jsonify({
            'success': True,
            'round': {
                'id': round_obj.id,
                'round_number': round_obj.round_number,
                'status': round_obj.status
            },
            'fixtures': fixtures_data,
            'picks': picks_data
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/players/<int:player_id>/status', methods=['PUT'])
@admin_required
def update_player_status(player_id):
    """Manually update player status (eliminate/reactivate)"""
    try:
        data = request.get_json()
        new_status = data.get('status')
        
        if new_status not in ['active', 'eliminated']:
            return jsonify({'success': False, 'error': 'Invalid status. Must be "active" or "eliminated"'}), 400
        
        player = Player.query.get_or_404(player_id)
        old_status = player.status
        player.status = new_status
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'player_id': player_id,
            'player_name': player.name,
            'old_status': old_status,
            'new_status': new_status
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/statistics')
@admin_required
def get_statistics():
    """Get comprehensive statistics for the competition"""
    try:
        # Competition overview stats
        total_players = Player.query.count()
        active_players = Player.query.filter_by(status='active').count()
        eliminated_players = Player.query.filter_by(status='eliminated').count()
        total_rounds = Round.query.count()
        completed_rounds = Round.query.filter_by(status='completed').count()
        active_round = Round.query.filter_by(status='active').first()
        
        # Individual player stats
        players = Player.query.all()
        player_stats = []
        
        for player in players:
            picks = Pick.query.filter_by(player_id=player.id).all()
            total_picks = len(picks)
            winning_picks = len([p for p in picks if p.is_winner])
            teams_used = list(set([p.team_picked for p in picks]))
            
            # Calculate survival streak
            survival_streak = 0
            for pick in reversed(picks):  # Start from most recent
                if pick.is_winner == True:
                    survival_streak += 1
                elif pick.is_winner == False:
                    break
            
            player_stats.append({
                'id': player.id,
                'name': player.name,
                'status': player.status,
                'total_picks': total_picks,
                'winning_picks': winning_picks,
                'success_rate': round((winning_picks / total_picks * 100) if total_picks > 0 else 0, 1),
                'teams_used': teams_used,
                'teams_used_count': len(teams_used),
                'current_streak': survival_streak
            })
        
        # Pick history for all players
        all_picks = Pick.query.join(Player).join(Round).all()
        pick_history = []
        
        for pick in all_picks:
            pick_history.append({
                'player_name': pick.player.name,
                'round_number': pick.round.round_number,
                'team_picked': pick.team_picked,
                'result': 'Winner' if pick.is_winner == True else ('Eliminated' if pick.is_winner == False else 'Pending'),
                'pick_date': pick.created_at.strftime('%Y-%m-%d %H:%M') if pick.created_at else 'Unknown'
            })
        
        return jsonify({
            'success': True,
            'competition_stats': {
                'total_players': total_players,
                'active_players': active_players,
                'eliminated_players': eliminated_players,
                'elimination_rate': round((eliminated_players / total_players * 100) if total_players > 0 else 0, 1),
                'total_rounds': total_rounds,
                'completed_rounds': completed_rounds,
                'current_round': active_round.round_number if active_round else None
            },
            'player_stats': player_stats,
            'pick_history': pick_history
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/export/<export_type>')
@admin_required
def export_data(export_type):
    """Export data in CSV format"""
    try:
        import csv
        from io import StringIO
        from flask import make_response
        
        output = StringIO()
        
        if export_type == 'players':
            writer = csv.writer(output)
            writer.writerow(['ID', 'Name', 'WhatsApp Number', 'Status', 'Unreachable', 'Created Date'])
            
            players = Player.query.all()
            for player in players:
                writer.writerow([
                    player.id,
                    player.name,
                    player.whatsapp_number,
                    player.status,
                    player.unreachable,
                    player.created_at.strftime('%Y-%m-%d %H:%M:%S') if player.created_at else ''
                ])
            
            filename = 'lms_players.csv'
            
        elif export_type == 'rounds':
            writer = csv.writer(output)
            writer.writerow(['Round ID', 'Round Number', 'PL Matchday', 'Status', 'Start Date', 'End Date', 'Fixture Count'])
            
            rounds = Round.query.all()
            for round_obj in rounds:
                fixture_count = Fixture.query.filter_by(round_id=round_obj.id).count()
                writer.writerow([
                    round_obj.id,
                    round_obj.round_number,
                    round_obj.pl_matchday,
                    round_obj.status,
                    round_obj.start_date.strftime('%Y-%m-%d %H:%M:%S') if round_obj.start_date else '',
                    round_obj.end_date.strftime('%Y-%m-%d %H:%M:%S') if round_obj.end_date else '',
                    fixture_count
                ])
            
            filename = 'lms_rounds.csv'
            
        elif export_type == 'picks':
            writer = csv.writer(output)
            writer.writerow(['Pick ID', 'Player Name', 'Round Number', 'Team Picked', 'Result', 'Is Winner', 'Is Eliminated', 'Pick Date'])
            
            picks = Pick.query.join(Player).join(Round).all()
            for pick in picks:
                result = 'Winner' if pick.is_winner == True else ('Eliminated' if pick.is_winner == False else 'Pending')
                writer.writerow([
                    pick.id,
                    pick.player.name,
                    pick.round.round_number,
                    pick.team_picked,
                    result,
                    pick.is_winner,
                    pick.is_eliminated,
                    pick.created_at.strftime('%Y-%m-%d %H:%M:%S') if pick.created_at else ''
                ])
            
            filename = 'lms_picks.csv'
            
        elif export_type == 'stats':
            writer = csv.writer(output)
            writer.writerow(['Player Name', 'Status', 'Total Picks', 'Winning Picks', 'Success Rate %', 'Teams Used', 'Current Streak'])
            
            players = Player.query.all()
            for player in players:
                picks = Pick.query.filter_by(player_id=player.id).all()
                total_picks = len(picks)
                winning_picks = len([p for p in picks if p.is_winner])
                success_rate = round((winning_picks / total_picks * 100) if total_picks > 0 else 0, 1)
                teams_used = list(set([p.team_picked for p in picks]))
                
                # Calculate current streak
                survival_streak = 0
                for pick in reversed(picks):
                    if pick.is_winner == True:
                        survival_streak += 1
                    elif pick.is_winner == False:
                        break
                
                writer.writerow([
                    player.name,
                    player.status,
                    total_picks,
                    winning_picks,
                    f"{success_rate}%",
                    ', '.join(teams_used),
                    survival_streak
                ])
            
            filename = 'lms_statistics.csv'
            
        elif export_type == 'full':
            # Create a comprehensive backup with multiple sheets/sections
            writer = csv.writer(output)
            
            # Players section
            writer.writerow(['=== PLAYERS ==='])
            writer.writerow(['ID', 'Name', 'WhatsApp Number', 'Status', 'Unreachable', 'Created Date'])
            players = Player.query.all()
            for player in players:
                writer.writerow([
                    player.id, player.name, player.whatsapp_number, player.status, 
                    player.unreachable, player.created_at.strftime('%Y-%m-%d %H:%M:%S') if player.created_at else ''
                ])
            
            writer.writerow([])  # Empty row separator
            
            # Rounds section
            writer.writerow(['=== ROUNDS ==='])
            writer.writerow(['Round ID', 'Round Number', 'PL Matchday', 'Status', 'Start Date', 'End Date'])
            rounds = Round.query.all()
            for round_obj in rounds:
                writer.writerow([
                    round_obj.id, round_obj.round_number, round_obj.pl_matchday, round_obj.status,
                    round_obj.start_date.strftime('%Y-%m-%d %H:%M:%S') if round_obj.start_date else '',
                    round_obj.end_date.strftime('%Y-%m-%d %H:%M:%S') if round_obj.end_date else ''
                ])
            
            writer.writerow([])
            
            # Picks section
            writer.writerow(['=== PICKS ==='])
            writer.writerow(['Pick ID', 'Player Name', 'Round Number', 'Team Picked', 'Result', 'Pick Date'])
            picks = Pick.query.join(Player).join(Round).all()
            for pick in picks:
                result = 'Winner' if pick.is_winner == True else ('Eliminated' if pick.is_winner == False else 'Pending')
                writer.writerow([
                    pick.id, pick.player.name, pick.round.round_number, pick.team_picked, result,
                    pick.created_at.strftime('%Y-%m-%d %H:%M:%S') if pick.created_at else ''
                ])
            
            filename = 'lms_complete_backup.csv'
            
        else:
            return jsonify({'success': False, 'error': 'Invalid export type'}), 400
        
        # Create response
        response = make_response(output.getvalue())
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = f'attachment; filename={filename}'
        
        return response
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/rounds/<int:round_id>/process-results', methods=['POST'])
@admin_required  
def process_round_results(round_id):
    """Process match results and eliminate players"""
    try:
        data = request.get_json()
        fixture_results = data.get('results', [])
        
        if not fixture_results:
            return jsonify({'success': False, 'error': 'No results provided'}), 400
        
        round_obj = Round.query.get_or_404(round_id)
        eliminated_players = []
        surviving_players = []
        
        # Update fixture results
        for result in fixture_results:
            fixture_id = result.get('fixture_id')
            home_score = result.get('home_score')  
            away_score = result.get('away_score')
            
            if fixture_id and home_score is not None and away_score is not None:
                fixture = Fixture.query.get(fixture_id)
                if fixture:
                    fixture.home_score = int(home_score)
                    fixture.away_score = int(away_score)
                    fixture.status = 'completed'
                    
                    # Determine winner/draw
                    if fixture.home_score > fixture.away_score:
                        winning_team = fixture.home_team
                    elif fixture.away_score > fixture.home_score:
                        winning_team = fixture.away_team
                    else:
                        winning_team = None  # Draw
                    
                    # Find picks for this fixture's teams
                    home_picks = Pick.query.filter_by(round_id=round_id, team_picked=fixture.home_team).all()
                    away_picks = Pick.query.filter_by(round_id=round_id, team_picked=fixture.away_team).all()
                    
                    # Process home team picks
                    for pick in home_picks:
                        if winning_team == fixture.home_team:
                            pick.is_winner = True
                            pick.is_eliminated = False
                            surviving_players.append(pick.player.name)
                        else:
                            pick.is_winner = False
                            pick.is_eliminated = True
                            pick.player.status = 'eliminated'
                            eliminated_players.append(pick.player.name)
                    
                    # Process away team picks
                    for pick in away_picks:
                        if winning_team == fixture.away_team:
                            pick.is_winner = True
                            pick.is_eliminated = False
                            surviving_players.append(pick.player.name)
                        else:
                            pick.is_winner = False
                            pick.is_eliminated = True
                            pick.player.status = 'eliminated'
                            eliminated_players.append(pick.player.name)
        
        # Mark round as completed
        round_obj.status = 'completed'
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'eliminated_players': list(set(eliminated_players)),
            'surviving_players': list(set(surviving_players)),
            'total_eliminated': len(set(eliminated_players)),
            'total_surviving': len(set(surviving_players))
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/reset-game', methods=['POST'])
@admin_required
def reset_game():
    """Reset the game by deleting all game data except players"""
    try:
        # Count items before deletion for reporting
        rounds_count = Round.query.count()
        fixtures_count = Fixture.query.count()
        picks_count = Pick.query.count()
        pick_tokens_count = PickToken.query.count()
        players_count = Player.query.count()
        
        # Delete in correct order to handle foreign key constraints
        # 1. Delete pick tokens (references players and rounds)
        PickToken.query.delete()
        
        # 2. Delete all picks (references players and rounds)
        Pick.query.delete()
        
        # 3. Delete all fixtures (references rounds)
        Fixture.query.delete()
        
        # 4. Delete all rounds (now safe to delete)
        Round.query.delete()
        
        # 5. Reset all players to active status (but keep the player records)
        Player.query.update({'status': 'active', 'unreachable': False})
        
        # Commit all changes
        db.session.commit()
        
        return jsonify({
            'success': True,
            'rounds_deleted': rounds_count,
            'fixtures_deleted': fixtures_count,
            'picks_deleted': picks_count,
            'pick_tokens_deleted': pick_tokens_count,
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
    can_edit = False  # Temporarily disabled until migration applied
    edits_remaining = 0  # Temporarily disabled until migration applied
    
    # If pick exists but token has no more edits, show read-only success page
    if existing_pick and not can_edit:
        return render_template('pick_success.html', 
                             player=player, 
                             round=round_obj, 
                             team_picked=existing_pick.team_picked,
                             already_picked=True,
                             can_edit=False,
                             edits_remaining=0)
    
    # Get fixtures for this round
    fixtures = Fixture.query.filter_by(round_id=round_obj.id).all()
    print(f"Found {len(fixtures)} fixtures for round {round_obj.id} (round number {round_obj.round_number})")
    
    # If no fixtures exist, this indicates a problem with round creation
    if not fixtures:
        print(f"ERROR: No fixtures found for round {round_obj.id}. This round may have been created without fixtures.")
    
    # Get player's previous picks to prevent reusing teams
    previous_picks = Pick.query.filter_by(player_id=player.id).all()
    used_teams = [pick.team_picked for pick in previous_picks]
    
    # Debug logging for team availability
    all_teams = []
    for fixture in fixtures:
        all_teams.extend([fixture.home_team, fixture.away_team])
    available_teams = [team for team in all_teams if team not in used_teams]
    
    print(f"Player {player.name}: {len(used_teams)} used teams, {len(available_teams)} available teams")
    print(f"Used teams: {used_teams}")
    print(f"Available teams: {set(available_teams)}")
    
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
        
        # Create or update the pick
        if existing_pick:
            # Update existing pick
            existing_pick.team_picked = team_picked
            # existing_pick.last_edited_at = datetime.utcnow()  # Temporarily disabled until migration applied
            is_new_pick = False
        else:
            # Create new pick
            pick = Pick(
                player_id=player.id,
                round_id=round_obj.id,
                team_picked=team_picked
            )
            db.session.add(pick)
            is_new_pick = True
        
        pick_token.mark_used()
        db.session.commit()
        
        return render_template('pick_success.html', 
                             player=player, 
                             round=round_obj, 
                             team_picked=team_picked,
                             already_picked=not is_new_pick,
                             can_edit=False,
                             edits_remaining=0)
    
    # GET request - show the pick form
    return render_template('pick_form.html', 
                         player=player, 
                         round=round_obj, 
                         fixtures=fixtures, 
                         used_teams=used_teams,
                         existing_pick=existing_pick,
                         can_edit=can_edit,
                         edits_remaining=edits_remaining)

@app.route('/register')
def player_registration():
    """Show player registration form for existing players to invite family members"""
    return render_template('player_registration.html')

@app.route('/register/<whatsapp_number>')
def register_with_whatsapp(whatsapp_number):
    """Show registration form pre-filled with WhatsApp number"""
    # Decode the whatsapp number (in case it's URL encoded)
    import urllib.parse
    decoded_number = urllib.parse.unquote(whatsapp_number)
    return render_template('player_registration.html', whatsapp_number=decoded_number)

@app.route('/api/register', methods=['POST'])
def api_register_player():
    """Register a new player via the public registration form"""
    try:
        data = request.get_json()
        
        if not data or not data.get('name'):
            return jsonify({'success': False, 'error': 'Player name is required'}), 400
        
        name = data['name'].strip()
        whatsapp = data.get('whatsapp_number', '').strip() or None
        
        # Check if player with same name already exists
        existing_player = Player.query.filter_by(name=name).first()
        if existing_player:
            return jsonify({'success': False, 'error': 'Player with this name already exists'}), 400
        
        # Create new player
        player = Player(
            name=name,
            whatsapp_number=whatsapp
        )
        
        db.session.add(player)
        db.session.commit()
        
        return jsonify({'success': True, 'message': f'Welcome {name}! You have been registered successfully.'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/registration-link', methods=['POST'])
@admin_required
def generate_registration_link():
    """Generate a shareable registration link for a player's WhatsApp number"""
    try:
        data = request.get_json()
        player_id = data.get('player_id')
        
        if not player_id:
            return jsonify({'success': False, 'error': 'Player ID is required'}), 400
        
        player = Player.query.get(player_id)
        if not player:
            return jsonify({'success': False, 'error': 'Player not found'}), 400
        
        if not player.whatsapp_number:
            return jsonify({'success': False, 'error': 'Player does not have a WhatsApp number'}), 400
        
        # Get base URL
        base_url = os.environ.get('BASE_URL')
        if not base_url:
            base_url = request.url_root.rstrip('/')
            if base_url.startswith('http://') and 'localhost' not in base_url and '127.0.0.1' not in base_url:
                base_url = base_url.replace('http://', 'https://')
        
        if not base_url.startswith(('http://', 'https://')):
            base_url = f"https://{base_url}"
        
        # Create registration link with the WhatsApp number
        encoded_whatsapp = urllib.parse.quote(player.whatsapp_number, safe='')
        registration_url = f"{base_url}/register/{encoded_whatsapp}"
        
        return jsonify({
            'success': True, 
            'registration_url': registration_url,
            'whatsapp_number': player.whatsapp_number,
            'player_name': player.name
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/general-registration-link', methods=['POST'])
@admin_required
def generate_general_registration_link():
    """Generate a general registration link for anyone to join"""
    try:
        # Get base URL
        base_url = os.environ.get('BASE_URL')
        if not base_url:
            base_url = request.url_root.rstrip('/')
            if base_url.startswith('http://') and 'localhost' not in base_url and '127.0.0.1' not in base_url:
                base_url = base_url.replace('http://', 'https://')
        
        if not base_url.startswith(('http://', 'https://')):
            base_url = f"https://{base_url}"
        
        # Create general registration link
        registration_url = f"{base_url}/register"
        
        return jsonify({
            'success': True, 
            'registration_url': registration_url,
            'link_type': 'general'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
