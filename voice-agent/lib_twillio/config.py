import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
PORT = int(os.getenv("PORT", "8000"))
APP_BASE_URL = "https://techie.devailab.work"

# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------
GOOGLE_API_KEY = ""

TWILIO_ACCOUNT_SID = ""
TWILIO_AUTH_TOKEN  = ""
TWILIO_PHONE_NUMBER = ""

# ---------------------------------------------------------------------------
# Maintainers roster
# ---------------------------------------------------------------------------
MAINTAINERS = {
    "electrician": {
        "name": "Adrien (Electrician)",
        "phone": "",
        "specialty": "electrical faults, lighting, circuit breakers, power outages",
    },
    "plumber": {
        "name": "Abhi (Plumber)",
        "phone": "",
        "specialty": "water leaks, blocked drains, pipes, dishwasher connections",
    },
    "general": {
        "name": "Akshay (General Maintenance)",
        "phone": "",
        "specialty": "general repairs, carpentry, HVAC, miscellaneous maintenance",
    },
}
