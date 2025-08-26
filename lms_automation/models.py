from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy
import secrets
import string

db = SQLAlchemy()

class Player(db.Model):
    __tablename__ = 'players'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    whatsapp_number = db.Column(db.String(20), nullable=True, unique=True)
    status = db.Column(db.String(20), default='active')  # active, eliminated, winner
    unreachable = db.Column(db.Boolean, default=False)
    
    picks = db.relationship('Pick', backref='player', lazy=True)
    
    def __repr__(self):
        return f'<Player {self.name}>'

class Round(db.Model):
    __tablename__ = 'rounds'
    
    id = db.Column(db.Integer, primary_key=True)
    round_number = db.Column(db.Integer, nullable=False)
    pl_matchday = db.Column(db.Integer, nullable=True)  # Premier League matchday (1-38)
    start_date = db.Column(db.DateTime, nullable=True)
    end_date = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='pending')  # pending, active, completed
    
    fixtures = db.relationship('Fixture', backref='round', lazy=True)
    picks = db.relationship('Pick', backref='round', lazy=True)
    
    def __repr__(self):
        return f'<Round {self.round_number} (PL MD {self.pl_matchday})>'

class Fixture(db.Model):
    __tablename__ = 'fixtures'
    
    id = db.Column(db.Integer, primary_key=True)
    round_id = db.Column(db.Integer, db.ForeignKey('rounds.id'), nullable=False)
    event_id = db.Column(db.String(50), nullable=True)  # External API event ID
    home_team = db.Column(db.String(100), nullable=False)
    away_team = db.Column(db.String(100), nullable=False)
    date = db.Column(db.Date, nullable=True)
    time = db.Column(db.Time, nullable=True)
    home_score = db.Column(db.Integer, nullable=True)
    away_score = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), default='scheduled')  # scheduled, live, completed, postponed
    
    def __repr__(self):
        return f'<Fixture {self.home_team} vs {self.away_team}>'

class Pick(db.Model):
    __tablename__ = 'picks'
    
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('players.id'), nullable=False)
    round_id = db.Column(db.Integer, db.ForeignKey('rounds.id'), nullable=False)
    team_picked = db.Column(db.String(100), nullable=False)
    is_winner = db.Column(db.Boolean, nullable=True)  # None=pending, True=won, False=lost
    is_eliminated = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<Pick {self.player.name} - {self.team_picked}>'

class PickToken(db.Model):
    __tablename__ = 'pick_tokens'
    
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('players.id'), nullable=False)
    round_id = db.Column(db.Integer, db.ForeignKey('rounds.id'), nullable=False)
    token = db.Column(db.String(64), nullable=False, unique=True, index=True)
    is_used = db.Column(db.Boolean, default=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used_at = db.Column(db.DateTime, nullable=True)
    
    # Relationships
    player = db.relationship('Player', backref='pick_tokens', lazy=True)
    round = db.relationship('Round', backref='pick_tokens', lazy=True)
    
    def __repr__(self):
        return f'<PickToken {self.token[:8]}... for {self.player.name if self.player else "Unknown"}>'
    
    @staticmethod
    def generate_token():
        """Generate a secure random token"""
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(32))
    
    @staticmethod
    def create_for_player_round(player_id, round_id, expires_hours=168):  # 7 days default
        """Create a new pick token for a player and round"""
        # Check if token already exists
        existing_token = PickToken.query.filter_by(
            player_id=player_id, 
            round_id=round_id, 
            is_used=False
        ).first()
        
        if existing_token:
            return existing_token
        
        # Create new token
        token = PickToken(
            player_id=player_id,
            round_id=round_id,
            token=PickToken.generate_token(),
            expires_at=datetime.utcnow() + timedelta(hours=expires_hours) if expires_hours else None
        )
        
        db.session.add(token)
        return token
    
    def is_valid(self):
        """Check if token is valid (not used and not expired)"""
        if self.is_used:
            return False
        if self.expires_at and datetime.utcnow() > self.expires_at:
            return False
        return True
    
    def mark_used(self):
        """Mark token as used"""
        self.is_used = True
        self.used_at = datetime.utcnow()
    
    def get_pick_url(self, base_url='https://localhost:5000'):
        """Get the full pick URL for this token"""
        # Ensure base_url is clean and properly formatted
        base_url = base_url.rstrip('/')
        
        # Ensure base_url has protocol (critical for mobile WhatsApp)
        if not base_url.startswith(('http://', 'https://')):
            base_url = f"https://{base_url}"
            
        return f"{base_url}/pick/{self.token}"