from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_session import Session
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import sqlite3, os, re, uuid, json
from openai import OpenAI

# Only load .env if not on Render
if os.environ.get("RENDER") != "true":
    from dotenv import load_dotenv
    load_dotenv()

from extensions import db, login_manager

app = Flask(__name__, instance_relative_config=True, template_folder="Templates")

# ---------------- CONFIG ---------------- #
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'your-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_TYPE'] = 'filesystem'
# Flask-Session 0.4 expects the deprecated `session_cookie_name` attribute which
# was removed in Flask 3. Without setting it manually, session initialization
# fails on newer Flask versions. Provide it here for backward compatibility.
app.session_cookie_name = app.config.get('SESSION_COOKIE_NAME', 'session')
Session(app)

# ---------------- INITIALIZE EXTENSIONS ---------------- #
db.init_app(app)
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = None

# ---------------- IMPORT MODELS ---------------- #
from models import User, FoodLog

# ---------------- LOGIN MANAGER LOADER ---------------- #
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ---------------- OPENAI CLIENT ---------------- #
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("Missing OPENAI_API_KEY environment variable")

client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------- HELPERS ---------------- #
def get_today():
    """Return today's date as YYYY-MM-DD using configured timezone."""
    tz_name = os.environ.get("TIMEZONE")
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
            return datetime.now(tz).strftime('%Y-%m-%d')
        except Exception:
            pass
    return datetime.now().strftime('%Y-%m-%d')

def normalize_calories(value):
    """Return a numeric calorie estimate from numbers or ranges."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = value.strip()
        match = re.match(r"^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$", value)
        if match:
            low = float(match.group(1))
            high = float(match.group(2))
            return (low + high) / 2
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0

@app.template_filter('format_entry')
def format_entry(quantity, food_name):
    q = str(quantity).lower().strip()
    f = food_name.lower().strip()
    if q in f:
        return f
    skip_of_units = {"slice", "slices", "medium", "small", "large", "cup", "cups", "piece", "pieces"}
    first_word = q.split()[0]
    if first_word in skip_of_units:
        return f"{q} {f}"
    return f"{q} of {f}"

@app.template_filter('datetimeformat')
def datetimeformat(value, format='%A, %B %d'):
    try:
        return datetime.strptime(value, '%Y-%m-%d').strftime(format)
    except Exception:
        return value

# ---------------- ROUTES ---------------- #
@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    date = request.values.get('date', get_today())
    entries_for_day = FoodLog.query.filter_by(user_id=current_user.id, date=date).all()
    total_calories = sum(entry.calories for entry in entries_for_day)

    try:
        formatted_today = datetime.strptime(date, '%Y-%m-%d').strftime('%A, %B %d')
    except Exception:
        formatted_today = date

    # determine previous and next dates for navigation
    try:
        curr_dt = datetime.strptime(date, '%Y-%m-%d')
        prev_date = (curr_dt - timedelta(days=1)).strftime('%Y-%m-%d')
        next_date = (curr_dt + timedelta(days=1)).strftime('%Y-%m-%d')
    except Exception:
        prev_date = next_date = date
    is_today = (date == get_today())

    # ----------- ChatGPT Estimator Logic -----------
    if 'messages' not in session:
        session['messages'] = [
            {
                "role": "system",
                "content": (
                    "You are a helpful calorie tracking assistant. When the user describes a meal, "
                    "estimate the calories. Return the estimate as a breakdown in plain English, "
                    "and if appropriate, provide a JSON list with food name, quantity, and calories. "
                    "Include zero-calorie foods like water or lettuce if mentioned."
                    "Return calories as a single numeric value—no ranges or textual estimates—in the JSON list."
                )
            }
        ]

    user_input = request.form.get("user_input", "").strip() if request.method == 'POST' else None
    parsed_foods = []

    if user_input:
        session['messages'].append({"role": "user", "content": user_input})
        try:
            response = client.chat.completions.create(
                model="gpt-4",
                messages=session['messages'],
                temperature=0
            )
            gpt_reply = response.choices[0].message.content
            session['messages'].append({"role": "assistant", "content": gpt_reply})

            # Extract JSON array using regex (e.g., [{"food": ..., "calories": ...}])
            match = re.search(r"\[\s*{.*?}\s*\]", gpt_reply, re.DOTALL)
            if match:
                food_json = match.group(0)
                parsed_foods = json.loads(food_json)  # do NOT skip 0-calorie items
        except Exception as e:
            print("JSON parse error:", e)
            print("GPT reply was:", gpt_reply)
            session['messages'].append({"role": "assistant", "content": "Sorry, there was a problem generating a response."})

    return render_template(
        "index.html",
        entries=entries_for_day,
        total=total_calories,
        today=date,
        formatted_today=formatted_today,
        messages=session['messages'],
        parsed_foods=parsed_foods,
        prev_date=prev_date,
        next_date=next_date,
        is_today=is_today
    )

@app.route('/add', methods=['POST'])
@login_required
def add():
    date = request.form.get('date', get_today())
    selected = request.form.getlist('selected')
    entries_to_add = []

    for idx in selected:
        food = request.form.get(f'food_{idx}', '').strip()
        quantity = request.form.get(f'quantity_{idx}', '').strip()
        calories_raw = request.form.get(f'calories_{idx}', '')
        try:
            calories = round(float(calories_raw))
            entries_to_add.append({
                "food": food,
                "quantity": quantity,
                "calories": calories
            })
        except:
            continue

    for entry in entries_to_add:
        new_entry = FoodLog(
            user_id=current_user.id,
            date=date,
            food=entry["food"],
            quantity=entry.get("quantity", ""),
            calories=entry["calories"]
        )
        db.session.add(new_entry)
    db.session.commit()
    return redirect(url_for('index', date=date))

@app.route('/history')
@login_required
def history():
    entries = FoodLog.query.filter_by(user_id=current_user.id).all()
    totals_by_date = {}
    for entry in entries:
        totals_by_date.setdefault(entry.date, 0)
        totals_by_date[entry.date] += entry.calories

    daily_totals_list = sorted(totals_by_date.items())
    chart_data = {
        datetime.strptime(date, "%Y-%m-%d").strftime("%A, %B %d"): cal
        for date, cal in daily_totals_list
    }
    return render_template("history.html", daily_totals=daily_totals_list, chart_data=chart_data)

@app.route('/day/<date>')
@login_required
def view_day(date):
     # maintain backward compatibility with old view route
    return redirect(url_for('index', date=date))

@app.route("/delete/<date>/<int:entry_id>")
@login_required
def delete(date, entry_id):
    entry = FoodLog.query.filter_by(id=entry_id, user_id=current_user.id).first()
    if entry:
        db.session.delete(entry)
        db.session.commit()
    return redirect(url_for('index', date=date))

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if User.query.filter_by(username=username).first():
            flash('Username already taken.', 'error')
            return redirect(url_for('signup'))

        hashed_pw = generate_password_hash(password)
        new_user = User(username=username, password_hash=hashed_pw)
        db.session.add(new_user)
        db.session.commit()

        login_user(new_user)
        return redirect(url_for('index'))

    return render_template("signup.html")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('index'))
        flash("Invalid credentials", "error")
        return redirect(url_for('login'))

    return render_template("login.html")

@app.route('/logout')
@login_required
def logout():
    logout_user()
    session.pop('messages', None)
    return redirect(url_for('login'))

# ---------------- RUN ---------------- #
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
