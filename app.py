from flask import Flask, render_template, request, redirect, url_for
import json
import os
from datetime import datetime
import requests

app = Flask(__name__)

DATA_FILE = 'data.json'
NUTRITIONIX_APP_ID = "33d2bfda"
NUTRITIONIX_API_KEY = "aa8adda96501b129a28434c08fd0134a"

# Ensure data file exists
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, 'w') as f:
        json.dump({}, f)

def load_data():
    with open(DATA_FILE, 'r') as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def get_today():
    return datetime.now().strftime('%Y-%m-%d')

def get_formatted_date(date_str):
    return datetime.strptime(date_str, '%Y-%m-%d').strftime('%m/%d')

@app.route('/', methods=['GET', 'POST'])
def index():
    data = load_data()
    today = get_today()
    try:
        today_obj = datetime.strptime(today, '%Y-%m-%d')
        formatted_today = today_obj.strftime('%A, %B %d')
    except Exception:
        formatted_today = today
    if request.method == 'POST':
        food_input = request.form['food']
        # Call Nutritionix
        headers = {
            "x-app-id": NUTRITIONIX_APP_ID,
            "x-app-key": NUTRITIONIX_API_KEY,
            "Content-Type": "application/json"
        }
        response = requests.post(
            "https://trackapi.nutritionix.com/v2/natural/nutrients",
            headers=headers,
            json={"query": food_input}
        )
        if response.status_code == 200:
            foods = response.json().get("foods", [])
            if foods:
                # Round calories for each food
                for food in foods:
                    food['nf_calories'] = round(food.get('nf_calories', 0))
                return render_template("confirm_food.html", foods=foods, original_input=food_input)
            else:
                return render_template("manual_entry.html", original_input=food_input)
        else:
            return render_template("manual_entry.html", original_input=food_input)

    today_entries = data.get(today, [])
    total_calories = round(sum(entry["calories"] for entry in today_entries))
    return render_template("index.html", entries=today_entries, total=total_calories, today=today, formatted_today=formatted_today)

@app.route('/add', methods=['POST'])
def add():
    data = load_data()
    today = get_today()
    error = None

    # Check if multiple foods are submitted
    selected = request.form.getlist('selected')
    total = int(request.form.get('total', 0))
    entries_to_add = []
    print('Selected:', selected)

    if selected:
        for idx in selected:
            try:
                idx = int(idx)
                food = request.form.get(f'food_{idx}', '').strip()
                calories_raw = request.form.get(f'calories_{idx}', '')
                quantity_raw = request.form.get(f'quantity_{idx}', '')
                unit = request.form.get(f'unit_{idx}', '').strip()
                if not food:
                    error = f"Food name for item {idx+1} cannot be empty."
                    break
                try:
                    calories = round(float(calories_raw))
                    if calories < 0:
                        error = f"Calories for item {idx+1} cannot be negative."
                        break
                except (ValueError, TypeError):
                    error = f"Calories for item {idx+1} must be a valid number."
                    break
                try:
                    quantity = float(quantity_raw)
                    if quantity <= 0:
                        error = f"Quantity for item {idx+1} must be positive."
                        break
                except (ValueError, TypeError):
                    quantity = None
                entry = {"food": food, "calories": calories}
                if quantity:
                    entry["quantity"] = quantity
                if unit:
                    entry["unit"] = unit
                entries_to_add.append(entry)
            except Exception:
                error = "Invalid selection."
                break

    else:
        # Fallback for manual entry
        food = request.form.get('food', '').strip()
        calories_raw = request.form.get('calories', '')
        if not food:
            error = "Food name cannot be empty."
        try:
            calories = round(float(calories_raw))
            if calories < 0:
                error = "Calories cannot be negative."
        except (ValueError, TypeError):
            error = "Calories must be a valid number."
        if not error:
            entries_to_add.append({"food": food, "calories": calories})

    print('Entries to add:', entries_to_add)

    if error:
        today_entries = data.get(today, [])
        total_calories = round(sum(entry["calories"] for entry in today_entries))
        return render_template("index.html", entries=today_entries, total=total_calories, today=today, formatted_today=get_formatted_date(today), error=error)

    if today not in data:
        data[today] = []
    data[today].extend(entries_to_add)
    save_data(data)

    return redirect(url_for('index'))

@app.route("/history")
def history():
    data = load_data()

    daily_totals_list = [
        (date, round(sum(entry['calories'] for entry in entries)))
        for date, entries in data.items()
    ]
    daily_totals_list.sort()

    daily_totals_dict = {
        datetime.strptime(date, "%Y-%m-%d").strftime("%m/%d"): round(sum(entry['calories'] for entry in entries))
        for date, entries in data.items()
        if date
    }

    return render_template(
        "history.html",
        daily_totals=daily_totals_list,
        chart_data=daily_totals_dict
    )

@app.route("/day/<date>")
def view_day(date):
    data = load_data()
    entries = data.get(date, [])
    total = round(sum(entry['calories'] for entry in entries))
    # Format date as 'Friday July 11, 2025'
    try:
        date_obj = datetime.strptime(date, '%Y-%m-%d')
        formatted_date = date_obj.strftime('%A %B %d, %Y')
    except Exception:
        formatted_date = date
    return render_template("day.html", entries=entries, total=total, formatted_date=formatted_date)

@app.route("/delete/<date>/<int:index>")
def delete(date, index):
    data = load_data()
    if date in data and 0 <= index < len(data[date]):
        del data[date][index]
        save_data(data)
    return redirect(url_for('index' if date == get_today() else 'view_day', date=date))

if __name__ == '__main__':
    app.run(debug=True)
