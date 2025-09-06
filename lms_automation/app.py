from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from flask_migrate import Migrate
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import urllib.parse
from functools import wraps
from io import BytesIO

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
from models import db, Player, Round, Fixture, Pick, PickToken, ReminderSchedule


# Initialize db with app
db.init_app(app)
migrate = Migrate(app, db)

# --- Game policy configuration ---
# Postponement policy thresholds (minutes)
app.config.setdefault('POSTPONEMENT_LENIENCY_MINUTES', 60)   # early postponement window
app.config.setdefault('EARLY_PICK_WINDOW_MINUTES', 120)      # pick must predate kickoff by this to qualify

# Cycles and rounds
app.config.setdefault('MAX_ROUNDS_PER_CYCLE', 20)

# Eligibility guidance thresholds (non-blocking guidance; hard gate is >=1 eligible team)
app.config.setdefault('EARLY_ROUND_MAX', 10)
app.config.setdefault('MID_ROUND_MAX', 20)

# --- Logging helpers ---
def log_auto_pick(pick: Pick, reason: str, postponed_event_id: str = None, announcement_time: datetime = None):
    """Record that a pick was auto-assigned with policy context."""
    try:
        pick.auto_assigned = True
        pick.auto_reason = reason
        if postponed_event_id:
            pick.postponed_event_id = postponed_event_id
        if announcement_time:
            pick.announcement_time = announcement_time
        db.session.add(pick)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Failed to log auto pick for pick_id={getattr(pick, 'id', None)}: {e}")

def set_round_special_measure(round_obj: Round, measure: str, note: str = None):
    """Apply and record a special measure on a round."""
    try:
        round_obj.special_measure = measure
        round_obj.special_note = note
        db.session.add(round_obj)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Failed to set special measure for round_id={getattr(round_obj, 'id', None)}: {e}")

# --- Optional auto-migration on startup (useful for Railway/Heroku) ---
def _auto_run_migrations_if_enabled():
    flag = os.environ.get('AUTO_MIGRATE', 'true').lower()
    if flag in ('1', 'true', 'yes', 'on'):
        try:
            from flask_migrate import upgrade as _upgrade
            with app.app_context():
                _upgrade()
                app.logger.info('Auto-migration completed (alembic upgrade head).')
        except Exception as e:
            app.logger.warning(f'Auto-migration failed or skipped: {e}')

_auto_run_migrations_if_enabled()

# Admin authentication
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')  # Change this!

# --- Helpers ---
def team_abbrev(team_name: str) -> str:
    if not team_name:
        return ''
    mapping = {
        'Arsenal': 'ARS',
        'Aston Villa': 'AVFC',
        'Aston Villa FC': 'AVFC',
        'AFC Bournemouth': 'BOU',
        'Bournemouth': 'BOU',
        'Bournemouth AFC': 'BOU',
        'Brentford': 'BRE',
        'Brighton': 'BHA',
        'Brighton & Hove Albion': 'BHA',
        'Burnley': 'BUR',
        'Chelsea': 'CHE',
        'Crystal Palace': 'CRY',
        'Everton': 'EVE',
        'Everton FC': 'EVE',
        'Fulham': 'FUL',
        'Liverpool': 'LIV',
        'Liverpool FC': 'LIV',
        'Luton Town': 'LUT',
        'Manchester City': 'MCI',
        'Manchester City FC': 'MCI',
        'Manchester United': 'MUN',
        'Manchester United FC': 'MUN',
        'Newcastle': 'NEW',
        'Newcastle United': 'NEW',
        'Nottingham Forest': 'NOT',
        'Sheffield United': 'SHE',
        'Tottenham': 'TOT',
        'Tottenham Hotspur': 'TOT',
        'Tottenham Hotspur FC': 'TOT',
        'West Ham': 'WHU',
        'West Ham United': 'WHU',
        'Wolves': 'WOL',
        'Wolverhampton Wanderers': 'WOL'
    }
    return mapping.get(team_name, team_name[:3].upper())

def generate_picks_grid_xlsx():
    """Generate XLSX file for picks grid. Returns BytesIO object."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter

        rounds = Round.query.order_by(Round.round_number).all()
        players = Player.query.order_by(Player.name).all()
        picks = Pick.query.all()
        pick_map = {(p.player_id, p.round_id): p for p in picks}

        wb = Workbook()
        ws = wb.active
        ws.title = 'Picks Grid'

        # Header
        header = ['Player', 'Status'] + [f"R{r.round_number}" for r in rounds]
        ws.append(header)
        header_fill = PatternFill('solid', fgColor='222222')
        header_font = Font(color='FFFFFF', bold=True)
        for col in range(1, len(header) + 1):
            cell = ws.cell(row=1, column=col)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center')

        red_fill = PatternFill('solid', fgColor='F8D7DA')
        red_font = Font(color='842029')

        # Determine latest round for secondary sort
        latest_round = max(rounds, key=lambda r: r.round_number) if rounds else None

        # Sort players: Active → latest round team (A→Z, players with no pick last) → name
        def sort_key(player):
            status = (player.status or '').lower()
            status_pri = 0 if status == 'active' else (1 if status == 'winner' else 2)
            team = None
            if latest_round:
                pk = pick_map.get((player.id, latest_round.id))
                team = pk.team_picked if pk else None
            # Players with a team come first (0), then alphabetically; None teams last (1)
            team_presence = 0 if team else 1
            return (status_pri, team_presence, (team or 'zzzz'), player.name)

        for player in sorted(players, key=sort_key):
            row = [player.name, (player.status or '').upper()]
            for r in rounds:
                pick_obj = pick_map.get((player.id, r.id))
                if not pick_obj:
                    row.append('')
                else:
                    if pick_obj.is_winner is True:
                        suffix = ' (W)'
                    elif pick_obj.is_winner is False:
                        suffix = ' (L)'
                    else:
                        suffix = ' (P)'
                    row.append(f"{team_abbrev(pick_obj.team_picked)}{suffix}")
            ws.append(row)

            # Apply eliminated styling to entire row
            if (player.status or '').lower() == 'eliminated':
                r_idx = ws.max_row
                for c in range(1, len(header) + 1):
                    cell = ws.cell(row=r_idx, column=c)
                    cell.fill = red_fill
                    cell.font = red_font

        # Autosize columns
        for col_idx, title in enumerate(header, start=1):
            width = max(10, min(20, len(title) + 2))
            if col_idx == 1:
                width = 22
            ws.column_dimensions[get_column_letter(col_idx)].width = width

        # Freeze header row and column A (Player)
        ws.freeze_panes = 'B2'

        # Enable filter on header so sorts treat row 1 as header
        last_col_letter = get_column_letter(len(header))
        ws.auto_filter.ref = f"A1:{last_col_letter}{ws.max_row}"

        bio = BytesIO()
        wb.save(bio)
        bio.seek(0)
        return bio
        
    except Exception as e:
        print(f"Error generating XLSX: {e}")
        return None

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

@app.route('/api/rounds/<int:round_id>', methods=['GET', 'PUT', 'DELETE'])
@admin_required
def handle_round_by_id(round_id):
    """Get detailed information about a specific round, update its status, or delete it"""
    round_obj = Round.query.get_or_404(round_id)
    
    if request.method == 'GET':
        try:
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
    
    elif request.method == 'PUT':
        try:
            data = request.get_json()
            new_status = data.get('status')
            
            if not new_status:
                return jsonify({'success': False, 'error': 'Status is required'}), 400
                
            if new_status not in ['pending', 'active', 'completed']:
                return jsonify({'success': False, 'error': 'Invalid status. Must be pending, active, or completed'}), 400
            
            # If activating a round, deactivate any other active rounds
            if new_status == 'active':
                current_active = Round.query.filter_by(status='active').first()
                if current_active and current_active.id != round_id:
                    current_active.status = 'completed'
            
            old_status = round_obj.status
            round_obj.status = new_status
            db.session.commit()
            
            return jsonify({
                'success': True,
                'round_id': round_id,
                'old_status': old_status,
                'new_status': new_status,
                'message': f'Round {round_obj.round_number} status updated from {old_status} to {new_status}'
            })
            
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
    
    elif request.method == 'DELETE':
        try:
            round_number = round_obj.round_number
            
            # Check if round has any picks
            picks_count = Pick.query.filter_by(round_id=round_id).count()
            if picks_count > 0:
                return jsonify({'success': False, 'error': f'Cannot delete round with {picks_count} existing picks'}), 400
            
            # Delete all related data in correct order (foreign key constraints)
            
            # 1. Delete pick tokens first
            tokens_deleted = PickToken.query.filter_by(round_id=round_id).delete()
            
            # 2. Delete all fixtures 
            fixtures_deleted = Fixture.query.filter_by(round_id=round_id).delete()
            
            # 3. Delete the round itself
            db.session.delete(round_obj)
            
            # Commit all deletions
            db.session.commit()
            
            return jsonify({
                'success': True,
                'message': f'Round {round_number} deleted successfully',
                'details': {
                    'fixtures_deleted': fixtures_deleted,
                    'tokens_deleted': tokens_deleted
                }
            })
            
        except Exception as e:
            db.session.rollback()
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

@app.route('/api/rounds/<int:round_id>/auto-populate-results', methods=['POST'])
@admin_required
def auto_populate_results(round_id):
    """Auto-populate match results from the football API"""
    try:
        round_obj = Round.query.get_or_404(round_id)
        fixtures = Fixture.query.filter_by(round_id=round_id).all()
        
        if not fixtures:
            return jsonify({'success': False, 'error': 'No fixtures found for this round'}), 400
        
        # Get updated results from API
        from football_api import FootballDataAPI
        api = FootballDataAPI()
        fixtures_data = api.get_premier_league_fixtures(round_obj.pl_matchday)
        
        if not fixtures_data or not fixtures_data.get('matches'):
            return jsonify({'success': False, 'error': 'Unable to fetch results from football API'}), 500
        
        updated_count = 0
        api_matches = fixtures_data['matches']
        
        # Update fixtures with API results
        for fixture in fixtures:
            # Find matching API fixture by team names
            for api_match in api_matches:
                if (api_match.get('homeTeam', {}).get('name') == fixture.home_team and 
                    api_match.get('awayTeam', {}).get('name') == fixture.away_team):
                    
                    # Check if match is finished and has scores
                    if api_match.get('status') == 'FINISHED':
                        score = api_match.get('score', {})
                        full_time = score.get('fullTime', {})
                        home_score = full_time.get('home')
                        away_score = full_time.get('away')
                        
                        if home_score is not None and away_score is not None:
                            fixture.home_score = home_score
                            fixture.away_score = away_score
                            fixture.status = 'completed'
                            updated_count += 1
                    break
        
        if updated_count > 0:
            db.session.commit()
            
        return jsonify({
            'success': True,
            'updated_fixtures': updated_count,
            'message': f'Updated {updated_count} fixtures with results from API'
        })
        
    except Exception as e:
        db.session.rollback()
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
                'pick_date': pick.timestamp.strftime('%Y-%m-%d %H:%M') if getattr(pick, 'timestamp', None) else 'Unknown'
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
                    team_abbrev(pick.team_picked),
                    result,
                    pick.is_winner,
                    pick.is_eliminated,
                    pick.timestamp.strftime('%Y-%m-%d %H:%M:%S') if getattr(pick, 'timestamp', None) else ''
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

@app.route('/api/export/picks-grid')
@admin_required
def export_picks_grid_csv():
    """Export a spreadsheet-style grid: Player, Status, R1..Rn with team and result."""
    try:
        import csv
        from io import StringIO
        from flask import make_response

        rounds = Round.query.order_by(Round.round_number).all()
        players = Player.query.order_by(Player.name).all()

        # Build a quick lookup for picks
        picks = Pick.query.all()
        pick_map = {(p.player_id, p.round_id): p for p in picks}

        def pick_cell(pick_obj):
            if not pick_obj:
                return ''
            if pick_obj.is_winner is True:
                suffix = ' (W)'
            elif pick_obj.is_winner is False:
                suffix = ' (L)'
            else:
                suffix = ' (P)'
            return f"{team_abbrev(pick_obj.team_picked)}{suffix}"

        output = StringIO()
        writer = csv.writer(output)

        # Header
        header = ['Player', 'Status'] + [f"R{r.round_number}" for r in rounds]
        writer.writerow(header)

        # Rows
        for player in players:
            row = [player.name, player.status]
            for r in rounds:
                row.append(pick_cell(pick_map.get((player.id, r.id))))
            writer.writerow(row)

        response = make_response(output.getvalue())
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = 'attachment; filename=lms_picks_grid.csv'
        return response
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/export/round-picks')
@admin_required
def export_round_picks_csv():
    """Export all picks for a specific round as CSV: Player, Team, Result."""
    try:
        import csv
        from io import StringIO
        from flask import make_response

        # Determine round number: query param or fallback to active or latest
        round_num_param = request.args.get('round', type=int)

        round_obj = None
        if round_num_param:
            round_obj = Round.query.filter_by(round_number=round_num_param).first()
        if not round_obj:
            round_obj = Round.query.filter_by(status='active').first()
        if not round_obj:
            round_obj = Round.query.order_by(Round.round_number.desc()).first()
        if not round_obj:
            return jsonify({'success': False, 'error': 'No rounds available'}), 404

        picks = Pick.query.filter_by(round_id=round_obj.id).join(Player).all()

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['Round', 'Player', 'Team', 'Result'])

        # Sort: Active first, then by team picked (A→Z), then by player name
        def sort_key(pick):
            player = pick.player
            status = (player.status or '').lower()
            status_pri = 0 if status == 'active' else (1 if status == 'winner' else 2)
            team = pick.team_picked or 'zzzz'
            return (status_pri, team, player.name)

        for pick in sorted(picks, key=sort_key):
            if pick.is_winner is True:
                result = 'Winner'
            elif pick.is_winner is False:
                result = 'Eliminated'
            else:
                result = 'Pending'
            writer.writerow([f"R{round_obj.round_number}", pick.player.name, team_abbrev(pick.team_picked), result])

        response = make_response(output.getvalue())
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = f'attachment; filename=lms_round_{round_obj.round_number}_picks.csv'
        return response
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/export/picks-grid-excel')
@admin_required
def export_picks_grid_excel():
    """Export a formatted Excel-compatible HTML table with eliminated rows highlighted."""
    try:
        from flask import make_response

        rounds = Round.query.order_by(Round.round_number).all()
        players = Player.query.order_by(Player.name).all()
        picks = Pick.query.all()
        pick_map = {(p.player_id, p.round_id): p for p in picks}

        def pick_cell(pick_obj):
            if not pick_obj:
                return ''
            if pick_obj.is_winner is True:
                suffix = ' (W)'
            elif pick_obj.is_winner is False:
                suffix = ' (L)'
            else:
                suffix = ' (P)'
            return f"{pick_obj.team_picked}{suffix}"

        # Build HTML
        html = []
        html.append('<html><head><meta charset="utf-8">')
        html.append('<style>table{border-collapse:collapse;font-family:Arial,sans-serif} td,th{border:1px solid #999;padding:6px 8px} th{background:#222;color:#fff} .row-elim td{background:#f8d7da !important;color:#842029} .status-badge{padding:2px 6px;border-radius:10px;font-weight:700} .status-active{background:#198754;color:#fff} .status-eliminated{background:#dc3545;color:#fff} .status-winner{background:#0d6efd;color:#fff}</style>')
        html.append('</head><body>')
        html.append('<table>')
        # Header
        html.append('<tr><th>Player</th><th>Status</th>')
        for r in rounds:
            html.append(f'<th>R{r.round_number}</th>')
        html.append('</tr>')
        # Rows
        for player in players:
            row_class = 'row-elim' if (player.status or '').lower() == 'eliminated' else ''
            status_class = f"status-{(player.status or '').lower()}"
            status_text = (player.status or '').upper()
            html.append(f'<tr class="{row_class}"><td>{player.name}</td><td><span class="status-badge {status_class}">{status_text}</span></td>')
            for r in rounds:
                html.append(f'<td>{pick_cell(pick_map.get((player.id, r.id)))}</td>')
            html.append('</tr>')
        html.append('</table></body></html>')

        response = make_response(''.join(html))
        response.headers['Content-Type'] = 'application/vnd.ms-excel'
        response.headers['Content-Disposition'] = 'attachment; filename=lms_picks_grid.xls'
        return response
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/export/round-picks-excel')
@admin_required
def export_round_picks_excel():
    """Export a formatted per-round table with eliminated rows highlighted."""
    try:
        from flask import make_response

        round_num_param = request.args.get('round', type=int)
        round_obj = None
        if round_num_param:
            round_obj = Round.query.filter_by(round_number=round_num_param).first()
        if not round_obj:
            round_obj = Round.query.filter_by(status='active').first()
        if not round_obj:
            round_obj = Round.query.order_by(Round.round_number.desc()).first()
        if not round_obj:
            return jsonify({'success': False, 'error': 'No rounds available'}), 404

        picks = Pick.query.filter_by(round_id=round_obj.id).join(Player).all()

        html = []
        html.append('<html><head><meta charset="utf-8">')
        html.append('<style>table{border-collapse:collapse;font-family:Arial,sans-serif} td,th{border:1px solid #999;padding:6px 8px} th{background:#222;color:#fff} .row-elim td{background:#f8d7da !important;color:#842029} .status-badge{padding:2px 6px;border-radius:10px;font-weight:700} .status-active{background:#198754;color:#fff} .status-eliminated{background:#dc3545;color:#fff} .status-winner{background:#0d6efd;color:#fff}</style>')
        html.append('</head><body>')
        html.append(f'<h3>Round {round_obj.round_number} Picks</h3>')
        html.append('<table>')
        html.append('<tr><th>Player</th><th>Status</th><th>Team</th><th>Result</th></tr>')
        for pick in picks:
            status = (pick.player.status or '').lower()
            row_class = 'row-elim' if status == 'eliminated' else ''
            if pick.is_winner is True:
                result = 'Winner'
            elif pick.is_winner is False:
                result = 'Eliminated'
            else:
                result = 'Pending'
            status_badge = f"<span class='status-badge status-{status}'>{status.upper()}</span>"
            html.append(f"<tr class='{row_class}'><td>{pick.player.name}</td><td>{status_badge}</td><td>{pick.team_picked}</td><td>{result}</td></tr>")
        html.append('</table></body></html>')

        response = make_response(''.join(html))
        response.headers['Content-Type'] = 'application/vnd.ms-excel'
        response.headers['Content-Disposition'] = f'attachment; filename=lms_round_{round_obj.round_number}_picks.xls'
        return response
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/export/picks-grid-xlsx')
@admin_required
def export_picks_grid_xlsx():
    """Export a real .xlsx workbook with eliminated rows highlighted for better compatibility (Numbers/Sheets)."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
        from flask import make_response

        rounds = Round.query.order_by(Round.round_number).all()
        players = Player.query.order_by(Player.name).all()
        picks = Pick.query.all()
        pick_map = {(p.player_id, p.round_id): p for p in picks}

        wb = Workbook()
        ws = wb.active
        ws.title = 'Picks Grid'

        # Header
        header = ['Player', 'Status'] + [f"R{r.round_number}" for r in rounds]
        ws.append(header)
        header_fill = PatternFill('solid', fgColor='222222')
        header_font = Font(color='FFFFFF', bold=True)
        for col in range(1, len(header) + 1):
            cell = ws.cell(row=1, column=col)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center')

        red_fill = PatternFill('solid', fgColor='F8D7DA')
        red_font = Font(color='842029')

        # Determine latest round for secondary sort
        latest_round = max(rounds, key=lambda r: r.round_number) if rounds else None

        # Sort players: Active → latest round team (A→Z, players with no pick last) → name
        def sort_key(player):
            status = (player.status or '').lower()
            status_pri = 0 if status == 'active' else (1 if status == 'winner' else 2)
            team = None
            if latest_round:
                pk = pick_map.get((player.id, latest_round.id))
                team = pk.team_picked if pk else None
            # Players with a team come first (0), then alphabetically; None teams last (1)
            team_presence = 0 if team else 1
            return (status_pri, team_presence, (team or 'zzzz'), player.name)

        for player in sorted(players, key=sort_key):
            row = [player.name, (player.status or '').upper()]
            for r in rounds:
                pick_obj = pick_map.get((player.id, r.id))
                if not pick_obj:
                    row.append('')
                else:
                    if pick_obj.is_winner is True:
                        suffix = ' (W)'
                    elif pick_obj.is_winner is False:
                        suffix = ' (L)'
                    else:
                        suffix = ' (P)'
                    row.append(f"{team_abbrev(pick_obj.team_picked)}{suffix}")
            ws.append(row)

            # Apply eliminated styling to entire row
            if (player.status or '').lower() == 'eliminated':
                r_idx = ws.max_row
                for c in range(1, len(header) + 1):
                    cell = ws.cell(row=r_idx, column=c)
                    cell.fill = red_fill
                    cell.font = red_font

        # Autosize a bit
        for col_idx, title in enumerate(header, start=1):
            width = max(10, min(20, len(title) + 2))
            if col_idx == 1:
                width = 22
            ws.column_dimensions[get_column_letter(col_idx)].width = width

        # Freeze header row and column A (Player)
        ws.freeze_panes = 'B2'

        # Enable filter on header so sorts treat row 1 as header
        last_col_letter = get_column_letter(len(header))
        ws.auto_filter.ref = f"A1:{last_col_letter}{ws.max_row}"

        bio = BytesIO()
        wb.save(bio)
        bio.seek(0)

        response = make_response(bio.getvalue())
        response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        response.headers['Content-Disposition'] = 'attachment; filename=lms_picks_grid.xlsx'
        return response
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/export/round-picks-xlsx')
@admin_required
def export_round_picks_xlsx():
    """Export a per-round .xlsx with eliminated rows highlighted."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import PatternFill, Font, Alignment
        from flask import make_response

        round_num_param = request.args.get('round', type=int)
        round_obj = None
        if round_num_param:
            round_obj = Round.query.filter_by(round_number=round_num_param).first()
        if not round_obj:
            round_obj = Round.query.filter_by(status='active').first()
        if not round_obj:
            round_obj = Round.query.order_by(Round.round_number.desc()).first()
        if not round_obj:
            return jsonify({'success': False, 'error': 'No rounds available'}), 404

        picks = Pick.query.filter_by(round_id=round_obj.id).join(Player).all()

        wb = Workbook()
        ws = wb.active
        ws.title = f'Round {round_obj.round_number}'

        header = ['Player', 'Status', 'Team', 'Result']
        ws.append(header)
        header_fill = PatternFill('solid', fgColor='222222')
        header_font = Font(color='FFFFFF', bold=True)
        for col in range(1, len(header) + 1):
            cell = ws.cell(row=1, column=col)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center')

        red_fill = PatternFill('solid', fgColor='F8D7DA')
        red_font = Font(color='842029')

        def result_text(p):
            if p.is_winner is True:
                return 'Winner'
            if p.is_winner is False:
                return 'Eliminated'
            return 'Pending'

        # Sort: Active first, then by team picked (A→Z), then by player name
        def sort_key(pk):
            status = (pk.player.status or '').lower()
            status_pri = 0 if status == 'active' else (1 if status == 'winner' else 2)
            team = pk.team_picked or 'zzzz'
            return (status_pri, team, pk.player.name)
        picks_sorted = sorted(picks, key=sort_key)

        for pk in picks_sorted:
            row = [pk.player.name, (pk.player.status or '').upper(), team_abbrev(pk.team_picked), result_text(pk)]
            ws.append(row)
            if (pk.player.status or '').lower() == 'eliminated':
                r_idx = ws.max_row
                for c in range(1, len(header) + 1):
                    cell = ws.cell(row=r_idx, column=c)
                    cell.fill = red_fill
                    cell.font = red_font

        # Autosize
        ws.column_dimensions['A'].width = 24
        ws.column_dimensions['B'].width = 12
        ws.column_dimensions['C'].width = 18
        ws.column_dimensions['D'].width = 12
        # Freeze header row and column A (Player)
        ws.freeze_panes = 'B2'

        # Enable filter on header row
        ws.auto_filter.ref = f"A1:D{ws.max_row}"

        bio = BytesIO()
        wb.save(bio)
        bio.seek(0)

        response = make_response(bio.getvalue())
        response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        response.headers['Content-Disposition'] = f'attachment; filename=lms_round_{round_obj.round_number}_picks.xlsx'
        return response
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/download-export/<filename>')
def download_export_file(filename):
    """Download an exported file from the exports directory"""
    try:
        # Security: only allow downloading files from exports directory with specific pattern
        if not filename.startswith('lms_picks_grid_after_round_') or not filename.endswith('.xlsx'):
            return "File not found", 404
        
        filepath = os.path.join('exports', filename)
        if not os.path.exists(filepath):
            return "File not found", 404
        
        from flask import send_file
        return send_file(filepath, as_attachment=True, download_name=filename)
    except Exception as e:
        return f"Error downloading file: {str(e)}", 500

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
        
        # Only mark round as completed if all fixtures have been processed
        total_fixtures = Fixture.query.filter_by(round_id=round_id).count()
        completed_fixtures = Fixture.query.filter_by(round_id=round_id, status='completed').count()
        
        if completed_fixtures == total_fixtures:
            round_obj.status = 'completed'
        
        db.session.commit()
        
        # Auto-generate XLSX file after processing results
        xlsx_file = generate_picks_grid_xlsx()
        xlsx_filename = None
        
        if xlsx_file:
            # Save the file to disk for direct WhatsApp sharing
            try:
                os.makedirs('exports', exist_ok=True)
                xlsx_filename = f'lms_picks_grid_after_round_{round_id}.xlsx'
                filepath = f'exports/{xlsx_filename}'
                with open(filepath, 'wb') as f:
                    f.write(xlsx_file.getvalue())
                print(f"XLSX file automatically generated after processing round {round_id} results: {filepath}")
                
            except Exception as e:
                print(f"Warning: Could not save XLSX file to disk: {e}")
        
        return jsonify({
            'success': True,
            'eliminated_players': list(set(eliminated_players)),
            'surviving_players': list(set(surviving_players)),
            'total_eliminated': len(set(eliminated_players)),
            'total_surviving': len(set(surviving_players)),
            'xlsx_generated': xlsx_file is not None,
            'xlsx_filename': xlsx_filename
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/import-historical-picks', methods=['POST'])
@admin_required
def import_historical_picks():
    """Import historical picks for rounds 1 and 2"""
    try:
        # Historical data with name mapping for exact matches
        historical_data = {
            1: {  # Round 1 picks - using exact database names
                "A. Frost": ("Liverpool", True),
                "Andy Urmson": ("Liverpool", True), 
                "Chris Hollows": ("Spurs", True),
                "Dan Groves": ("Liverpool", True),
                "Greg Leigh": ("Liverpool", True),
                "Jimmy Winning": ("Liverpool", True),
                "Mrs Shooter": ("Leeds", True),
                "P. Warby": ("Forest", True),
                "Rich Amis": ("Liverpool", True),
                "Stu Hall": ("Spurs", True),
                "Terry Leigh": ("Liverpool", True),
                "Vicky Hughes": ("Spurs", True)
            },
            2: {  # Round 2 picks - using exact database names
                "A. Frost": ("Arsenal", True),
                "Andy Urmson": ("Arsenal", True),
                "Chris Hollows": ("Arsenal", True), 
                "Dan Groves": ("Arsenal", True),
                "Greg Leigh": ("Arsenal", True),
                "Jimmy Winning": ("Chelsea", True),
                "Mrs Shooter": ("Liverpool", True),
                "P. Warby": ("Arsenal", True),
                "Rich Amis": ("Arsenal", True),
                "Stu Hall": ("Chelsea", True),
                "Terry Leigh": ("Arsenal", True),
                "Vicky Hughes": ("Arsenal", True)
            }
        }
        
        imported_count = 0
        not_found_players = []
        already_exists = []
        
        for round_num, picks_data in historical_data.items():
            round_obj = Round.query.filter_by(round_number=round_num).first()
            if not round_obj:
                continue
                
            for player_name, (team, is_winner) in picks_data.items():
                player = Player.query.filter_by(name=player_name).first()
                if not player:
                    not_found_players.append(player_name)
                    continue
                
                # Check if pick already exists using raw SQL
                result = db.session.execute(db.text(
                    "SELECT id FROM picks WHERE player_id = :player_id AND round_id = :round_id"
                ), {"player_id": player.id, "round_id": round_obj.id})
                
                if result.fetchone():
                    already_exists.append(f"{player_name} R{round_num}")
                    continue
                
                # Create the pick using raw SQL to avoid schema issues
                db.session.execute(db.text(
                    "INSERT INTO picks (player_id, round_id, team_picked, is_winner, is_eliminated) "
                    "VALUES (:player_id, :round_id, :team_picked, :is_winner, :is_eliminated)"
                ), {
                    "player_id": player.id,
                    "round_id": round_obj.id, 
                    "team_picked": team,
                    "is_winner": is_winner,
                    "is_eliminated": False if is_winner else (True if is_winner is False else False)
                })
                imported_count += 1
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Successfully imported {imported_count} historical picks',
            'imported_count': imported_count,
            'not_found_players': list(set(not_found_players)),
            'already_exists': already_exists
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/debug-used-teams/<int:player_id>')
@admin_required  
def debug_used_teams(player_id):
    """Debug endpoint to check which teams a player has used"""
    try:
        player = Player.query.get_or_404(player_id)
        
        # Use raw SQL to get picks
        result = db.session.execute(db.text(
            "SELECT r.round_number, picks.team_picked, picks.is_winner "
            "FROM picks JOIN rounds r ON picks.round_id = r.id "
            "WHERE picks.player_id = :player_id ORDER BY r.round_number"
        ), {"player_id": player_id})
        
        picks_data = result.fetchall()
        used_teams = [row[1] for row in picks_data]
        
        # Get current round fixtures to compare team names
        current_round = Round.query.filter_by(status='active').first()
        fixture_teams = []
        if current_round:
            fixture_result = db.session.execute(db.text(
                "SELECT home_team, away_team FROM fixtures WHERE round_id = :round_id"
            ), {"round_id": current_round.id})
            
            for home, away in fixture_result.fetchall():
                fixture_teams.extend([home, away])
        
        return jsonify({
            'success': True,
            'player_name': player.name,
            'player_status': player.status,
            'picks_history': [
                {
                    'round': row[0],
                    'team': row[1], 
                    'result': 'WIN' if row[2] else 'LOSE' if row[2] is False else 'PENDING'
                } for row in picks_data
            ],
            'used_teams': used_teams,
            'fixture_teams': list(set(fixture_teams)),
            'team_matches': {team: team in fixture_teams for team in used_teams},
            'total_picks': len(picks_data)
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/emergency-delete-round4', methods=['POST'])
@admin_required
def emergency_delete_round4():
    """Emergency endpoint to delete Round 4 with all related data"""
    try:
        # Use raw SQL to avoid model issues
        result = db.session.execute(db.text("SELECT id FROM rounds WHERE round_number = 4"))
        round4_row = result.fetchone()
        
        if not round4_row:
            return jsonify({'success': False, 'error': 'Round 4 not found'}), 404
        
        round4_id = round4_row[0]
        
        # Delete in correct order using raw SQL
        db.session.execute(db.text("DELETE FROM pick_tokens WHERE round_id = :round_id"), {"round_id": round4_id})
        db.session.execute(db.text("DELETE FROM picks WHERE round_id = :round_id"), {"round_id": round4_id})
        db.session.execute(db.text("DELETE FROM fixtures WHERE round_id = :round_id"), {"round_id": round4_id})
        db.session.execute(db.text("DELETE FROM rounds WHERE id = :round_id"), {"round_id": round4_id})
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Round 4 emergency deleted successfully using raw SQL'
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
    can_edit = pick_token.edit_count < 2
    edits_remaining = 2 - pick_token.edit_count
    
    # If pick exists but token has no more edits, show read-only success page
    if existing_pick and not can_edit:
        return render_template('pick_success.html', 
                             player=player, 
                             round=round_obj, 
                             team_picked=existing_pick.team_picked,
                             already_picked=True,
                             can_edit=False,
                             edits_remaining=0,
                             token=token)
    
    # Get fixtures for this round
    fixtures = Fixture.query.filter_by(round_id=round_obj.id).all()
    print(f"Found {len(fixtures)} fixtures for round {round_obj.id} (round number {round_obj.round_number})")
    
    # If no fixtures exist, this indicates a problem with round creation
    if not fixtures:
        print(f"ERROR: No fixtures found for round {round_obj.id}. This round may have been created without fixtures.")
    
    # Get player's previous picks to prevent reusing teams
    previous_picks = Pick.query.filter_by(player_id=player.id).all()
    used_teams = [pick.team_picked for pick in previous_picks]
    
    # Create a normalized team matching function to handle name variations
    def normalize_team_name(team_name):
        """Normalize team names for comparison"""
        if not team_name:
            return ""
        # Remove common suffixes and normalize
        normalized = team_name.lower()
        normalized = normalized.replace(' fc', '').replace(' afc', '').replace(' united fc', '')
        normalized = normalized.replace('tottenham hotspur', 'spurs').replace('nottingham forest', 'forest')
        normalized = normalized.replace('wolverhampton wanderers', 'wolves')
        normalized = normalized.replace('brighton & hove albion', 'brighton')
        normalized = normalized.replace('afc bournemouth', 'bournemouth')
        normalized = normalized.replace('west ham united', 'west ham')
        return normalized.strip()
    
    # Create a set of normalized used team names for faster lookup
    normalized_used_teams = {normalize_team_name(team) for team in used_teams}
    
    # Function to check if a team is already used
    def is_team_used(fixture_team_name):
        return normalize_team_name(fixture_team_name) in normalized_used_teams
    
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
                                 is_team_used=is_team_used,
                                 error="Please select a team")
        
        if is_team_used(team_picked):
            return render_template('pick_form.html', 
                                 player=player, 
                                 round=round_obj, 
                                 fixtures=fixtures, 
                                 used_teams=used_teams,
                                 is_team_used=is_team_used,
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
                                 is_team_used=is_team_used,
                                 error="Invalid team selection")
        
        # Create or update the pick
        if existing_pick:
            # Update existing pick
            existing_pick.team_picked = team_picked
            existing_pick.last_edited_at = datetime.utcnow()
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
                             can_edit=pick_token.edit_count < 2,
                             edits_remaining=2 - pick_token.edit_count,
                             token=token)
    
    # GET request - show the pick form
    return render_template('pick_form.html', 
                         player=player, 
                         round=round_obj, 
                         fixtures=fixtures, 
                         used_teams=used_teams,
                         existing_pick=existing_pick,
                         can_edit=can_edit,
                         edits_remaining=edits_remaining,
                         token=token,
                         is_team_used=is_team_used)

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

@app.route('/dashboard/<token>')
def player_dashboard(token):
    """Player dashboard accessible via token"""
    # Find the pick token
    pick_token = PickToken.query.filter_by(token=token).first()
    
    if not pick_token:
        return render_template('pick_error.html', error="Invalid dashboard link"), 404
    
    player = pick_token.player
    current_round = Round.query.filter_by(status='active').first()
    
    return render_template('player_dashboard.html', 
                         player=player, 
                         current_round=current_round,
                         token=token)

@app.route('/api/player/<token>/league-table')
def get_player_league_table(token):
    """API endpoint for league table data"""
    pick_token = PickToken.query.filter_by(token=token).first()
    if not pick_token:
        return jsonify({'success': False, 'error': 'Invalid token'}), 404
    
    try:
        players = Player.query.all()
        league_data = []
        
        for player in players:
            picks = Pick.query.filter_by(player_id=player.id).all()
            wins = sum(1 for pick in picks if pick.is_winner == True)
            losses = sum(1 for pick in picks if pick.is_winner == False)
            pending = sum(1 for pick in picks if pick.is_winner is None)
            rounds_survived = wins
            
            league_data.append({
                'name': player.name,
                'status': player.status,
                'rounds_survived': rounds_survived,
                'wins': wins,
                'losses': losses,
                'pending': pending,
                'total_picks': len(picks)
            })
        
        # Sort by status priority and then by rounds survived
        status_priority = {'active': 1, 'winner': 2, 'eliminated': 3}
        league_data.sort(key=lambda x: (status_priority.get(x['status'], 4), -x['rounds_survived'], x['name']))
        
        return jsonify({
            'success': True,
            'league_table': league_data
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/player/<token>/pick-history')
def get_player_pick_history(token):
    """API endpoint for player's pick history"""
    pick_token = PickToken.query.filter_by(token=token).first()
    if not pick_token:
        return jsonify({'success': False, 'error': 'Invalid token'}), 404
    
    try:
        player = pick_token.player
        picks = Pick.query.filter_by(player_id=player.id).join(Round).order_by(Round.round_number).all()
        
        pick_history = []
        for pick in picks:
            round_info = Round.query.get(pick.round_id)
            pick_history.append({
                'round_number': round_info.round_number,
                'pl_matchday': round_info.pl_matchday,
                'team_picked': pick.team_picked,
                'is_winner': pick.is_winner,
                'timestamp': pick.timestamp.strftime('%Y-%m-%d %H:%M') if pick.timestamp else None,
                'round_status': round_info.status
            })
        
        return jsonify({
            'success': True,
            'pick_history': pick_history,
            'player_name': player.name
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/player/<token>/upcoming-fixtures')
def get_player_upcoming_fixtures(token):
    """API endpoint for upcoming fixtures and available teams"""
    pick_token = PickToken.query.filter_by(token=token).first()
    if not pick_token:
        return jsonify({'success': False, 'error': 'Invalid token'}), 404
    
    try:
        player = pick_token.player
        current_round = Round.query.filter_by(status='active').first()
        
        if not current_round:
            return jsonify({
                'success': True,
                'current_round': None,
                'fixtures': [],
                'used_teams': [],
                'has_picked': False
            })
        
        # Get fixtures for current round
        fixtures = Fixture.query.filter_by(round_id=current_round.id).all()
        
        # Get player's used teams
        previous_picks = Pick.query.filter_by(player_id=player.id).all()
        used_teams = [pick.team_picked for pick in previous_picks]
        
        # Check if player has already picked for current round
        current_pick = Pick.query.filter_by(player_id=player.id, round_id=current_round.id).first()
        
        fixtures_data = []
        for fixture in fixtures:
            fixtures_data.append({
                'home_team': fixture.home_team,
                'away_team': fixture.away_team,
                'date': fixture.date.strftime('%Y-%m-%d') if fixture.date else None,
                'time': fixture.time.strftime('%H:%M') if fixture.time else None,
                'status': fixture.status,
                'home_used': fixture.home_team in used_teams,
                'away_used': fixture.away_team in used_teams
            })
        
        return jsonify({
            'success': True,
            'current_round': {
                'round_number': current_round.round_number,
                'pl_matchday': current_round.pl_matchday,
                'status': current_round.status
            },
            'fixtures': fixtures_data,
            'used_teams': used_teams,
            'has_picked': current_pick is not None,
            'current_pick': current_pick.team_picked if current_pick else None
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# Manual WhatsApp Reminder System
class WhatsAppReminder:
    """Class to handle WhatsApp reminder links for manual sending"""
    
    @staticmethod
    def generate_reminder_data(player, round_obj, reminder_type, pick_token):
        """Generate WhatsApp reminder data for manual sending"""
        
        if not player.whatsapp_number:
            return None
            
        # Get base URL
        base_url = os.environ.get('BASE_URL', 'https://localhost:5000')
        if not base_url.startswith(('http://', 'https://')):
            base_url = f"https://{base_url}"
        
        pick_url = pick_token.get_pick_url(base_url)
        dashboard_url = f"{base_url}/dashboard/{pick_token.token}"
        
        # Customize message based on reminder type
        if reminder_type == '4_hour':
            urgency = "⏰ 4 hours left!"
            time_msg = "You have 4 hours remaining"
        elif reminder_type == '1_hour':
            urgency = "🚨 URGENT - 1 hour left!"
            time_msg = "Only 1 hour remaining"
        else:
            urgency = "📝 Reminder"
            time_msg = "Don't forget"
        
        message = f"""{urgency}

Hi {player.name}! 👋

{time_msg} to submit your pick for Round {round_obj.round_number} (PL Matchday {round_obj.pl_matchday}).

Haven't picked yet? Don't get eliminated! 

🎯 Make your pick: {pick_url}

📊 Check your dashboard: {dashboard_url}

Good luck! 🍀
Last Man Standing"""
        
        # Generate WhatsApp web link
        encoded_message = message.replace('\n', '%0A').replace(' ', '%20')
        clean_number = player.whatsapp_number.replace('+', '')
        whatsapp_link = f"https://web.whatsapp.com/send?phone={clean_number}&text={encoded_message}"
        
        return {
            'player_name': player.name,
            'player_id': player.id,
            'whatsapp_number': player.whatsapp_number,
            'message': message,
            'whatsapp_link': whatsapp_link,
            'reminder_type': reminder_type,
            'round_number': round_obj.round_number
        }
    
def get_due_reminders():
    """Get reminders that are due and ready for manual sending"""
    try:
        with app.app_context():
            pending_reminders = ReminderSchedule.get_pending_reminders()
            reminder_data = []
            
            for reminder in pending_reminders:
                # Check if player has already made a pick for this round
                existing_pick = Pick.query.filter_by(
                    player_id=reminder.player_id,
                    round_id=reminder.round_id
                ).first()
                
                if existing_pick:
                    print(f"Player {reminder.player.name} already picked for R{reminder.round.round_number}, marking reminder as sent")
                    reminder.mark_as_sent()
                    continue
                
                # Get or create pick token
                pick_token = PickToken.create_for_player_round(reminder.player_id, reminder.round_id)
                db.session.commit()
                
                # Generate reminder data
                data = WhatsAppReminder.generate_reminder_data(
                    reminder.player,
                    reminder.round,
                    reminder.reminder_type,
                    pick_token
                )
                
                if data:
                    data['reminder_id'] = reminder.id
                    data['scheduled_time'] = reminder.scheduled_time.isoformat()
                    reminder_data.append(data)
                    
            return reminder_data
            
    except Exception as e:
        print(f"Error getting due reminders: {e}")
        return []

# API Routes for reminder management
@app.route('/api/admin/schedule-reminders/<int:round_id>', methods=['POST'])
@admin_required
def schedule_reminders_for_round(round_id):
    """Admin endpoint to manually schedule reminders for a round"""
    try:
        reminders_created = ReminderSchedule.create_reminders_for_round(round_id)
        return jsonify({
            'success': True,
            'message': f'Created {reminders_created} reminders',
            'reminders_created': reminders_created
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/due-reminders')
@admin_required
def get_due_reminders_api():
    """Get all due reminders ready for manual sending"""
    try:
        reminder_data = get_due_reminders()
        return jsonify({
            'success': True,
            'due_reminders': reminder_data,
            'count': len(reminder_data)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/mark-reminder-sent/<int:reminder_id>', methods=['POST'])
@admin_required
def mark_reminder_sent(reminder_id):
    """Mark a reminder as sent after manual WhatsApp sending"""
    try:
        reminder = ReminderSchedule.query.get(reminder_id)
        if not reminder:
            return jsonify({'success': False, 'error': 'Reminder not found'}), 404
        
        reminder.mark_as_sent()
        return jsonify({
            'success': True,
            'message': 'Reminder marked as sent'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/reminders-dashboard')
@admin_required
def reminders_dashboard():
    """Admin page for managing reminders"""
    current_round = Round.query.filter_by(status='active').first()
    return render_template('reminders_dashboard.html', current_round=current_round)

@app.route('/admin/statistics')
@admin_required
def admin_statistics_page():
    """Standalone Player Statistics Dashboard page (no JS fetch required)."""
    try:
        # Competition overview
        total_players = Player.query.count()
        active_players = Player.query.filter_by(status='active').count()
        eliminated_players = Player.query.filter_by(status='eliminated').count()
        total_rounds = Round.query.count()
        completed_rounds = Round.query.filter_by(status='completed').count()
        active_round = Round.query.filter_by(status='active').first()

        # Player stats
        players = Player.query.all()
        player_stats = []
        for player in players:
            picks = Pick.query.filter_by(player_id=player.id).all()
            total_picks = len(picks)
            winning_picks = len([p for p in picks if p.is_winner])
            teams_used = list(set([p.team_picked for p in picks]))
            # Current survival streak
            streak = 0
            for p in reversed(picks):
                if p.is_winner is True:
                    streak += 1
                elif p.is_winner is False:
                    break
            player_stats.append({
                'name': player.name,
                'status': player.status,
                'total_picks': total_picks,
                'winning_picks': winning_picks,
                'success_rate': round((winning_picks / total_picks * 100) if total_picks else 0, 1),
                'current_streak': streak,
            })

        # Pick history
        all_picks = Pick.query.join(Player).join(Round).all()
        pick_history = []
        for pick in all_picks:
            pick_history.append({
                'player_name': pick.player.name,
                'round_number': pick.round.round_number,
                'team_picked': team_abbrev(pick.team_picked),
                'result': 'Winner' if pick.is_winner is True else ('Eliminated' if pick.is_winner is False else 'Pending'),
                'pick_date': pick.timestamp.strftime('%Y-%m-%d %H:%M') if getattr(pick, 'timestamp', None) else 'Unknown'
            })

        competition_stats = {
            'total_players': total_players,
            'active_players': active_players,
            'eliminated_players': eliminated_players,
            'elimination_rate': round((eliminated_players / total_players * 100) if total_players > 0 else 0, 1),
            'total_rounds': total_rounds,
            'completed_rounds': completed_rounds,
            'current_round': active_round.round_number if active_round else None
        }

        # Order player stats: active first, then success rate desc, then name
        def ps_key(p):
            pri = 0 if p['status'] == 'active' else (1 if p['status'] == 'winner' else 2)
            return (pri, -p['success_rate'], p['name'])
        player_stats = sorted(player_stats, key=ps_key)

        # Sort pick history by round then name
        pick_history = sorted(pick_history, key=lambda h: (h['round_number'], h['player_name']))

        return render_template(
            'admin_statistics.html',
            competition_stats=competition_stats,
            player_stats=player_stats,
            pick_history=pick_history
        )
    except Exception as e:
        return render_template('admin_statistics.html', error=str(e), competition_stats={}, player_stats=[], pick_history=[]), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
# --- Public pages ---
@app.route('/rules')
def rules():
    return render_template('rules.html')
