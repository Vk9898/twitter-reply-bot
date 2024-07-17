import requests
import json
import imgkit
import redis
import time
import tweepy
import os
from io import BytesIO
from PIL import Image
from airtable import Airtable
from datetime import datetime, timedelta
import schedule
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables
REDIS_URL = os.getenv("REDIS_URL", 'redis://localhost:6379')
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

TEMPLATE_FILE_PATH = os.path.join(os.path.dirname(__file__), 'template.html')  # Get absolute path

# Attempt to load template from file
try:
    with open(TEMPLATE_FILE_PATH, 'r', encoding='utf-8') as file:
        HTML_TEMPLATE = file.read()
except FileNotFoundError:
    raise FileNotFoundError(f"Template file not found at: {TEMPLATE_FILE_PATH}")
except Exception as e:
    raise RuntimeError(f"Error loading HTML template: {e}")

# Check if required variables are set
if not all([TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET, TWITTER_BEARER_TOKEN]):
    raise EnvironmentError("One or more Twitter API environment variables are not set.")

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

        response_text = json_data['text']

        # Truncate the response text to 200 characters, but allow up to 3000 for the image
        truncated_text = response_text[:200]
        formatted_html = HTML_TEMPLATE.replace("{response_text}", response_text[:3000])
        
        img = imgkit.from_string(formatted_html, False, options={
            "width": 375, 
            "height": 812,
            "encoding": "UTF-8"
        })
        return Image.open(BytesIO(img)), truncated_text  # Return both the image and truncated text

    except requests.exceptions.RequestException as e:
        logging.error(f"Error getting Chatbase response: {e}")
        return None, "I'm sorry, I couldn't process your request at this time."  # Return None for image and error message for text

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
        self.tweet_response_limit = 35

        # For statics tracking for each run. This is not persisted anywhere, just logging
        self.mentions_found = 0
        self.mentions_replied = 0
        self.mentions_replied_errors = 0

    def generate_response(self, mentioned_conversation_tweet_text):
        return get_chatbot_response(mentioned_conversation_tweet_text)
    
    def respond_to_mention(self, mention, mentioned_conversation_tweet):
        response_image, response_text = self.generate_response(mentioned_conversation_tweet.text)
    
        try:
            media_id = self.twitter_api.media_upload(filename="response.png", file=response_image.tobytes())[0].media_id

            response_tweet = self.twitter_api.create_tweet(
                text=response_text, 
                in_reply_to_tweet_id=mention.id,
                media_ids=[media_id]
            )
            self.mentions_replied += 1

            self.airtable.insert({
                'mentioned_conversation_tweet_id': str(mentioned_conversation_tweet.id),
                'mentioned_conversation_tweet_text': mentioned_conversation_tweet.text,
                'tweet_response_id': response_tweet.data['id'],
                'tweet_response_text': response_text,
                'tweet_response_created_at': datetime.utcnow().isoformat(),
                'mentioned_at': mention.created_at.isoformat()
            })
            return True

        except Exception as e:
            print(e)
            self.mentions_replied_errors += 1
            return False  # Modified to return False instead of just return
    
    def get_me_id(self):
        return self.twitter_api.get_me()[0].id
    
    def get_mention_conversation_tweet(self, mention):
        if mention.conversation_id is not None:
            conversation_tweet = self.twitter_api.get_tweet(mention.conversation_id).data
            return conversation_tweet
        return None

    def get_mentions(self):
        now = datetime.utcnow()
        start_time = now - timedelta(minutes=20)
        start_time_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        return self.twitter_api.get_users_mentions(id=self.twitter_me_id,
                                                   start_time=start_time_str,
                                                   expansions=['referenced_tweets.id'],
                                                   tweet_fields=['created_at', 'conversation_id']).data

    def check_already_responded(self, mentioned_conversation_tweet_id):
        records = self.airtable.get_all(view='Grid view')
        for record in records:
            if record['fields'].get('mentioned_conversation_tweet_id') == str(mentioned_conversation_tweet_id):
                return True
        return False

    def respond_to_mentions(self):
        mentions = self.get_mentions()

        if not mentions:
            print("No mentions found")
            return
        
        self.mentions_found = len(mentions)

        for mention in mentions[:self.tweet_response_limit]:
            mentioned_conversation_tweet = self.get_mention_conversation_tweet(mention)
            
            if (mentioned_conversation_tweet.id != mention.id
                and not self.check_already_responded(mentioned_conversation_tweet.id)):

                self.respond_to_mention(mention, mentioned_conversation_tweet)
        return True
    
    def execute_replies(self):
        print(f"Starting Job: {datetime.utcnow().isoformat()}")
        self.respond_to_mentions()
        print(f"Finished Job: {datetime.utcnow().isoformat()}, Found: {self.mentions_found}, Replied: {self.mentions_replied}, Errors: {self.mentions_replied_errors}")

bot = TwitterBot()

def job():
    print(f"Job executed at {datetime.utcnow().isoformat()}")
    bot.execute_replies()

if __name__ == "__main__":
    schedule.every(3).minutes.do(job)
    while True:
        schedule.run_pending()
        time.sleep(1)
