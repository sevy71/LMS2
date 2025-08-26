import requests
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

class FootballDataAPI:
    def __init__(self):
        self.api_token = os.environ.get('FOOTBALL_API_TOKEN', 'fffc0c77c6d24545958210fcec5f4f03')
        self.base_url = 'https://api.football-data.org/v4'
        self.headers = {
            'X-Auth-Token': self.api_token,
            'Content-Type': 'application/json'
        }
        # Premier League competition ID
        self.premier_league_id = 2021

    def get_premier_league_fixtures(self, matchday: Optional[int] = None, season: str = None) -> Dict:
        """
        Fetch Premier League fixtures for the current season
        
        Args:
            matchday: Specific matchday to fetch (1-38), if None fetches all
            season: Season year to fetch (defaults to current season)
            
        Returns:
            Dictionary containing fixtures data
        """
        try:
            url = f"{self.base_url}/competitions/{self.premier_league_id}/matches"
            
            # Use current season (2025/26) if no season specified  
            if season is None:
                params = {'season': '2025'}  # Current 2025/26 season
            else:
                params = {'season': season}
            
            if matchday:
                params['matchday'] = matchday
            
            print(f"API Request: {url} with params: {params}")
            
            # Add a small delay to avoid rate limiting
            time.sleep(0.1)
            
            response = requests.get(url, headers=self.headers, params=params, timeout=10)
            
            print(f"API Response Status: {response.status_code}")
            
            if response.status_code == 429:
                print("Rate limit hit, waiting 60 seconds...")
                time.sleep(60)
                response = requests.get(url, headers=self.headers, params=params, timeout=10)
            
            if response.status_code == 403:
                print("API Error: 403 Forbidden - Check your API token and subscription")
                print(f"Using API token: {self.api_token[:10]}...")
                return {'matches': []}
            elif response.status_code == 404:
                print(f"API Error: 404 Not Found - Season or competition not found")
                return {'matches': []}  
            elif response.status_code != 200:
                print(f"API Error: {response.status_code} - {response.text}")
                return {'matches': []}
            
            data = response.json()
            print(f"API Response: Found {len(data.get('matches', []))} matches")
            
            # Print some debug info about the matches
            if data.get('matches'):
                first_match = data['matches'][0]
                print(f"First match sample: {first_match.get('homeTeam', {}).get('name')} vs {first_match.get('awayTeam', {}).get('name')} on {first_match.get('utcDate')}")
                
                # Print season info if available
                if 'season' in data:
                    print(f"Season info: {data['season']}")
            
            return data
            
        except requests.RequestException as e:
            print(f"Error fetching fixtures: {e}")
            if "429" in str(e):
                print("Rate limiting detected. The free tier has limited requests per minute.")
            return {'matches': []}

    def get_available_matchdays(self) -> List[int]:
        """
        Get list of available matchdays for the current season
        
        Returns:
            List of matchday numbers
        """
        try:
            fixtures_data = self.get_premier_league_fixtures()
            matchdays = set()
            
            for match in fixtures_data.get('matches', []):
                if match.get('matchday'):
                    matchdays.add(match['matchday'])
            
            return sorted(list(matchdays))
            
        except Exception as e:
            print(f"Error getting matchdays: {e}")
            return list(range(1, 39))  # Default to 1-38

    def format_fixtures_for_db(self, fixtures_data: Dict, target_matchday: int) -> List[Dict]:
        """
        Format fixtures data for database insertion
        
        Args:
            fixtures_data: Raw API response
            target_matchday: The matchday to filter for
            
        Returns:
            List of formatted fixture dictionaries
        """
        formatted_fixtures = []
        
        for match in fixtures_data.get('matches', []):
            if match.get('matchday') != target_matchday:
                continue
                
            # Parse match date
            match_date = None
            match_time = None
            if match.get('utcDate'):
                try:
                    dt = datetime.fromisoformat(match['utcDate'].replace('Z', '+00:00'))
                    match_date = dt.date()
                    match_time = dt.time()
                except ValueError:
                    pass
            
            # Extract team names
            home_team = match.get('homeTeam', {}).get('name', 'TBD')
            away_team = match.get('awayTeam', {}).get('name', 'TBD')
            
            # Extract scores if available
            score = match.get('score', {})
            full_time = score.get('fullTime', {})
            home_score = full_time.get('home')
            away_score = full_time.get('away')
            
            # Determine match status
            status_map = {
                'SCHEDULED': 'scheduled',
                'LIVE': 'live', 
                'IN_PLAY': 'live',
                'PAUSED': 'live',
                'FINISHED': 'completed',
                'POSTPONED': 'postponed',
                'SUSPENDED': 'postponed',
                'CANCELLED': 'postponed'
            }
            match_status = status_map.get(match.get('status'), 'scheduled')
            
            formatted_fixture = {
                'event_id': str(match.get('id', '')),
                'home_team': home_team,
                'away_team': away_team,
                'date': match_date,
                'time': match_time,
                'home_score': home_score,
                'away_score': away_score,
                'status': match_status,
                'pl_matchday': target_matchday
            }
            
            formatted_fixtures.append(formatted_fixture)
        
        return formatted_fixtures

    def get_matchday_info(self, matchday: int) -> Dict:
        """
        Get information about a specific matchday
        
        Args:
            matchday: The matchday number (1-38)
            
        Returns:
            Dictionary with matchday information
        """
        try:
            fixtures_data = self.get_premier_league_fixtures(matchday)
            matches = fixtures_data.get('matches', [])
            
            if not matches:
                return {
                    'matchday': matchday,
                    'fixture_count': 0,
                    'earliest_date': None,
                    'latest_date': None
                }
            
            dates = []
            for match in matches:
                if match.get('utcDate'):
                    try:
                        dt = datetime.fromisoformat(match['utcDate'].replace('Z', '+00:00'))
                        dates.append(dt.date())
                    except ValueError:
                        pass
            
            return {
                'matchday': matchday,
                'fixture_count': len(matches),
                'earliest_date': min(dates) if dates else None,
                'latest_date': max(dates) if dates else None
            }
            
        except Exception as e:
            print(f"Error getting matchday info: {e}")
            return {
                'matchday': matchday,
                'fixture_count': 0,
                'earliest_date': None,
                'latest_date': None
            }