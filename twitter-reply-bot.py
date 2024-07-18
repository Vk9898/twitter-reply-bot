import tweepy
from airtable import Airtable
from datetime import datetime, timedelta
import schedule
import time
import os
import requests
import logging
import json

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load your Twitter and Airtable API keys (preferably from environment variables, config file, or within the railyway app)
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
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

        # Truncate the response text to 200 characters
        truncated_text = json_data['text'][:200]

        return truncated_text 

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

        self.airtable = Airtable(AIRTABLE_BASE_KEY, AIRTABLE_TABLE_NAME, AIRTABLE_API_KEY)
        self.twitter_me_id = self.get_me_id()
        self.tweet_response_limit = 35 # How many tweets to respond to each time the program wakes up

        # For statics tracking for each run. This is not persisted anywhere, just logging
        self.mentions_found = 0
        self.mentions_replied = 0
        self.mentions_replied_errors = 0

    # Generate a response using the Chatbase API
    def generate_response(self, mentioned_conversation_tweet_text):
        return get_chatbot_response(mentioned_conversation_tweet_text)
    
    # Generate a response using the API
    def respond_to_mention(self, mention, mentioned_conversation_tweet):
        response_text = self.generate_response(mentioned_conversation_tweet.text)
        image_url = self.generate_image_from_response(response_text)

        try:
            if image_url:
                # Download the image and upload it as a tweet
                image_data = requests.get(image_url).content
                media = self.twitter_api.media_upload(filename="response.png", file=image_data)
                response_tweet = self.twitter_api.create_tweet(media_ids=[media.media_id], in_reply_to_tweet_id=mention.id)
            else:
                # If image generation fails, send a text tweet instead
                response_tweet = self.twitter_api.create_tweet(text=response_text, in_reply_to_tweet_id=mention.id)
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
            'tweet_response_created_at': datetime.utcnow().isoformat(),
            'mentioned_at': mention.created_at.isoformat()
        })
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
        # If doing this in prod make sure to deal with pagination. There could be a lot of mentions!
        # Get current time in UTC
        now = datetime.utcnow()

        # Subtract 2 hours to get the start time
        start_time = now - timedelta(minutes=20)

        # Convert to required string format
        start_time_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        return self.twitter_api.get_users_mentions(id=self.twitter_me_id,
                                                   start_time=start_time_str,
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
        <style>
        body {{ font-family: 'Roboto', sans-serif; background-color: #15202b; color: white; padding: 20px; }}
        .response-box {{ background-color: #192734; padding: 15px; border-radius: 8px; }}
        </style>
        </head>
        <body>
        <div class="response-box">{response_text}</div>
        </body>
        </html>
        """

        data = {
            'html': html_template,
            'css': "",  # You can add more CSS here if needed
            'google_fonts': "Roboto"
        }

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
