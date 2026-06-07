"""
InstaAI - Instagram Growth Studio
Single-file Flask app. Works on Render, Railway, Koyeb, Fly.io, PythonAnywhere.
Instagram login uses Meta's official OAuth — works on all servers.
"""
import os, json, requests, hashlib, secrets
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory, session, redirect
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))
CORS(app, supports_credentials=True)

# ── CONFIG ─────────────────────────────────────────────
GROQ_KEY      = os.getenv('GROQ_API_KEY', '')
IG_APP_ID     = os.getenv('IG_APP_ID', '')
IG_APP_SECRET = os.getenv('IG_APP_SECRET', '')
BASE_URL      = os.getenv('BASE_URL', 'http://localhost:5050')
PORT          = int(os.getenv('PORT', 5050))

# ── DATABASE ───────────────────────────────────────────
DB_PATH = os.getenv('DATABASE_URL', f"sqlite:///{os.path.join(os.path.dirname(__file__), 'instaai.db')}")
engine  = create_engine(DB_PATH, connect_args={"check_same_thread": False} if 'sqlite' in DB_PATH else {})
Session = sessionmaker(bind=engine)
Base    = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id            = Column(Integer, primary_key=True)
    ig_user_id    = Column(String, unique=True)
    username      = Column(String)
    full_name     = Column(String)
    access_token  = Column(Text)
    token_expires = Column(DateTime)
    followers     = Column(Integer, default=0)
    media_count   = Column(Integer, default=0)
    profile_pic   = Column(String)
    created_at    = Column(DateTime, default=datetime.utcnow)

class ScheduledPost(Base):
    __tablename__ = 'scheduled_posts'
    id            = Column(Integer, primary_key=True)
    ig_user_id    = Column(String)
    username      = Column(String)
    image_url     = Column(Text)
    caption       = Column(Text)
    hashtags      = Column(Text)
    location      = Column(String)
    scheduled_time= Column(DateTime)
    post_type     = Column(String, default='feed')
    status        = Column(String, default='pending')
    ig_media_id   = Column(String, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# ── SCHEDULER ──────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="UTC")

def _execute_post(post_id: int):
    db = Session()
    try:
        post = db.query(ScheduledPost).filter_by(id=post_id).first()
        if not post or post.status != 'pending': return
        user = db.query(User).filter_by(ig_user_id=post.ig_user_id).first()
        if not user: return
        full_caption = f"{post.caption}\n\n{post.hashtags}".strip()
        result = _ig_post_photo(user.access_token, user.ig_user_id, post.image_url, full_caption)
        post.status   = 'posted' if result.get('id') else 'failed'
        post.ig_media_id = result.get('id')
        db.commit()
    except Exception as e:
        print(f"[Scheduler] Error: {e}")
    finally:
        db.close()

scheduler.start()

# ── INSTAGRAM GRAPH API HELPERS ────────────────────────
IG_BASE = "https://graph.facebook.com/v19.0"

def _ig_get(path, token, **params):
    r = requests.get(f"{IG_BASE}{path}", params={"access_token": token, **params}, timeout=10)
    return r.json()

def _ig_post_photo(token, account_id, image_url, caption):
    # Step 1: create container
    r1 = requests.post(f"{IG_BASE}/{account_id}/media",
         data={"image_url": image_url, "caption": caption, "access_token": token}, timeout=15)
    d1 = r1.json()
    if "id" not in d1: return d1
    # Step 2: publish
    r2 = requests.post(f"{IG_BASE}/{account_id}/media_publish",
         data={"creation_id": d1["id"], "access_token": token}, timeout=15)
    return r2.json()

def _get_long_lived_token(short_token):
    r = requests.get("https://graph.instagram.com/access_token", params={
        "grant_type": "ig_exchange_token",
        "client_secret": IG_APP_SECRET,
        "access_token": short_token
    }, timeout=10)
    return r.json()

def _get_ig_account(token):
    """Get Instagram account info directly from token."""
    info = requests.get("https://graph.instagram.com/me", params={
        "fields": "id,username,name,followers_count,media_count,profile_picture_url,biography",
        "access_token": token
    }, timeout=10).json()
    if "id" in info:
        info["page_token"] = token
        return [info]
    return []

# ── AI HELPER ──────────────────────────────────────────
def _ask_groq(prompt, max_tokens=600):
    if not GROQ_KEY:
        return "⚠️ Add your free Groq API key in Settings (console.groq.com)"
    try:
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={"model": "llama3-70b-8192", "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]}, timeout=20)
        return r.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        return f"AI error: {str(e)}"

def _search_location(q):
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 6, "addressdetails": 1},
            headers={"User-Agent": "InstaAI/1.0"}, timeout=6)
        return [{"name": p.get("display_name","").split(",")[0].strip(),
                 "full": p.get("display_name",""),
                 "lat": p.get("lat"), "lon": p.get("lon")} for p in r.json()]
    except: return []

# ══════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/health')
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

# ── OAUTH ──────────────────────────────────────────────
@app.route('/api/oauth/url')
def oauth_url():
    """Generate Instagram OAuth URL."""
    if not IG_APP_ID:
        return jsonify({"error": "IG_APP_ID not configured. Add it in environment variables."})
    redirect_uri = f"{BASE_URL}/api/oauth/callback"
    state = secrets.token_hex(16)
    session['oauth_state'] = state
    # Use Facebook OAuth dialog
    url = (f"https://www.facebook.com/dialog/oauth?"
           f"client_id={IG_APP_ID}"
           f"&redirect_uri={redirect_uri}"
           f"&scope=instagram_basic,instagram_content_publish,instagram_manage_insights,pages_show_list,pages_read_engagement"
           f"&response_type=code&state={state}")
    return jsonify({"url": url})

@app.route('/api/oauth/callback')
def oauth_callback():
    """Handle OAuth callback from Instagram/Facebook."""
    code  = request.args.get('code')
    state = request.args.get('state')
    if not code:
        return redirect('/?error=oauth_denied')
    redirect_uri = f"{BASE_URL}/api/oauth/callback"
    # Exchange code for token
    r = requests.post("https://graph.facebook.com/v19.0/oauth/access_token", data={
        "client_id": IG_APP_ID, "client_secret": IG_APP_SECRET,
        "redirect_uri": redirect_uri, "code": code
    }, timeout=10)
    token_data = r.json()
    if "access_token" not in token_data:
        return redirect(f'/?error=token_failed')
    short_token = token_data["access_token"]
    # Get long-lived token
    ll = _get_long_lived_token(short_token)
    long_token = ll.get("access_token", short_token)
    expires_in = ll.get("expires_in", 5184000)
    # Get IG accounts
    db = Session()
    try:
        accounts = _get_ig_account(long_token)
        if not accounts:
            return redirect('/?error=no_ig_business_account')
        acc = accounts[0]
        token_to_use = acc.get("page_token", long_token)
        user = db.query(User).filter_by(ig_user_id=acc["id"]).first()
        if not user:
            user = User(ig_user_id=acc["id"])
            db.add(user)
        user.username     = acc.get("username", "")
        user.full_name    = acc.get("name", "")
        user.access_token = token_to_use
        user.token_expires= datetime.utcnow() + timedelta(seconds=expires_in)
        user.followers    = acc.get("followers_count", 0)
        user.media_count  = acc.get("media_count", 0)
        user.profile_pic  = acc.get("profile_picture_url", "")
        db.commit()
        session['ig_user_id'] = acc["id"]
        session['username']   = acc.get("username", "")
        return redirect('/?login=success')
    except Exception as e:
        return redirect(f'/?error={str(e)[:50]}')
    finally:
        db.close()

@app.route('/api/auth/me')
def auth_me():
    uid = session.get('ig_user_id')
    if not uid: return jsonify({"logged_in": False})
    db = Session()
    try:
        user = db.query(User).filter_by(ig_user_id=uid).first()
        if not user: return jsonify({"logged_in": False})
        return jsonify({"logged_in": True, "username": user.username,
                        "full_name": user.full_name, "followers": user.followers,
                        "media_count": user.media_count, "profile_pic": user.profile_pic,
                        "ig_user_id": user.ig_user_id})
    finally:
        db.close()

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    session.clear()
    return jsonify({"success": True})

@app.route('/api/auth/users')
def auth_users():
    """List all connected accounts (for account switcher)."""
    db = Session()
    try:
        users = db.query(User).all()
        return jsonify({"users": [{"ig_user_id": u.ig_user_id, "username": u.username,
                                    "full_name": u.full_name, "followers": u.followers,
                                    "profile_pic": u.profile_pic} for u in users]})
    finally:
        db.close()

@app.route('/api/auth/switch', methods=['POST'])
def auth_switch():
    uid = (request.json or {}).get('ig_user_id')
    db  = Session()
    try:
        user = db.query(User).filter_by(ig_user_id=uid).first()
        if not user: return jsonify({"error": "User not found"})
        session['ig_user_id'] = user.ig_user_id
        session['username']   = user.username
        return jsonify({"success": True, "username": user.username})
    finally:
        db.close()

# ── INSTAGRAM DATA ─────────────────────────────────────
def _current_user():
    uid = session.get('ig_user_id')
    if not uid: return None, None
    db = Session()
    user = db.query(User).filter_by(ig_user_id=uid).first()
    db.close()
    return user, uid

@app.route('/api/instagram/account')
def ig_account():
    user, _ = _current_user()
    if not user: return jsonify({"error": "Not logged in"})
    fresh = _ig_get(f"/{user.ig_user_id}", user.access_token,
                    fields="id,username,name,followers_count,follows_count,media_count,biography,profile_picture_url,website")
    if "error" not in fresh:
        db = Session()
        u  = db.query(User).filter_by(ig_user_id=user.ig_user_id).first()
        if u:
            u.followers   = fresh.get("followers_count", u.followers)
            u.media_count = fresh.get("media_count", u.media_count)
            db.commit()
        db.close()
    return jsonify(fresh)

@app.route('/api/instagram/media')
def ig_media():
    user, _ = _current_user()
    if not user: return jsonify({"media": []})
    data = _ig_get(f"/{user.ig_user_id}/media", user.access_token,
                   fields="id,caption,media_type,media_url,thumbnail_url,timestamp,like_count,comments_count,permalink")
    return jsonify(data)

@app.route('/api/instagram/insights')
def ig_insights():
    user, _ = _current_user()
    if not user: return jsonify({"error": "Not logged in"})
    data = _ig_get(f"/{user.ig_user_id}/insights", user.access_token,
                   metric="impressions,reach,profile_views,follower_count", period="day")
    return jsonify(data)

@app.route('/api/instagram/post', methods=['POST'])
def ig_post():
    user, _ = _current_user()
    if not user: return jsonify({"error": "Not logged in"})
    d = request.json or {}
    result = _ig_post_photo(user.access_token, user.ig_user_id, d.get('image_url',''), d.get('caption',''))
    return jsonify(result)

# ── LOCATION ───────────────────────────────────────────
@app.route('/api/location/search')
def loc_search():
    q = request.args.get('q','')
    return jsonify({"results": _search_location(q) if q else []})

# ── AI ─────────────────────────────────────────────────
@app.route('/api/ai/caption', methods=['POST'])
def ai_caption():
    d   = request.json or {}
    loc = f" targeting people in {d['location']}" if d.get('location') else ""
    out = _ask_groq(
        f"Write an Instagram caption about: {d.get('topic','')}\n"
        f"Tone: {d.get('tone','engaging')}\nAudience: {d.get('audience','general')}{loc}\n"
        f"Rules: 2-4 sentences, 1 CTA, no hashtags, authentic. Return ONLY the caption.")
    return jsonify({"caption": out})

@app.route('/api/ai/hashtags', methods=['POST'])
def ai_hashtags():
    d   = request.json or {}
    loc = f", location: {d['location']}" if d.get('location') else ""
    out = _ask_groq(
        f"Generate 30 Instagram hashtags for:\n\"{d.get('caption','')}\"\n"
        f"Niche: {d.get('niche','')}{loc}\n"
        f"Mix: 10 high-volume, 10 medium, 10 micro-niche.\n"
        f"Return ONLY hashtags separated by spaces starting with #.")
    return jsonify({"hashtags": out})

@app.route('/api/ai/best-time', methods=['POST'])
def ai_best_time():
    d   = request.json or {}
    raw = _ask_groq(
        f"Best 5 times to post on Instagram for: {d.get('audience','general')}, "
        f"timezone: {d.get('timezone','UTC')}.\n"
        f"Return ONLY JSON array: [{{\"day\":\"Monday\",\"time\":\"6:00 PM\",\"reason\":\"why\"}}]")
    try:
        return jsonify({"suggestions": json.loads(raw.replace('```json','').replace('```','').strip())})
    except:
        return jsonify({"suggestions": [
            {"day":"Monday","time":"6:00 PM","reason":"After-work peak"},
            {"day":"Wednesday","time":"12:00 PM","reason":"Midweek lunch"},
            {"day":"Friday","time":"5:00 PM","reason":"End of week"},
            {"day":"Saturday","time":"10:00 AM","reason":"Weekend morning"},
            {"day":"Sunday","time":"7:00 PM","reason":"Evening browse"}]})

@app.route('/api/ai/analyze-audience', methods=['POST'])
def ai_audience():
    d   = request.json or {}
    loc = f" in {d['location']}" if d.get('location') else ""
    raw = _ask_groq(
        f"Analyze Instagram audience for niche: {d.get('niche','')}{loc}.\n"
        f"Return ONLY JSON: {{\"age_range\":\"\",\"gender_split\":\"\","
        f"\"interests\":[],\"pain_points\":[],\"content_preferences\":[],\"growth_tips\":[]}}", 800)
    try:
        return jsonify({"analysis": json.loads(raw.replace('```json','').replace('```','').strip())})
    except:
        return jsonify({"analysis": {"age_range":"18-34","gender_split":"Mixed",
                                      "interests":[],"pain_points":[],"content_preferences":[],"growth_tips":[]}})

@app.route('/api/ai/ideas', methods=['POST'])
def ai_ideas():
    d   = request.json or {}
    raw = _ask_groq(
        f"Generate 9 Instagram content ideas for niche: {d.get('niche','')}.\n"
        f"Return ONLY JSON array: [{{\"title\":\"\",\"type\":\"Reel|Post|Story|Carousel\",\"hook\":\"\"}}]", 1000)
    try:
        return jsonify({"ideas": json.loads(raw.replace('```json','').replace('```','').strip())})
    except:
        return jsonify({"ideas": []})

# ── SCHEDULER ──────────────────────────────────────────
@app.route('/api/schedule', methods=['POST'])
def create_schedule():
    user, _ = _current_user()
    if not user: return jsonify({"error": "Not logged in"})
    d  = request.json or {}
    db = Session()
    try:
        sched_dt = datetime.fromisoformat(d.get('scheduled_time',''))
        post = ScheduledPost(ig_user_id=user.ig_user_id, username=user.username,
                             image_url=d.get('image_url',''), caption=d.get('caption',''),
                             hashtags=d.get('hashtags',''), location=d.get('location',''),
                             scheduled_time=sched_dt, post_type=d.get('post_type','feed'), status='pending')
        db.add(post); db.commit(); db.refresh(post)
        pid = post.id
        scheduler.add_job(_execute_post, DateTrigger(run_date=sched_dt),
                          args=[pid], id=f"post_{pid}", replace_existing=True)
        return jsonify({"status":"scheduled","post_id":pid})
    except Exception as e:
        return jsonify({"error": str(e)})
    finally:
        db.close()

@app.route('/api/schedule')
def list_schedule():
    user, _ = _current_user()
    if not user: return jsonify({"posts":[]})
    db = Session()
    try:
        posts = db.query(ScheduledPost).filter_by(ig_user_id=user.ig_user_id)\
                  .order_by(ScheduledPost.scheduled_time).all()
        return jsonify({"posts": [{
            "id":p.id,"caption":p.caption,"hashtags":p.hashtags,"location":p.location,
            "scheduled_time":p.scheduled_time.isoformat() if p.scheduled_time else None,
            "post_type":p.post_type,"status":p.status,"image_url":p.image_url
        } for p in posts]})
    finally:
        db.close()

@app.route('/api/schedule/<int:pid>', methods=['DELETE'])
def delete_schedule(pid):
    db = Session()
    try:
        post = db.query(ScheduledPost).filter_by(id=pid).first()
        if not post: return jsonify({"error":"Not found"})
        try: scheduler.remove_job(f"post_{pid}")
        except: pass
        db.delete(post); db.commit()
        return jsonify({"status":"deleted"})
    finally:
        db.close()

# ── SETTINGS ───────────────────────────────────────────
@app.route('/api/settings')
def get_settings():
    return jsonify({
        "groq_configured": bool(GROQ_KEY),
        "ig_oauth_configured": bool(IG_APP_ID and IG_APP_SECRET),
        "base_url": BASE_URL
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
