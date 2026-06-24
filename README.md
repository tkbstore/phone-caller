# tkbase-phone-caller

AI phone caller with Twilio. Claude calls you with rate limiting.

## Install

```bash
pip install git+https://github.com/tkbstore/phone-caller.git
```

## Setup (initial only)

1. Create Twilio credentials file:
   ```bash
   mkdir -p ~/.config/tkbase
   cat > ~/.config/tkbase/twilio_credentials.json << 'EOF'
   {
     "account_sid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
     "auth_token": "your_auth_token_here",
     "from_number": "+1234567890"
   }
   EOF
   ```

2. Get your Twilio credentials from https://console.twilio.com/

All tkbase repos (sales-ai, finance-ai, etc.) share the `~/.config/tkbase/` config directory.

## Usage

```python
from tkbase_phone_caller import PhoneCaller

# Credentials auto-discovered from ~/.config/tkbase/
caller = PhoneCaller(
    rate_limit=5,       # max 5/min
    daily_limit=50,     # max 50/day
)
caller.call_sync(
    to="+819012345678",
    message="会議が15分後に始まります。",
)
```

### Custom TwiML

```python
caller.call_sync(
    to="+819012345678",
    twiml='<Response><Say voice="Polly.Mizuki" language="ja-JP">こんにちは</Say></Response>',
)
```

### Safety features

- `rate_limit`: Per-minute cap (default 5)
- `daily_limit`: Daily cap (default 50)
- `dry_run=True`: Log without calling
- `state_dir`: Persist counters across restarts
- Exceeding limits raises `RateLimitExceeded`
