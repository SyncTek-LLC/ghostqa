"""Minimal Flask demo app for SpecterQA testing.

This app provides a simple signup flow that SpecterQA can test against:
  - Homepage with welcome message and nav
  - Signup page with email/password form
  - Dashboard page shown after successful signup

Run: python app.py
Visit: http://localhost:5000
"""

from flask import Flask, render_template, request, redirect, url_for, session

app = Flask(__name__)
app.secret_key = "specterqa-demo-secret-key"


@app.route("/")
def homepage():
    """Landing page with welcome message and navigation."""
    return render_template("homepage.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Signup page with email/password form."""
    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        if email and password:
            session["email"] = email
            return redirect(url_for("dashboard"))
        return render_template("signup.html", error="Please fill in all fields.")
    return render_template("signup.html", error=None)


@app.route("/dashboard")
def dashboard():
    """Dashboard page shown after successful signup."""
    email = session.get("email", "user@example.com")
    return render_template("dashboard.html", email=email)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
