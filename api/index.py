import sys
import os

# Add parent directory to path to import telegram_bot
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram_bot import Config, TelegramAPI, FirestoreDB, BotLogic

def handler(request):
    """Vercel serverless function handler"""
    # Get request data
    if request.method == 'POST':
        try:
            import json
            update = request.get_json()
            
            if update:
                # Initialize bot components
                api = TelegramAPI(Config.TOKEN)
                db = FirestoreDB()
                bot = BotLogic(api, db)
                
                # Handle update
                bot.handle_update(update)
                
                return {
                    'statusCode': 200,
                    'body': json.dumps({'ok': True}),
                    'headers': {'Content-Type': 'application/json'}
                }
        except Exception as e:
            return {
                'statusCode': 500,
                'body': json.dumps({'ok': False, 'error': str(e)}),
                'headers': {'Content-Type': 'application/json'}
            }
    
    return {
        'statusCode': 400,
        'body': json.dumps({'ok': False}),
        'headers': {'Content-Type': 'application/json'}
    }
