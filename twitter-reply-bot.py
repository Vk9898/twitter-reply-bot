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
import re

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

redis_url = os.getenv("REDIS_URL")
redis_client = redis.Redis.from_url(redis_url)

# Load your API keys (preferably from environment variables)
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
HCTI_API_USER_ID = os.getenv("HCTI_API_USER_ID")
HCTI_API_KEY = os.getenv("HCTI_API_KEY")

CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

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

def get_chatbot_response(user_message):
    url = 'https://www.chatbase.co/api/v1/chat'
    headers = {
        'Authorization': f'Bearer {CHATBASE_API_KEY}',
        'Content-Type': 'application/json'
    }

    data = {
        "messages": [
            {"content": user_message, "role": "user"}
        ],
        "chatbotId": CHATBOT_ID,
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

def format_text_to_html(text):
    text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    
    lines = text.strip().split('\n')
    formatted_lines = []
    for line in lines:
        if line.strip() == '':
            continue
        if not (line.startswith('<h2>') or line.startswith('<h3>') or line.startswith('<strong>')):
            line = f'<p>{line}</p>'
        formatted_lines.append(line)
    
    return '\n'.join(formatted_lines)

def summarize_with_claude(text):
    """Summarize text using Claude."""
    url = 'https://api.anthropic.com/v1/complete'
    headers = {
        'Content-Type': 'application/json',
        'x-api-key': CLAUDE_API_KEY,
        'anthropic-version': '2023-06-01'
    }
    payload = {
        'model': 'claude-2',
        'prompt': f'\n\nHuman: Summarize the following text in approximately 240 characters. Ensure the summary ends with a complete sentence:\n\n{text}\n\nAssistant: Here is a summary of approximately 240 characters, ending with a complete sentence:',
        'max_tokens_to_sample': 400,
        'temperature': 0.5,
        'stop_sequences': ["\n\nHuman:"]
    }
    
    logging.info(f"Headers: {headers}")
    logging.info(f"Payload: {json.dumps(payload)}")
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        summary = response.json().get('completion', '').strip()
        return summary
    except requests.exceptions.RequestException as e:
        logging.error(f"Error summarizing with Claude: {e}")
        if response is not None:
            logging.error(f"Response content: {response.content}")
        return None

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
        self.tweet_response_limit = 35
        
        self.mentions_found = 0
        self.mentions_replied = 0
        self.mentions_replied_errors = 0

        self.auth = tweepy.OAuthHandler(TWITTER_API_KEY, TWITTER_API_SECRET)
        self.auth.set_access_token(TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET)
        self.api_v1 = tweepy.API(self.auth)

    def generate_response(self, original_tweet_text, user_comment=None):
        if user_comment:
            prompt = f"Original tweet: '{original_tweet_text}'\nUser comment: '{user_comment}'\nPlease provide insights, fact-check, or additional information based on the original tweet and the user's comment."
        else:
            prompt = original_tweet_text
        
        return get_chatbot_response(prompt)
    
    def respond_to_mention(self, mention, mentioned_conversation_tweet):
        user_comment = mention.text if mention.id != mentioned_conversation_tweet.id else None
        response_text = self.generate_response(mentioned_conversation_tweet.text, user_comment)
        image_url = self.generate_image_from_response(response_text)
        
        # Summarize the response using Claude
        summary = summarize_with_claude(response_text)
        if summary:
            tweet_text = f"{summary}\n\nMore at ftxclaims.com"
        else:
            tweet_text = "More at ftxclaims.com"  # Fallback if summarization fails

        try:
            if image_url:
                auth = OAuth1Session(
                    TWITTER_API_KEY,
                    client_secret=TWITTER_API_SECRET,
                    resource_owner_key=TWITTER_ACCESS_TOKEN,
                    resource_owner_secret=TWITTER_ACCESS_TOKEN_SECRET,
                )

                image_data = requests.get(image_url).content

                upload_url = "https://upload.twitter.com/1.1/media/upload.json"
                files = {"media": image_data}
                response = auth.post(upload_url, files=files)
                response.raise_for_status()

                media_id = response.json()["media_id"]

                response_tweet = self.twitter_api.create_tweet(
                    text=tweet_text, 
                    media_ids=[media_id], 
                    in_reply_to_tweet_id=mention.id
                )
            else:
                response_tweet = self.twitter_api.create_tweet(
                    text=tweet_text, 
                    in_reply_to_tweet_id=mention.id
                )
        except Exception as e:
            print(e)
            self.mentions_replied_errors += 1
            return
        
        self.airtable.insert({
            'mentioned_conversation_tweet_id': str(mentioned_conversation_tweet.id),
            'mentioned_conversation_tweet_text': mentioned_conversation_tweet.text,
            'tweet_response_id': response_tweet.data['id'],
            'tweet_response_text': response_text,
            'mentioned_at': mention.created_at.isoformat()
        })
        redis_client.set("last_tweet_id", str(mention.id)) 

        return True
    
    def get_me_id(self):
        return self.twitter_api.get_me()[0].id
    
    def get_mention_conversation_tweet(self, mention):
        if mention.conversation_id == mention.id:
            return mention
        elif mention.conversation_id:
            # Get the tweet that was replied to
            replied_to_tweet = self.twitter_api.get_tweet(
                mention.conversation_id, 
                expansions=['referenced_tweets.id'],
                tweet_fields=['author_id', 'created_at', 'text']
            ).data
            
            # If the replied to tweet is not from the bot, use it as the context
            if replied_to_tweet.author_id != self.twitter_me_id:
                return replied_to_tweet
            
            # Otherwise, use the original mention as before
            return mention
        return None

    def get_mentions(self):
        since_id = redis_client.get("last_tweet_id")
        if since_id:
            since_id = int(since_id)
        else:
            since_id = 1

        return self.twitter_api.get_users_mentions(
            id=self.twitter_me_id,
            since_id=since_id, 
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
        
            if not self.check_already_responded(mentioned_conversation_tweet.id):
                self.respond_to_mention(mention, mentioned_conversation_tweet)
                self.mentions_replied += 1

        return True
    
    def execute_replies(self):
        print(f"Starting Job: {datetime.utcnow().isoformat()}")
        self.respond_to_mentions()
        print(f"Finished Job: {datetime.utcnow().isoformat()}, Found: {self.mentions_found}, Replied: {self.mentions_replied}, Errors: {self.mentions_replied_errors}")
    
    def generate_image_from_response(self, response_text):
        formatted_response_text = format_text_to_html(response_text)

        html_template = f"""
        <!DOCTYPE html>
        <html>
        <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Roboto&display=swap');
        body {{ 
            font-family: 'Roboto', sans-serif; 
            background-color: #15202B; 
            color: #FFFFFF; 
            padding: 20px; 
            width: 1170px;
            height: 2532px;
            margin: 0 auto; 
            overflow: hidden; 
            box-sizing: border-box; 
        }}
        .response-box {{ 
            background-color: #192734;
            padding: 30px; 
            border-radius: 16px; 
            overflow-wrap: break-word;
            word-wrap: break-word; 
            width: calc(100% - 40px); 
            box-sizing: border-box; 
            border: 2px solid #38444D;
            font-size: 48px;
            line-height: 1.4;
            margin: 20px auto;
        }}
        p {{
            margin: 1em 0; 
        }}
        h2 {{
            color: #1DA1F2;
            font-size: 60px;
        }}
        h3 {{
            color: #F5A623;
            font-size: 54px;
            font-weight: bold;
        }}
        strong {{
            color: #F5A623;
        }}
        </style>
        </head>
        <body>
        <div class="response-box">{formatted_response_text}</div>
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

# Global bot instance to maintain state and avoid re-authentication
bot = TwitterBot()

def job():
    print(f"Job executed at {datetime.utcnow().isoformat()}")
    bot = TwitterBot()
    bot.execute_replies()

if __name__ == "__main__":
    schedule.every(3).minutes.do(job)
    while True:
        schedule.run_pending()
        time.sleep(1)
