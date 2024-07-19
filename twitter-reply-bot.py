import tweepy
from airtable import Airtable
from datetime import datetime, timedelta
import schedule
import time
import os
import requests
import logging
import json
import redis
from redis import Redis
from requests_oauthlib import OAuth1Session

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

redis_url = os.getenv("REDIS_URL")
redis_client = redis.Redis.from_url(redis_url)

# Load your Twitter and Airtable API keys (preferably from environment variables, config file, or within the railyway app)
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")

AIRTABLE_PERSONAL_ACCESS_TOKEN = os.getenv("AIRTABLE_PERSONAL_ACCESS_TOKEN")
AIRTABLE_BASE_KEY = os.getenv("AIRTABLE_BASE_KEY")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME")

CHATBASE_API_KEY = os.getenv("CHATBASE_API_KEY")
CHATBASE_API_URL = os.getenv("CHATBASE_API_URL")
CHATBOT_ID = os.getenv("CHATBOT_ID")

HCTI_API_ENDPOINT = "https://hcti.io/v1/image"
HCTI_API_USER_ID = os.getenv("HCTI_API_USER_ID")  # Get from environment variables
HCTI_API_KEY = os.getenv("HCTI_API_KEY")         # Get from environment variables

# Check if required variables are set
if not all([TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET, TWITTER_BEARER_TOKEN]):
    raise EnvironmentError("One or more Twitter API environment variables are not set.")

def fetch_airtable_data():
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_KEY}/{AIRTABLE_TABLE_NAME}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_PERSONAL_ACCESS_TOKEN}"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()["records"]

# Function to get Chatbase chatbot response with conversation context
def get_chatbot_response(user_message):
    url = 'https://www.chatbase.co/api/v1/chat'
    headers = {
        'Authorization': f'Bearer {os.environ.get("CHATBASE_API_KEY")}',
        'Content-Type': 'application/json'
    }

    data = {
        "messages": [
            {"content": user_message, "role": "user"}
        ],
        "chatbotId": os.environ.get("CHATBOT_ID"),
        "stream": False,
        "temperature": 0 
    }

    try:
        response = requests.post(url, headers=headers, data=json.dumps(data))
        response.raise_for_status()  
        json_data = response.json()
        return json_data['text']

    except requests.exceptions.RequestException as e:
        logging.error(f"Error getting Chatbase response: {e}")
        return "I'm sorry, I couldn't process your request at this time."

# TwitterBot class to help us organize our code and manage shared state
class TwitterBot:
    def __init__(self):
        self.twitter_api = tweepy.Client(bearer_token=TWITTER_BEARER_TOKEN,
                                         consumer_key=TWITTER_API_KEY,
                                         consumer_secret=TWITTER_API_SECRET,
                                         access_token=TWITTER_ACCESS_TOKEN,
                                         access_token_secret=TWITTER_ACCESS_TOKEN_SECRET,
                                         wait_on_rate_limit=True)

        self.airtable = Airtable(AIRTABLE_BASE_KEY, AIRTABLE_TABLE_NAME, AIRTABLE_PERSONAL_ACCESS_TOKEN)
        self.twitter_me_id = self.get_me_id()
        self.tweet_response_limit = 35 # How many tweets to respond to each time the program wakes up
        
        # For statics tracking for each run. This is not persisted anywhere, just logging
        self.mentions_found = 0
        self.mentions_replied = 0
        self.mentions_replied_errors = 0

        self.auth = tweepy.OAuthHandler(TWITTER_API_KEY, TWITTER_API_SECRET)
        self.auth.set_access_token(TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET)
        self.api_v1 = tweepy.API(self.auth)

    # Generate a response using the Chatbase API
    def generate_response(self, mentioned_conversation_tweet_text):
        return get_chatbot_response(mentioned_conversation_tweet_text)
    
    # Generate a response using the API
    def respond_to_mention(self, mention, mentioned_conversation_tweet):
        response_text = self.generate_response(mentioned_conversation_tweet.text)
        image_url = self.generate_image_from_response(response_text)

        try:
            if image_url:
                # Use OAuth1Session for v1.1 authentication
                auth = OAuth1Session(
                    TWITTER_API_KEY,
                    client_secret=TWITTER_API_SECRET,
                    resource_owner_key=TWITTER_ACCESS_TOKEN,
                    resource_owner_secret=TWITTER_ACCESS_TOKEN_SECRET,
                )

                # Download the image
                image_data = requests.get(image_url).content

                # Upload to Twitter v1.1
                upload_url = "https://upload.twitter.com/1.1/media/upload.json"
                files = {"media": image_data}
                response = auth.post(upload_url, files=files)
                response.raise_for_status()

                media_id = response.json()["media_id"]

                # Use tweepy.Client for v2 tweet creation
                response_tweet = self.twitter_api.create_tweet(media_ids=[media_id], in_reply_to_tweet_id=mention.id)

            else:
                # If image generation fails, send a text tweet instead (using v2 API)
                response_tweet = self.twitter_api.create_tweet(
                    text=response_text, 
                    in_reply_to_tweet_id=mention.id
                )
        except Exception as e:
            print(e)
            self.mentions_replied_errors += 1
            return
        
        # Log the response in Airtable if it was successful
        self.airtable.insert({
            'mentioned_conversation_tweet_id': str(mentioned_conversation_tweet.id),
            'mentioned_conversation_tweet_text': mentioned_conversation_tweet.text,
            'tweet_response_id': response_tweet.data['id'],
            'tweet_response_text': response_text,
            'mentioned_at': mention.created_at.isoformat()
        })
        redis_client.set("last_tweet_id", str(mention.id)) 

        return True
    
    # Returns the ID of the authenticated user for tweet creation purposes
    def get_me_id(self):
        return self.twitter_api.get_me()[0].id
    
    # Returns the parent tweet text of a mention if it exists. Otherwise returns None
    # We use this to since we want to respond to the parent tweet, not the mention itself
    def get_mention_conversation_tweet(self, mention):
        # Check to see if mention has a field 'conversation_id' and if it's not null
        if mention.conversation_id is not None:
            conversation_tweet = self.twitter_api.get_tweet(mention.conversation_id).data
            return conversation_tweet
        return None

    # Get mentioned to the user that's authenticated and running the bot.
    # Using a lookback window of 2 hours to avoid parsing over too many tweets
    def get_mentions(self):
        since_id = redis_client.get("last_tweet_id")
        if since_id:
            since_id = int(since_id)  # Convert to int for Twitter API
        else:
            since_id = 1

        return self.twitter_api.get_users_mentions(
            id=self.twitter_me_id,
            since_id=since_id, 
            expansions=['referenced_tweets.id'],
            tweet_fields=['created_at', 'conversation_id']).data

    # Checking to see if we've already responded to a mention with what's logged in Airtable
    def check_already_responded(self, mentioned_conversation_tweet_id):
        records = self.airtable.get_all(view='Grid view')
        for record in records:
            if record['fields'].get('mentioned_conversation_tweet_id') == str(mentioned_conversation_tweet_id):
                return True
        return False

    # Run through all mentioned tweets and generate a response
    def respond_to_mentions(self):
        mentions = self.get_mentions()

        # If no mentions, just return
        if not mentions:
            print("No mentions found")
            return
        
        self.mentions_found = len(mentions)

        for mention in mentions[:self.tweet_response_limit]:
            # Getting the mention's conversation tweet
            mentioned_conversation_tweet = self.get_mention_conversation_tweet(mention)
            
            # If the mention *is* the conversation or you've already responded, skip it and don't respond
            if (mentioned_conversation_tweet.id != mention.id
                and not self.check_already_responded(mentioned_conversation_tweet.id)):

                self.respond_to_mention(mention, mentioned_conversation_tweet)
        return True
    
    # The main entry point for the bot with some logging
    def execute_replies(self):
        print(f"Starting Job: {datetime.utcnow().isoformat()}")
        self.respond_to_mentions()
        print(f"Finished Job: {datetime.utcnow().isoformat()}, Found: {self.mentions_found}, Replied: {self.mentions_replied}, Errors: {self.mentions_replied_errors}")
    
    def generate_image_from_response(self, response_text):
    # Basic HTML template with styling
    html_template = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto&display=swap');
    body {{ 
        font-family: 'Roboto', sans-serif; 
        background-color: white; 
        color: black; 
        padding: 10px; 
        width: 1170px; /* iPhone 14 width */
        height: 2532px; /* iPhone 14 height */
        margin: 0 auto; 
        overflow: hidden; 
        box-sizing: border-box; 
    }}
    .response-box {{ 
        background-color: #fff2ac;
        background-image: linear-gradient(to right, #ffe359 0%, #fff2ac 100%);
        padding: 15px; 
        border-radius: 8px; 
        overflow-wrap: break-word;
        word-wrap: break-word; 
        max-width: 100%; /* Ensure the box doesn't overflow its container */
        box-sizing: border-box; /* Include padding and border in the element's total width and height */
    }}
    </style>
    </head>
    <body>
    <div class="response-box">{response_text}</div>
    </body>
    </html>
    """

    data = {
        'html': html_template,
        'google_fonts': "Roboto"
    }

    try:
        image_response = requests.post(url=HCTI_API_ENDPOINT, data=data, auth=(HCTI_API_USER_ID, HCTI_API_KEY))
        image_response.raise_for_status()
        image_url = image_response.json()['url']
        return image_url
    except requests.exceptions.RequestException as e:
        logging.error(f"Error generating image: {e}")
        return None

        try:
            image_response = requests.post(url=HCTI_API_ENDPOINT, data=data, auth=(HCTI_API_USER_ID, HCTI_API_KEY))
            image_response.raise_for_status()
            image_url = image_response.json()['url']
            return image_url
        except requests.exceptions.RequestException as e:
            logging.error(f"Error generating image: {e}")
            return None  # Handle the error (e.g., tweet a text message instead)
        
# Global bot instance to maintain state and avoid re-authentication
bot = TwitterBot()
# The job that we'll schedule to run every X minutes

def job():
    print(f"Job executed at {datetime.utcnow().isoformat()}")
    bot = TwitterBot()
    bot.execute_replies()

if __name__ == "__main__":
    # Schedule the job to run every 5 minutes. Edit to your liking, but watch out for rate limits
    schedule.every(3).minutes.do(job)
    while True:
        schedule.run_pending()
        time.sleep(1)
