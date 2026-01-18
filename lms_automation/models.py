from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy
import secrets
import string

db = SQLAlchemy()

class Player(db.Model):
    __tablename__ = 'players'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    whatsapp_number = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(20), default='active')  # active, eliminated, winner
    unreachable = db.Column(db.Boolean, default=False)

    # Admin bookkeeping: last known date player paid entry fee (informational only)
    last_entry_fee_paid_at = db.Column(db.Date, nullable=True)

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
    first_kickoff_at = db.Column(db.DateTime, nullable=True)
    special_measure = db.Column(db.String(50), nullable=True)  # universal_bye, frozen, void, override
    special_note = db.Column(db.Text, nullable=True)
    cycle_number = db.Column(db.Integer, default=1)
    
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
    last_edited_at = db.Column(db.DateTime, nullable=True)
    
    # Audit fields for auto-pick/postponement policy
    auto_assigned = db.Column(db.Boolean, default=False)
    auto_reason = db.Column(db.String(50), nullable=True)  # missed_deadline, postponement_early, postponement_late, etc.
    postponed_event_id = db.Column(db.String(50), nullable=True)
    announcement_time = db.Column(db.DateTime, nullable=True)
    
    def __repr__(self):
        return f'<Pick {self.player.name} - {self.team_picked}>'

class PickToken(db.Model):
    __tablename__ = 'pick_tokens'
    
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('players.id'), nullable=False)
    round_id = db.Column(db.Integer, db.ForeignKey('rounds.id'), nullable=False)
    token = db.Column(db.String(64), nullable=False, unique=True, index=True)
    is_used = db.Column(db.Boolean, default=False)
    edit_count = db.Column(db.Integer, default=0)
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
    def create_for_player_round(player_id, round_id, expires_hours=168, force_new=False):  # default kept for fallback when no deadline
        """Create or fetch a pick token for a player and round.
        - Reuse existing token only if it's still valid and not forced to regenerate.
        - Prefer setting expiry to the round deadline (end_date) when available.
        """
        # Try to reuse an existing valid token unless forced
        if not force_new:
            existing_token = PickToken.query.filter_by(
                player_id=player_id,
                round_id=round_id
            ).first()
            if existing_token and existing_token.is_valid():
                return existing_token

        # Determine expiry from round deadline when present
        round_obj = Round.query.get(round_id)
        expires_at = None
        if round_obj and round_obj.end_date:
            # Use the round deadline; if it's in the past, fall back to expires_hours window
            if round_obj.end_date > datetime.utcnow():
                expires_at = round_obj.end_date
            elif expires_hours:
                expires_at = datetime.utcnow() + timedelta(hours=expires_hours)
        elif expires_hours:
            expires_at = datetime.utcnow() + timedelta(hours=expires_hours)

        # Create new token
        token = PickToken(
            player_id=player_id,
            round_id=round_id,
            token=PickToken.generate_token(),
            expires_at=expires_at
        )

        db.session.add(token)
        return token
    
    def is_valid(self):
        """Check if token is valid (not exceeded edit limit and not expired)"""
        if self.edit_count >= 2:
            return False
        if self.expires_at and datetime.utcnow() > self.expires_at:
            return False
        return True
    
    def mark_used(self):
        """Increment edit count and update used_at timestamp"""
        self.edit_count += 1
        self.used_at = datetime.utcnow()
        if self.edit_count >= 2:
            self.is_used = True
    
    def get_pick_url(self, base_url='https://localhost:5000'):
        """Get the full pick URL for this token"""
        # Ensure base_url is clean and properly formatted
        base_url = base_url.rstrip('/')
        
        # Ensure base_url has protocol (critical for mobile WhatsApp)
        if not base_url.startswith(('http://', 'https://')):
            base_url = f"https://{base_url}"
            
        return f"{base_url}/pick/{self.token}"

class ReminderSchedule(db.Model):
    __tablename__ = 'reminder_schedules'
    
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('players.id'), nullable=False)
    round_id = db.Column(db.Integer, db.ForeignKey('rounds.id'), nullable=False)
    reminder_type = db.Column(db.String(20), nullable=False)  # '4_hour' or '2_hour'
    scheduled_time = db.Column(db.DateTime, nullable=False)
    sent_at = db.Column(db.DateTime, nullable=True)
    is_sent = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    player = db.relationship('Player', backref='reminder_schedules')
    round = db.relationship('Round', backref='reminder_schedules')
    
    def __repr__(self):
        return f'<ReminderSchedule {self.reminder_type} for {self.player.name} R{self.round.round_number}>'
    
    @staticmethod
    def create_reminders_for_round(round_id):
        """Create reminder schedules for all active players in a round.
        Anchors reminders to first_kickoff_at when available, otherwise end_date.
        Generates 4-hour and 2-hour reminders before the anchor time.
        """
        round_obj = Round.query.get(round_id)
        if not round_obj:
            return False

        # Determine anchor time (prefer first_kickoff_at; fallback to end_date)
        anchor_time = round_obj.first_kickoff_at or round_obj.end_date
        if not anchor_time:
            # Try derive from fixtures
            try:
                fixtures = round_obj.fixtures or []
                earliest = None
                for fx in fixtures:
                    if getattr(fx, 'date', None) and getattr(fx, 'time', None):
                        dt = datetime.combine(fx.date, fx.time)
                        if earliest is None or dt < earliest:
                            earliest = dt
                anchor_time = earliest
            except Exception:
                anchor_time = None
        if not anchor_time:
            return False
        
        active_players = Player.query.filter_by(status='active').all()
        
        # Calculate reminder times relative to anchor
        four_hour_reminder = anchor_time - timedelta(hours=4)
        two_hour_reminder = anchor_time - timedelta(hours=2)
        
        reminders_created = 0
        
        for player in active_players:
            # Check if reminders already exist
            existing_4h = ReminderSchedule.query.filter_by(
                player_id=player.id, 
                round_id=round_id, 
                reminder_type='4_hour'
            ).first()
            
            existing_2h = ReminderSchedule.query.filter_by(
                player_id=player.id, 
                round_id=round_id, 
                reminder_type='2_hour'
            ).first()
            
            # Create 4-hour reminder (create even if scheduled time is in the past so it shows as due)
            if not existing_4h:
                reminder_4h = ReminderSchedule(
                    player_id=player.id,
                    round_id=round_id,
                    reminder_type='4_hour',
                    scheduled_time=four_hour_reminder
                )
                db.session.add(reminder_4h)
                reminders_created += 1
            
            # Create 2-hour reminder (create even if scheduled time is in the past so it shows as due)
            if not existing_2h:
                reminder_2h = ReminderSchedule(
                    player_id=player.id,
                    round_id=round_id,
                    reminder_type='2_hour',
                    scheduled_time=two_hour_reminder
                )
                db.session.add(reminder_2h)
                reminders_created += 1
        
        db.session.commit()
        return reminders_created
    
    @staticmethod
    def get_pending_reminders():
        """Get all reminders that are due and haven't been sent"""
        return ReminderSchedule.query.filter(
            ReminderSchedule.is_sent == False,
            ReminderSchedule.scheduled_time <= datetime.utcnow()
        ).all()
    
    def mark_as_sent(self):
        """Mark reminder as sent"""
        self.is_sent = True
        self.sent_at = datetime.utcnow()
        db.session.commit()
