import os.path
import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]

def get_upcoming_events():
    """Reads the next 5 events from the user's primary calendar."""
    creds = None
    
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    elif os.path.exists("../token.json"):
        creds = Credentials.from_authorized_user_file("../token.json", SCOPES)
        
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            return "❌ Error: My calendar access token is expired or missing."

    try:
        service = build("calendar", "v3", credentials=creds)

        # Get current time (UTC)
        now = datetime.datetime.utcnow().isoformat() + "Z"
        
        # Fetch events
        events_result = service.events().list(
            calendarId="primary", 
            timeMin=now,
            maxResults=5, 
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        
        events = events_result.get("items", [])

        if not events:
            return "No upcoming events found."

        calendar_text = "Here is the user's upcoming schedule:\n"
        for event in events:
            start = event["start"].get("dateTime", event["start"].get("date"))
            clean_start = start.replace("T", " ").split("+")[0]
            summary = event["summary"]
            calendar_text += f"- {clean_start}: {summary}\n"
            
        return calendar_text

    except Exception as e:
        return f"❌ Calendar Error: {e}"
    
def create_event(summary, start_time_iso, end_time_iso=None):
    """Creates an event in the user's calendar."""
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            return "❌ Error: Token invalid."

    try:
        service = build("calendar", "v3", credentials=creds)
        
        if not end_time_iso:
            start_dt = datetime.datetime.fromisoformat(start_time_iso)
            end_dt = start_dt + datetime.timedelta(hours=1)
            end_time_iso = end_dt.isoformat()

        event = {
            'summary': summary,
            'start': {
                'dateTime': start_time_iso,
                'timeZone': 'Asia/Kuala_Lumpur', 
            },
            'end': {
                'dateTime': end_time_iso,
                'timeZone': 'Asia/Kuala_Lumpur',
            },
        }

        event = service.events().insert(calendarId='primary', body=event).execute()
        return f"✅ Event created: {event.get('htmlLink')}"

    except Exception as e:
        return f"❌ Failed to create event: {e}"

def delete_event(event_name):
    """Searches for an event by name and deletes the first match."""
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            return "❌ Error: Calendar token is expired or missing."

    try:
        service = build("calendar", "v3", credentials=creds)
        now = datetime.datetime.utcnow().isoformat() + "Z"
        
        # Step 1: Search the calendar for the exact text you typed
        events_result = service.events().list(
            calendarId="primary", 
            timeMin=now,
            maxResults=10, 
            singleEvents=True,
            orderBy="startTime",
            q=event_name 
        ).execute()
        
        events = events_result.get("items", [])
        
        if not events:
            return f"I couldn't find any upcoming event matching '{event_name}'."
        
        # Step 2: Grab the hidden ID of the first match and delete it
        event_to_delete = events[0]
        event_id = event_to_delete['id']
        actual_summary = event_to_delete.get('summary', 'Unknown Event')
        
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        
        return f"🗑️ Done! I have deleted '{actual_summary}' from your schedule."
        
    except Exception as e:
        return f"❌ Failed to delete event: {e}"
    
def clear_events_for_day(date_iso):
    """Deletes all events on a specific date (YYYY-MM-DD)."""
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            return "❌ Error: Calendar token is expired."

    try:
        service = build("calendar", "v3", credentials=creds)
        
        # Define the start and end of that specific day
        start_of_day = f"{date_iso}T00:00:00+08:00"
        end_of_day = f"{date_iso}T23:59:59+08:00"
        
        # Get all events for that specific day
        events_result = service.events().list(
            calendarId="primary", 
            timeMin=start_of_day,
            timeMax=end_of_day,
            singleEvents=True
        ).execute()
        
        events = events_result.get("items", [])
        
        if not events:
            return f"Your schedule is already empty on {date_iso}!"
        
        # Loop through and delete them all
        count = 0
        for event in events:
            service.events().delete(calendarId='primary', eventId=event['id']).execute()
            count += 1
        
        return f"🧨 Boom! I have completely cleared {count} events from your schedule for {date_iso}."
        
    except Exception as e:
        return f"❌ Failed to clear events: {e}"

# Test block moved to the absolute bottom
if __name__ == "__main__":
    print(get_upcoming_events())