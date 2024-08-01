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
    url = 'https://api.anthropic.com/v1/complete'
    headers = {
        'Content-Type': 'application/json',
        'x-api-key': CLAUDE_API_KEY,
        'anthropic-version': '2023-06-01'
    }
    payload = {
        'model': 'claude-2',
        'prompt': f'\n\nHuman: Summarize the following text in approximately 200 characters or less. Ensure the summary ends with a complete sentence:\n\n{text}\n\nAssistant: Here is a summary of approximately 200 characters or less, ending with a complete sentence:',
        'max_tokens_to_sample': 400,
        'temperature': 0.5,
        'stop_sequences': ["\n\nHuman:"]
    }
    
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

def clean_tweet_text(text):
    # Remove URLs
    text = re.sub(r'http\S+|www\S+|https\S+', '', text, flags=re.MULTILINE)
    
    # Remove mentions (@username)
    text = re.sub(r'@\w+', '', text)
    
    # Remove hashtags (#topic) and cashtags ($topic)
    text = re.sub(r'[#$]\w+', '', text)
    
    # Remove non-alphanumeric characters (except spaces and punctuation)
    text = re.sub(r'[^a-zA-Z0-9\s.,!?]', '', text)
    
    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text

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
        
        self.redis_client = redis.Redis.from_url(redis_url)

    def generate_response(self, original_tweet_text, user_comment=None):
        cleaned_original_tweet = clean_tweet_text(original_tweet_text)
        
        if user_comment:
            cleaned_user_comment = clean_tweet_text(user_comment)
            prompt = f"Original tweet: '{cleaned_original_tweet}'\nUser comment: '{cleaned_user_comment}'\nPlease provide insights, fact-check, or additional information based on the original tweet and the user's comment."
        else:
            prompt = cleaned_original_tweet
        
        return get_chatbot_response(prompt)
    
    def respond_to_mention(self, mention, mentioned_conversation_tweet):
        try:
            user_comment = mention.text if mention.id != mentioned_conversation_tweet.id else None
            cleaned_conversation_text = clean_tweet_text(mentioned_conversation_tweet.text)
            cleaned_user_comment = clean_tweet_text(user_comment) if user_comment else None
            
            response_text = self.generate_response(cleaned_conversation_text, cleaned_user_comment)
            logging.info(f"Generated response: {response_text[:100]}...")  # Log first 100 chars of response

            image_url = self.generate_image_from_response(response_text)
            logging.info(f"Generated image URL: {image_url}")

            summary = summarize_with_claude(response_text)
            if summary:
                tweet_text = f"{summary}\n\nMore at ftxclaims.com"
                if len(tweet_text) > 280:  # Twitter's character limit
                    tweet_text = tweet_text[:265] + "..."  # Truncate if too long
            else:
                tweet_text = "More at ftxclaims.com"  # Fallback if summarization fails
            logging.info(f"Tweet text: {tweet_text}")

            media_id = None
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
                logging.info(f"Uploaded media with ID: {media_id}")

            # Create tweet with or without media
            if media_id:
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

            logging.info(f"Tweet sent successfully. Tweet ID: {response_tweet.data['id']}")

            self.airtable.insert({
                'mentioned_conversation_tweet_id': str(mentioned_conversation_tweet.id),
                'mentioned_conversation_tweet_text': cleaned_conversation_text,
                'tweet_response_id': response_tweet.data['id'],
                'tweet_response_text': response_text,
                'mentioned_at': mention.created_at.isoformat()
            })

            return True

        except Exception as e:
            logging.error(f"Error in respond_to_mention: {str(e)}")
            self.mentions_replied_errors += 1
            return False
    
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
        last_tweet_id = self.redis_client.get("last_tweet_id")
        if last_tweet_id:
            since_id = int(last_tweet_id)
        else:
            # If no last_tweet_id is found, use a default value or fetch recent mentions
            since_id = 1

        logging.info(f"Fetching mentions since tweet ID: {since_id}")

        mentions = []
        pagination_token = None
        max_results = 100  # Adjust this value based on your needs and rate limits

        while True:
            try:
                response = self.twitter_api.get_users_mentions(
                    id=self.twitter_me_id,
                    since_id=since_id,
                    max_results=max_results,
                    pagination_token=pagination_token,
                    tweet_fields=['created_at', 'conversation_id', 'in_reply_to_user_id']
                )

                if response.data:
                    mentions.extend(response.data)

                if response.meta.get('next_token'):
                    pagination_token = response.meta['next_token']
                else:
                    break

            except tweepy.TweepError as e:
                logging.error(f"Error fetching mentions: {str(e)}")
                break

        logging.info(f"Fetched {len(mentions)} mentions")
        return mentions

    def check_already_responded(self, mentioned_conversation_tweet_id):
        records = self.airtable.get_all(view='Grid view')
        for record in records:
            if record['fields'].get('mentioned_conversation_tweet_id') == str(mentioned_conversation_tweet_id):
                return True
        return False

    def respond_to_mentions(self):
        mentions = self.get_mentions()

        if not mentions:
            logging.info("No new mentions found")
            return

        self.mentions_found = len(mentions)
        logging.info(f"Found {self.mentions_found} new mentions")

        # Sort mentions by ID to process them in chronological order
        mentions.sort(key=lambda x: x.id)

        for mention in mentions[:self.tweet_response_limit]:
            mentioned_conversation_tweet = self.get_mention_conversation_tweet(mention)

            if not self.check_already_responded(mentioned_conversation_tweet.id):
                success = self.respond_to_mention(mention, mentioned_conversation_tweet)
                if success:
                    self.mentions_replied += 1
                    logging.info(f"Successfully replied to mention {mention.id}")
                else:
                    logging.warning(f"Failed to reply to mention {mention.id}")

            # Update the last_tweet_id in Redis after processing each mention
            self.redis_client.set("last_tweet_id", str(mention.id))

        logging.info(f"Mentions found: {self.mentions_found}, Replied: {self.mentions_replied}, Errors: {self.mentions_replied_errors}")
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
    bot.execute_replies()

if __name__ == "__main__":
    schedule.every(3).minutes.do(job)
    while True:
        schedule.run_pending()
        time.sleep(1)
