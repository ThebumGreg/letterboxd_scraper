"""One quote a day: ZenQuotes when the internet is there, else one from the built-in list. The same quote is kept all day."""
import json
import zlib
from datetime import date

from synchub import http, settings

BUILT_IN = (
    ("The secret of getting ahead is getting started.", "Mark Twain"),
    ("It always seems impossible until it's done.", "Nelson Mandela"),
    ("Well done is better than well said.", "Benjamin Franklin"),
    ("The best way to predict the future is to create it.", "Peter Drucker"),
    ("Do what you can, with what you have, where you are.", "Theodore Roosevelt"),
    ("Whether you think you can or you think you can't, you're right.", "Henry Ford"),
    ("You miss 100% of the shots you don't take.", "Wayne Gretzky"),
    ("A journey of a thousand miles begins with a single step.", "Lao Tzu"),
    ("Act as if what you do makes a difference. It does.", "William James"),
    ("Small deeds done are better than great deeds planned.", "Peter Marshall"),
    ("Today is a good day to have a good day.", "Unknown"),
    ("Fall seven times, stand up eight.", "Japanese proverb"),
    ("Energy and persistence conquer all things.", "Benjamin Franklin"),
    ("What you do today can improve all your tomorrows.", "Ralph Marston"),
    ("Start where you are. Use what you have. Do what you can.", "Arthur Ashe"),
    ("Quality is not an act, it is a habit.", "Aristotle"),
    ("The harder I work, the luckier I get.", "Samuel Goldwyn"),
    ("Be the change that you wish to see in the world.", "Mahatma Gandhi"),
    ("Everything you've ever wanted is on the other side of fear.", "George Addair"),
    ("Don't watch the clock; do what it does. Keep going.", "Sam Levenson"),
    ("Believe you can and you're halfway there.", "Theodore Roosevelt"),
    ("Opportunities don't happen. You create them.", "Chris Grosser"),
    ("Happiness is not by chance, but by choice.", "Jim Rohn"),
    ("Kindness is a language the deaf can hear and the blind can see.", "Mark Twain"),
    ("Your attitude, not your aptitude, will determine your altitude.", "Zig Ziglar"),
    ("The only way to do great work is to love what you do.", "Steve Jobs"),
    ("Success is the sum of small efforts repeated day in and day out.", "Robert Collier"),
    ("Courage is not the absence of fear, but the triumph over it.", "Nelson Mandela"),
    ("Make each day your masterpiece.", "John Wooden"),
    ("Learn as if you will live forever, live as if you will die tomorrow.", "Mahatma Gandhi"),
)


def fallback(day):
    text, author = BUILT_IN[zlib.crc32(day.isoformat().encode()) % len(BUILT_IN)]
    return {"text": text, "author": author, "source": "built-in"}


def _fetch():
    res = http.request("quote", "today", "GET", "https://zenquotes.io/api/today", timeout=8)
    if res.status_code != 200:
        return None
    first = (res.json() or [{}])[0]
    text, author = str(first.get("q") or "").strip(), str(first.get("a") or "").strip()
    # ZenQuotes answers with a rate-limit sentence in the same shape when it is asked too often
    if not text or len(text) > 300 or "too many requests" in text.lower() or author.lower() == "zenquotes.io":
        return None
    return {"text": text, "author": author, "source": "zenquotes.io"}


def today(day=None):
    """-> {"text", "author", "source"}. Never raises."""
    day = day or date.today()
    try:
        saved = json.loads(settings.get("dash_quote") or "{}")
    except ValueError:
        saved = {}
    if saved.get("date") == day.isoformat() and saved.get("text") and (saved.get("source") != "built-in" or saved.get("tried_at", 0) + 1800 > _now()):
        return saved
    quote = None
    try:
        quote = _fetch()
    except Exception:
        pass
    quote = quote or fallback(day)
    quote.update(date=day.isoformat(), tried_at=_now())
    settings.set_value("dash_quote", json.dumps(quote))
    return quote


def _now():
    import time
    return int(time.time())
