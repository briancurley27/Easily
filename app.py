from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import requests
import os
import re
import uuid
from openai import OpenAI

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    raise ValueError("Missing OPENAI_API_KEY environment variable")

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///calories.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# ---------------- MODELS ---------------- #

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(150), nullable=False)

class FoodLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.String(10))
    food = db.Column(db.String(100))
    quantity = db.Column(db.String(50))
    calories = db.Column(db.Integer)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ---------------- HELPERS ---------------- #

def get_today():
    return datetime.now().strftime('%Y-%m-%d')

def get_formatted_date(date_str):
    return datetime.strptime(date_str, '%Y-%m-%d').strftime('%m/%d')

@app.template_filter('datetimeformat')
def datetimeformat(value, format='%A, %B %d'):
    try:
        return datetime.strptime(value, '%Y-%m-%d').strftime(format)
    except Exception:
        return value

def parse_foods_with_chatgpt(user_input):
    prompt = f"""
Extract all the foods and portion sizes from this sentence and return them as JSON.
Input: "{user_input}"
Output format:
[
  {{
    "food": "food name",
    "quantity": "amount (e.g., 1 slice, 2 cups, handful)"
  }}
]
"""
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        content = response.choices[0].message.content
        print("ChatGPT raw output:", content)
        return eval(content)
    except Exception as e:
        print("ChatGPT parsing error:", e)
        return []

def estimate_calories_with_chatgpt(food, quantity):
    prompt = f"Roughly estimate the calories in {quantity} of {food}. Return just a number."
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        content = response.choices[0].message.content.strip()
        match = re.search(r"\d+(\.\d+)?", content)
        if match:
            return int(float(match.group()))
    except Exception as e:
        print("ChatGPT calorie estimation error:", e)
    return None

USDA_API_KEY = os.environ.get("USDA_API_KEY")
if not USDA_API_KEY:
    raise ValueError("Missing USDA_API_KEY environment variable")

def search_usda_food(food_name):
    USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
    USDA_FOOD_URL = "https://api.nal.usda.gov/fdc/v1/food/{}"
    params = {
        "api_key": USDA_API_KEY,
        "query": food_name,
        "pageSize": 1,
        "dataType": ["Foundation", "SR Legacy"]
    }
    try:
        search_response = requests.get(USDA_SEARCH_URL, params=params).json()
        if not search_response.get("foods"):
            return None
        food = search_response["foods"][0]
        fdc_id = food["fdcId"]
        detail_response = requests.get(USDA_FOOD_URL.format(fdc_id), params={"api_key": USDA_API_KEY}).json()
        for nutrient in detail_response.get("foodNutrients", []):
            if nutrient.get("nutrientName") == "Energy":
                return {
                    "description": food["description"],
                    "calories_per_100g": nutrient.get("value", None),
                    "fdcId": fdc_id
                }
    except Exception as e:
        print("USDA lookup error:", e)
    return None

# ---------------- ROUTES ---------------- #

@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    today = get_today()
    today_entries = FoodLog.query.filter_by(user_id=current_user.id, date=today).all()
    try:
        today_obj = datetime.strptime(today, '%Y-%m-%d')
        formatted_today = today_obj.strftime('%A, %B %d')
    except Exception:
        formatted_today = today

    error = None

    if request.method == 'POST':
        try:
            food_input = request.form['food']
            parsed_foods = parse_foods_with_chatgpt(food_input)
            results = []
            for i, item in enumerate(parsed_foods):
                food = item.get("food", "")
                quantity = item.get("quantity", "")
                usda = search_usda_food(food)
                if usda:
                    calories = usda.get("calories_per_100g")
                    source = "USDA"
                else:
                    calories = estimate_calories_with_chatgpt(food, quantity)
                    source = "ChatGPT"
                results.append({
                    "food": food,
                    "quantity": quantity,
                    "calories": round(calories) if calories is not None else "N/A",
                    "source": source
                })
            return render_template("confirm_food.html", foods=results, enumerate=enumerate)
        except Exception as e:
            print("Error estimating calories:", e)
            error = "There was an error estimating calories. Please try again."

    total_calories = sum(entry.calories for entry in today_entries)
    return render_template("index.html", entries=today_entries, total=total_calories, today=today, formatted_today=formatted_today, error=error)

@app.route('/add', methods=['POST'])
@login_required
def add():
    today = get_today()
    selected = request.form.getlist('selected')
    entries_to_add = []

    if selected:
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
    else:
        food = request.form.get('food', '').strip()
        quantity = request.form.get('quantity', '').strip()
        calories_raw = request.form.get('calories', '')
        try:
            calories = round(float(calories_raw))
            entries_to_add.append({"food": food, "quantity": quantity, "calories": calories})
        except:
            pass

    for entry in entries_to_add:
        new_entry = FoodLog(user_id=current_user.id, date=today, food=entry["food"], quantity=entry.get("quantity", ""), calories=entry["calories"])
        db.session.add(new_entry)
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/history')
@login_required
def history():
    entries = FoodLog.query.filter_by(user_id=current_user.id).all()
    totals_by_date = {}
    for entry in entries:
        if entry.date not in totals_by_date:
            totals_by_date[entry.date] = 0
        totals_by_date[entry.date] += entry.calories
    daily_totals_list = sorted(totals_by_date.items())
    daily_totals_dict = {
        datetime.strptime(date, "%Y-%m-%d").strftime("%m/%d"): cal
        for date, cal in daily_totals_list
    }
    return render_template("history.html", daily_totals=daily_totals_list, chart_data=daily_totals_dict)

@app.route('/day/<date>')
@login_required
def view_day(date):
    entries = FoodLog.query.filter_by(user_id=current_user.id, date=date).all()
    total = sum(entry.calories for entry in entries)
    try:
        date_obj = datetime.strptime(date, '%Y-%m-%d')
        formatted_date = date_obj.strftime('%A %B %d, %Y')
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
            return "Username already taken."
        hashed_pw = generate_password_hash(password)
        new_user = User(username=username, password_hash=hashed_pw)
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))
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
        return "Invalid credentials"
    return render_template("login.html")

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
