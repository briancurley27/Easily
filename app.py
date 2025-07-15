from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_session import Session
from datetime import datetime
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
Session(app)

# ---------------- INITIALIZE EXTENSIONS ---------------- #
db.init_app(app)
login_manager.init_app(app)
login_manager.login_view = 'login'

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
    return datetime.now().strftime('%Y-%m-%d')

@app.template_filter('format_entry')
def format_entry(quantity, food):
    q = quantity.lower().strip()
    f = food.lower().strip()
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
    today = get_today()
    today_entries = FoodLog.query.filter_by(user_id=current_user.id, date=today).all()
    total_calories = sum(entry.calories for entry in today_entries)

    try:
        formatted_today = datetime.strptime(today, '%Y-%m-%d').strftime('%A, %B %d')
    except:
        formatted_today = today

    return render_template("index.html", entries=today_entries, total=total_calories, today=today, formatted_today=formatted_today)

@app.route('/chat', methods=['GET', 'POST'])
@login_required
def chat():
    if 'messages' not in session:
        session['messages'] = [
            {"role": "system", "content": "You are a helpful calorie tracking assistant. When the user describes a meal, estimate the calories. Return the estimate as a breakdown in plain English, and if appropriate, provide a JSON list with food name, quantity, and calories."}
        ]

    user_input = None
    gpt_reply = None
    parsed_foods = []

    if request.method == 'POST':
        user_input = request.form.get("user_input", "").strip()
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

                try:
                    start = gpt_reply.index("[")
                    end = gpt_reply.rindex("]") + 1
                    food_json = gpt_reply[start:end]
                    parsed_foods = json.loads(food_json)
                except:
                    parsed_foods = []

            except Exception as e:
                gpt_reply = "Sorry, there was a problem generating a response."
                session['messages'].append({"role": "assistant", "content": gpt_reply})

    return render_template("chat.html", messages=session['messages'], parsed_foods=parsed_foods)

@app.route('/add', methods=['POST'])
@login_required
def add():
    today = get_today()
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
            date=today,
            food=entry["food"],
            quantity=entry.get("quantity", ""),
            calories=entry["calories"]
        )
        db.session.add(new_entry)
    db.session.commit()
    return redirect(url_for('index'))

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
    entries = FoodLog.query.filter_by(user_id=current_user.id, date=date).all()
    total = sum(entry.calories for entry in entries)
    try:
        formatted_date = datetime.strptime(date, '%Y-%m-%d').strftime('%A %B %d, %Y')
    except:
        formatted_date = date
    return render_template("day.html", entries=entries, total=total, formatted_date=formatted_date)

@app.route("/delete/<date>/<int:entry_id>")
@login_required
def delete(date, entry_id):
    entry = FoodLog.query.filter_by(id=entry_id, user_id=current_user.id).first()
    if entry:
        db.session.delete(entry)
        db.session.commit()
    return redirect(url_for('index' if date == get_today() else 'view_day', date=date))

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
    return redirect(url_for('login'))

# ---------------- RUN ---------------- #
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
