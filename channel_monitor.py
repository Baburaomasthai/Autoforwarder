import asyncio
import json
import logging
import os
import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")

class PublicChannelMonitor:
    def __init__(self):
        self.bot_token = BOT_TOKEN
        self.session = None
        self.last_message_ids = {}
        self.forwarded_messages = set()
        self.load_persistent_state()
        
    async def start_session(self):
        self.session = aiohttp.ClientSession()
        
    async def close_session(self):
        if self.session:
            await self.session.close()
            
    def load_config(self):
        try:
            with open("config.json", "r") as f:
                return json.load(f)
        except:
            return {"source_channels": [], "target_channel": "", "running": True}
            
    def load_persistent_state(self):
        try:
            with open("persistent_state.json", "r") as f:
                state = json.load(f)
                self.last_message_ids = state.get("last_message_ids", {})
                self.forwarded_messages = set(state.get("forwarded_messages", []))
                logger.info(f"Loaded persistent state: {len(self.last_message_ids)} channels tracked")
        except:
            logger.info("No persistent state found, starting fresh")
            
    def save_persistent_state(self):
        try:
            state = {
                "last_message_ids": self.last_message_ids,
                "forwarded_messages": list(self.forwarded_messages)
            }
            with open("persistent_state.json", "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save persistent state: {e}")
            
    async def get_channel_messages(self, channel_username):
        """Get recent messages from public channel via Telegram web preview"""
        try:
            channel_name = channel_username.replace("@", "")
            url = f"https://t.me/s/{channel_name}"
            async with self.session.get(url) as response:
                if response.status == 200:
                    html = await response.text()
                    return self.parse_channel_html(html, channel_name)
                else:
                    logger.error(f"Failed to fetch {channel_name}: {response.status}")
        except Exception as e:
            logger.error(f"Error fetching messages from {channel_username}: {e}")
        return []
        
    def parse_channel_html(self, html, channel_name):
        """Parse Telegram channel HTML to extract message IDs"""
        messages = []
        try:
            soup = BeautifulSoup(html, 'html.parser')
            containers = soup.find_all('div', class_=lambda x: x and 'tgme_widget_message' in x)
            for container in containers:
                post_id = container.get("data-post")
                if not post_id:
                    continue
                message_id = post_id.split("/")[-1]
                try:
                    message_id = str(int(message_id))
                except:
                    continue
                messages.append({
                    "id": message_id,
                    "channel": channel_name
                })
        except Exception as e:
            logger.error(f"Error parsing HTML for {channel_name}: {e}")
        return messages
        
    async def forward_message_by_id(self, message, target_channel):
        """Use forwardMessage API to forward actual media"""
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/forwardMessage"
            data = {
                "chat_id": target_channel,
                "from_chat_id": f"@{message['channel']}",
                "message_id": int(message["id"])
            }
            async with self.session.post(url, json=data) as response:
                if response.status == 200:
                    logger.info(f"✅ Forwarded message {message['id']} from @{message['channel']}")
                else:
                    logger.error(f"❌ Failed to forward {message['id']} - Status: {response.status}")
        except Exception as e:
            logger.error(f"❌ Error forwarding message ID: {e}")
        
    async def check_for_new_messages(self):
        config = self.load_config()
        if not config.get("running", True):
            return
        
        source_channels = config.get("source_channels", [])
        target_channel = config.get("target_channel", "")
        if not source_channels or not target_channel:
            return
        
        for channel in source_channels:
            try:
                messages = await self.get_channel_messages(channel)
                last_id = self.last_message_ids.get(channel, "0")
                sorted_messages = sorted(messages, key=lambda x: int(x["id"]), reverse=True)
                new_messages = [m for m in sorted_messages if int(m["id"]) > int(last_id)]
                
                if new_messages:
                    highest_id = max(int(msg["id"]) for msg in new_messages)
                    for message in new_messages[:3]:
                        msg_key = f"{channel}_{message['id']}"
                        if msg_key not in self.forwarded_messages:
                            await self.forward_message_by_id(message, target_channel)
                            self.forwarded_messages.add(msg_key)
                            await asyncio.sleep(1)
                    self.last_message_ids[channel] = str(highest_id)
                    self.save_persistent_state()
            except Exception as e:
                logger.error(f"Error checking channel {channel}: {e}")
                
    async def run(self):
        await self.start_session()
        logger.info("🔄 Monitor started")

        config = self.load_config()
        if config.get("source_channels"):
            for channel in config["source_channels"]:
                messages = await self.get_channel_messages(channel)
                if messages:
                    latest_id = max(int(msg["id"]) for msg in messages)
                    self.last_message_ids[channel] = str(latest_id)
                    logger.info(f"📌 Initialized {channel} at message ID {latest_id}")
        
        try:
            while True:
                if self.load_config().get("running", True):
                    await self.check_for_new_messages()
                await asyncio.sleep(45)
        except KeyboardInterrupt:
            logger.info("🛑 Monitor stopped")
        finally:
            await self.close_session()

async def main():
    monitor = PublicChannelMonitor()
    await monitor.run()

if __name__ == "__main__":
    asyncio.run(main())